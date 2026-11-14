"""Capture actual Qt layouts at requested window sizes without hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6 import QtWidgets,QtGui
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial


def main():
    p=argparse.ArgumentParser();p.add_argument('--label',default='after');args=p.parse_args()
    dest=ROOT/'results/gui_061';dest.mkdir(parents=True,exist_ok=True)
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont('Segoe UI',9))
    worker=DeviceWorker(lambda *a,**kw:MockSerial());window=MainWindow(worker,demo=True,auto_port='DEMO',auto_start=True)
    window.show();measurements=[]
    def pump(seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:app.processEvents();time.sleep(.005)
    try:
        pump(1.2)
        for width,height in ((1366,768),(1694,1188),(1920,1080)):
            window.resize(width,height);pump(.4)
            window.grab().save(str(dest/f'{args.label}_{width}x{height}.png'))
            buttons=[window.start_button,window.stop_button,window.record_button,window.freeze]
            measurements.append(dict(requested=[width,height],actual=[window.width(),window.height()],
                plot_height=window.renderer.plot.height(),overview_height=window.renderer.overview.height(),
                x_range=window.renderer.view.viewRange()[0],window_seconds=window.time_window_s,
                buttons=[dict(text=b.text(),width=b.width(),minimum=b.minimumSizeHint().width(),visible=b.isVisible()) for b in buttons]))
            if args.label=='after' and width==1694:
                window.renderer.splitter.setSizes([420,window.renderer.splitter.height()-420])
                pump(.1)
                window.grab().save(str(dest/'after_1694x1188_split420.png'))
                measurements[-1]['plot_after_splitter_adjustment']=window.renderer.plot.height()
                # Keep the remaining size comparison at the default ratio.
                window.renderer.splitter.setSizes([420,180])
        (dest/f'{args.label}_layout.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2),encoding='utf8')
    finally:
        window.close();end=time.monotonic()+10
        while worker.is_alive() and time.monotonic()<end:pump(.01)
        pump(.3);window.close()
    print(json.dumps(measurements,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
