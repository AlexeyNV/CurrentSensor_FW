"""Bounded mode-switch HIL; no flash/calibration writes or automatic reconnect."""
import argparse,json,sys,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from current_sensor.device_worker import DeviceWorker
from current_sensor.device_protocol import DeviceProtocol
from current_sensor.connection_monitor import ConnectionMonitor
from tools.hil.run import discover


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--poll-only',action='store_true')
    parser.add_argument('--label',default=None)
    args=parser.parse_args()
    label=args.label or 'mode_matrix'
    if Path(label).name!=label or label in ('.','..'):
        parser.error('label must be a single directory name')
    root=ROOT/'results/gui_062a'/label
    if (root/'summary.json').exists():parser.error('evidence already exists; choose a new --label')
    root.mkdir(parents=True,exist_ok=True);result={'cases':[]};worker=None
    log=(root/'commands.log').open('w',encoding='utf8')
    def write(kind,payload):log.write(f'{time.monotonic():.6f} {kind} {payload}\n');log.flush()
    monitor=ConnectionMonitor('COM18',lambda e:write('USB_PRESENCE',json.dumps(e)))
    seen=[]
    def pump(seconds=.01):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            while worker and not worker.events.empty():
                kind,payload=worker.events.get();seen.append((kind,payload))
                if kind!='summary':write(kind,payload)
            time.sleep(.005)
    def wait(predicate,seconds=12):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            pump()
            if predicate():return
        raise TimeoutError('HIL wait; state='+worker.state)
    def request(action,**kwargs):write('REQUEST',(action,kwargs));worker.request(action,**kwargs)
    def stream(profile,mode,seconds=1):
        event_start=len(seen)
        request('profile',name=profile,mode=mode)
        wait(lambda:any(k=='profile' for k,p in seen[event_start:]))
        assert worker.profile['name']==('FAST' if mode=='native' else profile)
        assert worker.profile['acq_mode']==('DMA' if mode=='native' else mode.upper())
        request('start',profile=profile,mode=mode);wait(lambda:worker.state in ('streaming','disconnected'))
        if worker.state!='streaming':raise RuntimeError('START disconnected')
        pump(seconds);request('stop');wait(lambda:worker.state in ('idle','disconnected'))
        if worker.state!='idle':raise RuntimeError('STOP disconnected')
        snapshot=worker.snapshot_report();result['cases'].append(dict(profile=profile,mode=mode,report=snapshot))
        if snapshot['health']['acquisition_losses']:
            result.setdefault('quality_warnings',[]).append(dict(profile=profile,mode=mode,
                acquisition=snapshot['acquisition'],counters=snapshot['device_counters']))
        if (snapshot['session_errors'] or snapshot['health']['transport_errors'] or
                (snapshot['device_counters'] or {}).get('SPI_ERRORS') or
                int(snapshot.get('dma_errors') or 0)):
            raise RuntimeError('HIL capture errors; stop further mode tests')
    try:
        result['device']=discover('COM18');worker=DeviceWorker();request('connect',port='COM18')
        wait(lambda:worker.state=='idle');assert worker.info['info']['FW'] in ('0.5.0','1.0.0')
        result['calibration_before']=dict(worker.info['cal show'])
        worker.request('spool_enabled',enabled=False)
        stream('FAST','native',2)
        request('disconnect');wait(lambda:worker.state=='disconnected')
        request('connect',port='COM18');wait(lambda:worker.state=='idle')
        for mode in (('poll',) if args.poll_only else ('dma','poll')):
            for profile in ('FAST','BALANCED','PRECISION','LOW_NOISE'):
                from current_sensor.mode_capabilities import selection_error
                error=selection_error(mode,worker.info['info']['FW'],profile,worker.info['profiles'])
                if error:
                    result.setdefault('blocked_preflight',[]).append(dict(profile=profile,mode=mode,error=error))
                    continue
                stream(profile,mode,1)
                if any(not e['present'] for e in monitor.history):
                    raise RuntimeError('USB disappeared/re-enumerated; no more mode tests')
        stream('FAST','native',1)
        result['status']='COMPLETE'
    except Exception as error:
        result.update(status='FAIL',error=str(error),traceback=traceback.format_exc())
        if worker:
            result['failure_report']=worker.snapshot_report()
            if worker.decoder:result['failure_text_tail']=bytes(worker.decoder.text_tail).decode('ascii',errors='replace')
        write('FAIL',result['traceback'])
    finally:
        if worker:
            request('shutdown')
            try:wait(lambda:not worker.is_alive(),15)
            except TimeoutError as error:
                result['shutdown_error']=str(error);result['status']='FAIL';write('SHUTDOWN_ERROR',str(error))
        # Exactly one independent text probe after releasing the worker. Stop
        # on an unresponsive/missing device rather than resetting/retrying.
        if (not worker or not worker.is_alive()) and monitor.latest and monitor.latest['present']:
            import serial
            port=None
            try:
                port=serial.Serial('COM18',115200,timeout=.05,write_timeout=1)
                protocol=DeviceProtocol(port,lambda s:write('RECOVERY',s))
                result['post_probe']=protocol.command('ping',timeout=2)
                result['restored_native']=protocol.select('FAST','native')
                result['calibration_after']=protocol.command('cal show')
                assert result.get('calibration_before')==result['calibration_after']
            except Exception as error:
                result['recovery_error']=str(error);result['status']='FAIL';write('RECOVERY_ERROR',str(error))
            finally:
                if port:port.close()
        pump(.2);monitor.finish();monitor.join(2);result['usb_history']=monitor.history
        result['worker_trace']=str(worker.trace.path) if worker else None
        (root/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');log.close()
    print(result['status'],root,flush=True)
    return int(result['status']=='FAIL')


if __name__=='__main__':raise SystemExit(main())
