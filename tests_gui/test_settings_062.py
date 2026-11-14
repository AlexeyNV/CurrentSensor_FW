import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile,time,unittest
from pathlib import Path
import numpy as np
from PySide6 import QtCore,QtWidgets
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial
from current_sensor.data import RAW_DTYPE,EnvelopeIndex
from current_sensor.anomaly_diagnostics import analyze_history,DetectorRules
from current_sensor.analysis_statistics import panel_statistics
from current_sensor.export import ExportSource
from tools.gui_settings062 import settings_geometry


class Settings062Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self,predicate=lambda:False,timeout=.1):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():return True
            time.sleep(.003)
        return bool(predicate())

    def window(self,active=False):
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        window=MainWindow(worker,demo=True,auto_port='DEMO' if active else None,auto_start=active)
        window.show();self.pump()
        def close():
            window.demo=True;window.close()
            self.pump(lambda:not worker.is_alive() and window.close_phase=='done',8)
            self.app.processEvents();window.close()
        self.addCleanup(close);return window,worker

    def test_every_settings_section_fits_without_scrollbars(self):
        window,worker=self.window()
        for width,height in ((1366,768),(1694,1188),(1920,1080)):
            window.resize(width,height);self.pump()
            window.advanced_area.available_rect=lambda:QtCore.QRect(0,0,width,height)
            base=window.renderer.height();dock_width=window.findChildren(QtWidgets.QDockWidget)[0].width()
            window.recording_settings.setChecked(True);self.pump()
            for i in range(window.advanced_tabs.count()):
                window.advanced_tabs.setCurrentIndex(i);self.pump()
                report=settings_geometry(window)
                self.assertEqual(report['errors'],[],report)
                self.assertEqual(report['main_size'],[width,height])
                self.assertEqual(window.findChildren(QtWidgets.QDockWidget)[0].width(),dock_width)
                self.assertIn(window.advanced_area.presentation,('inline','page'))
                self.assertFalse(hasattr(window.advanced_area,'dialog'))
            window.recording_settings.setChecked(False);self.pump()
            self.assertGreaterEqual(window.renderer.height(),base-3)

    def test_panel_close_reopen_keeps_values_and_live_recording(self):
        window,worker=self.window(active=True)
        self.assertTrue(self.pump(lambda:worker.state=='streaming',5))
        window.resize(1366,768);self.pump()
        with tempfile.TemporaryDirectory() as d:
            window.directory.setText(d);window.record_button.setChecked(True)
            self.assertTrue(self.pump(lambda:worker.recorder is not None,3))
            window.recording_settings.setChecked(True);self.pump()
            window.advanced_tabs.setCurrentIndex(4);self.pump()
            window.recording_name.setText('unchanged')
            count=worker.service.count
            window.recording_settings.setChecked(False);self.pump()
            self.assertFalse(window.recording_settings.isChecked())
            self.assertTrue(window.follow)
            self.assertTrue(self.pump(lambda:worker.service.count>count+500,3))
            self.assertIsNotNone(worker.recorder)
            window.recording_settings.setChecked(True);self.pump()
            self.assertEqual(window.advanced_tabs.currentIndex(),4)
            self.assertEqual(window.recording_name.text(),'unchanged')
            worker.request('stop');self.assertTrue(self.pump(lambda:worker.state=='idle' and worker.recorder is None,5))

    def test_page_resize_and_escape_leave_all_controls_reachable(self):
        from PySide6 import QtGui
        window,worker=self.window();window.resize(1366,768);self.pump()
        window.advanced_area.available_rect=lambda:QtCore.QRect(0,0,1366,768)
        window.recording_settings.setChecked(True);window.advanced_tabs.setCurrentIndex(4);self.pump()
        self.assertIn(window.advanced_area.presentation,('inline','page'))
        self.assertFalse(hasattr(window.advanced_area,'dialog'))
        self.assertEqual(settings_geometry(window)['errors'],[])
        window.advanced_area.escape.activated.emit();self.pump()
        self.assertFalse(window.recording_settings.isChecked())
        window.recording_settings.setChecked(True);self.pump()
        self.assertEqual(window.advanced_tabs.currentIndex(),4)

    def data(self):
        data=np.zeros(10000,RAW_DTYPE);data['slot']=np.arange(10000)
        data['time_s']=np.arange(10000)*.000084;data['tim2_read_us']=np.arange(10000)*84
        data['vshunt_code']=np.rint(25*np.sin(np.arange(10000))).astype(int);data['vshunt_code'][5000]=421013
        return data

    def test_overview_modes_markers_preserve_data_and_main_axes(self):
        window,worker=self.window();data=self.data();original=data.tobytes()
        analyzer=analyze_history(data,'test',DetectorRules(10,10000,1000,True))
        window.renderer.set_overview(data);window.renderer.set_overview_events(analyzer.events)
        main=window.renderer.view.viewRange()
        self.assertEqual(window.overview_scale.currentText(),'Robust Y')
        low,high=window.renderer.overview.viewRange()[1]
        self.assertLess(high-low,20)
        self.assertEqual(len(window.renderer.overview_markers.points()),1)
        window.overview_scale.setCurrentText('Full RAW')
        self.assertGreater(window.renderer.overview.viewRange()[1][1],25000)
        np.testing.assert_allclose(window.renderer.view.viewRange(),main)
        self.assertEqual(data.tobytes(),original)

    def test_comparison_uses_same_detector_results_and_counts(self):
        window,worker=self.window();data=self.data();before=data.tobytes()
        analyzer=analyze_history(data,'same',DetectorRules(10,10000,1000,True))
        result=panel_statistics(ExportSource(data,{},True,'TEST',analyzer=analyzer),0,data['time_s'][-1])
        window.apply_panel_statistics(result,None);window.statistics_mode.setCurrentIndex(2)
        self.assertIn('Без SUSPECT',window.panel_values.text())
        self.assertIn('Это не доказанные ошибки',window.statistics_policy.text())
        self.assertEqual(result['raw']['count'],10000);self.assertEqual(result['excluding_suspect']['count'],9999)
        self.assertGreater(result['raw']['std'],result['excluding_suspect']['std'])
        window.statistics_mode.setCurrentIndex(1);self.assertTrue(window.exclude_suspect.isChecked())
        window.exclude_suspect.setChecked(False);self.assertEqual(window.statistics_mode.currentIndex(),0)
        self.assertEqual(data.tobytes(),before)

    def test_marker_navigation_requests_real_raw_cursor(self):
        window,worker=self.window();data=self.data()
        window.source=data;window.follow=False;window.history_loaded=True;window.source_count=len(data)
        window.source_analyzer=analyze_history(data,'same',DetectorRules(10,10000,1000,True))
        window.renderer.set_overview(data);window.refresh_overview_events()
        event=window.source_analyzer.events[0]
        window.renderer.overview_event_clicked.emit(event)
        self.assertTrue(self.pump(lambda:window.last_cursor_stats is not None and window.last_cursor_stats['count']==1,3))
        self.assertIn('seq=5000',window.cursor_label.text())
        self.assertIn('TIM2=420000',window.statusBar().currentMessage())


if __name__=='__main__':unittest.main()
