"""Host-only FAST 10 kHz DEMO with active GUI and segmented archive."""
import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import psutil
from PySide6 import QtWidgets
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from current_sensor.mock_device import MockSerial


def pump(app,predicate,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise TimeoutError('GUI benchmark state transition timed out')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=5)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.seconds<1:
        parser.error('--seconds must be at least 1')
    app=QtWidgets.QApplication([])
    process=psutil.Process()
    peak=process.memory_info().rss
    with tempfile.TemporaryDirectory() as directory:
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        window=MainWindow(worker,demo=True,auto_start=False,record_directory=directory)
        window.show()
        try:
            window.long_record.setChecked(True)
            window.disk_reserve.setValue(0)
            window.record_button.setChecked(True)
            worker.request('connect',port='DEMO')
            pump(app,lambda:worker.state=='idle')
            worker.request('start',profile='FAST',mode='dma')
            pump(app,lambda:worker.state=='streaming')
            started=time.monotonic()
            while time.monotonic()-started<args.seconds:
                app.processEvents()
                peak=max(peak,process.memory_info().rss)
                time.sleep(.002)
            worker.request('stop')
            pump(app,lambda:worker.state=='idle' and worker.recorder is None)
            archive=worker.archive
            report=worker.snapshot_report()
            if archive.error or archive.samples!=worker.service.count:
                raise AssertionError(f'archive mismatch: {archive.error}; {archive.samples}/{worker.service.count}')
            result=dict(benchmark='host GUI DEMO; not STM32 hardware',seconds=args.seconds,
                samples=worker.service.count,archive_samples=archive.samples,
                rate_hz=worker.service.count/max(args.seconds,0.001),
                archive_status=archive.status,archive_bytes=archive.bytes_written,
                segments=len(archive.segments),archive_dropped=archive.dropped_samples,
                transport_crc=report.get('transport',{}).get('crc_errors',0),
                slot_gaps=report['acquisition']['running_slot_gaps'],
                gui_draw_count=window.draw_count,
                gui_draw_fps=window.draw_count/args.seconds,
                gui_period_median_ms=statistics.median(window.ui_periods) if window.ui_periods else None,
                process_rss_peak_mib=peak/1024**2)
        finally:
            window.close()
            pump(app,lambda:window.close_phase=='done' and not worker.is_alive())
    rendered=json.dumps(result,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(rendered+'\n',encoding='utf-8')
    print(rendered)


if __name__=='__main__':
    main()
