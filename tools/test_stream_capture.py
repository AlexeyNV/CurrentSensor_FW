import contextlib
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

from stream_capture import (FrameDecoder, HEADER, STATS_KEYS, analyze_capture,
                            capture, elapsed_us_from_status, parse_arguments, read_text_report,
                            synchronize_cli,
                            unique_paths, wait_start_ack)


def packet(sequence, slot, codes, kind=1, period=100, adc_config=0xAA28, stamp=1000):
    count = len(codes) if kind == 1 else 0
    payload = (struct.pack(f"<{count}i", *codes) +
               struct.pack(f"<{count}H", *(index * period for index in range(count)))
               if kind == 1 else struct.pack("<19I", *range(19)) if kind in (2, 3)
               else struct.pack("<HHHHHHI", adc_config, period, 32, 4, 2048, 1024, 0)
               if kind == 4 else struct.pack("<I", 1))
    length = 32 + len(payload) + 4
    header = HEADER.pack(b"CSF4", 1, kind, 32, length, count, sequence,
                         slot, stamp, period, 0, 0)
    body = header + payload
    return body + struct.pack("<I", zlib.crc32(body))


class DecoderTests(unittest.TestCase):
    def test_64bit_duration_prefers_text_and_checks_binary_end(self):
        end = {"ACQ_ELAPSED_US": 5}
        self.assertEqual(elapsed_us_from_status(
            end, {"ACQ_ELAPSED_US64": str((1 << 32) + 5)},
            "ACQ_ELAPSED_US64", "ACQ_ELAPSED_US"),
            ((1 << 32) + 5, "TEXT_US64", None))
        self.assertEqual(elapsed_us_from_status(
            end, {}, "ACQ_ELAPSED_US64", "ACQ_ELAPSED_US"),
            (5, "CSF4_END_U32", None))
        for invalid in ("-1", "abc", str((1 << 64) + 5), str((1 << 32) + 6)):
            duration, source, error = elapsed_us_from_status(
                end, {"ACQ_ELAPSED_US64": invalid},
                "ACQ_ELAPSED_US64", "ACQ_ELAPSED_US")
            self.assertEqual((duration, source), (5, "CSF4_END_U32"))
            self.assertIsNotNone(error)

    def test_stream_duration_rollover_requires_exact_end_modulo(self):
        duration = 5402197401
        end = {"STREAM_ELAPSED_US": duration & 0xFFFFFFFF}
        self.assertEqual(elapsed_us_from_status(
            end, {"STREAM_ELAPSED_US64": str(duration)},
            "STREAM_ELAPSED_US64", "STREAM_ELAPSED_US"),
            (duration, "TEXT_US64", None))
        fallback, source, error = elapsed_us_from_status(
            end, {"STREAM_ELAPSED_US64": str(duration + 667)},
            "STREAM_ELAPSED_US64", "STREAM_ELAPSED_US")
        self.assertEqual((fallback, source),
                         (end["STREAM_ELAPSED_US"], "CSF4_END_U32"))
        self.assertEqual(error, "inconsistent STREAM_ELAPSED_US64")

    def test_acq_mode_arguments(self):
        self.assertEqual(parse_arguments(["--port", "COM1"]).acq_mode, "dma")
        self.assertEqual(parse_arguments(["--port", "COM1", "--acq-mode", "poll"]).acq_mode,
                         "poll")
        self.assertEqual(parse_arguments(["--port", "COM1", "--acq-mode", "dma"]).acq_mode,
                         "dma")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as rejected:
                parse_arguments(["--port", "COM1", "--acq-mode", "invalid"])
        self.assertEqual(rejected.exception.code, 2)

    def test_cli_sync_discards_partial_line_and_stale_error(self):
        class FakePort:
            def __init__(self):
                self.rx = bytearray(b"ERR: UNKNOWN_COMMAND\r\n")
                self.writes = []

            def reset_input_buffer(self):
                self.rx.clear()

            def write(self, data):
                self.writes.append(data)
                if data == b"\n":
                    self.rx.extend(b"ERR: UNKNOWN_COMMAND\r\n")
                if data == b"ping\n":
                    self.rx.extend(b"pong\r\n")

            def read(self, size):
                data = bytes(self.rx[:size])
                del self.rx[:size]
                return data

        port = FakePort()
        synchronize_cli(port)
        self.assertEqual(port.writes[:2], [b"\n", b"ping\n"])
        port.rx.extend(b"ERR: UNKNOWN_COMMAND\r\nSTREAM: BINARY V1\r\n")
        wait_start_ack(port, 0.1)

    def test_analysis_keeps_outliers_and_distinguishes_faults(self):
        decoder = FrameDecoder()
        decoder.slot_gaps = 2
        decoder.ended = True
        decoder.period_us = 100
        records = [(0, 1000, 0), (1, 1100, -524288),
                   (4, 1400, 160), (5, 1540, 524287)]
        final_stats = dict.fromkeys(STATS_KEYS, 0)
        final_stats["LOST_SAMPLE_SLOTS"] = 2
        final_stats["ADC_SAMPLES"] = len(records)
        final_stats["USB_SAMPLES_SENT"] = len(records)
        report = analyze_capture(records, decoder, final_stats, -10, 10000, 5)
        self.assertEqual(report["anomaly_count"], 2)
        self.assertEqual(report["full"]["count"], 4)
        self.assertEqual(report["clean"]["count"], 2)
        self.assertEqual(report["missed_slots"], 2)
        self.assertEqual(report["period_violations"], 1)
        self.assertTrue(report["statuses"]["acquisition_loss"])
        self.assertTrue(report["statuses"]["period_violation"])
        self.assertTrue(report["statuses"]["suspicious_value"])
        self.assertFalse(report["statuses"]["transport_frame_error"])
        self.assertFalse(report["statuses"]["transport_data_loss"])

    def test_negative_zero_noise_is_clean_and_outlier_only_is_not_transport_error(self):
        decoder = FrameDecoder()
        decoder.ended = True
        decoder.period_us = 100
        final_stats = dict.fromkeys(STATS_KEYS, 0)
        final_stats["ADC_SAMPLES"] = 3
        final_stats["USB_SAMPLES_SENT"] = 3
        records = [(0, 1000, -16), (1, 1100, 16), (2, 1200, 8192)]
        report = analyze_capture(records, decoder, final_stats, -50, 50, 5)
        self.assertEqual(report["clean"]["count"], 2)
        self.assertEqual(report["anomaly_count"], 1)
        self.assertTrue(report["statuses"]["suspicious_value"])
        self.assertFalse(report["statuses"]["transport_frame_error"])
        self.assertFalse(report["statuses"]["transport_data_loss"])
        self.assertFalse(report["statuses"]["acquisition_loss"])

        decoder.crc_errors = 1
        damaged = analyze_capture(records, decoder, final_stats, -50, 50, 5)
        self.assertTrue(damaged["statuses"]["transport_frame_error"])
        self.assertFalse(damaged["statuses"]["acquisition_loss"])

    def test_unique_paths_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "samples.csv"
            path.write_text("old", encoding="utf-8")
            output, histogram, metadata, anomalies = unique_paths(path, Path(folder) / "hist.csv")
            self.assertNotEqual(output, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(len({output, histogram, metadata, anomalies}), 4)

    def test_timestamp_wrap_and_transport_gap(self):
        decoder = FrameDecoder()
        decoder.ended = True
        decoder.period_us = 100
        decoder.slot_gaps = 1
        final_stats = dict.fromkeys(STATS_KEYS, 0)
        final_stats["ADC_SAMPLES"] = 2
        final_stats["USB_SAMPLES_SENT"] = 2
        report = analyze_capture([(0, 0xFFFFFFF0, -1), (1, 84, 0)],
                                 decoder, final_stats, -1, 1, 5)
        self.assertEqual(report["period_violations"], 0)
        self.assertTrue(report["statuses"]["transport_data_loss"])
        self.assertFalse(report["statuses"]["acquisition_loss"])

    def test_fragments_and_signed_codes(self):
        decoder = FrameDecoder()
        data = packet(0, 0, [-524288, 524287])
        self.assertEqual(decoder.feed(data[:7]), [])
        self.assertEqual(decoder.feed(data[7:24]), [])
        frames = decoder.feed(data[24:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["samples"],
                         [(0, 1000, -524288), (1, 1100, 524287)])
        self.assertEqual(decoder.crc_errors, 0)

    def test_crc_resync_and_gaps(self):
        decoder = FrameDecoder()
        corrupt = bytearray(packet(0, 0, [1]))
        corrupt[-1] ^= 0x20
        frames = decoder.feed(b"junk" + corrupt + packet(2, 4, [2]))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decoder.crc_errors, 1)
        self.assertEqual(decoder.slot_gaps, 4)
        self.assertGreater(decoder.resync_bytes, 0)
        frames = decoder.feed(packet(4, 8, [3]))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decoder.frame_gaps, 1)
        self.assertEqual(decoder.slot_gaps, 7)

    def test_end_and_text_boundary(self):
        decoder = FrameDecoder()
        boundary = b"STREAM: TEXT\r\nSTATUS: OK\r\n"
        frames = decoder.feed(packet(0, 0, [0]) + packet(1, 0, [], kind=3) +
                              boundary[:9])
        decoder.feed(boundary[9:])
        self.assertEqual([frame["type"] for frame in frames], [1, 3])
        self.assertTrue(decoder.ended)
        self.assertEqual(decoder.text_tail, boundary)
        self.assertEqual(frames[1]["stats"]["ADC_SAMPLES"], 0)

    def test_multiline_report_after_end_boundary(self):
        class Port:
            def __init__(self):
                self.writes = []
                self.lines = iter((b"SESSION_TYPE: STREAM\r\n",
                                   b"ACQ_ELAPSED_US64: 5402197401\r\n",
                                   b"TIMER_TICKS: 10000\r\n",
                                   b"SAMPLES: 9999\r\n",
                                   b"RATE_HZ: 9999\r\n",
                                   b"STATUS: OK\r\n"))

            def write(self, data):
                self.writes.append(data)

            def readline(self):
                return next(self.lines, b"")

        port = Port()
        fields = read_text_report(port, "stream status")
        self.assertEqual(port.writes, [b"stream status\n"])
        self.assertEqual(fields["ACQ_ELAPSED_US64"], "5402197401")
        self.assertEqual(fields["TIMER_TICKS"], "10000")
        self.assertEqual(fields["SAMPLES"], "9999")
        self.assertEqual(fields["RATE_HZ"], "9999")

    def test_binary_error(self):
        decoder = FrameDecoder()
        frames = decoder.feed(packet(0, 0, [], kind=5))
        self.assertEqual(frames[0]["error_code"], 1)
        self.assertEqual(decoder.format_errors, 0)

    def test_invalid_signed_20bit_code(self):
        decoder = FrameDecoder()
        decoder.feed(packet(0, 0, [524288]))
        self.assertEqual(decoder.code_errors, 1)

    def test_capture_start_stop_and_text_recovery(self):
        end = bytearray(packet(2, 0, [], kind=3))
        struct.pack_into("<19I", end, 32, 2, 2, *([0] * 17))
        struct.pack_into("<I", end, len(end) - 4, zlib.crc32(end[:-4]))

        class FakeSerial:
            latest = None
            reject_mode = False
            ignore_mode = False
            reject_timing = False
            post_mode_override = None
            elapsed_us64 = None

            def __init__(self, *args, **kwargs):
                self.rx = bytearray()
                self.mode = "DMA"
                self.writes = []
                self.closed = False
                FakeSerial.latest = self

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True
                return False

            def reset_input_buffer(self):
                self.rx.clear()

            def write(self, data):
                self.writes.append(data)
                if data == b"stream start\n":
                    self.rx.extend(b"STREAM: BINARY V1\r\n" + packet(0, 0, [-1, 2]))
                elif data in (b"acq mode poll\n", b"acq mode dma\n"):
                    if FakeSerial.reject_mode:
                        self.rx.extend(b"ERR: ACQ_ACTIVE\r\n")
                    else:
                        if not FakeSerial.ignore_mode:
                            self.mode = "POLL" if data == b"acq mode poll\n" else "DMA"
                        self.rx.extend(b"STATUS: OK\r\n")
                elif data == b"acq config\n":
                    self.rx.extend(f"ACQ_MODE_SELECTED: {self.mode}\r\nSTATUS: OK\r\n".encode("ascii"))
                elif data == b"acq profile show\n":
                    self.rx.extend(b"PROFILE_ID: 0\r\nPROFILE: FAST\r\nSAMPLE_RATE_HZ: 10000\r\n"
                                   b"SAMPLE_PERIOD_US: 100\r\nVSHCT_US: 50\r\nAVG: 1\r\n"
                                   b"ADC_CONFIG: 0xAA28\r\nADCRANGE: 0\r\nFW: 0.4.3\r\n"
                                   b"STATUS: OK\r\n")
                elif data == b"stream config\n":
                    self.rx.extend(packet(1, 0, [], kind=4))
                elif data == b"stream stop\n":
                    self.rx.extend(end + b"STREAM: TEXT\r\nSTATUS: OK\r\n")
                elif data == b"ping\n":
                    self.rx.extend(b"pong\r\n")
                elif data == b"stream status\n":
                    self.rx.extend(b"SESSION_TYPE: STREAM\r\nSESSION_ID: 1\r\n"
                                   b"PROFILE: FAST\r\nSAMPLE_PERIOD_US: 100\r\n"
                                   b"PROFILE_ADC_CONFIG: 0xAA28\r\nADC_CONFIG_READBACK: 0xAA28\r\n"
                                   + f"ACQ_MODE: {FakeSerial.post_mode_override or self.mode}\r\n".encode("ascii") +
                                   b"ADC_SAMPLES: 2\r\nUSB_SAMPLES_SENT: 2\r\n"
                                   b"LOST_SAMPLE_SLOTS: 0\r\nSPI_ERRORS: 0\r\n"
                                   + (f"ACQ_ELAPSED_US64: {FakeSerial.elapsed_us64}\r\n"
                                      f"STREAM_ELAPSED_US64: {FakeSerial.elapsed_us64}\r\n".encode("ascii")
                                      if FakeSerial.elapsed_us64 is not None else b"") +
                                   b"STATUS: OK\r\n")
                elif data == b"stream timing\n":
                    if FakeSerial.reject_timing:
                        self.rx.extend(b"ERR: BAD_COMMAND\r\n")
                    else:
                        self.rx.extend(b"SESSION_ID: 1\r\nMAX_BUILD_US: 7\r\nSTATUS: OK\r\n")
                elif data == b"stream anomaly\n":
                    self.rx.extend(b"SESSION_ID: 1\r\nANOMALY_EVENTS: 0\r\nSTATUS: OK\r\n")
                return len(data)

            def read(self, size):
                result = bytes(self.rx[:size])
                del self.rx[:size]
                return result

            def readline(self):
                index = self.rx.find(b"\n")
                if index < 0:
                    return b""
                return self.read(index + 1)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "samples.csv"
            histogram = Path(folder) / "hist.csv"
            fake_module = types.SimpleNamespace(Serial=FakeSerial)
            with patch.dict(sys.modules, {"serial": fake_module}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(capture("COM_TEST", 0.001, output, histogram, 0,
                                            range_min_ma=-10))
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 3)
            self.assertTrue(histogram.exists())
            metadata = json.loads((Path(folder) / "samples_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["full"]["count"], 2)
            self.assertEqual(metadata["anomaly_count"], 0)
            self.assertTrue(metadata["post_status_matches_end"])
            self.assertTrue(metadata["diagnostics_session_match"])
            self.assertEqual(metadata["post_stream_status"]["SESSION_ID"], "1")
            self.assertEqual(metadata["actual_acq_mode"], "DMA")
            self.assertEqual(metadata["requested_acq_mode"], "dma")
            self.assertEqual(metadata["clean"]["count"], 2)
            self.assertEqual(metadata["post_stream_timing"]["MAX_BUILD_US"], "7")
            self.assertEqual(metadata["post_stream_anomaly"]["ANOMALY_EVENTS"], "0")
            self.assertEqual(bytes.fromhex(metadata["post_end_text_hex"]),
                             b"STREAM: TEXT\r\nSTATUS: OK\r\n")
            self.assertEqual(bytes.fromhex(metadata["first_cli_reply_hex"]),
                             b"pong\r\n")
            self.assertIn(b"acq mode dma\n", FakeSerial.latest.writes)
            self.assertEqual(FakeSerial.latest.writes[-3:],
                             [b"stream status\n", b"stream timing\n", b"stream anomaly\n"])

            FakeSerial.elapsed_us64 = 1 << 32
            try:
                with patch.dict(sys.modules, {"serial": fake_module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertTrue(capture("COM_TEST", 0.001, Path(folder) / "rollover.csv",
                                                range_min_ma=-10))
                rollover_meta = json.loads((Path(folder) / "rollover_metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(rollover_meta["session_duration_us"], 1 << 32)
                self.assertEqual(rollover_meta["session_duration_source"], "TEXT_US64")
                self.assertEqual(rollover_meta["stream_duration_us"], 1 << 32)
                self.assertAlmostEqual(rollover_meta["actual_acquisition_rate_hz"],
                                       2 * 1000000 / (1 << 32))
            finally:
                FakeSerial.elapsed_us64 = None

            FakeSerial.elapsed_us64 = (1 << 32) + 1
            try:
                with patch.dict(sys.modules, {"serial": fake_module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertFalse(capture("COM_TEST", 0.001, Path(folder) / "bad_duration.csv",
                                                 range_min_ma=-10))
                bad_duration_meta = json.loads((Path(folder) / "bad_duration_metadata.json").read_text(encoding="utf-8"))
                self.assertTrue(bad_duration_meta["statuses"]["diagnostics_error"])
                self.assertFalse(bad_duration_meta["statuses"]["transport_frame_error"])
                self.assertIn("acquisition_duration", bad_duration_meta["post_diagnostic_errors"])
                self.assertEqual(bad_duration_meta["session_duration_source"], "CSF4_END_U32")
            finally:
                FakeSerial.elapsed_us64 = None

            with patch.dict(sys.modules, {"serial": fake_module}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(capture("COM_TEST", 0.001, Path(folder) / "poll.csv",
                                            range_min_ma=-10, acq_mode="poll"))
            poll_meta = json.loads((Path(folder) / "poll_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(poll_meta["actual_acq_mode"], "POLL")
            self.assertEqual(poll_meta["requested_acq_mode"], "poll")
            self.assertLess(FakeSerial.latest.writes.index(b"acq mode poll\n"),
                            FakeSerial.latest.writes.index(b"stream start\n"))
            self.assertLess(FakeSerial.latest.writes.index(b"acq config\n"),
                            FakeSerial.latest.writes.index(b"stream start\n"))
            self.assertEqual(poll_meta["pre_stream_acq_mode"], "POLL")

            with patch.dict(sys.modules, {"serial": fake_module}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(capture("COM_TEST", 0.001, Path(folder) / "dma.csv",
                                            range_min_ma=-10, acq_mode="dma"))
            self.assertIn(b"acq mode dma\n", FakeSerial.latest.writes)

            with patch.dict(sys.modules, {"serial": fake_module}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(capture("COM_TEST", 0.001, Path(folder) / "suspect.csv",
                                            range_min_ma=0, range_max_ma=0))
            suspect_meta = json.loads((Path(folder) / "suspect_metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(suspect_meta["statuses"]["suspicious_value"])
            self.assertFalse(suspect_meta["statuses"]["transport_frame_error"])

            FakeSerial.reject_mode = True
            try:
                with patch.dict(sys.modules, {"serial": fake_module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "acq mode dma rejected"):
                            capture("COM_TEST", 0.001, Path(folder) / "rejected.csv",
                                    acq_mode="dma")
                self.assertNotIn(b"stream start\n", FakeSerial.latest.writes)
            finally:
                FakeSerial.reject_mode = False

            FakeSerial.ignore_mode = True
            try:
                with patch.dict(sys.modules, {"serial": fake_module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "acq config did not confirm"):
                            capture("COM_TEST", 0.001,
                                    Path(folder) / "mismatch.csv", acq_mode="poll")
                self.assertNotIn(b"stream start\n", FakeSerial.latest.writes)
            finally:
                FakeSerial.ignore_mode = False

            FakeSerial.post_mode_override = "DMA"
            try:
                with patch.dict(sys.modules, {"serial": fake_module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertFalse(capture("COM_TEST", 0.001,
                                                 Path(folder) / "post_mismatch.csv",
                                                 acq_mode="poll"))
                mismatch_meta = json.loads((Path(folder) / "post_mismatch_metadata.json").read_text(encoding="utf-8"))
                self.assertTrue(mismatch_meta["statuses"]["mode_verification_error"])
                self.assertFalse(mismatch_meta["mode_confirmed"])
                self.assertFalse(mismatch_meta["statuses"]["transport_frame_error"])
            finally:
                FakeSerial.post_mode_override = None

            FakeSerial.reject_timing = True
            try:
                with patch.dict(sys.modules, {"serial": fake_module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertFalse(capture("COM_TEST", 0.001,
                                                 Path(folder) / "diagnostic.csv"))
                diagnostic_meta = json.loads((Path(folder) / "diagnostic_metadata.json").read_text(encoding="utf-8"))
                self.assertTrue(diagnostic_meta["statuses"]["diagnostics_error"])
                self.assertFalse(diagnostic_meta["statuses"]["transport_frame_error"])
                self.assertIn("timing", diagnostic_meta["post_diagnostic_errors"])
            finally:
                FakeSerial.reject_timing = False


if __name__ == "__main__":
    unittest.main()
