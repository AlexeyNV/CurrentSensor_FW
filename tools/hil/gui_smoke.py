"""Exercise existing Qt UI/worker on real hardware, with bounded waits."""
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
import uuid
import argparse

ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6 import QtWidgets, QtGui
from current_sensor.device_worker import DeviceWorker
from current_sensor.main_window import MainWindow
from tools.hil.run import discover

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--record-mode',choices=('short','long'),default='short')
    args=parser.parse_args()
    dest=ROOT/'results/hil'/('gui_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    dest.mkdir(parents=True)
    app=QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists(): QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont('Segoe UI',9))
    worker=DeviceWorker(); window=MainWindow(worker); window.show()
    result={'status':'FAIL','utc':datetime.now(timezone.utc).isoformat()}
    def wait(predicate, seconds=10):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            app.processEvents(); time.sleep(.005)
            if predicate(): return
        raise TimeoutError(f'GUI wait; state={worker.state}; errors={worker.session_errors}')
    try:
        device=discover(); worker.request('connect',port=device['port']); wait(lambda:worker.state=='idle')
        if worker.info['info']['FW']!='1.0.0': raise RuntimeError('Expected installed FW 1.0.0')
        worker.request('record_start',directory=str(dest),mode=args.record_mode,
                       archive_options={'segment_mib':25,'reserve_mib':0})
        window.mode.setCurrentIndex(2); window.start_measurement(); wait(lambda:worker.state=='streaming')
        wait(lambda:worker.recorder is not None and worker.service.count>100)
        window.resize(1366,768);app.processEvents()
        count=worker.service.count
        window.recording_settings.setChecked(True)
        for page in range(window.advanced_tabs.count()):
            window.advanced_tabs.setCurrentIndex(page)
            until=time.monotonic()+.08
            while time.monotonic()<until:app.processEvents();time.sleep(.005)
            if hasattr(window.advanced_area,'dialog') or window.advanced_area.isWindow():
                raise RuntimeError('Separate settings window created')
            if worker.state!='streaming' or worker.recorder is None:raise RuntimeError('Settings interrupted recording')
        window.recording_settings.setChecked(False);app.processEvents()
        result['settings_while_recording']=dict(sections=5,separate_windows=0,
            samples_before=count,samples_after=worker.service.count)
        stop=time.monotonic()+5
        while time.monotonic()<stop: app.processEvents(); time.sleep(.005)
        window.identity_label.setText('USB CDC · FW 1.0.0 · Native DMA · RAW24')
        window.grab().save(str(dest/'gui_live.png'))
        worker.request('stop'); wait(lambda:worker.state=='idle' and worker.recorder is None,15)
        result.update(worker.snapshot_report()); result['device']=device
        native=worker.diagnostics.get('acq native report',{})
        errors=list(worker.session_errors)
        transport=result['transport']
        if any(transport.get(k) for k in ('crc_errors','format_errors','frame_gaps','slot_gaps','code_errors')): errors.append('transport error')
        if result['acquisition']['rate_hz']<10000: errors.append('rate below 10000')
        if native.get('NATIVE_RUNTIME_EVENTS_LOST')!='0': errors.append('Native runtime loss')
        if result['health']['transport_errors'] or result['health']['acquisition_losses']: errors.append('health error')
        result['errors']=errors; result['status']='FAIL' if errors else 'WARNING' if native.get('NATIVE_DMA_OVERRUNS')!='0' else 'PASS'
        window.mode.setCurrentIndex(1);window.profiles.setCurrentIndex(window.profiles.findData('FAST'))
        app.processEvents()
        if window.start_button.isEnabled() or window.apply_profile.isEnabled():raise RuntimeError('Unsupported POLL/FAST allowed')
        result['unsupported_preserves_connection']=worker.state=='idle' and worker.port is not None and worker.port.is_open
        worker.request('profile',name='FAST',mode='native')
        wait(lambda:worker.protocol.confirmed==('FAST','native'))
        worker.request('disconnect'); wait(lambda:worker.state=='disconnected')
        worker.request('connect',port=discover()['port']); wait(lambda:worker.state=='idle')
        result['reconnected']=True
    except Exception as exc:
        result['error']=str(exc)
    finally:
        worker.request('shutdown'); wait(lambda:not worker.is_alive(),15)
        window.close_phase='done'; window.close(); app.processEvents()
        (dest/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(result['status'],dest)
    return int(result['status']=='FAIL')
if __name__=='__main__': raise SystemExit(main())
