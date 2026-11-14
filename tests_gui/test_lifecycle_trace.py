import tempfile
import time
import unittest

from current_sensor.lifecycle_trace import LifecycleTrace


class LifecycleTraceTests(unittest.TestCase):
    def test_watchdog_writes_independent_thread_stacks(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = LifecycleTrace(directory, threshold_s=0.05)
            try:
                trace.begin('serial', 'SERIAL_CLOSE_STARTED', 'port=COM_TEST')
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if 'WATCHDOG_STACKS' in trace.path.read_text(encoding='utf-8'):
                        break
                    time.sleep(0.02)
                trace.end('serial', 'SERIAL_CLOSE_FINISHED')
            finally:
                trace.close()
            text = trace.path.read_text(encoding='utf-8')
            self.assertIn('SERIAL_CLOSE_STARTED', text)
            self.assertIn('WATCHDOG_STACKS', text)
            self.assertIn('blocked_stage=SERIAL_CLOSE_STARTED', text)
            self.assertIn('Current thread', text)
            self.assertIn('SERIAL_CLOSE_FINISHED', text)
            self.assertIn('pid=', text)
            self.assertIn('tid=', text)

    def test_watchdog_detects_stalled_ui_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = LifecycleTrace(directory, threshold_s=0.05)
            try:
                trace.heartbeat_ui()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if 'UI_UNRESPONSIVE' in trace.path.read_text(encoding='utf-8'):
                        break
                    time.sleep(0.02)
                trace.disable_ui_watch()
            finally:
                trace.close()
            self.assertIn('UI_UNRESPONSIVE', trace.path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
