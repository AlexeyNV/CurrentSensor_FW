import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PySide6 import QtWidgets
from current_sensor.acquisition_service import AcquisitionService
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from current_sensor.mock_device import MockSerial
from current_sensor.recorder import open_history


class SessionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, predicate, timeout=8):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail('GUI condition timed out')

    def window(self, capacity=128, demo=True):
        worker=DeviceWorker(lambda *a,**kw: MockSerial(skip_at=100), AcquisitionService(capacity))
        window=MainWindow(worker,demo=demo,auto_port='DEMO',auto_start=True)
        window.show()
        def cleanup():
            window.demo=True
            window.close()
            self.pump(lambda: not worker.is_alive())
            self.pump(lambda: window.close_phase=='done')
            window.close()
        self.addCleanup(cleanup)
        self.pump(lambda: worker.service.count>500)
        return window,worker

    def test_cursor_live_freeze_eviction_export_and_reopen(self):
        window,worker=self.window()
        with tempfile.TemporaryDirectory() as directory:
            window.directory.setText(directory)
            window.record_button.setChecked(True)
            self.pump(lambda: worker.recorder is not None)
            window.cursor_toggle.setChecked(True)
            window.renderer.x_cursors[0].setValue(.002)
            window.renderer.x_cursors[1].setValue(.02)
            self.pump(lambda: window.last_cursor_stats is not None and window.last_cursor_stats['interval_start_s']==.002)
            stats=window.last_cursor_stats.copy()
            self.assertEqual(stats['statistics_source'],'RAW')
            self.assertEqual(stats['slot_gaps'],1)
            self.assertTrue(stats['complete'])
            window.freeze.setChecked(True)
            frozen_end=window.source_count
            view_before=window.renderer.view.viewRange()
            self.pump(lambda: worker.service.count>frozen_end+500)
            self.assertGreater(int(worker.service.snapshot()['slot'][0]),200)
            np.testing.assert_allclose(window.renderer.view.viewRange(),view_before)
            window.filter.setCurrentText('MEDIAN5')
            window.calibrated.setCurrentText('CALIBRATED')
            window.renderer.x_cursors[0].setValue(.02)
            window.renderer.x_cursors[1].setValue(.002)
            window.save_interval()
            self.pump(lambda: window.export_jobs==0 and len(list(Path(directory).glob('capture_export_*.json')))==1)
            report=json.loads(next(Path(directory).glob('capture_export_*.json')).read_text(encoding='utf-8'))
            self.assertEqual(report['sample_count'],stats['count'])
            self.assertEqual(report['slot_gaps'],stats['slot_gaps'])
            self.assertAlmostEqual(report['avg_ma'],stats['mean'])
            self.assertAlmostEqual(report['rms_ma'],stats['rms'])
            self.assertEqual(report['cursor_x1_s'],.02)
            self.assertEqual(report['cursor_x2_s'],.002)
            self.assertEqual(worker.state,'streaming')
            self.assertIsNotNone(worker.recorder)
            before_png=window.renderer.view.viewRange()
            window.save_png(str(Path(directory)/'scope.png'))
            self.assertGreater((Path(directory)/'scope.png').stat().st_size,100)
            np.testing.assert_allclose(window.renderer.view.viewRange(),before_png)
            worker.request('stop')
            self.pump(lambda: worker.state=='idle' and worker.recorder is None and worker.spool.done.is_set())
            self.assertEqual(worker.decoder.crc_errors,0)
            self.assertEqual(worker.service.running_gaps,1)
            self.assertEqual(worker.spool.count,worker.service.count)
            filename=next(Path(directory).glob('capture_export_*.json'))
            window.load_history(str(filename))
            self.pump(lambda: window.history_loaded)
            window.renderer.x_cursors[0].setValue(.002)
            window.renderer.x_cursors[1].setValue(.02)
            self.pump(lambda: window.last_cursor_stats['source_kind']=='HISTORY')
            self.assertEqual(window.last_cursor_stats['count'],stats['count'])

    def test_full_session_after_stop_no_manual_recording_and_roi(self):
        window,worker=self.window()
        with tempfile.TemporaryDirectory() as directory:
            window.directory.setText(directory)
            self.assertIsNone(worker.recorder)
            worker.request('stop')
            self.pump(lambda: worker.state=='idle' and worker.spool.done.is_set())
            window.save_session()
            self.pump(lambda: window.export_jobs==0 and window.last_recording is not None)
            report=json.loads(Path(window.last_recording).read_text(encoding='utf-8'))
            self.assertEqual(report['sample_count'],worker.service.count)
            self.assertTrue(report['exported_full_available_session'])
            self.assertTrue(report['acquisition_finished'])
            self.assertTrue(report['recording_finished'])
            self.assertNotIn('state',report)
            window.roi_toggle.setChecked(True)
            window.renderer.roi.setRegion((.002,.005))
            window.save_interval()
            self.pump(lambda: window.export_jobs==0)
            report=json.loads(Path(window.last_recording).read_text(encoding='utf-8'))
            self.assertEqual(report['export_type'],'ROI')
            self.assertEqual(report['sample_count'],31)

    def test_actual_scales_manual_auto_robust_and_calibration(self):
        window,worker=self.window(capacity=10000)
        window.freeze.setChecked(True)
        window.renderer.view.setXRange(2,2.025,padding=0)
        window.renderer.view.setYRange(-20,60,padding=0)
        self.assertAlmostEqual(window.time_window_s,.025)
        self.assertIn('0.0025',window.time_div.currentText())
        self.assertIn('10',window.current_div.currentText())
        self.assertAlmostEqual(window.y_min.value(),-20)
        self.assertAlmostEqual(window.y_max.value(),60)
        self.assertEqual(window.y_mode.currentIndex(),0)
        window.y_mode.setCurrentIndex(1)
        self.assertTrue(window.auto_y.isChecked())
        window.y_mode.setCurrentIndex(2)
        self.assertTrue(window.auto_y.isChecked())
        window.calibrated.setCurrentText('CALIBRATED')
        window.source_metadata={'calibration':{'ENABLED':'1','OFFSET_UA':'1000','GAIN_PPM':'1100000'}}
        scale,shift=window.display_calibration()
        self.assertAlmostEqual(scale,1.1)
        self.assertAlmostEqual(-1*scale+shift,-2.2)
        window.source_metadata['calibration']['ENABLED']='0'
        self.assertEqual(window.display_calibration(),(1,0))

    def test_partial_cursor_range_without_spool(self):
        worker=DeviceWorker(lambda *a,**kw: MockSerial(),AcquisitionService(128))
        window=MainWindow(worker,demo=True,auto_port='DEMO',auto_start=False)
        window.show()
        try:
            window.auto_spool.setChecked(False)
            self.pump(lambda: worker.state=='idle')
            window.start_measurement()
            self.pump(lambda: worker.service.count>1000)
            window.freeze.setChecked(True)
            self.assertIsNone(worker.spool)
            window.cursor_toggle.setChecked(True)
            window.renderer.x_cursors[0].setValue(0)
            window.renderer.x_cursors[1].setValue(.0001)
            self.pump(lambda: window.last_cursor_stats is not None and window.last_cursor_stats['interval_start_s']==0)
            self.assertFalse(window.last_cursor_stats['complete'])
            self.assertEqual(window.last_cursor_stats['count'],0)
            self.assertIn('НЕТ ОТСЧЁТОВ',window.interval_label.text())
        finally:
            window.close()
            self.pump(lambda: window.close_phase=='done')

    def test_cancel_close_keeps_device_owner_alive_then_discard(self):
        window,worker=self.window(demo=False)
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.Cancel):
            window.close()
            self.pump(lambda: not window.closing)
        self.assertTrue(worker.is_alive())
        self.assertEqual(worker.state,'idle')
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.Discard):
            window.close()
            self.pump(lambda: window.close_phase=='done')
        self.assertFalse(worker.is_alive())

    def test_close_save_and_spool_failure_does_not_stop_acquisition(self):
        window,worker=self.window(demo=False)
        with tempfile.TemporaryDirectory() as directory:
            window.directory.setText(directory)
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.Save):
                window.close()
                self.pump(lambda:window.close_phase=='done')
            report=json.loads(next(Path(directory).glob('capture_export_*.json')).read_text(encoding='utf-8'))
            self.assertTrue(report['recording_saved'])
            self.assertEqual(report['sample_count'],worker.service.count)
        second,receiver=self.window()
        receiver.spool.max_bytes=0
        self.pump(lambda:receiver.spool.error is not None)
        count=receiver.service.count
        self.pump(lambda:receiver.service.count>count+300)
        self.assertEqual(receiver.state,'streaming')
        self.assertEqual(receiver.decoder.crc_errors,0)

    def test_cursor_final_result_after_rapid_moves(self):
        window,worker=self.window(capacity=10000)
        window.freeze.setChecked(True)
        window.cursor_toggle.setChecked(True)
        for end in np.linspace(.003,.02,100):
            window.renderer.x_cursors[0].setValue(.001)
            window.renderer.x_cursors[1].setValue(float(end))
        self.pump(lambda:window.last_cursor_stats is not None and
                  window.last_cursor_stats['interval_end_s']==.02)
        self.assertEqual(window.last_cursor_stats['interval_start_s'],.001)
        self.assertEqual(window.last_cursor_stats['count'],190)
        self.assertEqual(window.last_cursor_stats['slot_gaps'],1)

    def test_recovered_history_is_saved_on_close(self):
        from current_sensor.spool import RawSpool
        from current_sensor.data import RAW_DTYPE
        with tempfile.TemporaryDirectory() as directory:
            samples = np.zeros(10, RAW_DTYPE)
            samples['slot'] = np.arange(10)
            samples['time_s'] = np.arange(10)*.0001
            samples['vshunt_code'] = 160
            original = RawSpool({}, directory=Path(directory)/'spool', free_reserve_bytes=0)
            original.start()
            original.submit(samples)
            original.finish({})
            original.join(5)
            worker = DeviceWorker(lambda *a,**kw: MockSerial())
            window = MainWindow(worker, demo=False, auto_start=False)
            window.show()
            window.directory.setText(str(Path(directory)/'saved'))
            window.pending_recovery[str(original.path)] = (original.path, 10, {})
            window.load_history(str(original.path))
            self.pump(lambda: window.source_spool is not None)
            self.assertEqual(window.source_spool.count,10)
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.Save):
                window.close()
                self.pump(lambda: window.close_phase=='done')
            report=json.loads(next((Path(directory)/'saved').glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual(report['sample_count'],10)
            self.assertFalse(report['source_history_complete'])
            self.assertFalse(original.path.exists())


if __name__=='__main__':
    unittest.main()
