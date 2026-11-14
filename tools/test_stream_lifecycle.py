import contextlib
import io
import json
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from stream_capture import capture
from test_stream_capture import packet


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self):
        self.value += 0.5


class LifecycleSerial:
    instances = []
    scenario = "normal"
    clock = None

    def __init__(self, *args, **kwargs):
        self.rx = bytearray()
        self.closed = False
        self.streaming = False
        self.stop_requested = False
        self.stop_read_count = 0
        self.read_count = 0
        self.queue_relieved = threading.Event()
        self.writes = []
        self.ping_count = 0
        LifecycleSerial.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        if self.scenario == "close_error":
            raise OSError("port close failed")

    def reset_input_buffer(self):
        self.rx.clear()

    def _end(self):
        end = bytearray(packet(2, 0, [], kind=3))
        struct.pack_into("<19I", end, 32, 2, 2, *([0] * 17))
        struct.pack_into("<I", end, len(end) - 4, zlib.crc32(end[:-4]))
        self.rx.extend(end + b"STREAM: TEXT\r\nSTATUS: OK\r\n")

    def write(self, data):
        self.writes.append(data)
        if data == b"ping\n":
            self.ping_count += 1
            if self.scenario == "ping_write_timeout" and self.ping_count > 1:
                raise OSError("Write timeout")
            self.rx.extend(b"pong\r\n")
        elif data == b"acq mode dma\n":
            self.rx.extend(b"STATUS: OK\r\n")
        elif data == b"acq config\n":
            self.rx.extend(b"ACQ_MODE_SELECTED: DMA\r\nSTATUS: OK\r\n")
        elif data == b"acq profile show\n":
            self.rx.extend(b"PROFILE_ID: 0\r\nPROFILE: FAST\r\nSAMPLE_RATE_HZ: 10000\r\n"
                           b"SAMPLE_PERIOD_US: 100\r\nVSHCT_US: 50\r\nAVG: 1\r\n"
                           b"ADC_CONFIG: 0xAA28\r\nADCRANGE: 0\r\nFW: 0.4.4-debug\r\nSTATUS: OK\r\n")
        elif data == b"stream start\n":
            self.rx.extend(b"STREAM: BINARY V1\r\n" + packet(0, 0, [-1, 2]))
        elif data == b"stream config\n":
            self.rx.extend(packet(1, 0, [], kind=4))
            self.streaming = True
        elif data == b"stream stop\n":
            self.stop_requested = True
            self.stop_read_count = self.read_count
            if self.scenario == "write_timeout":
                raise OSError("Write timeout")
            if self.scenario == "full_queues":
                if not self.queue_relieved.wait(1.0):
                    raise OSError("OUT queue stayed full")
            if self.scenario not in ("no_end", "delayed_end"):
                self._end()
        elif data == b"stream status\n":
            self.rx.extend(b"SESSION_TYPE: STREAM\r\nSESSION_ID: 1\r\n"
                           b"PROFILE: FAST\r\nSAMPLE_PERIOD_US: 100\r\n"
                           b"PROFILE_ADC_CONFIG: 0xAA28\r\n"
                           b"ADC_CONFIG_READBACK: 0xAA28\r\nACQ_MODE: DMA\r\n"
                           b"ADC_SAMPLES: 2\r\nUSB_SAMPLES_SENT: 2\r\n"
                           b"LOST_SAMPLE_SLOTS: 0\r\nSPI_ERRORS: 0\r\nSTATUS: OK\r\n")
        elif data == b"stream timing\n":
            if self.scenario == "diagnostic_write_timeout":
                raise OSError("Write timeout")
            self.rx.extend(b"SESSION_ID: 1\r\nSTATUS: OK\r\n")
        elif data == b"stream anomaly\n":
            self.rx.extend(b"SESSION_ID: 1\r\nSTATUS: OK\r\n")
        return len(data)

    def read(self, size):
        if self.streaming:
            self.clock.advance()
            self.read_count += 1
            if self.stop_requested and self.read_count >= self.stop_read_count + 3:
                self.queue_relieved.set()
            if (self.scenario == "delayed_end" and self.stop_requested and
                    self.read_count >= self.stop_read_count + 3):
                self._end()
                self.scenario = "normal"
            if self.scenario == "disconnect" and self.read_count >= 2:
                raise OSError("USB disconnected")
            time.sleep(0.001)
        result = bytes(self.rx[:size])
        del self.rx[:size]
        return result

    def readline(self):
        index = self.rx.find(b"\n")
        if index < 0:
            return b""
        result = bytes(self.rx[:index + 1])
        del self.rx[:index + 1]
        return result


class LifecycleTests(unittest.TestCase):
    def test_port_open_and_close_stages(self):
        with tempfile.TemporaryDirectory() as folder:
            def fail_open(*args, **kwargs):
                raise OSError("port busy")

            output = Path(folder) / "open.csv"
            with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=fail_open)}), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    capture("COM_TEST", 1, output)
            report = json.loads((Path(folder) / "open_metadata.json").read_text())
            self.assertEqual(report["failure"]["stage"], "PORT_OPEN")
            self.assertFalse(report["failure"]["end_received"])

            LifecycleSerial.scenario = "close_error"
            LifecycleSerial.clock = Clock()
            output = Path(folder) / "close.csv"
            with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=LifecycleSerial)}), \
                 patch("stream_capture.time.monotonic", LifecycleSerial.clock), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    capture("COM_TEST", 1, output, range_min_ma=-10)
            report = json.loads((Path(folder) / "close_metadata.json").read_text())
            self.assertEqual(report["failure"]["stage"], "PORT_CLOSE")
            self.assertTrue(report["failure"]["end_received"])

    def capture_case(self, folder, name, seconds, scenario="normal"):
        LifecycleSerial.scenario = scenario
        LifecycleSerial.clock = Clock()
        output = Path(folder) / f"{name}.csv"
        fake_module = types.SimpleNamespace(Serial=LifecycleSerial)
        with patch.dict(sys.modules, {"serial": fake_module}), \
             patch("stream_capture.time.monotonic", LifecycleSerial.clock), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            result = capture("COM_TEST", seconds, output, range_min_ma=-10)
        metadata = json.loads((Path(folder) / f"{name}_metadata.json").read_text())
        return result, output, metadata, LifecycleSerial.instances[-1]

    def test_normal_stop_one_five_and_sixty_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            for seconds in (1, 5, 60):
                result, output, metadata, port = self.capture_case(
                    folder, f"duration_{seconds}", seconds)
                self.assertTrue(result)
                self.assertTrue(port.closed)
                self.assertIn(b"stream stop\n", port.writes)
                self.assertTrue(metadata["session_complete"])
                self.assertTrue(metadata["csv_matches_end"])
                self.assertEqual(metadata["actual_samples"], 2)
                self.assertEqual(len(output.read_text().splitlines()), 3)

    def test_stop_drains_active_stream_and_full_queues(self):
        with tempfile.TemporaryDirectory() as folder:
            for scenario in ("full_queues", "delayed_end"):
                result, _, metadata, port = self.capture_case(folder, scenario, 1, scenario)
                self.assertTrue(result)
                self.assertTrue(port.closed)
                self.assertTrue(metadata["session_complete"])
                self.assertGreaterEqual(port.read_count - port.stop_read_count, 3)

    def test_missing_end_is_explicit_incomplete_session(self):
        with tempfile.TemporaryDirectory() as folder:
            result, output, metadata, port = self.capture_case(folder, "missing_end", 1, "no_end")
            self.assertFalse(result)
            self.assertTrue(port.closed)
            self.assertTrue(output.exists())
            self.assertFalse(metadata["session_complete"])
            self.assertEqual(metadata["incomplete_reason"], "END_MISSING")
            self.assertTrue(metadata["statuses"]["incomplete_session"])

    def test_write_timeout_and_disconnect_preserve_csv_and_close_port(self):
        with tempfile.TemporaryDirectory() as folder:
            for scenario, stage in (("write_timeout", "STOP_COMMAND_WRITE"),
                                    ("disconnect", "STREAM_ACTIVE")):
                LifecycleSerial.scenario = scenario
                LifecycleSerial.clock = Clock()
                output = Path(folder) / f"{scenario}.csv"
                fake_module = types.SimpleNamespace(Serial=LifecycleSerial)
                stderr = io.StringIO()
                with patch.dict(sys.modules, {"serial": fake_module}), \
                     patch("stream_capture.time.monotonic", LifecycleSerial.clock), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(stderr):
                    with self.assertRaises(OSError):
                        capture("COM_TEST", 5 if scenario == "disconnect" else 1,
                                output, range_min_ma=-10)
                port = LifecycleSerial.instances[-1]
                self.assertTrue(port.closed)
                self.assertTrue(output.exists())
                metadata = json.loads((Path(folder) / f"{scenario}_metadata.json").read_text())
                self.assertFalse(metadata["session_complete"])
                self.assertEqual(metadata["failure"]["stage"], stage)
                self.assertEqual(metadata["failure"]["actual_samples"], 2)
                self.assertGreaterEqual(metadata["failure"]["csf4_frames"], 1)
                self.assertFalse(metadata["failure"]["end_received"])
                self.assertTrue(metadata["failure"]["port_closed"])
                self.assertIn("Traceback (most recent call last)", stderr.getvalue())
                self.assertIn(stage, stderr.getvalue())
                result, _, recovered, reopened = self.capture_case(
                    folder, f"after_{scenario}", 1)
                self.assertTrue(result)
                self.assertTrue(recovered["session_complete"])
                self.assertTrue(reopened.closed)

    def test_post_end_write_timeout_identifies_ping_or_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            LifecycleSerial.scenario = "ping_write_timeout"
            LifecycleSerial.clock = Clock()
            output = Path(folder) / "ping_timeout.csv"
            fake_module = types.SimpleNamespace(Serial=LifecycleSerial)
            with patch.dict(sys.modules, {"serial": fake_module}), \
                 patch("stream_capture.time.monotonic", LifecycleSerial.clock), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    capture("COM_TEST", 1, output, range_min_ma=-10)
            metadata = json.loads((Path(folder) / "ping_timeout_metadata.json").read_text())
            self.assertEqual(metadata["failure"]["stage"], "READ_FINAL_DIAGNOSTICS")
            self.assertEqual(metadata["failure"]["command"], "ping")
            self.assertTrue(metadata["failure"]["end_received"])
            self.assertTrue(LifecycleSerial.instances[-1].closed)

            result, _, metadata, port = self.capture_case(
                folder, "diagnostic_timeout", 1, "diagnostic_write_timeout")
            self.assertFalse(result)
            self.assertTrue(port.closed)
            self.assertTrue(metadata["session_complete"])
            details = metadata["post_diagnostic_failure_details"]["timing"]
            self.assertEqual(details["stage"], "READ_FINAL_DIAGNOSTICS")
            self.assertEqual(details["command"], "stream timing")
            self.assertTrue(details["end_received"])


if __name__ == "__main__":
    unittest.main()
