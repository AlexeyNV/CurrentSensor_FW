"""Actual headless Qt run on saved hardware data; no serial or firmware writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np
import psutil
from PySide6 import QtWidgets,QtGui
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial
from current_sensor.export import export_raw


def main():
    dest=ROOT/'results/gui_060';dest.mkdir(parents=True,exist_ok=True)
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont('Segoe UI',9))
    worker=DeviceWorker(lambda *a,**kw:MockSerial())
    window=MainWindow(worker,demo=True);window.resize(1600,1060);window.show()
    result={'hardware_test':False,'source':'Previously recorded hardware CSV, offline Qt execution','runs':[]}
    process=psutil.Process();peak=process.memory_info().rss
    def wait(predicate,timeout=120):
        nonlocal peak
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            app.processEvents();peak=max(peak,process.memory_info().rss)
            if predicate():return
            time.sleep(.005)
        raise TimeoutError('Qt benchmark timeout: '+window.statusBar().currentMessage())
    try:
        for name in ('LOAD_100_LONG','ZERO_BEFORE'):
            source=ROOT/'results/hil/accuracy_100ma'/name/'raw.csv'
            with source.open('rb') as file:before=hashlib.file_digest(file,'sha256').hexdigest()
            start=time.perf_counter();window.load_history(source)
            wait(lambda:window.source_metadata.get('_source_path')==str(source.resolve()))
            elapsed=time.perf_counter()-start
            wait(lambda:window.last_panel_result is not None and window.last_panel_result['raw']['count']==len(window.source))
            window.grab().save(str(dest/(name+'_overview.png')))
            run=dict(name=name,samples=len(window.source),open_seconds=elapsed,
                graph_elements=len(window.renderer.curves[0].getData()[0]),
                statistics=window.last_panel_result,raw_memmap=isinstance(window.source,np.memmap))
            if name=='ZERO_BEFORE':
                event=window.source_analyzer.events[0];window.navigate_candidate(event,False)
                window.y_mode.setCurrentIndex(1)
                window.tabs.setCurrentIndex(0);window.redraw();app.processEvents()
                window.grab().save(str(dest/'suspect_marked.png'))
                window.visual_mode.setCurrentText('Display Clean');window.draw_scope();app.processEvents()
                window.grab().save(str(dest/'suspect_clean.png'))
                window.visual_mode.setCurrentText('Marked')
                window.cursor_toggle.setChecked(True)
                for c,t in zip(window.renderer.x_cursors,(event['time_s']-.001,event['time_s']+.001)):c.setValue(t)
                wait(lambda:window.last_cursor_stats is not None and window.last_cursor_stats['count']>0)
                window.grab().save(str(dest/'sample_cursors.png'))
                selected=(event['time_s']-.001,event['time_s']+.001)
                exported=export_raw(window.export_source(interval=selected),dest/'export','CURSORS',selected,*selected)
                run['export']=str(exported['path']);run['export_samples']=exported['samples']
                window.tabs.setCurrentWidget(window.events_view);window.refresh_candidate_view()
                app.processEvents();window.grab().save(str(dest/'suspect_catalog.png'))
            with source.open('rb') as file:after=hashlib.file_digest(file,'sha256').hexdigest()
            run['source_sha256_before']=before;run['source_sha256_after']=after
            assert before==after
            result['runs'].append(run)
        result['peak_rss_mib']=peak/1024**2
        result['ui_callback_ms']={key:float(np.percentile(window.stage_timings['event_callback'],p))
            for key,p in [('p50',50),('p99',99),('max',100)]}
        result['status']='PASS'
    except Exception as error:
        import traceback
        result.update(status='FAIL',error=str(error),traceback=traceback.format_exc())
    finally:
        window.demo=True;window.close()
        wait(lambda:not worker.is_alive() and window.close_phase=='done',20)
        window.close();app.processEvents()
        (dest/'offline_benchmark.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(result['status'],result.get('error'),flush=True)
    return int(result['status']!='PASS')


if __name__=='__main__':raise SystemExit(main())
