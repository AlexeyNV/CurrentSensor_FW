"""Offscreen Qt screenshots: pending supported and blocked selections, no hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6 import QtCore,QtGui,QtWidgets
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial


def main():
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont('Segoe UI',9))
    worker=DeviceWorker(lambda *a,**kw:MockSerial())
    window=MainWindow(worker,demo=True,auto_port='DEMO')
    window.resize(1694,1188);window.show()
    dest=ROOT/'results/gui_062a/screenshots';dest.mkdir(parents=True,exist_ok=True)
    def pump(seconds=.15):
        until=time.monotonic()+seconds
        while time.monotonic()<until:app.processEvents();time.sleep(.003)
    try:
        end=time.monotonic()+5
        while worker.state!='idle' and time.monotonic()<end:pump(.02)
        assert worker.state=='idle';pump()
        # Only capability/display metadata is simulated. Do not apply or start.
        worker.info['info']['FW']='0.5.0';worker.info['profile']['firmware_version']='0.5.0'
        window.update_identity(worker.info)
        window.update_profile(worker.info['profile']);pump()
        window.advanced_area.available_rect=lambda:QtCore.QRect(0,0,1694,1188)
        window.recording_settings.setChecked(True)
        window.advanced_tabs.setCurrentIndex(3);pump()
        before=list(worker.port.writes)
        window.mode.setCurrentIndex(2);pump()
        assert window.start_button.isEnabled()
        window.grab().save(str(dest/'supported_native.png'))
        window.mode.setCurrentIndex(1);pump()
        assert not window.start_button.isEnabled() and not window.apply_profile.isEnabled()
        window.grab().save(str(dest/'unsupported_poll_fast.png'))
        assert before==worker.port.writes
        (dest/'proof.json').write_text(json.dumps(dict(source='DEMO Qt; no hardware configuration',
            firmware_capabilities='0.5.0',combobox_io_count=0,unsupported_start_enabled=False),indent=2),encoding='utf8')
    finally:
        window.close()
        end=time.monotonic()+8
        while worker.is_alive() and time.monotonic()<end:pump(.02)
        pump();window.close()


if __name__=='__main__':main()
