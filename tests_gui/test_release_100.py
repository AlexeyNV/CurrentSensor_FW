import os,time,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6 import QtCore,QtGui,QtWidgets
from current_sensor import __version__
from current_sensor.mode_capabilities import native_capable,selection_error
from current_sensor.device_protocol import DeviceProtocol
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from current_sensor.mock_device import MockSerial
from test_mode_switch_062a import ModernSerial


class ReleaseSerial(ModernSerial):
    def _profile_fields(self):return dict(super()._profile_fields(),FW='1.0.0')
    def write(self,data):
        if data==b'info\nping\n':
            self.writes.append('info');self._text(dict(FW='1.0.0'));return super().write(b'ping\n')
        return super().write(data)


class ReleaseTests(unittest.TestCase):
    def test_version_and_native_release_compatibility(self):
        self.assertEqual(__version__,'1.0.0');self.assertTrue(native_capable('1.0.0'))
        p=DeviceProtocol(ReleaseSerial());p.connect_info()
        result=p.select('FAST','native')
        self.assertEqual(result['firmware_version'],'1.0.0')
        self.assertEqual(result['protocol_version'],2)
        self.assertEqual(result['adc_config'],0xAA68)
        self.assertIsNone(selection_error('native','0.5.0','FAST'))
        self.assertTrue(selection_error('native','0.4.3','FAST'))
        self.assertTrue(selection_error('poll','1.0.0','FAST'))

    def test_page_is_child_preserves_manual_cursor_and_interval(self):
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w=DeviceWorker(lambda *a,**kw:MockSerial());window=MainWindow(w,demo=True)
        def pump():
            for _ in range(15):app.processEvents();time.sleep(.003)
        old_font=app.font()
        from pathlib import Path
        font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
        if font.exists():QtGui.QFontDatabase.addApplicationFont(str(font))
        try:
            app.setFont(QtGui.QFont('Segoe UI',14));window.resize(1366,768);window.show();pump()
            window.follow=False;window.renderer.set_follow_mode(False)
            window.cursor_toggle.setChecked(True)
            window.renderer.x_cursors[0].setValue(-.7);window.renderer.x_cursors[1].setValue(-.2)
            positions=(window.renderer.x_cursors[0].value(),window.renderer.x_cursors[1].value())
            window.renderer.view.setXRange(-.8,-.1,padding=0);view=window.renderer.view.viewRange()[0]
            window.recording_settings.setChecked(True);window.advanced_tabs.setCurrentIndex(4);pump()
            self.assertFalse(hasattr(window.advanced_area,'dialog'))
            self.assertEqual(window.advanced_area.presentation,'page')
            self.assertFalse(window.tabs.isVisible())
            self.assertTrue(window.isAncestorOf(window.advanced_tabs))
            window.recording_settings.setChecked(False);pump()
            self.assertFalse(window.follow)
            self.assertEqual(positions,(window.renderer.x_cursors[0].value(),window.renderer.x_cursors[1].value()))
            self.assertEqual(view,window.renderer.view.viewRange()[0])
            self.assertTrue(window.tabs.isVisible())
            window.recording_settings.setChecked(True);pump()
            self.assertEqual(window.advanced_tabs.currentIndex(),4)
        finally:
            app.setFont(old_font);window.close();end=time.monotonic()+8
            while w.is_alive() and time.monotonic()<end:pump()
            window.close_phase='done';window.close()
