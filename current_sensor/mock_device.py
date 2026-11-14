"""Explicit DEMO/CSF4 replay serial device; never opens real hardware."""
import struct
import time
import zlib
from pathlib import Path
import numpy as np
import serial
from tools.stream_capture import HEADER, STATS_KEYS, FrameDecoder

# Device-side fixture, not a second source of profile truth in the GUI.
MOCK_PROFILES = {"FAST": (10000, 100, 50, 0xAA28), "BALANCED": (5000, 200, 150, 0xAAA8),
                 "PRECISION": (2500, 400, 280, 0xAAE8), "LOW_NOISE": (1000, 1000, 540, 0xAB28)}


def encode_frame(kind, sequence, period, slot=0, stamp=0, codes=(), stats=None, adc=0xAA28):
    if kind == 1:
        payload = struct.pack(f"<{len(codes)}i", *codes) + struct.pack(
            f"<{len(codes)}H", *(index * period for index in range(len(codes))))
    elif kind in (2, 3):
        payload = struct.pack("<19I", *(stats or dict.fromkeys(STATS_KEYS, 0)).values())
    elif kind == 4:
        payload = struct.pack("<HHHHHHI", adc, period, 32, 4, 2048, 1024, 0)
    else:
        payload = struct.pack("<I", 2)
    body = HEADER.pack(b"CSF4", 1, kind, 32, 36 + len(payload), len(codes), sequence & 0xFFFFFFFF,
                       slot & 0xFFFFFFFF, stamp & 0xFFFFFFFF, period, 0, 0) + payload
    return body + struct.pack("<I", zlib.crc32(body))


class MockSerial:
    def __init__(self, *args, replay=None, corrupt_at=None, skip_at=None, drop_after=None, **kwargs):
        self.rx = bytearray()
        self.closed = False
        self.streaming = False
        self.profile = "FAST"
        self.mode = "DMA"
        self.sequence = self.slot = self.samples = self.lost = self.frames = self.session = 0
        self.started = time.monotonic()
        self.cal = dict(OFFSET_UA="0", GAIN_PPM="1000000", ENABLED="0", SOURCE="DEFAULT", DIRTY="0")
        self.saved_cal = None
        self.replay = Path(replay) if replay else None
        self.replay_file = None
        self.source_decoder = None
        self.final_stats = None
        self.corrupt_at, self.skip_at, self.drop_after = corrupt_at, skip_at, drop_after
        self.writes = []
        if self.replay:
            detector = FrameDecoder()
            with self.replay.open("rb") as source:
                detector.feed(source.read(4096))
            matches = [name for name, values in MOCK_PROFILES.items() if values[1] == detector.period_us]
            if not matches:
                raise ValueError("Replay has no supported CSF4 header")
            self.profile = matches[0]

    def close(self):
        self.closed = True
        if self.replay_file:
            self.replay_file.close()

    def reset_input_buffer(self):
        self.rx.clear()

    def _text(self, fields=None, error=None):
        if error:
            self.rx.extend(f"ERR: {error}\r\n".encode())
        else:
            text = "".join(f"{key}: {value}\r\n" for key, value in (fields or {}).items())
            self.rx.extend((text + "STATUS: OK\r\n").encode())

    def _profile_fields(self):
        rate, period, ct, adc = MOCK_PROFILES[self.profile]
        return dict(PROFILE_ID=list(MOCK_PROFILES).index(self.profile), PROFILE=self.profile,
                    SAMPLE_RATE_HZ=rate, SAMPLE_PERIOD_US=period, VSHCT_US=ct, AVG=1,
                    ADC_CONFIG=f"0x{adc:04X}", PROFILE_ADC_CONFIG=f"0x{adc:04X}",
                    ADCRANGE=0, ACQ_MODE=self.mode, FW="0.4.3-DEMO")

    def write(self, data):
        if self.closed:
            raise serial.SerialException("Mock USB disconnected")
        for command in data.decode("ascii").splitlines():
            command = command.strip()
            self.writes.append(command)
            if not command:
                continue
            if command == "stream stop" and self.streaming:
                self._end()
            elif self.streaming:
                # Replay preserves stored frame sequences; never inject synthetic
                # CONFIG/STATS among original packets. Missing CONFIG in a file
                # fragment is honestly reported as an unverified session.
                if not self.replay:
                    self._control(4 if command == "stream config" else 2 if command in ("stream status", "stream stats") else 5)
            elif command == "ping":
                self.rx.extend(b"pong\r\n")
            elif command == "info":
                self._text(dict(FW="0.4.3-DEMO", MCU="MOCK - NO HARDWARE", USB="simulated"))
            elif command == "ina":
                self._text(dict(MANUFACTURER_ID="0x5449 OK", DEVICE_ID="0x2291"))
            elif command == "regs":
                self._text(dict(CONFIG="0x0000", ADC_CONFIG="0xFB68", DIAG_ALRT="0x0003"))
            elif command == "acq profile list":
                self._text(dict([(f"PROFILE_{i}", f"{name},{rate},{period},{ct},1,0x{adc:04X}")
                                 for i, (name, (rate, period, ct, adc)) in enumerate(MOCK_PROFILES.items())]
                                + [("PROFILE_COUNT", len(MOCK_PROFILES))]))
            elif command.startswith("acq profile set "):
                name = command.split()[-1].upper()
                if name not in MOCK_PROFILES:
                    self._text(error="INVALID_PROFILE")
                elif self.replay and name != self.profile:
                    self._text(error="REPLAY_PROFILE_FIXED")
                else:
                    self.profile = name
                    self._text(self._profile_fields())
            elif command == "acq profile show":
                self._text(self._profile_fields())
            elif command.startswith("acq mode "):
                self.mode = command.split()[-1].upper()
                self._text()
            elif command == "acq config":
                self._text(dict(self._profile_fields(), ACQ_MODE_SELECTED=self.mode))
            elif command == "stream start":
                self.streaming = True
                self.sequence = self.slot = self.samples = self.frames = self.lost = 0
                self.session += 1
                self.final_stats = None
                self.started = self.replay_due = time.monotonic()
                if self.replay:
                    self.replay_file = self.replay.open("rb")
                    self.source_decoder = FrameDecoder()
                self.rx.extend(b"STREAM: BINARY V1\r\n")
            elif command == "stream status":
                fields = dict(self._profile_fields(), SESSION_TYPE="STREAM", SESSION_ID=self.session,
                              ADC_CONFIG_READBACK=self._profile_fields()["ADC_CONFIG"],
                              DMA_ERRORS=0, DMA_TIMEOUTS=0, TIMER_PERIOD_VIOLATIONS=0)
                fields.update(self._stats())
                self._text(fields)
            elif command in ("stream timing", "stream anomaly"):
                self._text(dict(SESSION_TYPE="STREAM", SESSION_ID=self.session,
                                DEMO="SIMULATED", ANOMALY_EVENTS=0))
            elif command.startswith("stream anomaly set "):
                self._text()
            elif command.startswith("cal "):
                self._calibration(command)
            else:
                self._text(error="BAD_COMMAND")
        return len(data)

    def _calibration(self, command):
        parts = command.split()
        try:
            if command == "cal show":
                self._text(self.cal)
                return
            if len(parts) == 4 and parts[1] == "set":
                value = int(parts[3])
                if parts[2] == "offset_ua" and -100000 <= value <= 100000:
                    self.cal["OFFSET_UA"] = str(value)
                elif parts[2] == "gain_ppm" and 900000 <= value <= 1100000:
                    self.cal["GAIN_PPM"] = str(value)
                else:
                    raise ValueError()
                self.cal.update(SOURCE="RAM", DIRTY="1")
            elif parts[1] == "enable" and parts[2] in ("0", "1"):
                self.cal.update(ENABLED=parts[2], SOURCE="RAM", DIRTY="1")
            elif command == "cal save":
                self.cal.update(SOURCE="FLASH", DIRTY="0")
                self.saved_cal = self.cal.copy()
            elif command == "cal reset":
                self.cal.update(OFFSET_UA="0", GAIN_PPM="1000000", ENABLED="0", SOURCE="DEFAULT", DIRTY="1")
            elif command == "cal load":
                self.cal = (self.saved_cal or dict(OFFSET_UA="0", GAIN_PPM="1000000", ENABLED="0", SOURCE="DEFAULT", DIRTY="0")).copy()
            else:
                raise ValueError()
            self._text()
        except (ValueError, IndexError):
            self._text(error="OUT_OF_RANGE")

    def _stats(self):
        if self.final_stats is not None:
            return self.final_stats
        period = MOCK_PROFILES[self.profile][1]
        result = dict.fromkeys(STATS_KEYS, 0)
        result.update(ADC_SAMPLES=self.samples, USB_SAMPLES_SENT=self.samples,
                      FRAMES_SENT=self.frames, LOST_SAMPLE_SLOTS=self.lost,
                      MIN_SAMPLE_PERIOD_US=period, MAX_SAMPLE_PERIOD_US=period,
                      ACQ_ELAPSED_US=self.slot * period, STREAM_ELAPSED_US=self.slot * period)
        return result

    def _control(self, kind):
        _, period, _, adc = MOCK_PROFILES[self.profile]
        self.rx.extend(encode_frame(kind, self.sequence, period, stats=self._stats(), adc=adc))
        self.sequence += 1

    def _end(self):
        self.final_stats = self._stats().copy()
        self._control(3)
        self.streaming = False
        if self.replay_file:
            self.replay_file.close()
            self.replay_file = None
        self.rx.extend(b"STREAM: TEXT\r\nSTATUS: OK\r\n")

    def _produce(self):
        if self.drop_after is not None and self.samples >= self.drop_after:
            raise serial.SerialException("Simulated USB unplug")
        if self.replay_file:
            if time.monotonic() < self.replay_due:
                return
            wire = self.replay_file.read(4096)
            frames = self.source_decoder.feed(wire)
            for frame in frames:
                # Replay original complete packets, including their CRC and timestamps.
                self.rx.extend(frame["packet"])
                self.sequence = frame["sequence"] + 1
                if frame["type"] == 1:
                    self.samples += len(frame["samples"])
                    self.slot = frame["slot"] + len(frame["samples"])
                    self.replay_due += len(frame["samples"]) * frame["period_us"] / 1e6
                if frame["type"] == 3:
                    self.final_stats = frame["stats"]
                    self.streaming = False
                    self.replay_file.close()
                    self.replay_file = None
                    self.rx.extend(b"STREAM: TEXT\r\nSTATUS: OK\r\n")
            if not wire and self.streaming:
                self._end()
            return
        rate, period, _, _ = MOCK_PROFILES[self.profile]
        due = min(int((time.monotonic() - self.started) * rate), self.slot + 256)
        while self.slot < due:
            if self.skip_at == self.slot:
                self.slot += 1
                self.lost += 1
                continue
            count = min(32, due - self.slot)
            if self.skip_at is not None and self.slot < self.skip_at < self.slot + count:
                count = self.skip_at - self.slot
            slots = np.arange(self.slot, self.slot + count)
            codes = (1872 + 30 * np.sin(slots * .071) + 12 * np.sin(slots * .931)).astype(np.int32)
            codes[(slots % rate >= rate // 3) & (slots % rate < rate // 3 + 2)] += 80000
            codes[(slots // rate) % 4 == 2] += 1600
            frame = encode_frame(1, self.sequence, period, self.slot,
                                 0xFFFF0000 + self.slot * period, codes=codes.tolist())
            if self.sequence == self.corrupt_at:
                frame = frame[:-1] + bytes([frame[-1] ^ 0x40])
            self.rx.extend(frame)
            self.sequence += 1
            self.frames += 1
            self.slot += count
            self.samples += count

    def read(self, size=1):
        if self.closed:
            raise serial.SerialException("Mock port closed")
        if not self.rx and self.streaming:
            self._produce()
        if not self.rx:
            time.sleep(.001)
        result = bytes(self.rx[:size])
        del self.rx[:size]
        return result

    def readline(self):
        line = bytearray()
        deadline = time.monotonic() + .02
        while time.monotonic() < deadline:
            byte = self.read(1)
            line.extend(byte)
            if byte == b"\n":
                break
        return bytes(line)
