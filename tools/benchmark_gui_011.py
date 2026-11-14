"""GUI RAW range queries, 10 kHz DEMO, spool/export and Qt timers."""
import argparse
import json
import os
import platform
import tempfile
import time
from pathlib import Path
import numpy as np
import psutil
from PySide6 import QtCore, QtGui, QtWidgets
from current_sensor.data import RAW_DTYPE
from current_sensor.range_stats import RawRangeIndex
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from current_sensor.mock_device import MockSerial
from current_sensor import __version__


def distribution(values):
    a=np.asarray(list(values), dtype=float)
    return dict(count=len(a), mean_ms=float(a.mean()) if len(a) else 0,
                p95_ms=float(np.percentile(a,95)) if len(a) else 0,
                max_ms=float(a.max()) if len(a) else 0)


def range_case(n, queries):
    with tempfile.TemporaryDirectory() as directory:
        data=np.memmap(Path(directory)/'samples.raw.bin',RAW_DTYPE,mode='w+',shape=(n,))
        for lo in range(0,n,65536):
            hi=min(n,lo+65536)
            slots=np.arange(lo,hi,dtype=np.uint32)
            data['slot'][lo:hi]=slots+(slots>=n//2)*3
            data['time_s'][lo:hi]=data['slot'][lo:hi]*.0001
            data['tim2_read_us'][lo:hi]=(data['time_s'][lo:hi]*1e6).astype(np.uint64).astype(np.uint32)
            data['vshunt_code'][lo:hi]=np.rint(1600+800*np.sin(slots*.01)).astype(np.int32)
        data['vshunt_code'][n//3]=320000
        data['vshunt_code'][n//4:n//4+3]=-160000
        data.flush()
        start=time.perf_counter()
        index=RawRangeIndex.from_data(data)
        build_ms=(time.perf_counter()-start)*1000
        rng=np.random.default_rng(111)
        times=[]
        for _ in range(queries):
            lo,hi=sorted(rng.integers(0,n,2))
            started=time.perf_counter()
            stats=index.query(data,float(data['time_s'][lo]),float(data['time_s'][hi]))
            times.append((time.perf_counter()-started)*1000)
            if stats['count']!=hi-lo+1:
                raise AssertionError('inclusive range count mismatch')
        full=index.query(data,0,float(data['time_s'][-1]))
        reference=data['vshunt_code'].astype(float)/16
        assert full['max']==20000 and full['min']==-10000 and full['slot_gaps']==3
        assert abs(full['mean']-reference.mean())<1e-9
        assert abs(full['rms']-np.sqrt(np.mean(reference**2)))<1e-9
        report=dict(samples=n,index_build_ms=build_ms,queries=distribution(times),
                    rss_mb=psutil.Process().memory_info().rss/1024**2,
                    exact_raw_reference_passed=True)
        del reference,data,index
        return report


def pump(app,predicate,seconds=10):
    end=time.monotonic()+seconds
    while not predicate() and time.monotonic()<end:
        app.processEvents(); time.sleep(.001)
    if not predicate():
        raise TimeoutError('Qt benchmark state timeout')


def gui_case(app, interval_ms, seconds, screenshot=None):
    with tempfile.TemporaryDirectory() as directory:
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        window=MainWindow(worker,demo=True,auto_port='DEMO',auto_start=True,record_directory=directory)
        window.timer.setInterval(interval_ms)
        window.show()
        pump(app,lambda:worker.service.count>=1000)
        window.cursor_toggle.setChecked(True)
        window.renderer.x_cursors[0].setValue(.01)
        window.renderer.x_cursors[1].setValue(.05)
        mover=QtCore.QTimer()
        mover.setInterval(100)
        def move_cursor():
            now=worker.service.summary()['time_s']
            window.renderer.x_cursors[1].setValue(max(.02,now-.02))
        mover.timeout.connect(move_cursor); mover.start()
        for values in window.stage_timings.values(): values.clear()
        window.ui_periods.clear()
        window.renderer.plot.paint_times.clear()
        window.renderer.overview.paint_times.clear()
        start,cpu,draws,paints=(time.perf_counter(),time.process_time(),window.draw_count,window.renderer.plot.paint_count)
        loop=QtCore.QEventLoop()
        QtCore.QTimer.singleShot(int(seconds*350),window.save_session)
        QtCore.QTimer.singleShot(int(seconds*650),window.save_interval)
        if screenshot:
            QtCore.QTimer.singleShot(int(seconds*800),lambda:window.grab().save(screenshot))
        QtCore.QTimer.singleShot(int(seconds*1000),loop.quit)
        loop.exec()
        elapsed=time.perf_counter()-start
        mover.stop()
        report=dict(timer_ms=interval_ms,seconds=elapsed,
                    graph_updates_per_second=(window.draw_count-draws)/elapsed,
                    main_paints_per_second=(window.renderer.plot.paint_count-paints)/elapsed,
                    cpu_percent_one_core=(time.process_time()-cpu)/elapsed*100,
                    rss_mb=psutil.Process().memory_info().rss/1024**2,
                    stages={name:distribution(values) for name,values in window.stage_timings.items()},
                    main_paint=distribution(window.renderer.plot.paint_times),
                    overview_paint=distribution(window.renderer.overview.paint_times),
                    event_period=distribution(window.ui_periods))
        worker.request('stop')
        pump(app,lambda:worker.state=='idle' and worker.spool.done.is_set() and
             worker.detector.done.is_set() and window.export_jobs==0)
        report['acquisition']=worker.service.summary()
        report['transport']=worker.snapshot_report().get('transport')
        report['spool_samples']=worker.spool.count
        report['spool_error']=worker.spool.error
        report['detector']=dict(analyzed_samples=len(worker.detector.analyzer.index.mask),
                                candidate_events=worker.detector.analyzer.event_count,
                                complete=worker.detector.analyzer.complete,
                                error=worker.detector.analyzer.error)
        if report['detector']['analyzed_samples']!=report['acquisition']['samples']:
            raise AssertionError('candidate detector fell behind or lost RAW samples')
        report['spool_write']=distribution(worker.spool.write_times)
        report['export_wall']=distribution(window.export_timings)
        exports=[]
        for path in Path(directory).glob('capture_export_*.json'):
            meta=json.loads(path.read_text(encoding='utf-8'))
            data=np.memmap(path.parent/meta['files']['raw'],RAW_DTYPE,mode='r')
            values=data['vshunt_code'].astype(float)/16
            valid=(len(data)==meta['sample_count'] and
                   abs(float(values.mean())-meta['avg_ma'])<1e-9 and
                   abs(float(np.sqrt(np.mean(values**2)))-meta['rms_ma'])<1e-9)
            exports.append(dict(type=meta['export_type'],samples=len(data),raw_statistics_verified=valid))
            del data,values
        report['exports']=exports
        window.close()
        pump(app,lambda:window.close_phase=='done')
        return report


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--output',default='build/gui_011_benchmark.json')
    parser.add_argument('--timers',type=int,nargs='+',default=[16,20,33])
    parser.add_argument('--seconds',type=float,default=5)
    parser.add_argument('--sizes',type=int,nargs='*',default=[100000,600000,3000000,10000000])
    parser.add_argument('--queries',type=int,default=500)
    parser.add_argument('--screenshot')
    args=parser.parse_args()
    app=QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font)); app.setFont(QtGui.QFont('Segoe UI',9))
    report=dict(gui_version=__version__,platform=platform.platform(),python=platform.python_version(),qt_platform=app.platformName(),
                range_queries=[],live_mock=[],hardware_test=False)
    for n in args.sizes:
        result=range_case(n,args.queries)
        report['range_queries'].append(result)
        print(json.dumps(result),flush=True)
    for interval in args.timers:
        result=gui_case(app,interval,args.seconds,args.screenshot if interval==args.timers[-1] else None)
        report['live_mock'].append(result)
        print(json.dumps({k:result[k] for k in ('timer_ms','graph_updates_per_second','main_paints_per_second','spool_samples','spool_error')}),flush=True)
    path=Path(args.output); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
