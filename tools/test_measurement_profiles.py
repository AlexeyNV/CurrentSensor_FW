import contextlib
import csv
import io
import json
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from stream_capture import (FrameDecoder, STATS_KEYS, analyze_capture, capture,
                            get_active_profile, get_available_profiles,
                            parse_arguments, resolution_stats, set_profile)
from test_stream_capture import packet


# Expected values deliberately independent of the firmware implementation.
PROFILES = {"fast": (10000, 100, 50, 0xAA28),
            "balanced": (5000, 200, 150, 0xAAA8),
            "precision": (2500, 400, 280, 0xAAE8),
            "low_noise": (1000, 1000, 540, 0xAB28)}


class ProfileSerial:
    initial_profile = "fast"
    ignore_set = False
    reject_set = False
    wrong_post_profile = False
    latest = None

    def __init__(self, *args, **kwargs):
        self.rx = bytearray()
        self.writes = []
        self.profile = self.initial_profile
        self.mode = "DMA"
        self.closed = False
        ProfileSerial.latest = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def reset_input_buffer(self):
        self.rx.clear()

    def profile_response(self):
        rate, period, vshct, adc = PROFILES[self.profile]
        return (f"PROFILE_ID: {list(PROFILES).index(self.profile)}\r\n"
                f"PROFILE: {self.profile.upper()}\r\nSAMPLE_RATE_HZ: {rate}\r\n"
                f"SAMPLE_PERIOD_US: {period}\r\nVSHCT_US: {vshct}\r\nAVG: 1\r\n"
                f"ADC_CONFIG: 0x{adc:04X}\r\nADCRANGE: 0\r\nACQ_MODE: {self.mode}\r\n"
                "FW: 0.4.3\r\nSTATUS: OK\r\n").encode("ascii")

    def write(self, data):
        self.writes.append(data)
        _, period, _, adc = PROFILES[self.profile]
        if data == b"ping\n":
            self.rx.extend(b"pong\r\n")
        elif data.startswith(b"acq mode "):
            self.mode = data.decode().split()[-1].upper()
            self.rx.extend(b"STATUS: OK\r\n")
        elif data == b"acq config\n":
            self.rx.extend(f"ACQ_MODE_SELECTED: {self.mode}\r\nSTATUS: OK\r\n".encode())
        elif data.startswith(b"acq profile set "):
            if self.reject_set:
                self.rx.extend(b"ERR: BUSY_STREAMING\r\n")
            else:
                if not self.ignore_set:
                    self.profile = data.decode().split()[-1].lower()
                self.rx.extend(self.profile_response())
        elif data == b"acq profile show\n":
            self.rx.extend(self.profile_response())
        elif data == b"acq profile list\n":
            for index, (name, values) in enumerate(PROFILES.items()):
                rate, slot_period, vshct, setting = values
                self.rx.extend(f"PROFILE_{index}: {name.upper()},{rate},{slot_period},{vshct},1,0x{setting:04X}\r\n".encode())
            self.rx.extend(b"PROFILE_COUNT: 4\r\nSTATUS: OK\r\n")
        elif data == b"stream start\n":
            self.rx.extend(b"STREAM: BINARY V1\r\n" + packet(0, 0, [-16, 16], period=period))
        elif data == b"stream config\n":
            self.rx.extend(packet(1, 0, [], kind=4, period=period, adc_config=adc))
        elif data == b"stream stop\n":
            end = bytearray(packet(2, 0, [], kind=3, period=period))
            stats = dict.fromkeys(STATS_KEYS, 0)
            stats.update(ADC_SAMPLES=2, USB_SAMPLES_SENT=2, ACQ_ELAPSED_US=2 * period)
            struct.pack_into("<19I", end, 32, *stats.values())
            struct.pack_into("<I", end, len(end) - 4, zlib.crc32(end[:-4]))
            self.rx.extend(end + b"STREAM: TEXT\r\nSTATUS: OK\r\n")
        elif data == b"stream status\n":
            actual_name = "WRONG" if self.wrong_post_profile else self.profile.upper()
            self.rx.extend((f"SESSION_TYPE: STREAM\r\nSESSION_ID: 7\r\nACQ_MODE: {self.mode}\r\n"
                            f"PROFILE: {actual_name}\r\nSAMPLE_PERIOD_US: {period}\r\n"
                            f"PROFILE_ADC_CONFIG: 0x{adc:04X}\r\nADC_CONFIG_READBACK: 0x{adc:04X}\r\n"
                            "ADC_SAMPLES: 2\r\nUSB_SAMPLES_SENT: 2\r\n"
                            "LOST_SAMPLE_SLOTS: 0\r\nSPI_ERRORS: 0\r\nSTATUS: OK\r\n").encode())
        elif data in (b"stream timing\n", b"stream anomaly\n"):
            self.rx.extend(b"SESSION_TYPE: STREAM\r\nSESSION_ID: 7\r\nSTATUS: OK\r\n")
        return len(data)

    def read(self, size):
        result = bytes(self.rx[:size])
        del self.rx[:size]
        return result

    def readline(self):
        index = self.rx.find(b"\n")
        return self.read(index + 1) if index >= 0 else b""


class ProfileTests(unittest.TestCase):
    def tearDown(self):
        ProfileSerial.initial_profile = "fast"
        ProfileSerial.ignore_set = False
        ProfileSerial.reject_set = False
        ProfileSerial.wrong_post_profile = False

    def test_arguments(self):
        self.assertIsNone(parse_arguments(["--port", "COM1"]).profile)
        for profile in PROFILES:
            self.assertEqual(parse_arguments(["--port", "COM1", "--profile", profile]).profile,
                             profile)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_arguments(["--port", "COM1", "--profile", "unknown"])

    def test_profile_control_api_and_mismatch(self):
        port = ProfileSerial()
        profiles = get_available_profiles(port)
        self.assertEqual(len(profiles), 4)
        for profile, values in zip(profiles, PROFILES.values()):
            self.assertEqual(profile["sample_period_us"], values[1])
        self.assertEqual(set_profile(port, "BALANCED")["name"], "BALANCED")
        self.assertEqual(get_active_profile(port)["vshct_us"], 150)
        with self.assertRaises(ValueError):
            set_profile(port, "fast\nstream start")
        ProfileSerial.ignore_set = True
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            set_profile(port, "precision")
        ProfileSerial.reject_set = True
        with self.assertRaisesRegex(RuntimeError, "BUSY_STREAMING"):
            set_profile(port, "fast")

    def run_capture(self, folder, profile=None):
        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=ProfileSerial)}), \
                contextlib.redirect_stdout(io.StringIO()):
            success = capture("COM_TEST", 0.001, Path(folder) / "data.csv", profile=profile)
        metadata = json.loads((Path(folder) / "data_metadata.json").read_text())
        with (Path(folder) / "data.csv").open(newline="") as source:
            rows = list(csv.DictReader(source))
        return success, metadata, rows

    def test_capture_each_profile_csv_metadata_and_diagnostics(self):
        for name, (rate, period, vshct, adc) in PROFILES.items():
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as folder:
                success, metadata, rows = self.run_capture(folder, name)
                self.assertTrue(success)
                self.assertEqual(metadata["requested_profile"], name)
                self.assertEqual(metadata["actual_profile"], name.upper())
                self.assertEqual(metadata["sample_rate_hz"], rate)
                self.assertEqual(metadata["sample_period_us"], period)
                self.assertEqual(metadata["vshct_us"], vshct)
                self.assertEqual(metadata["adc_config"], f"0x{adc:04X}")
                self.assertEqual(metadata["firmware_version"], "0.4.3")
                self.assertEqual(metadata["protocol_version"], 1)
                self.assertEqual(metadata["session_duration_us"], 2 * period)
                self.assertEqual(metadata["actual_samples"], 2)
                self.assertEqual(metadata["period_violations"], 0)
                self.assertEqual(int(rows[1]["tim2_read_us"]) - int(rows[0]["tim2_read_us"]), period)
                self.assertEqual([int(row["sample_period_us"]) for row in rows], [period, period])
                self.assertTrue(ProfileSerial.latest.closed)
                self.assertEqual(ProfileSerial.latest.writes[-3:],
                                 [b"stream status\n", b"stream timing\n", b"stream anomaly\n"])

    def test_omitted_profile_keeps_device_selection(self):
        ProfileSerial.initial_profile = "precision"
        with tempfile.TemporaryDirectory() as folder:
            success, metadata, _ = self.run_capture(folder)
            self.assertTrue(success)
            self.assertEqual(metadata["actual_profile"], "PRECISION")
            self.assertIsNone(metadata["requested_profile"])
            self.assertFalse(any(command.startswith(b"acq profile set ")
                                 for command in ProfileSerial.latest.writes))

    def test_rejected_or_ignored_selection_does_not_start(self):
        for setting in ("reject_set", "ignore_set"):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as folder:
                setattr(ProfileSerial, setting, True)
                with self.assertRaises(RuntimeError):
                    self.run_capture(folder, "low_noise")
                self.assertNotIn(b"stream start\n", ProfileSerial.latest.writes)
                self.assertTrue(ProfileSerial.latest.closed)
                setattr(ProfileSerial, setting, False)

    def test_post_profile_mismatch_fails_without_claiming_crc_damage(self):
        ProfileSerial.wrong_post_profile = True
        with tempfile.TemporaryDirectory() as folder:
            success, metadata, _ = self.run_capture(folder, "balanced")
            self.assertFalse(success)
            self.assertTrue(metadata["statuses"]["profile_verification_error"])
            self.assertEqual(metadata["actual_profile"], "WRONG")
            self.assertFalse(metadata["statuses"]["transport_frame_error"])

    def test_csf4_v1_all_periods_loss_and_wrap(self):
        for _, period, _, _ in PROFILES.values():
            decoder = FrameDecoder()
            frames = decoder.feed(packet(0, 0, [0, -1], period=period, stamp=0xFFFFFFF0))
            frames += decoder.feed(packet(1, 3, [78], period=period,
                                          stamp=(0xFFFFFFF0 + 3 * period) & 0xFFFFFFFF))
            decoder.ended = True
            records = [sample for frame in frames for sample in frame["samples"]]
            stats = dict.fromkeys(STATS_KEYS, 0)
            stats.update(ADC_SAMPLES=3, USB_SAMPLES_SENT=3, LOST_SAMPLE_SLOTS=1)
            report = analyze_capture(records, decoder, stats, -50, 50, 5)
            self.assertEqual(decoder.period_us, period)
            self.assertEqual(report["period_violations"], 0)
            self.assertEqual(report["missed_slots"], 1)
            self.assertTrue(report["statuses"]["acquisition_loss"])
            self.assertFalse(report["statuses"]["transport_frame_error"])
        decoder = FrameDecoder()
        decoder.feed(packet(0, 0, [0], period=200) + packet(1, 1, [1], period=400))
        self.assertEqual(decoder.format_errors, 1)

    def test_resolution_observed_spacing_and_contiguous_changes(self):
        records = [(0, 0xFFFFFFF0, -78), (1, 84, 0), (2, 184, 0),
                   (4, 384, 78), (5, 1000084, 156)]
        result = resolution_stats(records)
        self.assertEqual(result["unique_codes"], 4)
        self.assertEqual(result["observed_code_spacing"], [{"delta_code": 78, "count": 3}])
        self.assertEqual(result["contiguous_pairs"], 3)
        self.assertEqual(result["pairs_excluded_by_slot_gap"], 1)
        self.assertAlmostEqual(result["identical_adjacent_fraction"], 1 / 3)
        self.assertEqual(len(result["drift_windows"]), 2)
        self.assertEqual(result["most_common_codes"][0], {"code": 0, "count": 2, "fraction": 0.4})
        self.assertEqual(resolution_stats([])["unique_codes"], 0)


if __name__ == "__main__":
    unittest.main()
