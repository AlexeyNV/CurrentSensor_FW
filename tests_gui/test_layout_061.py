import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile,time,unittest
from pathlib import Path
import numpy as np
from PySide6 import QtCore,QtWidgets
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial
from current_sensor.data import RAW_DTYPE
from current_sensor.export import ExportSource
from current_sensor.analysis_statistics import rolling_statistics,panel_statistics


class Layout061Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self,predicate=lambda:False,timeout=.15):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():return True
            time.sleep(.003)
        return bool(predicate())

    def make_window(self,active=False):
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        window=MainWindow(worker,demo=True,auto_port='DEMO' if active else None,auto_start=active)
        window.show()
        def close():
            window.demo=True;window.close();self.pump(lambda:not worker.is_alive() and window.close_phase=='done',8)
            self.app.processEvents();window.close()
        self.addCleanup(close)
        self.pump(timeout=.1)
        return window,worker

    def test_layout_sizes_main_buttons_and_splitter_persistence(self):
        window,worker=self.make_window()
        for width,height in ((1366,768),(1694,1188),(1920,1080)):
            window.resize(width,height);self.pump(timeout=.1)
            self.assertEqual((window.width(),window.height()),(width,height))
            for b in (window.start_button,window.stop_button,window.record_button,window.freeze,window.live_button):
                self.assertTrue(b.isVisible());self.assertGreaterEqual(b.width(),b.minimumSizeHint().width())
                self.assertGreaterEqual(b.mapTo(window,QtCore.QPoint(0,0)).x(),0)
                self.assertLessEqual(b.mapTo(window,QtCore.QPoint(b.width(),b.height())).x(),width)
            self.assertGreater(window.renderer.plot.height(),150)
        window.resize(1694,1188);self.pump(timeout=.1)
        splitter=window.renderer.splitter;splitter.setSizes([420,splitter.height()-420])
        self.pump(timeout=.05);self.assertTrue(350<=window.renderer.plot.height()<=450)
        with tempfile.TemporaryDirectory() as d:
            window.settings=QtCore.QSettings(str(Path(d)/'ui.ini'),QtCore.QSettings.Format.IniFormat)
            window.demo=False;window.save_splitter();window.demo=True
            saved=window.settings.value('layout061/plot_splitter')
            self.assertIsInstance(saved,QtCore.QByteArray)
            splitter.setSizes([150,600]);self.assertTrue(splitter.restoreState(saved))
            self.assertTrue(350<=window.renderer.plot.height()<=450)

    def test_live_follow_manual_pause_recording_and_return(self):
        window,worker=self.make_window(active=True)
        self.assertTrue(self.pump(lambda:worker.service.count>1000,5))
        window.window_seconds.setCurrentIndex(1)
        self.pump(timeout=.12)
        left,right=window.renderer.view.viewRange()[0]
        self.assertAlmostEqual(right-left,5,places=6)
        self.assertLess(abs(right-worker.service.summary()['time_s']),.2)
        window.manual_view();window.renderer.view.setXRange(.01,.03,padding=0)
        fixed=window.renderer.view.viewRange()[0]
        count=worker.service.count;self.assertTrue(self.pump(lambda:worker.service.count>count+500,3))
        np.testing.assert_allclose(window.renderer.view.viewRange()[0],fixed)
        self.assertIn('MANUAL',window.view_state.text())
        self.assertTrue(self.pump(lambda:hasattr(window,'last_average_result') and window.last_average_result is not None,3))
        average_end=window.last_average_result['last_time_s']
        with tempfile.TemporaryDirectory() as directory:
            window.directory.setText(directory);window.record_button.setChecked(True)
            self.assertTrue(self.pump(lambda:worker.recorder is not None,3))
            window.freeze.setChecked(True);count=worker.service.count
            self.assertTrue(self.pump(lambda:worker.service.count>count+500,3))
            self.assertIsNotNone(worker.recorder);self.assertEqual(worker.state,'streaming')
            self.assertTrue(self.pump(lambda:window.last_average_result['last_time_s']>average_end,3))
            window.refresh_compact_status(worker.snapshot_report())
            self.assertIn('Запись выполняется',window.record_state.text())
            window.live_button.click();self.pump(timeout=.15)
            self.assertIn('LIVE FOLLOW',window.view_state.text())
            self.assertAlmostEqual(np.diff(window.renderer.view.viewRange()[0])[0],5,places=6)
            worker.request('stop');self.assertTrue(self.pump(lambda:worker.state=='idle' and worker.recorder is None,5))

    def test_partial_status_and_statistics_snapshot_are_explicit(self):
        window,worker=self.make_window()
        window.last_recording_result={'integrity':'PARTIAL','error':None}
        report=worker.snapshot_report();report['acquisition']['samples']=200
        for _ in range(3):window.refresh_compact_status(report)
        self.assertIn('Неполная запись (PARTIAL)',window.record_state.text())
        raw=np.zeros(100,RAW_DTYPE);raw['time_s']=np.arange(100)*.001;raw['slot']=np.arange(100)
        result=panel_statistics(ExportSource(raw,{},True,'TEST'),0,.099)
        window.apply_panel_statistics(result,None)
        self.assertIn('N 100',window.statistics_asof.text())
        self.assertIn('t=0.000',window.statistics_asof.text())
        self.assertIn('200',window.counters.text())
        self.assertFalse(window.statistics_details.isVisible())

    def test_average_100ms_keeps_suspect_raw_and_inclusive_timestamps(self):
        raw=np.zeros(1001,RAW_DTYPE);raw['time_s']=np.arange(1001)*.0001
        raw['vshunt_code']=1600;raw['vshunt_code'][500]=160000
        original=raw.tobytes();result=rolling_statistics(ExportSource(raw,{},True,'TEST'),0,.1)
        self.assertEqual(result['count'],1001)
        self.assertAlmostEqual(result['mean_ma'],float(np.mean(raw['vshunt_code'].astype(float)/16)))
        self.assertEqual(result['policy'],'ALL_RAW_INCLUDING_SUSPECT')
        self.assertEqual(raw.tobytes(),original)

    def test_last_raw_indicator_is_not_replaced_by_calibration_or_filter(self):
        window,worker=self.make_window()
        worker.service.ingest([{'type':1,'samples':[(0,42,8192)]}])
        worker.session_metadata={'calibration':{'ENABLED':'1','GAIN_PPM':'1100000','OFFSET_UA':'1000'}}
        window.calibrated.setCurrentIndex(1);window.filter.setCurrentText('MEDIAN5')
        report=worker.snapshot_report();window.update_summary(report)
        self.assertIn('512.0000 mA RAW',window.current.text())
        window.units.setCurrentText('A');window.update_summary(report)
        self.assertIn('0.512000 A RAW',window.current.text())


if __name__=='__main__':unittest.main()
