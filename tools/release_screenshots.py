"""Real Qt screenshots from existing recordings. No synthetic measurements."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse,json,shutil,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6 import QtGui,QtWidgets
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from current_sensor.mock_device import MockSerial


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',type=Path,required=True,help='Existing CSF4 session.json')
    parser.add_argument('--zero-csv',type=Path,required=True,help='Existing ZERO_BEFORE RAW CSV')
    args=parser.parse_args()
    app=QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont('Segoe UI',9))
    dest=ROOT/'docs/images/v1.0.0';dest.mkdir(parents=True,exist_ok=True)
    settings=ROOT/'results/gui_100/settings'
    for source,target in [('settings_1_1694x1188_0.png','settings_inline.png'),
                          ('settings_1.5_1366x768_4.png','settings_page.png')]:
        shutil.copyfile(settings/source,dest/target)
    archive=args.archive.resolve()
    shutil.copyfile(archive.parent.parent/'gui_live.png',dest/'main_live.png')
    worker=DeviceWorker(lambda *a,**kw:MockSerial());window=MainWindow(worker,demo=True)
    window.resize(1694,1188);window.show()
    def wait(predicate,timeout=60):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            app.processEvents();time.sleep(.005)
            if predicate():return
        raise TimeoutError(window.statusBar().currentMessage())
    def pump(seconds=.2):
        end=time.monotonic()+seconds
        while time.monotonic()<end:app.processEvents();time.sleep(.005)
    def grab(name,source):
        window.statusBar().showMessage('REPLAY — '+source+'; исходные отсчёты не изменены')
        window.identity_label.setText('REPLAY / CurrentSensor GUI 1.0.0')
        pump();window.grab().save(str(dest/name))
    try:
        window.load_history(archive);wait(lambda:window.source_archive is not None)
        wait(lambda:window.last_panel_result is not None)
        grab('archive.png','архив CSF4 v2, FW 1.0.0')
        source=args.zero_csv.resolve()
        window.load_history(source)
        wait(lambda:window.source_metadata.get('_source_path')==str(source.resolve()))
        wait(lambda:window.last_panel_result is not None and window.last_panel_result['raw']['count']==len(window.source))
        grab('current_plot.png','ZERO_BEFORE, ранее записанная FW 0.5.0')
        window.cursor_toggle.setChecked(True)
        window.renderer.x_cursors[0].setValue(.1);window.renderer.x_cursors[1].setValue(.2)
        window.renderer.view.setXRange(.095,.205,padding=0)
        wait(lambda:window.last_cursor_stats is not None and window.last_cursor_stats['count']>1)
        grab('cursors.png','X1–X2 по полному RAW ZERO_BEFORE')
        window.statistics_mode.setCurrentIndex(2);pump()
        grab('statistics.png','полный RAW и отдельная диагностика без SUSPECT')
        event=max(window.source_analyzer.events,key=lambda e:e['current_ma'])
        window.renderer.overview_event_clicked.emit(event)
        wait(lambda:window.last_cursor_stats is not None and window.last_cursor_stats['count']==1)
        grab('suspect.png','SUSPECT из существующей записи FW 0.5.0; причина не установлена')
        proof=dict(gui_version='1.0.0',settings_source='DEMO Qt geometry checks',
                   live_source='Hardware Native DMA FW 1.0.0',archive_source='Hardware CSF4 v2 FW 1.0.0',
                   analysis_source='REPLAY of existing FW 0.5.0 ZERO_BEFORE',images=sorted(p.name for p in dest.glob('*.png')))
        (dest/'sources.json').write_text(json.dumps(proof,indent=2),encoding='utf8')
    finally:
        window.close();wait(lambda:not worker.is_alive(),10);window.close_phase='done';window.close()


if __name__=='__main__':main()
