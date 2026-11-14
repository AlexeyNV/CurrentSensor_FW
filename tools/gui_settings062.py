"""Stage A geometry proof and real Qt screenshots for every settings section."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse,json,sys,time,hashlib,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6 import QtCore,QtGui,QtWidgets
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial
import psutil
import numpy as np


def analysis_benchmark(window,pump,dest):
    report={'runs':[]};peak=psutil.Process().memory_info().rss
    def wait(predicate):
        nonlocal peak
        end=time.monotonic()+90
        while time.monotonic()<end:
            pump(.01);peak=max(peak,psutil.Process().memory_info().rss)
            if predicate():return
        raise TimeoutError(window.statusBar().currentMessage())
    try:
        for name in ('ZERO_BEFORE','LOAD_100_LONG'):
            source=ROOT/'results/hil/accuracy_100ma'/name/'raw.csv'
            with source.open('rb') as f:before=hashlib.file_digest(f,'sha256').hexdigest()
            start=time.perf_counter();window.load_history(source)
            wait(lambda:window.source_metadata.get('_source_path')==str(source.resolve()))
            elapsed=time.perf_counter()-start
            wait(lambda:window.last_panel_result is not None and window.last_panel_result['raw']['count']==len(window.source))
            result=dict(source=name,samples=len(window.source),open_seconds=elapsed,statistics=window.last_panel_result,
                main_graph_elements=len(window.renderer.curves[0].getData()[0]),memmap=isinstance(window.source,np.memmap))
            if name=='ZERO_BEFORE':
                main_range=window.renderer.view.viewRange()
                ranges={}
                for mode in ('Full RAW','Robust Y'):
                    window.overview_scale.setCurrentText(mode);pump(.05)
                    ranges[mode]=window.renderer.overview.viewRange()[1]
                    np.testing.assert_allclose(window.renderer.view.viewRange(),main_range)
                    window.grab().save(str(dest/('overview_'+mode.replace(' ','_')+'.png')))
                result['overview_ranges']=ranges
                result['overview_markers']=len(window.renderer.overview_markers.points())
                event=max(window.source_analyzer.events,key=lambda e:e['current_ma'])
                window.renderer.overview_event_clicked.emit(event)
                wait(lambda:window.last_cursor_stats is not None and window.last_cursor_stats['count']==1)
                assert f"seq={event['slot']}" in window.cursor_label.text()
                assert 'RAW24=0x66C9D0' in window.cursor_label.text()
                window.grab().save(str(dest/'overview_marker_raw.png'))
                window.statistics_mode.setCurrentIndex(2);pump(.05)
                window.grab().save(str(dest/'statistics_comparison.png'))
                result['selected_raw']=window.cursor_label.text()
            with source.open('rb') as f:after=hashlib.file_digest(f,'sha256').hexdigest()
            assert before==after
            result.update(sha256_before=before,sha256_after=after);report['runs'].append(result)
        report.update(status='PASS',peak_sampled_rss_mib=peak/1024**2)
    except Exception as error:report.update(status='FAIL',error=str(error),traceback=traceback.format_exc())
    (dest/'analysis_benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report


def settings_geometry(window):
    panel=window.advanced_area;page=window.advanced_tabs.currentWidget();errors=[]
    if hasattr(panel,'dialog') or panel.isWindow() or window.advanced_tabs.isWindow():errors.append('SEPARATE_SETTINGS_WINDOW')
    if not window.isAncestorOf(page):errors.append('SETTINGS_NOT_CHILD_OF_MAIN')
    if panel.presentation not in ('inline','page'):errors.append('INVALID_SETTINGS_PRESENTATION')
    rect=page.rect();controls=[]
    for cell in page.cells:
        control=cell.layout().itemAt(cell.layout().count()-1).widget()
        pos=cell.mapTo(page,QtCore.QPoint(0,0));bounds=QtCore.QRect(pos,cell.size())
        if not rect.contains(bounds):errors.append('CELL_OUTSIDE_PAGE: '+getattr(control,'text',lambda:type(control).__name__)())
        if not control.isVisibleTo(page):errors.append('CONTROL_HIDDEN: '+type(control).__name__)
        if control.width()<control.minimumSizeHint().width():errors.append('CONTROL_TOO_NARROW: '+type(control).__name__)
        controls.append(dict(type=type(control).__name__,geometry=[bounds.x(),bounds.y(),bounds.width(),bounds.height()]))
    for i,a in enumerate(controls):
        for b in controls[i+1:]:
            if QtCore.QRect(*a['geometry']).intersects(QtCore.QRect(*b['geometry'])):errors.append('CELLS_OVERLAP')
    for bar in window.advanced_tabs.findChildren(QtWidgets.QScrollBar):
        if bar.isVisibleTo(page):errors.append('SETTINGS_SCROLLBAR_VISIBLE')
    if panel.presentation=='inline' and window.renderer.plot.height()<150:errors.append('PLOT_TOO_SHORT')
    return dict(section=window.advanced_tabs.tabText(window.advanced_tabs.currentIndex()),
        presentation=panel.presentation,page_size=[page.width(),page.height()],controls=controls,errors=errors,
        main_size=[window.width(),window.height()],plot_height=window.renderer.plot.height())


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--font-scale',type=float,default=1.)
    parser.add_argument('--analysis',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'results/gui_100/settings')
    args=parser.parse_args();dest=args.output;dest.mkdir(parents=True,exist_ok=True)
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():QtGui.QFontDatabase.addApplicationFont(str(font))
    selected=QtGui.QFont('Segoe UI');selected.setPointSizeF(9*args.font_scale);app.setFont(selected)
    worker=DeviceWorker(lambda *a,**kw:MockSerial());window=MainWindow(worker,demo=True)
    window.directory.setText('recordings')  # Public DEMO screenshots contain no workstation paths.
    window.show();results=[]
    def pump(seconds=.12):
        end=time.monotonic()+seconds
        while time.monotonic()<end:app.processEvents();time.sleep(.003)
    try:
        pump()
        for width,height in ((1366,768),(1694,1188),(1920,1080)):
            window.resize(width,height);pump()
            # Offscreen Qt has a synthetic 800x800 screen. Simulate the requested
            # work area; production uses the actual QScreen.availableGeometry.
            window.advanced_area.available_rect=lambda w=width,h=height:QtCore.QRect(0,0,w,h)
            closed_height=window.renderer.height()
            window.recording_settings.setChecked(True);pump()
            for i in range(window.advanced_tabs.count()):
                window.advanced_tabs.setCurrentIndex(i);pump()
                item=settings_geometry(window);item['requested']=[width,height];results.append(item)
                if item['main_size']!=[width,height]:item['errors'].append('MAIN_RESIZED')
                target=window
                target.grab().save(str(dest/f'settings_{args.font_scale:g}_{width}x{height}_{i}.png'))
            window.recording_settings.setChecked(False);pump()
            assert window.renderer.height()>=closed_height-3
            assert not hasattr(window.advanced_area,'dialog')
        errors=[e for r in results for e in r['errors']]
        report=dict(status='FAIL' if errors else 'PASS',font_scale=args.font_scale,
            scale_note='Font stress in Qt, not a native Windows DPI certification',sections=results)
        (dest/f'stage_a_{args.font_scale:g}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(report['status'],errors,flush=True)
        if args.analysis:
            analysis=analysis_benchmark(window,pump,dest);print('analysis',analysis['status'],analysis.get('error'),flush=True)
            if analysis['status']!='PASS':report['status']='FAIL'
    finally:
        window.close();end=time.monotonic()+8
        while worker.is_alive() and time.monotonic()<end:pump(.01)
        pump();window.close()
    return int(report['status']!='PASS')


if __name__=='__main__':raise SystemExit(main())
