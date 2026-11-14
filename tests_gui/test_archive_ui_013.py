import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import time
import json
import unittest
from pathlib import Path
from PySide6 import QtWidgets
from current_sensor.acquisition_service import AcquisitionService
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from current_sensor.mock_device import MockSerial


class ArchiveUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self,predicate,timeout=10):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('timed out waiting for GUI archive state')

    def test_long_record_freeze_cursor_history_and_live(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=DeviceWorker(lambda *a,**kw:MockSerial(),AcquisitionService(128))
            window=MainWindow(worker,demo=True,auto_start=False,record_directory=directory)
            window.show()
            try:
                window.long_record.setChecked(True)
                window.disk_reserve.setValue(0)
                window.record_button.setChecked(True)
                self.assertFalse(window.long_record.isEnabled())
                worker.request('connect',port='DEMO')
                self.pump(lambda:worker.state=='idle')
                worker.request('start',profile='FAST',mode='dma')
                self.pump(lambda:worker.service.count>1000)
                self.assertIsNone(worker.spool)
                window.freeze.setChecked(True)
                self.pump(lambda:window.source_archive is not None)
                self.assertGreater(window.source_archive.sample_count,128)
                window.cursor_toggle.setChecked(True)
                window.renderer.x_cursors[0].setValue(.01)
                window.renderer.x_cursors[1].setValue(.05)
                self.pump(lambda:window.last_cursor_stats is not None and
                          window.last_cursor_stats['count']>0)
                self.assertEqual(window.last_cursor_stats['statistics_source'],'RAW')
                self.assertIn('каталог неполон',window.clean_stats_label.text())
                window.return_live()
                self.assertTrue(window.follow)
                worker.request('stop')
                self.pump(lambda:worker.state=='idle' and worker.recorder is None)
                self.pump(lambda:not window.record_button.isChecked())
                window.update_summary(worker.snapshot_report())
                stopped_label=window.archive_state.text()
                self.assertIn('измерение ',stopped_label)
                self.assertIn('Архив COMPLETE',stopped_label)
                time.sleep(.06)
                window.update_summary(worker.snapshot_report())
                self.assertEqual(window.archive_state.text(),stopped_label)
                self.assertTrue(window.long_record.isEnabled())
                window.load_history(str(worker.archive.manifest_path))
                self.pump(lambda:window.source_archive is not None and
                          window.source_archive.manifest['status']=='COMPLETE')
                self.assertIsNotNone(window.source_analyzer)
                window.update_summary(worker.snapshot_report())
                loaded_label=window.archive_state.text()
                time.sleep(.05)
                window.update_summary(worker.snapshot_report())
                self.assertEqual(window.archive_state.text(),loaded_label)
                self.assertIn('измерение ',loaded_label)
                window.cursor_toggle.setChecked(True)
                window.renderer.x_cursors[0].setValue(.01)
                window.renderer.x_cursors[1].setValue(.05)
                window.cursor_dirty=True
                self.pump(lambda:window.last_cursor_stats is not None and
                          window.last_cursor_stats.get('filtered',{}).get('complete') is True)
                window.save_interval()
                self.pump(lambda:window.export_jobs==0 and
                          bool(list(Path(directory).glob('capture_export_*.json'))))
                export=json.loads(next(Path(directory).glob('capture_export_*.json')).read_text(encoding='utf-8'))
                self.assertEqual(export['export_source'],'ARCHIVE')
                self.assertEqual(export['statistics_source'],'RAW')
                self.assertEqual(export['sample_count'],window.last_cursor_stats['count'])
                window.return_live()
                window.load_history(str(worker.archive.manifest_path))
                window.return_live()
                self.pump(lambda:all(not thread.is_alive() for thread in window.history_threads))
                self.app.processEvents()
                self.assertTrue(window.follow)
                self.assertIsNone(window.source_archive)
            finally:
                window.close()
                self.pump(lambda:window.close_phase=='done' and not worker.is_alive())


if __name__=='__main__':
    unittest.main()
