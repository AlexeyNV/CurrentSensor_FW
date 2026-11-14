"""Capture and validate CurrentSensor_FW CSF4 v1 USB CDC frames."""

import argparse
import csv
import json
import math
import re
import struct
import sys
import threading
import time
import traceback
import uuid
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


MAGIC = b"CSF4"
PROFILE_NAMES = ("fast", "balanced", "precision", "low_noise")
HEADER = struct.Struct("<4sBBHHHIIIHHI")
STATS_KEYS = (
    "ADC_SAMPLES", "USB_SAMPLES_SENT", "FRAMES_SENT", "FRAMES_DROPPED",
    "USB_BUSY_COUNT", "TX_BUFFER_OVERFLOWS", "ACQ_BUFFER_OVERFLOWS",
    "LOST_SAMPLE_SLOTS", "SPI_ERRORS", "MAX_SERVICE_US",
    "MIN_SAMPLE_PERIOD_US", "MAX_SAMPLE_PERIOD_US",
    "EFFECTIVE_STREAM_RATE_HZ", "BUFFER_DEPTH", "MAX_BUFFER_DEPTH",
    "MAX_BUFFER_AGE_US", "ACQ_ELAPSED_US", "STREAM_ELAPSED_US",
    "MAX_TRANSPORT_POLL_US",
)


def elapsed_us_from_status(final_stats, post_status, wide_key, legacy_key):
    """Prefer the text 64-bit duration and verify its CSF4 modulo-32 view."""
    legacy = final_stats[legacy_key] if final_stats else None
    if not post_status or wide_key not in post_status:
        return legacy, "CSF4_END_U32" if legacy is not None else None, None
    value = post_status[wide_key]
    if not value.isascii() or not value.isdecimal():
        return legacy, "CSF4_END_U32" if legacy is not None else None, f"invalid {wide_key}"
    duration = int(value)
    if duration > (1 << 64) - 1 or (legacy is not None and
                                   duration & 0xFFFFFFFF != legacy):
        return legacy, "CSF4_END_U32" if legacy is not None else None, f"inconsistent {wide_key}"
    return duration, "TEXT_US64", None


class FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.text_tail = bytearray()
        self.ended = False
        self.crc_errors = 0
        self.format_errors = 0
        self.resync_bytes = 0
        self.frame_gaps = 0
        self.slot_gaps = 0
        self.last_sequence = None
        self.next_slot = 0
        self.code_errors = 0
        self.period_us = None
        self.version = None
        self.config = None

    def feed(self, data):
        if self.ended:
            self.text_tail.extend(data)
            return []
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 4:
            if self.buffer[:4] != MAGIC:
                position = self.buffer.find(MAGIC, 1)
                discard = position if position >= 0 else max(0, len(self.buffer) - 3)
                del self.buffer[:discard]
                self.resync_bytes += discard
                if len(self.buffer) < 4:
                    break
            if len(self.buffer) < HEADER.size:
                break
            header = HEADER.unpack_from(self.buffer)
            _, version, kind, header_len, frame_len, count, sequence, slot, stamp, period, flags, lost = header
            expected_length = 32 + (6 * count if kind == 1 else 76 if kind in (2, 3) else 16 if kind == 4 else 4 if kind == 5 else -1) + 4
            if (version not in (1, 2) or header_len != 32 or frame_len != expected_length or
                    frame_len > 228 or count > 32 or
                    (version == 1 and (period == 0 or flags != 0)) or
                    (version == 2 and (period != 0 or flags != 1)) or
                    (kind == 1 and count == 0) or (kind != 1 and count != 0)):
                self.format_errors += 1
                del self.buffer[0]
                continue
            if len(self.buffer) < frame_len:
                break
            packet = bytes(self.buffer[:frame_len])
            actual_crc = zlib.crc32(packet[:-4]) & 0xFFFFFFFF
            expected_crc = struct.unpack_from("<I", packet, frame_len - 4)[0]
            if actual_crc != expected_crc:
                self.crc_errors += 1
                del self.buffer[0]
                continue
            del self.buffer[:frame_len]
            if self.period_us is not None and period != self.period_us:
                self.format_errors += 1
            else:
                self.period_us = period
            self.version = version
            if self.last_sequence is not None:
                self.frame_gaps += (sequence - self.last_sequence - 1) & 0xFFFFFFFF
            self.last_sequence = sequence
            frame = {"type": kind, "sequence": sequence, "slot": slot,
                     "timestamp_us": stamp, "lost_since_prev": lost,
                     "period_us": period, "protocol_version": version,
                     "packet": packet}
            if kind == 1:
                codes = struct.unpack_from(f"<{count}i", packet, 32)
                if version == 2:
                    raw = struct.unpack_from(f"<{count}I", packet, 32)
                    self.code_errors += sum(value > 0xFFFFFF for value in raw)
                    frame['raw24'] = raw
                    codes = tuple((value >> 4) - (0x100000 if value & 0x800000 else 0) for value in raw)
                offsets = struct.unpack_from(f"<{count}H", packet, 32 + 4 * count)
                self.code_errors += sum(code < -524288 or code > 524287 for code in codes)
                slot_delta = (slot - self.next_slot) & 0xFFFFFFFF
                if slot_delta < 0x80000000:
                    self.slot_gaps += slot_delta
                else:
                    self.format_errors += 1
                self.next_slot = (slot + count) & 0xFFFFFFFF
                frame["samples"] = [
                    ((slot + index) & 0xFFFFFFFF, (stamp + offsets[index]) & 0xFFFFFFFF, code)
                    for index, code in enumerate(codes)
                ]
            elif kind in (2, 3):
                frame["stats"] = dict(zip(STATS_KEYS, struct.unpack_from("<19I", packet, 32)))
            elif kind == 4:
                frame["config"] = struct.unpack_from("<HHHHHHI", packet, 32)
                self.config = frame["config"]
                if self.config[1] != period:
                    self.format_errors += 1
            else:
                frame["error_code"] = struct.unpack_from("<I", packet, 32)[0]
            frames.append(frame)
            if kind == 3:
                self.ended = True
                self.text_tail.extend(self.buffer)
                self.buffer.clear()
                break
        return frames


def wait_start_ack(port, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    line = bytearray()
    rejected = None
    while time.monotonic() < deadline:
        character = port.read(1)
        if character:
            line.extend(character)
            if character == b"\n":
                if line == b"STREAM: BINARY V1\r\n":
                    return
                if line.startswith(b"ERR:"):
                    rejected = bytes(line)
                line.clear()
    raise TimeoutError(f"no stream start acknowledgement; last response: {rejected!r}")


def synchronize_cli(port, attempts=3):
    """Terminate a partial PuTTY line and verify the text command boundary."""
    port.reset_input_buffer()
    port.write(b"\n")
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        if not port.read(256):
            break
    for _ in range(attempts):
        port.write(b"ping\n")
        deadline = time.monotonic() + 1.0
        line = bytearray()
        while time.monotonic() < deadline:
            character = port.read(1)
            if not character:
                continue
            line.extend(character)
            if character == b"\n":
                if line == b"pong\r\n":
                    return
                line.clear()
        port.write(b"\n")
    raise TimeoutError("could not synchronize the USB CDC text CLI")


def read_text_report(port, command, timeout_s=3.0):
    port.write(command.encode("ascii") + b"\n")
    deadline = time.monotonic() + timeout_s
    fields = {}
    while time.monotonic() < deadline:
        line = port.readline().decode("ascii", errors="replace").strip()
        if line.startswith("ERR:"):
            raise RuntimeError(f"{command} rejected: {line}")
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
        if line == "STATUS: OK":
            return fields
    raise TimeoutError(f"no complete response to {command}")


def read_stream_status(port, timeout_s=3.0):
    return read_text_report(port, "stream status", timeout_s)


def get_available_profiles(port):
    """Read the device's profile table; frequency values are owned by firmware."""
    fields = read_text_report(port, "acq profile list")
    try:
        count = int(fields["PROFILE_COUNT"])
        if not 1 <= count <= 256:
            raise ValueError("invalid profile count")
        profiles = []
        for identifier in range(count):
            name, rate, period, vshct, avg, adc = fields[f"PROFILE_{identifier}"].split(",")
            profiles.append(validate_profile({
                "PROFILE_ID": str(identifier), "PROFILE": name,
                "SAMPLE_RATE_HZ": rate, "SAMPLE_PERIOD_US": period,
                "VSHCT_US": vshct, "AVG": avg, "ADC_CONFIG": adc,
                "ADCRANGE": "0"}))
        return profiles
    except (KeyError, ValueError) as error:
        raise RuntimeError(f"invalid profile list: {fields!r}") from error


def validate_profile(fields):
    try:
        result = {"id": int(fields["PROFILE_ID"]), "name": fields["PROFILE"].upper(),
                  "sample_rate_hz": int(fields["SAMPLE_RATE_HZ"]),
                  "sample_period_us": int(fields["SAMPLE_PERIOD_US"]),
                  "vshct_us": int(fields["VSHCT_US"]), "avg": int(fields["AVG"]),
                  "adc_config": int(fields["ADC_CONFIG"], 0),
                  "adcrange": int(fields["ADCRANGE"]),
                  "firmware_version": fields.get("FW"),
                  "acq_mode": fields.get("ACQ_MODE")}
        if (result["sample_rate_hz"] * result["sample_period_us"] != 1000000 or
                not 0 < result["sample_period_us"] <= 65535 or
                not 0 < result["vshct_us"] < result["sample_period_us"] or
                result["avg"] != 1 or result["adcrange"] != 0 or
                not 0 <= result["adc_config"] <= 65535 or
                result["adc_config"] >> 12 != 0xA or
                result["adc_config"] & 7):
            raise ValueError("inconsistent measurement parameters")
        return result
    except (KeyError, ValueError) as error:
        raise RuntimeError(f"invalid profile response: {fields!r}") from error


def get_active_profile(port):
    return validate_profile(read_text_report(port, "acq profile show"))


def set_profile(port, profile_id):
    name = str(profile_id).lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) is None:
        raise ValueError(f"unknown profile: {profile_id}")
    confirmed = validate_profile(read_text_report(port, f"acq profile set {name}"))
    actual = get_active_profile(port)
    if confirmed["name"].lower() != name or actual != confirmed:
        raise RuntimeError(f"requested profile {name} was not confirmed: {actual}")
    return actual


def start_stream(port):
    port.write(b"stream start\n")
    wait_start_ack(port)


def stop_stream(port):
    port.write(b"stream stop\n")


class StopWriter:
    """Send STOP while the reader continues draining the active CSF4 stream."""

    def __init__(self, port):
        self.port = port
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._write, daemon=True)

    def _write(self):
        try:
            written = self.port.write(b"stream stop\n")
            if written != len(b"stream stop\n"):
                raise OSError(f"short stream stop write: {written} bytes")
        except Exception as error:
            self.error = error
        finally:
            self.done.set()

    def start(self):
        self.thread.start()

    def join(self, timeout=1.5):
        self.thread.join(timeout)


def _failure_details(progress, error):
    decoder = progress.get("decoder")
    records = progress.get("records", ())
    started = progress.get("session_started", progress["created"])
    port = progress.get("port")
    return {
        "stage": progress.get("stage", "PORT_OPEN"),
        "command": progress.get("command"),
        "exception_type": type(error).__name__,
        "exception": str(error),
        "elapsed_s": time.monotonic() - started,
        "actual_samples": len(records),
        "csf4_frames": progress.get("csf4_frames", 0),
        "data_frames": progress.get("data_frames", 0),
        "status_frames": progress.get("status_frames", 0),
        "error_frames": progress.get("error_frames", 0),
        "crc_errors": decoder.crc_errors if decoder else 0,
        "format_errors": decoder.format_errors if decoder else 0,
        "frame_gaps": decoder.frame_gaps if decoder else 0,
        "slot_gaps": decoder.slot_gaps if decoder else 0,
        "code_errors": decoder.code_errors if decoder else 0,
        "end_received": decoder.ended if decoder else False,
        "port_closed": (not port.is_open if hasattr(port, "is_open") else
                        port.closed if hasattr(port, "closed") else None),
    }


def _set_stage(progress, stage, command=None):
    if progress.get("stage") == stage and progress.get("command") == command:
        return
    progress["stage"] = stage
    progress["command"] = command
    print(f"capture stage: {stage}; command: {command or '-'}; "
          f"elapsed_s: {time.monotonic() - progress.get('session_started', progress['created']):.3f}",
          file=sys.stderr, flush=True)


def _report_capture_failure(progress, error):
    details = _failure_details(progress, error)
    print(f"capture failure: {json.dumps(details, ensure_ascii=False)}", file=sys.stderr)
    traceback.print_exc()
    metadata = progress.get("metadata")
    if metadata is not None and not metadata.exists():
        try:
            with metadata.open("x", encoding="utf-8") as json_file:
                json.dump({"session_complete": False, "failure": details,
                           "files": {"samples": str(progress.get("output")),
                                     "metadata": str(metadata)}}, json_file, indent=2)
        except OSError as metadata_error:
            print(f"partial metadata write failed: {metadata_error}", file=sys.stderr)


def get_stream_status(port):
    return read_stream_status(port)


def unique_paths(output=None, histogram=None, metadata=None, anomalies=None):
    """Never overwrite a previous capture, including explicitly named files."""
    stem = datetime.now(timezone.utc).strftime("stream_%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    base = Path(output).with_suffix("") if output else Path(stem)
    requested = [output or str(base) + "_samples.csv",
                 histogram or str(base) + "_histogram.csv",
                 metadata or str(base) + "_metadata.json",
                 anomalies or str(base) + "_anomalies.csv"]
    for suffix in range(10000):
        paths = [Path(name) if suffix == 0 else
                 Path(name).with_name(f"{Path(name).stem}_{suffix:03d}{Path(name).suffix}")
                 for name in requested]
        if len(set(paths)) == len(paths) and not any(path.exists() for path in paths):
            return paths
    raise RuntimeError("could not find unused capture filenames")


def current_stats(records):
    codes = [item[2] for item in records]
    if not codes:
        return {"count": 0, "min_mA": None, "max_mA": None,
                "mean_mA": None, "stddev_mA": None}
    mean = math.fsum(codes) / len(codes)
    variance = math.fsum((code - mean) ** 2 for code in codes) / len(codes)
    return {"count": len(codes), "min_mA": min(codes) / 16,
            "max_mA": max(codes) / 16, "mean_mA": mean / 16,
            "stddev_mA": math.sqrt(variance) / 16}


def resolution_stats(records, window_us=1000000):
    """Describe raw codes without filtering or claiming measured ENOB/accuracy."""
    codes = Counter(item[2] for item in records)
    ordered = sorted(codes)
    spacings = Counter(b - a for a, b in zip(ordered, ordered[1:]))
    changes = Counter()
    windows = {}
    elapsed_us = 0
    previous = None
    for record in records:
        if previous is not None:
            elapsed_us += (record[1] - previous[1]) & 0xFFFFFFFF
            if record[0] == previous[0] + 1:
                changes[record[2] - previous[2]] += 1
        window = elapsed_us // window_us
        total, count = windows.get(window, (0, 0))
        windows[window] = (total + record[2], count + 1)
        previous = record
    pairs = sum(changes.values())
    drift = [{"start_elapsed_us": index * window_us, "count": count,
              "mean_mA": total / count / 16}
             for index, (total, count) in sorted(windows.items())]
    return {
        "unique_codes": len(codes),
        "most_common_codes": [{"code": code, "count": count,
                               "fraction": count / len(records)}
                              for code, count in codes.most_common(10)],
        "observed_code_spacing": [{"delta_code": delta, "count": count}
                                  for delta, count in sorted(spacings.items())],
        "adjacent_code_changes": [{"delta_code": delta, "count": count}
                                  for delta, count in sorted(changes.items())],
        "contiguous_pairs": pairs,
        "pairs_excluded_by_slot_gap": max(0, len(records) - 1 - pairs),
        "identical_adjacent_fraction": changes[0] / pairs if pairs else None,
        "drift_window_us": window_us, "drift_windows": drift,
        "last_minus_first_window_mA": drift[-1]["mean_mA"] - drift[0]["mean_mA"] if drift else None,
    }


def analyze_capture(records, decoder, final_stats, min_ma, max_ma, tolerance_us,
                    period_us=None):
    if min_ma > max_ma:
        raise ValueError("range minimum exceeds maximum")
    anomalous = [item for item in records if not min_ma <= item[2] / 16 <= max_ma]
    clean = [item for item in records if min_ma <= item[2] / 16 <= max_ma]
    nominal_period = period_us if period_us is not None else decoder.period_us
    period_violations = 0
    for previous, current in zip(records, records[1:]):
        slot_delta = current[0] - previous[0]
        if slot_delta <= 0:
            period_violations += 1
        elif slot_delta == 1:
            actual_us = (current[1] - previous[1]) & 0xFFFFFFFF
            if nominal_period is None or abs(actual_us - nominal_period) > tolerance_us:
                period_violations += 1
    acquisition_loss = (final_stats["LOST_SAMPLE_SLOTS"] if final_stats
                        else decoder.slot_gaps)
    acquisition_overflow = final_stats["ACQ_BUFFER_OVERFLOWS"] if final_stats else 0
    transport_loss = bool(final_stats and (
        final_stats["USB_SAMPLES_SENT"] != len(records) or
        final_stats["ADC_SAMPLES"] != len(records) + acquisition_overflow or
        final_stats["FRAMES_DROPPED"] or final_stats["TX_BUFFER_OVERFLOWS"] or
        decoder.slot_gaps > acquisition_loss + acquisition_overflow))
    frame_error = bool(decoder.crc_errors or decoder.format_errors or
                       decoder.frame_gaps or decoder.code_errors or
                       not decoder.ended or final_stats is None)
    expected_slots = len(records) + acquisition_loss + acquisition_overflow
    return {
        "full": current_stats(records), "clean": current_stats(clean),
        "resolution": resolution_stats(records),
        "anomaly_count": len(anomalous),
        "extremes": sorted(records, key=lambda item: abs(item[2]), reverse=True)[:10],
        "period_violations": period_violations,
        "missed_slots": acquisition_loss,
        "missed_slot_percent": 100 * (acquisition_loss + acquisition_overflow) / expected_slots if expected_slots else 0,
        "statuses": {
            "transport_frame_error": frame_error,
            "transport_data_loss": transport_loss,
            "acquisition_loss": bool(acquisition_loss or acquisition_overflow or
                                     (final_stats and final_stats["SPI_ERRORS"])),
            "period_violation": bool(period_violations),
            "suspicious_value": bool(anomalous),
        },
        "anomalies": anomalous,
    }


def capture(port_name, seconds, output=None, histogram=None, status_interval=0,
            metadata=None, anomalies=None, range_min_ma=-50, range_max_ma=10000,
            period_tolerance_us=5, acq_mode="dma", profile=None):
    progress = {"created": time.monotonic(), "stage": None, "command": None}
    try:
        _set_stage(progress, "PORT_OPEN")
        return _capture(port_name, seconds, output, histogram, status_interval,
                        metadata, anomalies, range_min_ma, range_max_ma,
                        period_tolerance_us, acq_mode, profile, progress)
    except BaseException as error:
        error.capture_stage = progress.get("stage")
        error.capture_command = progress.get("command")
        _report_capture_failure(progress, error)
        stop_writer = progress.get("stop_writer")
        if stop_writer is not None:
            stop_writer.join()
        raise


def _capture(port_name, seconds, output, histogram, status_interval,
             metadata, anomalies, range_min_ma, range_max_ma,
             period_tolerance_us, acq_mode, profile, progress):
    try:
        import serial
    except ImportError as error:
        raise RuntimeError("install pyserial: python -m pip install pyserial") from error
    decoder = FrameDecoder()
    records = []
    progress["decoder"] = decoder
    progress["records"] = records
    data_frames = 0
    final_stats = None
    status_frames = 0
    error_frames = 0
    interrupted = False
    text_mode_ok = False
    ping_reply = b""
    pre_stream_mode = None
    post_status = None
    post_timing = None
    post_anomaly = None
    diagnostic_errors = {}
    diagnostic_failure_details = {}
    captured_at_utc = datetime.now(timezone.utc).isoformat()
    output, histogram, metadata, anomalies = unique_paths(output, histogram, metadata, anomalies)
    progress["output"] = output
    progress["metadata"] = metadata
    with serial.Serial(port_name, baudrate=115200,
                       timeout=0.05, write_timeout=1) as port:
        progress["port"] = port
        _set_stage(progress, "STREAM_START_COMMAND", "CLI synchronize (newline, ping)")
        synchronize_cli(port)
        if acq_mode is not None:
            if acq_mode not in ("poll", "dma"):
                raise ValueError("acq_mode must be poll or dma")
            _set_stage(progress, "STREAM_START_COMMAND", f"acq mode {acq_mode}")
            read_text_report(port, progress["command"])
            _set_stage(progress, "STREAM_START_COMMAND", "acq config")
            pre_stream_mode = read_text_report(port, "acq config").get(
                "ACQ_MODE_SELECTED")
            if pre_stream_mode is None or pre_stream_mode.lower() != acq_mode:
                raise RuntimeError(
                    f"acq config did not confirm {acq_mode}: {pre_stream_mode!r}")
        _set_stage(progress, "STREAM_START_COMMAND",
                   f"acq profile set {profile}" if profile is not None
                   else "acq profile show")
        active_profile = set_profile(port, profile) if profile is not None else get_active_profile(port)
        _set_stage(progress, "STREAM_START_COMMAND", "stream start")
        start_stream(port)
        _set_stage(progress, "STREAM_ACTIVE", "stream config")
        port.write(b"stream config\n")
        started = time.monotonic()
        progress["session_started"] = started
        stop_at = started + seconds
        next_status = started + status_interval if status_interval else float("inf")
        stop_sent = False
        stop_writer = None
        deadline = stop_at + 5.0
        with output.open("x", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(("slot", "tim2_read_us", "vshunt_code", "sample_period_us"))
            while time.monotonic() < deadline:
                try:
                    now = time.monotonic()
                    if not stop_sent and now >= stop_at:
                        _set_stage(progress, "STOP_COMMAND_WRITE", "stream stop")
                        stop_writer = StopWriter(port)
                        progress["stop_writer"] = stop_writer
                        stop_writer.start()
                        stop_sent = True
                        deadline = now + 5.0
                    if stop_writer is not None and stop_writer.done.is_set():
                        if stop_writer.error is not None:
                            raise stop_writer.error
                        _set_stage(progress, "WAIT_FOR_END", "stream stop")
                    if not stop_sent and now >= next_status:
                        _set_stage(progress, "STREAM_ACTIVE", "stream status")
                        port.write(b"stream status\n")
                        next_status += status_interval
                    for frame in decoder.feed(port.read(1024)):
                        progress["csf4_frames"] = progress.get("csf4_frames", 0) + 1
                        if frame["type"] == 1:
                            data_frames += 1
                            progress["data_frames"] = data_frames
                            for slot, stamp, code in frame["samples"]:
                                writer.writerow((slot, stamp, code, frame["period_us"]))
                                records.append((slot, stamp, code))
                        elif frame["type"] == 2:
                            status_frames += 1
                            progress["status_frames"] = status_frames
                        elif frame["type"] == 3:
                            final_stats = frame["stats"]
                        elif frame["type"] == 5:
                            error_frames += 1
                            progress["error_frames"] = error_frames
                    if decoder.ended and (b"STATUS: OK\r\n" in decoder.text_tail or
                                          b"ERR: " in decoder.text_tail):
                        break
                except KeyboardInterrupt:
                    if interrupted:
                        raise
                    interrupted = True
                    if not stop_sent:
                        _set_stage(progress, "STOP_COMMAND_WRITE", "stream stop")
                        stop_writer = StopWriter(port)
                        progress["stop_writer"] = stop_writer
                        stop_writer.start()
                        stop_sent = True
                    deadline = time.monotonic() + 5.0
        if stop_writer is not None:
            stop_writer.join()
            if not stop_writer.done.is_set():
                _set_stage(progress, "STOP_COMMAND_WRITE", "stream stop")
                raise TimeoutError("stream stop write did not finish")
            if stop_writer.error is not None:
                _set_stage(progress, "STOP_COMMAND_WRITE", "stream stop")
                raise stop_writer.error
        duration = time.monotonic() - started
        _set_stage(progress, "READ_FINAL_DIAGNOSTICS")
        if decoder.ended and b"STREAM: TEXT\r\n" in decoder.text_tail:
            _set_stage(progress, "READ_FINAL_DIAGNOSTICS", "ping")
            port.write(b"ping\n")
            ping_reply = port.readline()
            if ping_reply != b"pong\r\n":
                print(f"text mode check: unexpected {ping_reply!r}")
            else:
                text_mode_ok = True
                for command, key in (("stream status", "status"),
                                     ("stream timing", "timing"),
                                     ("stream anomaly", "anomaly")):
                    _set_stage(progress, "READ_FINAL_DIAGNOSTICS", command)
                    try:
                        fields = read_text_report(port, command)
                        if key == "status":
                            post_status = fields
                        elif key == "timing":
                            post_timing = fields
                        else:
                            post_anomaly = fields
                    except (OSError, RuntimeError, TimeoutError) as error:
                        diagnostic_errors[key] = str(error)
                        diagnostic_failure_details[key] = _failure_details(progress, error)
                        print(f"capture diagnostic failure: "
                              f"{json.dumps(diagnostic_failure_details[key], ensure_ascii=False)}",
                              file=sys.stderr)
                        traceback.print_exc()
        else:
            print("text mode check: no complete END/text transition")
        _set_stage(progress, "PORT_CLOSE")
    progress["port_closed"] = True
    _set_stage(progress, "POSTPROCESS")
    count = len(records)
    report = analyze_capture(records, decoder, final_stats, range_min_ma,
                             range_max_ma, period_tolerance_us,
                             active_profile["sample_period_us"])
    post_status_matches_end = bool(final_stats and post_status and all(
        post_status.get(key) == str(final_stats[key]) for key in
        ("ADC_SAMPLES", "USB_SAMPLES_SENT", "LOST_SAMPLE_SLOTS", "SPI_ERRORS")))
    diagnostics_session_match = bool(
        post_status and post_timing and post_anomaly and
        post_status.get("SESSION_TYPE") == "STREAM" and
        post_status.get("SESSION_ID") is not None and
        post_timing.get("SESSION_ID") == post_status["SESSION_ID"] and
        post_anomaly.get("SESSION_ID") == post_status["SESSION_ID"])
    actual_mode = post_status.get("ACQ_MODE", "UNKNOWN").lower() if post_status else "unknown"
    mode_confirmed = actual_mode in ("poll", "dma") and (
        acq_mode is None or actual_mode == acq_mode)
    if b"STATUS: OK\r\n" not in decoder.text_tail or error_frames:
        report["statuses"]["transport_frame_error"] = True
    report["statuses"]["text_mode_error"] = not text_mode_ok
    session_complete = bool(decoder.ended and final_stats and
                            b"STREAM: TEXT\r\n" in decoder.text_tail and
                            b"STATUS: OK\r\n" in decoder.text_tail)
    report["statuses"]["incomplete_session"] = not session_complete
    csv_matches_end = bool(final_stats and count == final_stats["USB_SAMPLES_SENT"])
    report["statuses"]["diagnostics_error"] = bool(
        diagnostic_errors or not post_status_matches_end or
        not diagnostics_session_match)
    report["statuses"]["mode_verification_error"] = bool(
        acq_mode is not None and not mode_confirmed)
    profile_confirmed = bool(post_status and decoder.config and
        post_status.get("PROFILE") == active_profile["name"] and
        post_status.get("SAMPLE_PERIOD_US") == str(active_profile["sample_period_us"]) and
        post_status.get("PROFILE_ADC_CONFIG") == f"0x{active_profile['adc_config']:04X}" and
        post_status.get("ADC_CONFIG_READBACK") == f"0x{active_profile['adc_config']:04X}" and
        decoder.period_us == active_profile["sample_period_us"] and
        decoder.config[0] == active_profile["adc_config"] and
        decoder.config[1] == active_profile["sample_period_us"])
    actual_profile_name = post_status.get("PROFILE", "UNKNOWN") if post_status else "UNKNOWN"
    report["statuses"]["profile_verification_error"] = not profile_confirmed
    if post_status:
        try:
            if any(int(post_status.get(key, "0")) for key in ("DMA_ERRORS", "DMA_TIMEOUTS")):
                report["statuses"]["acquisition_loss"] = True
            if int(post_status.get("TIMER_PERIOD_VIOLATIONS", "0")):
                report["statuses"]["period_violation"] = True
        except ValueError:
            report["statuses"]["diagnostics_error"] = True
    acquisition_duration_us, acquisition_duration_source, acq_duration_error = (
        elapsed_us_from_status(final_stats, post_status,
                               "ACQ_ELAPSED_US64", "ACQ_ELAPSED_US"))
    stream_duration_us, stream_duration_source, stream_duration_error = (
        elapsed_us_from_status(final_stats, post_status,
                               "STREAM_ELAPSED_US64", "STREAM_ELAPSED_US"))
    if acq_duration_error:
        diagnostic_errors["acquisition_duration"] = acq_duration_error
    if stream_duration_error:
        diagnostic_errors["stream_duration"] = stream_duration_error
    if acq_duration_error or stream_duration_error:
        report["statuses"]["diagnostics_error"] = True
    actual_rate_hz = (final_stats["ADC_SAMPLES"] * 1000000 / acquisition_duration_us
                      if final_stats and acquisition_duration_us else None)
    bins = Counter(item[2] for item in records)
    with histogram.open("x", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(("vshunt_code", "current_mA", "count", "fraction"))
        for bin_number in sorted(bins):
            writer.writerow((bin_number, bin_number / 16, bins[bin_number], bins[bin_number] / count))
    with anomalies.open("x", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(("slot", "tim2_read_us", "vshunt_code", "code_20bit_hex", "current_mA"))
        for slot, stamp, code in report["anomalies"]:
            writer.writerow((slot, stamp, code, f"0x{code & 0xFFFFF:05X}", code / 16))
    report["anomalies"] = report["anomalies"][:10]
    report.update({"port": port_name, "captured_at_utc": captured_at_utc,
                   "requested_profile": profile,
                   "pre_stream_profile": active_profile["name"],
                   "actual_profile": actual_profile_name,
                   "profile_confirmed": profile_confirmed,
                   "profile_parameters": active_profile,
                   "firmware_version": active_profile["firmware_version"],
                   "protocol_version": decoder.version,
                   "adc_config": f"0x{active_profile['adc_config']:04X}",
                   "sample_rate_hz": active_profile["sample_rate_hz"],
                   "sample_period_us": active_profile["sample_period_us"],
                   "vshct_us": active_profile["vshct_us"], "avg": active_profile["avg"],
                   "adcrange": active_profile["adcrange"],
                   "session_duration_us": acquisition_duration_us,
                   "session_duration_source": acquisition_duration_source,
                   "stream_duration_us": stream_duration_us,
                   "stream_duration_source": stream_duration_source,
                   "actual_samples": count,
                   "port_closed": progress["port_closed"],
                   "session_complete": session_complete,
                   "incomplete_reason": (None if session_complete else
                                          "END_MISSING" if not decoder.ended else
                                          "TEXT_ACK_MISSING"),
                   "csv_matches_end": csv_matches_end,
                   "actual_acquisition_rate_hz": actual_rate_hz,
                   "requested_acq_mode": acq_mode,
                   "pre_stream_acq_mode": pre_stream_mode,
                   "actual_acq_mode": actual_mode.upper(),
                   "mode_confirmed": mode_confirmed,
                   "requested_seconds": seconds,
                   "recording_seconds": duration, "status_interval_seconds": status_interval,
                   "range_min_mA": range_min_ma, "range_max_mA": range_max_ma,
                   "period_tolerance_us": period_tolerance_us,
                   "data_frames": data_frames, "status_frames": status_frames,
                   "error_frames": error_frames, "frame_gaps": decoder.frame_gaps,
                   "slot_gaps": decoder.slot_gaps, "crc_errors": decoder.crc_errors,
                   "format_errors": decoder.format_errors, "code_errors": decoder.code_errors,
                   "resync_bytes": decoder.resync_bytes, "device_counters": final_stats,
                   "device_snapshot_type": "CSF4_END" if final_stats else None,
                   "post_stream_status": post_status,
                   "post_stream_timing": post_timing,
                   "post_stream_anomaly": post_anomaly,
                   "post_diagnostic_errors": diagnostic_errors,
                   "post_diagnostic_failure_details": diagnostic_failure_details,
                   "post_status_matches_end": post_status_matches_end,
                   "diagnostics_session_match": diagnostics_session_match,
                   "text_mode_ok": text_mode_ok,
                   "post_end_text_hex": decoder.text_tail.hex(),
                   "first_cli_reply_hex": ping_reply.hex(),
                   "files": {"samples": str(output), "histogram": str(histogram),
                             "anomalies": str(anomalies), "metadata": str(metadata)}})
    with metadata.open("x", encoding="utf-8") as json_file:
        json.dump(report, json_file, indent=2)
    print(f"recording_s: {duration:.3f}")
    print(f"profile_requested: {profile or 'UNCHANGED'}; profile_actual: {actual_profile_name}; "
          f"sample_rate_hz: {active_profile['sample_rate_hz']}; "
          f"sample_period_us: {active_profile['sample_period_us']}; confirmed: {profile_confirmed}")
    print(f"acquisition_rate_hz: {actual_rate_hz}; acquisition_duration_us: {acquisition_duration_us}")
    print(f"acq_mode_requested: {acq_mode or 'UNSPECIFIED'}; "
          f"acq_mode_actual: {actual_mode.upper()}")
    print(f"post_capture_reports: status={post_status is not None}; "
          f"timing={post_timing is not None}; anomaly={post_anomaly is not None}; "
          f"session_match={diagnostics_session_match}")
    print(f"session_complete: {session_complete}; csv_matches_end: {csv_matches_end}; "
          f"end_received: {decoder.ended}")
    print(f"post_end_text_hex: {decoder.text_tail.hex()}; "
          f"first_cli_reply_hex: {ping_reply.hex()}")
    if post_anomaly is not None:
        print(f"device_ANOMALY_EVENTS: {post_anomaly.get('ANOMALY_EVENTS', 'UNKNOWN')}")
    if interrupted:
        print("capture interrupted; stream stop was requested")
    print(f"data_frames: {data_frames}; status_frames: {status_frames}; error_frames: {error_frames}; samples: {count}")
    print(f"host_rate_hz: {count / duration:.2f}; frame_gaps: {decoder.frame_gaps}; slot_gaps: {decoder.slot_gaps}")
    print(f"crc_errors: {decoder.crc_errors}; format_errors: {decoder.format_errors}; resync_bytes: {decoder.resync_bytes}")
    for name in ("full", "clean"):
        print(f"{name}_current_mA: {report[name]}")
    print(f"unique_vshunt_codes: {report['resolution']['unique_codes']}; "
          f"identical_adjacent_fraction: {report['resolution']['identical_adjacent_fraction']}")
    print(f"most_common_codes: {report['resolution']['most_common_codes']}")
    print(f"missed_slots: {report['missed_slots']} ({report['missed_slot_percent']:.3f}%); "
          f"period_violations: {report['period_violations']}; "
          f"outside_range: {report['anomaly_count']}")
    for slot, stamp, code in report["extremes"]:
        print(f"extreme slot={slot} tim2_us={stamp} code={code} "
              f"code20=0x{code & 0xFFFFF:05X} current_mA={code / 16:.4f}")
    print(f"statuses: {report['statuses']}")
    if diagnostic_errors:
        print(f"post_capture_diagnostic_errors: {diagnostic_errors}")
    print(f"raw CSV: {output}; histogram CSV: {histogram}; anomalies CSV: {anomalies}; metadata JSON: {metadata}")
    if final_stats:
        for key, value in final_stats.items():
            print(f"device_{key}: {value}")
    else:
        print("device_END: MISSING")
    return not any(value for key, value in report["statuses"].items()
                   if key != "suspicious_value")


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="CDC COM port, e.g. COM7")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--acq-mode", choices=("poll", "dma"), default="dma",
                        help="select and verify the acquisition mode before streaming")
    parser.add_argument("--profile", choices=PROFILE_NAMES,
                        help="select and verify a profile; omitted keeps the device selection")
    parser.add_argument("--output", help="samples CSV path; an existing file receives a numbered suffix")
    parser.add_argument("--histogram", help="histogram CSV path; an existing file receives a numbered suffix")
    parser.add_argument("--metadata", help="metadata JSON path")
    parser.add_argument("--anomalies", help="out-of-range samples CSV path")
    parser.add_argument("--range-min-ma", type=float, default=-50.0)
    parser.add_argument("--range-max-ma", type=float, default=10000.0)
    parser.add_argument("--period-tolerance-us", type=int, default=5)
    parser.add_argument("--status-interval", type=float, default=0.0,
                        help="request binary status frames during capture, seconds; 0 disables")
    args = parser.parse_args(argv)
    if (not math.isfinite(args.seconds) or args.seconds <= 0 or
            not math.isfinite(args.status_interval) or args.status_interval < 0 or
            not math.isfinite(args.range_min_ma) or not math.isfinite(args.range_max_ma) or
            args.range_min_ma > args.range_max_ma or args.period_tolerance_us < 0):
        parser.error("invalid duration, current range, or period tolerance")
    return args


def main():
    args = parse_arguments()
    try:
        success = capture(args.port, args.seconds, args.output,
                          args.histogram, args.status_interval, args.metadata,
                          args.anomalies, args.range_min_ma, args.range_max_ma,
                          args.period_tolerance_us, args.acq_mode, args.profile)
    except (OSError, RuntimeError, TimeoutError) as error:
        raise SystemExit(f"capture failed at {getattr(error, 'capture_stage', 'UNKNOWN')} "
                         f"command={getattr(error, 'capture_command', None)!r}: "
                         f"{type(error).__name__}: {error}") from error
    if not success:
        raise SystemExit("capture integrity or diagnostics check failed; see statuses above")


if __name__ == "__main__":
    main()
