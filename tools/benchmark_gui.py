"""Reproducible host benchmark; offscreen FPS is render throughput, not monitor FPS.

Run from repository root: python -m tools.benchmark_gui --output docs/gui_benchmark.json
Use --backend vispy for the optional OpenGL comparison, isolated from production UI.
"""
import argparse
import gc
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
import numpy as np
import psutil
from PySide6 import QtWidgets, QtGui, QtCore
import pyqtgraph as pg
from current_sensor.data import RAW_DTYPE, LIVE_DTYPE, MedianBank, EnvelopeIndex, FILTERS
from current_sensor.renderer import PyQtGraphRenderer, VisPyRenderer


def synthetic(n):
    data = np.zeros(n, LIVE_DTYPE)
    data['slot'] = np.arange(n, dtype=np.uint32)
    data['slot'][n//2:] += 3
    data['time_s'] = data['slot'].astype(float) * .0001
    data['tim2_read_us'] = (data['time_s'] * 1e6).astype(np.uint64).astype(np.uint32)
    rng = np.random.default_rng(41)
    data['vshunt_code'] = rng.integers(-80, 81, n, dtype=np.int32) + 1600
    data['vshunt_code'][n//4:n//3] += 1600
    peaks = [n//10, n//3+100, 4*n//5]
    for width, start in enumerate(peaks, 1):
        data['vshunt_code'][start:start+width] = 160000 + width * 1600
    data['vshunt_code'][n//5] = -320000
    return data, peaks + [n//5]


def filters():
    results = []
    for rate in (1000, 2500, 5000, 10000):
        data, _ = synthetic(rate)
        for mode in FILTERS:
            batch_times = []
            start, cpu = time.perf_counter(), time.process_time()
            for repeat in range(20):
                bank = MedianBank()
                for lo in range(0, rate, 128):
                    t = time.perf_counter()
                    # Production computes both medians once per batch, independent
                    # of selected view. RAW and record arrays remain untouched.
                    med3, med5 = bank.process(data['vshunt_code'][lo:lo+128], data['slot'][lo:lo+128])
                    selected = data['vshunt_code'][lo:lo+128] if mode == 'RAW' else med5 if mode == 'MEDIAN5' else med3
                    if mode == 'RAW + MEDIAN3':
                        selected = (data['vshunt_code'][lo:lo+128], med3)
                    batch_times.append((time.perf_counter()-t)*1000)
            elapsed = time.perf_counter()-start
            results.append(dict(rate_hz=rate, mode=mode, processing_ms_per_second=elapsed*1000/20,
                                max_batch_ms=max(batch_times), throughput_samples_s=20*rate/elapsed,
                                cpu_ms_per_second=(time.process_time()-cpu)*1000/20))
    return results


def render_case(app, n, backend, iterations):
    data, peaks = synthetic(n)
    proc = psutil.Process()
    index_start = time.perf_counter()
    index = EnvelopeIndex(data)
    duration = float(data['time_s'][-1])
    trace = index.query(0, duration, 1000)
    cold_ms = (time.perf_counter()-index_start)*1000
    assert all(p in trace.indices for p in peaks), 'lost synthetic pulse extremum'
    if backend == 'vispy':
        widget = VisPyRenderer()
        widget.resize(1100, 700); widget.show(); app.processEvents()
        widget.view.camera.set_range(x=(0,duration), y=(-22000,12000))
    elif backend == 'builtin':
        widget = pg.PlotWidget()
        widget.resize(1100,700)
        curve = widget.plot(pen=pg.mkPen('#52dfc5', width=1))
        curve.setClipToView(True); curve.setDownsampling(auto=True, method='peak')
        connection = np.ones(n, bool); connection[n//2-1] = False; connection[-1] = False
        curve.setData(data['time_s'], data['vshunt_code'].astype(float)/16, connect=connection)
        widget.setYRange(-22000, 12000)
        widget.show(); app.processEvents()
    else:
        widget = PyQtGraphRenderer()
        widget.resize(1100, 700); widget.show(); app.processEvents()
        widget.view.setYRange(-22000, 12000, padding=0)
    prep, update, pan_zoom = [], [], []
    wall, cpu = time.perf_counter(), time.process_time()
    rss = proc.memory_info().rss
    for i in range(iterations):
        # Alternating full overview, pan and zoom at successive positions.
        span = duration if i % 3 == 0 else duration * (.1 if i % 3 == 1 else .01)
        left = (duration-span) * ((i % 11)/10)
        t = time.perf_counter()
        trace = index.query(left, left+span, 1000)
        prep.append((time.perf_counter()-t)*1000)
        t = time.perf_counter()
        if backend == 'vispy':
            widget.view.camera.set_range(x=(left,left+span), y=(-22000,12000))
            widget.draw([trace])
            rendered = widget.canvas.render()  # forces completion/readback
            if not np.any(rendered[:,:,:3] != rendered[0,0,:3]):
                raise RuntimeError('OpenGL returned an empty image')
        elif backend == 'builtin':
            widget.setXRange(left,left+span,padding=0)
        else:
            widget.view.setXRange(left,left+span,padding=0)
            widget.draw([trace], data[:0])
        app.processEvents()
        if backend != 'vispy':
            widget.grab()  # forces Qt raster paint instead of timing only setData
        update.append((time.perf_counter()-t)*1000)
        pan_zoom.append(prep[-1]+update[-1])
        rss = max(rss, proc.memory_info().rss)
    elapsed = time.perf_counter()-wall
    result = dict(samples=n, backend=backend, frames=iterations, cold_index_ms=cold_ms,
                  preparation_ms_mean=float(np.mean(prep)), update_ms_mean=float(np.mean(update)),
                  update_ms_max=max(update), zoom_pan_ms_p95=float(np.percentile(pan_zoom,95)),
                  rendered_updates_per_second=iterations/elapsed,
                  cpu_percent_one_core=(time.process_time()-cpu)/elapsed*100,
                  rss_peak_mb=rss/1024**2, exact_peak_indices_preserved=backend!='builtin',
                  note='builtin peak can move extremum timestamps; custom and VisPy use exact-time envelope')
    if backend == 'vispy':
        widget.canvas.close()
    widget.close(); widget.deleteLater(); app.processEvents()
    del data, index, trace, widget
    gc.collect()
    return result


def live(app, seconds):
    from current_sensor.device_worker import DeviceWorker
    from current_sensor.mock_device import MockSerial
    from current_sensor.main_window import MainWindow
    with tempfile.TemporaryDirectory() as directory:
        worker = DeviceWorker(lambda *a,**kw: MockSerial())
        window = MainWindow(worker, demo=True, auto_port='DEMO', auto_start=True, record_directory=directory)
        window.record_button.setChecked(True)
        window.show()
        deadline = time.monotonic()+5
        while worker.service.count < 100 and time.monotonic() < deadline:
            app.processEvents(); time.sleep(.001)
        results = []
        try:
            for mode in FILTERS:
                window.filter.setCurrentText(mode)
                start, cpu, draw = time.perf_counter(), time.process_time(), window.draw_count
                # Use the real Qt event loop. A Python processEvents/sleep loop
                # introduces Windows timer quantization and undercounts UI FPS.
                loop = QtCore.QEventLoop()
                QtCore.QTimer.singleShot(int(seconds*1000), loop.quit)
                loop.exec()
                rss = psutil.Process().memory_info().rss
                elapsed = time.perf_counter()-start
                results.append(dict(mode=mode, seconds=elapsed, graph_updates_per_second=(window.draw_count-draw)/elapsed,
                                    cpu_percent_one_core=(time.process_time()-cpu)/elapsed*100, rss_peak_mb=rss/1024**2))
            worker.request('stop')
            deadline = time.monotonic()+8
            while (worker.state != 'idle' or worker.recorder) and time.monotonic()<deadline:
                app.processEvents(); time.sleep(.001)
            report = worker.snapshot_report()
            metadata = json.loads(next(Path(directory).glob('*.json')).read_text())
            report['recorded_samples'] = metadata['recorded_samples']
            report['recorded_equals_received'] = metadata['recorded_samples'] == worker.service.count
        finally:
            worker.request('shutdown')
            worker.join(8)
            window.close(); app.processEvents()
        return dict(modes=results, report=report)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', default='build/gui_benchmark.json')
    parser.add_argument('--backend', choices=['pyqtgraph','builtin','vispy'], default='pyqtgraph')
    parser.add_argument('--sizes', nargs='+', type=int, default=[100000,600000,3000000,10000000])
    parser.add_argument('--iterations', type=int, default=30)
    parser.add_argument('--live-seconds', type=float, default=3)
    args = parser.parse_args()
    app = QtWidgets.QApplication([])
    font = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font)); app.setFont(QtGui.QFont('Segoe UI',9))
    report = dict(platform=platform.platform(), python=sys.version, numpy=np.__version__,
                  qt=app.platformName(), backend=args.backend, pyqtgraph=pg.__version__,
                  measurement='synchronous paint/readback; offscreen throughput is not monitor presentation FPS',
                  render=[])
    try:
        for n in args.sizes:
            row = render_case(app,n,args.backend,args.iterations)
            report['render'].append(row)
            print(json.dumps(row), flush=True)
        if args.backend == 'pyqtgraph':
            report['filters'] = filters()
            report['live_mock'] = live(app,args.live_seconds)
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        print(report['error'],flush=True)
    finally:
        destination = Path(args.output); destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return int('error' in report)


if __name__ == '__main__':
    raise SystemExit(main())
