"""Operator-gated 100 mA bench. Never flashes, changes calibration or drives loads."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback
import uuid
import math
import re
import shutil
ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT))
from tools.hil.run import Link, discover
from tools.hil.native_stream import capture
from tools.hil.accuracy_analysis import analyze

STAGES={'ZERO_BEFORE':60,'LOAD_100':60,'ZERO_AFTER':60,'LOAD_100_LONG':300}
def parse_current(text):
    match=re.fullmatch(r'\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(mA|A|мА|А)\s*',text,re.I)
    if not match: raise ValueError('Specify units: 117 mA or 0.117 A')
    value=float(match[1].replace(',','.'))*(1000 if match[2].lower() in ('a','а') else 1)
    if not math.isfinite(value): raise ValueError('Nonfinite reference')
    return value
def save(path, data):
    temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(data,indent=2),encoding='utf8'); temp.replace(path)

def report(root, manifest):
    stages=manifest['stages']; zero1=stages.get('ZERO_BEFORE',{}).get('analysis'); zero2=stages.get('ZERO_AFTER',{}).get('analysis')
    complete=all(stages.get(name,{}).get('state')=='COMPLETE' for name in STAGES)
    manifest['session_complete']=complete
    manifest['status']='ERROR' if any(s['state']=='ERROR' for s in stages.values()) else 'WARNING' if complete else 'INCOMPLETE'
    manifest['accuracy_scope']='Measured loaded point(s) near 100 mA only; no 0..10 A accuracy certification'
    if zero1 and zero2:
        manifest['zero_drift_ma']={kind:zero2[kind]['mean_ma']-zero1[kind]['mean_ma'] for kind in ('full','robust') if zero1[kind]['n'] and zero2[kind]['n']}
    lines=['# CurrentSensor ~100 mA accuracy bench','',
        f"Session: {manifest['status']}. Only actual captures are reported. No correction, calibration write or automatic load control.",'',
        'RAW uncertainty and transport integrity are separate. Robust exclusions never certify every sample.','',
        '| Stage | State | N | Median mA | Mean mA | RMS mA | STD mA | Min / Max mA | P99 / P99.9 mA |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |']
    for name, stage in stages.items():
        if stage.get('capture_status')=='PASS' and stage.get('warnings'): stage['capture_status']='WARNING'
        for kind in ('full','robust'):
            s=stage.get('analysis',{}).get(kind,{})
            if s.get('n'):
                lines.append(f"| {name} {kind} | {stage['state']} | {s['n']} | {s['median_ma']:.6f} | {s['mean_ma']:.6f} | {s['rms_ma']:.6f} | {s['std_ma']:.6f} | {s['min_ma']:.6f} / {s['max_ma']:.6f} | {s['p99_ma']:.6f} / {s['p999_ma']:.6f} |")
    lines+=['',f"Zero drift (after-before): {manifest.get('zero_drift_ma','not yet measured')}",'']
    for name,stage in stages.items():
        lines+=[f'## {name}', '',f"State: {stage['state']}",f"Capture: {stage.get('capture_status','not captured')}",'']
        lines += [f"Capture warnings: {stage.get('warnings',[])}",'']
        capture_file=root/name/'capture_summary.json'
        if capture_file.exists():
            capture_data=json.loads(capture_file.read_text()); native=capture_data['reports']['native']
            lines += [f"Transport: {capture_data.get('transport')}",
                f"Native runtime losses={native.get('NATIVE_RUNTIME_EVENTS_LOST')}, DMA errors={native.get('DMA_ERRORS')}, timeouts={native.get('DMA_TIMEOUTS')}, buffer overflows={native.get('BUFFER_OVERFLOWS')}",
                f"Max service cycle us={native.get('NATIVE_MAX_SERVICE_CYCLE_US')}, cycles >=VSHCT={native.get('NATIVE_DMA_OVERRUNS')}, min/max sample period us={native.get('MIN_SAMPLE_PERIOD_US')}/{native.get('MAX_SAMPLE_PERIOD_US')}",'']
        analysis=stage.get('analysis',{})
        lines += [f"Exclusions: {analysis.get('exclusions')}",f"Criteria: {analysis.get('criteria')}",'']
        lines += [f"Anomaly statistics: {analysis.get('anomaly_statistics')}",'']
        for key in ('comparison_full','comparison_robust','comparison_calibrated_full'):
            if key in analysis: lines += [f'{key}: `{json.dumps(analysis[key])}`','']
        lines += [f"Transport/timing: see {name}/capture_summary.json. Anomaly contexts: {name}/contexts/.",'']
    lines += ['## Interpretation','',
        'Fluke bound uses 1.5% of reading + 3 digits plus half the endpoint change, conservatively added, not an expanded uncertainty.',
        'Manufacturer-spec results remain conditional unless calibration age/temperature/range validity are confirmed.',
        'Two endpoint readings cannot bound unobserved load variation. Percent difference is relative to the reference, not full scale.',
        'A mean/median within +/-10 mA does not certify instantaneous readings when RAW outliers fail that limit.',
        'No accuracy claim is extended beyond this point to 0..10 A.','']
    lines += ['## Reference instrument','',f"Configuration: `{json.dumps(manifest['fluke'])}`",'',
        '[Fluke 17B+ manufacturer specifications](https://www.fluke.com/en-us/product/electrical-testing/digital-multimeters/fluke-17b-plus).',
        'Reference error bounds and verdicts are listed per stage above. Unknown calibration date or ambient conditions prevent a formal accuracy certification.',
        'Readings beyond +/-10 mA of the endpoint reference are counted separately from MAD exclusions. They remain in RAW and in robust statistics unless the fixed statistical criterion independently excludes them.',
        'Timing warnings (service cycle >=VSHCT) are retained even when CRC, sequence gaps and runtime lost-event counters are zero.',''] if complete else []
    (root/'accuracy_report.md').write_text('\n'.join(lines),encoding='utf8')
    with (root/'anomalies.csv').open('w',newline='',encoding='utf8') as file:
        writer=csv.writer(file); header=False
        for name in stages:
            path=root/name/'anomalies.csv'
            if path.exists():
                with path.open(newline='',encoding='utf8') as source:
                    reader=csv.reader(source); names=next(reader)
                    if not header: writer.writerow(('stage',*names)); header=True
                    for row in reader: writer.writerow((name,*row[:-1],f'{name}/{row[-1]}'))
        if not header: writer.writerow(('stage','index','context'))
    save(root/'summary.json',manifest)

def run_stage(root, name, confirmation, before=None, after=None, import_capture=None, port='auto', reanalyze=False):
    if any(x is not None and (not math.isfinite(x) or abs(x)>1000) for x in (before,after)):
        raise ValueError('Fluke reading must be finite mA within this small-current bench range')
    root=Path(root); root.mkdir(parents=True,exist_ok=True); path=root/'summary.json'
    manifest=json.loads(path.read_text()) if path.exists() else {'utc':datetime.now(timezone.utc).isoformat(),'stages':{},
        'fluke':{'model':'17B+','connector':'10 A','resolution_ma':1.,'range_confirmed':False,
                 'reference_conditions_confirmed':False,'accuracy_percent':1.5,'digits':3}}
    stage=manifest['stages'].get(name)
    if reanalyze and stage is None:
        raise ValueError('Offline reanalysis requires an existing stage; no capture started')
    if stage is None:
        if confirmation!=name: raise ValueError('Explicit operator stage confirmation required')
        previous=list(STAGES)[:list(STAGES).index(name)]
        if any(manifest['stages'].get(s,{}).get('state')!='COMPLETE' for s in previous):
            raise ValueError('Previous stage is not complete')
        if name.startswith('LOAD') and before is None: raise ValueError('Fluke BEFORE reading in mA required')
        if name.startswith('LOAD') and not 80<=before<=130: raise ValueError('Expected positive reference near 100 mA (80..130); confirm units/load')
        stage={'state':'CONFIRMED','confirmation':confirmation,'confirmed_utc':datetime.now(timezone.utc).isoformat(),
               'fluke_before_ma':before,'seconds_requested':STAGES[name]}
        manifest['stages'][name]=stage; report(root,manifest)
        dest=root/name; dest.mkdir(exist_ok=True)
        try:
            if import_capture:
                source=Path(import_capture); original=json.loads((source/'summary.json').read_text())['runs'][0]
                # Copy originals byte for byte; leave the original HIL directory intact.
                if original.get('protocol_version')!=2 or original.get('elapsed_us64',0)<STAGES[name]*.99e6:
                    raise ValueError('Imported capture has wrong protocol or insufficient duration')
                raw_copy=dest/'raw.csv'
                if raw_copy.exists(): raise FileExistsError(raw_copy)
                shutil.copyfile(original['csv'],raw_copy)
                shutil.copyfile(source/'cdc.log',dest/'cdc.log')
                shutil.copyfile(source/'summary.json',dest/'source_hil_summary.json')
                capture_result=dict(original,csv=str(raw_copy.resolve())); stage['original_directory']=str(source.resolve())
            else:
                link=None
                with (dest/'cdc.log').open('w',encoding='utf8') as log:
                    try:
                        stage['device']=discover(port); link=Link(stage['device']['port'],log)
                        info=link.command('info'); stage['info']=info
                        if info.get('FW')!='0.5.0': raise RuntimeError('Expected existing FW 0.5.0; no flashing allowed')
                        link.command('acq stop'); stage['state']='ACQUIRING'; report(root,manifest)
                        capture_result=capture(link,STAGES[name],dest/'raw.csv')
                        stage['calibration_after']=link.command('cal show')
                        if capture_result['calibration']!=stage['calibration_after']: raise RuntimeError('Calibration state changed')
                    finally:
                        if link:
                            try: link.command('acq stop'); link.command('acq capture continuous')
                            finally: link.close()
            save(dest/'capture_summary.json',capture_result)
            stage['capture_status']=capture_result['status']; stage['source_csv']=capture_result['csv']
            stage['warnings']=capture_result['warnings']
            if capture_result['errors']: raise RuntimeError(f'Capture invalid: {capture_result["errors"]}')
            baseline=manifest.setdefault('calibration',capture_result['calibration'])
            if baseline!=capture_result['calibration']: raise RuntimeError('Calibration differs between stages')
            stage['state']='WAITING_FLUKE_AFTER' if name.startswith('LOAD') else 'ANALYZING'
            report(root,manifest)
        except Exception as exc:
            stage.update(state='ERROR',error=str(exc),traceback=traceback.format_exc()); report(root,manifest); raise
    if stage['state']=='COMPLETE' and not reanalyze: return manifest
    if stage['state']=='ERROR': raise RuntimeError('Failed stage preserved; inspect logs before a new session')
    if name.startswith('LOAD'):
        if after is not None: stage['fluke_after_ma']=after
        if stage.get('fluke_after_ma') is None: return manifest
    stage['state']='ANALYZING'; report(root,manifest)
    reference=dict(before=stage['fluke_before_ma'],after=stage['fluke_after_ma'],
        resolution_ma=manifest['fluke']['resolution_ma'],
        reference_conditions_confirmed=manifest['fluke']['reference_conditions_confirmed']) if name.startswith('LOAD') else None
    stage['analysis']=analyze(stage['source_csv'],root/name,reference)
    stage['state']='COMPLETE'; report(root,manifest); return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,default=ROOT/'results/hil'/('accuracy_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]))
    parser.add_argument('--stage',choices=STAGES); parser.add_argument('--confirm-stage',choices=STAGES)
    parser.add_argument('--before-ma',type=float); parser.add_argument('--after-ma',type=float)
    parser.add_argument('--import-capture',type=Path); parser.add_argument('--port',default='auto')
    parser.add_argument('--reanalyze',action='store_true',help='Recompute saved RAW offline, without serial or new confirmation')
    args=parser.parse_args()
    if args.stage:
        result=run_stage(args.session,args.stage,args.confirm_stage,args.before_ma,args.after_ma,args.import_capture,args.port,args.reanalyze)
        print(args.session,result['stages'][args.stage]['state']); return
    for name in STAGES:
        current=json.loads((args.session/'summary.json').read_text())['stages'].get(name,{}) if (args.session/'summary.json').exists() else {}
        if current.get('state')=='COMPLETE': continue
        if current.get('state')=='WAITING_FLUKE_AFTER':
            after=parse_current(input(f'{name}: Fluke AFTER, include A or mA: '))
            run_stage(args.session,name,None,after=after,port=args.port); continue
        if input(f'{name}: manually set load, close other serial clients; type {name} to confirm: ').strip()!=name:
            raise SystemExit('Operator did not confirm; no recording started')
        before=parse_current(input('Fluke BEFORE, include A or mA: ')) if name.startswith('LOAD') else None
        result=run_stage(args.session,name,name,before,port=args.port)
        if name.startswith('LOAD'):
            after=parse_current(input('Fluke AFTER (A or mA), keep load stable: '))
            result=run_stage(args.session,name,None,after=after,port=args.port)
    print(args.session)
if __name__=='__main__': main()
