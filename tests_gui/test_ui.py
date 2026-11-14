import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PySide6 import QtWidgets
from current_sensor.main_window import MainWindow
from current_sensor.calibration_view import CalibrationView
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial
from current_sensor.recorder import open_history


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail('Qt condition timed out')

    def test_cal_save_requires_explicit_confirmation(self):
        view = CalibrationView()
        sent = []
        view.command.connect(sent.append)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.No):
            view.confirm_save()
        self.assertEqual(sent, [])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.Yes):
            view.confirm_save()
        self.assertEqual(sent, ['cal save'])
        view.close()

    def test_disconnect_failed_is_visible_and_cannot_reconnect(self):
        worker = DeviceWorker(lambda *a, **kw: MockSerial())
        window = MainWindow(worker, demo=True, auto_start=False)
        window.show()
        try:
            window.set_connection_state('disconnecting')
            self.assertEqual(window.connection_state.text(), 'DISCONNECTING')
            window.set_connection_state('disconnect_failed')
            self.assertEqual(window.connection_state.text(), 'DISCONNECT FAILED')
            self.assertFalse(window.connect_button.isEnabled())
            self.assertTrue(window.disconnect_button.isEnabled())
        finally:
            window.close()
            self.pump(lambda: not worker.is_alive())
            self.app.processEvents()
            window.close()

    def test_live_freeze_zoom_filters_cursors_and_full_raw_recording(self):
        worker = DeviceWorker(lambda *a, **kw: MockSerial())
        window = MainWindow(worker, demo=True, auto_port='DEMO', auto_start=True)
        window.show()
        try:
            with tempfile.TemporaryDirectory() as directory:
                window.directory.setText(directory)
                window.record_button.setChecked(True)
                self.pump(lambda: worker.service.count > 1000)
                window.freeze.setChecked(True)
                frozen = window.source.copy()
                count = worker.service.count
                for mode in range(4):
                    window.filter.setCurrentIndex(mode)
                    window.renderer.view.setXRange(.01, .03, padding=0)
                    window.redraw(); window.draw_scope()
                    self.app.processEvents()
                self.pump(lambda: worker.service.count > count+500)
                np.testing.assert_array_equal(window.source, frozen)
                window.cursor_toggle.setChecked(True)
                window.renderer.x_cursors[0].setValue(.01)
                window.renderer.x_cursors[1].setValue(.02)
                self.assertIn('0.01', window.cursor_label.text())
                window.region_update(.01, .02)
                self.assertIn('RMS', window.region_label.text())
                window.return_live()
                self.assertTrue(window.follow)
                worker.request('stop')
                self.pump(lambda: worker.state == 'idle' and worker.recorder is None)
                history, metadata = open_history(next(Path(directory).glob('*.json')))
                self.assertEqual(metadata['recorded_samples'], worker.service.count)
                np.testing.assert_array_equal(history['vshunt_code'], worker.service.snapshot()['vshunt_code'])
        finally:
            window.close()
            self.pump(lambda: not worker.is_alive())
            self.app.processEvents()
            window.close()


if __name__ == '__main__':
    unittest.main()
