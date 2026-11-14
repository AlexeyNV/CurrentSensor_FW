"""Read-only RAW24 investigation; derived files go exclusively to --output.

Single-bit compatibility is descriptive, never a declaration of SPI corruption.
"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
import numpy as np
from tools.hil.accuracy_analysis import DTYPE, masks, statistics
from tools.hil.verify_capture import verify


def xor_details(a, b):
    value=int(a)^int(b)
    return dict(xor=f'0x{value:06X}',hamming=value.bit_count(),
                bits=[bit for bit in range(24) if value&(1<<bit)])


def sign_cross(a,b):
    return bool((a<0 and b>0) or (a>0 and b<0))


def read_raw_csv(source,binary):
    # Early FW0.5 HIL CSVs lack calibrated current. It is not used in this
    # RAW investigation; store NaN in the derived cache rather than invent it.
    count=0; batch=[]
    with Path(source).open(newline='',encoding='utf8') as f, Path(binary).open('wb') as out:
        for row in csv.DictReader(f):
            batch.append((int(row['sequence']),int(row['tim2_us']),int(row['elapsed_us64']),
                int(row['raw24'],0),int(row['vshunt_code']),float(row['current_ma']),float(row.get('current_cal_ma','nan'))))
            if len(batch)>=32768:
                np.array(batch,dtype=DTYPE).tofile(out);count+=len(batch);batch.clear()
        if batch: np.array(batch,dtype=DTYPE).tofile(out);count+=len(batch)
    return np.memmap(binary,dtype=DTYPE,mode='r',shape=(count,)) if count else np.empty(0,dtype=DTYPE)


def event_details(data,index,excluded):
    i=int(index); row=data[i]; raw=int(row['raw24'])
    lo=max(0,i-20); hi=min(len(data),i+21)
    neighbours=np.array([j for j in range(lo,hi) if j!=i],dtype=np.int64)
    normal=np.flatnonzero(~excluded)
    # Nearest in signed physical CODE, across all non-flagged samples.
    # This is NOT the closest bit pattern or an assumed original word.
    codes=data['code'][normal].astype(np.int64)
    nearest=int(normal[np.argmin(np.abs(codes-int(row['code'])))]) if len(normal) else None
    def neighbour(j):
        return dict(index=int(j),sequence=int(data[j]['sequence']),raw24=f'0x{int(data[j]["raw24"]):06X}',
                    code=int(data[j]['code']),current_ma=float(data[j]['raw_ma']),
                    tim2_us=int(data[j]['tim2_us']),elapsed_us64=int(data[j]['elapsed_us']),
                    **xor_details(raw,data[j]['raw24']))
    prev=neighbour(i-1) if i else None; following=neighbour(i+1) if i+1<len(data) else None
    start=i; end=i
    while start and excluded[start-1]: start-=1
    while end+1<len(data) and excluded[end+1]: end+=1
    crossings=sign_cross(data[i-1]['code'],data[i+1]['code']) if prev and following else None
    return dict(index=i,sequence=int(row['sequence']),tim2_us=int(row['tim2_us']),
        elapsed_us64=int(row['elapsed_us']),raw24=f'0x{raw:06X}',raw_binary=f'{raw:024b}',
        code=int(row['code']),current_ma=float(row['raw_ma']),
        neighbour_median_ma=float(np.median(data['raw_ma'][neighbours])) if len(neighbours) else None,
        previous=prev,following=following,nearest_normal_signed_value=neighbour(nearest) if nearest is not None else None,
        neighbouring_sign_cross=crossings,
        previous_sign=int(np.sign(data[i-1]['code'])) if prev else None,
        event_sign=int(np.sign(row['code'])),following_sign=int(np.sign(data[i+1]['code'])) if following else None,
        dt_before_us=int(row['elapsed_us'])-prev['elapsed_us64'] if prev else None,
        dt_after_us=following['elapsed_us64']-int(row['elapsed_us']) if following else None,
        run_samples=end-start+1,run_observed_span_us=int(data[end]['elapsed_us'])-int(data[start]['elapsed_us']),
        pulse_duration_note='Sample run only; physical pulse duration cannot be inferred from discrete SPI results',
        neighbours=[dict(relative_index=int(j-i),**neighbour(int(j))) for j in neighbours])


def plot_event(data,event,path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtWidgets
    import pyqtgraph as pg
    import pyqtgraph.exporters
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    i=event['index']; lo=max(0,i-20); hi=min(len(data),i+21)
    part=data[lo:hi]; times=(part['elapsed_us'].astype(np.int64)-int(data[i]['elapsed_us']))/1000
    widget=pg.GraphicsLayoutWidget()
    widget.addLabel(f"{event['dataset']} seq={event['sequence']} RAW={event['raw24']} code={event['code']}")
    widget.nextRow(); p=widget.addPlot(title='Current, ALL RAW; no replacement'); p.setLabel('left','mA')
    p.plot(times,part['raw_ma'],pen='c',symbol='o',symbolSize=3)
    p.plot([0],[event['current_ma']],pen=None,symbol='o',symbolBrush='r')
    widget.nextRow(); p=widget.addPlot(title='Unsigned RAW24 (sign crossing wraps around 0xFFFFFF)')
    p.plot(times,part['raw24'],pen='y',symbol='o',symbolSize=3)
    widget.nextRow(); p=widget.addPlot(title='RAW bits 23..0 (each row: low=bit, high=bit+0.7)')
    for bit in range(24):
        p.plot(times,bit+((part['raw24']>>bit)&1)*.7,pen=pg.intColor(bit,hues=24))
    p.setLabel('bottom','Time relative to event',units='ms'); p.setLabel('left','Bit position')
    widget.resize(1100,850)
    pg.exporters.ImageExporter(widget.scene()).export(str(path)); widget.close()
    return app # caller keeps QApplication alive


def analyze_dataset(name,source,output,verify_directory=None,verification=None):
    dest=output/name; dest.mkdir(parents=True,exist_ok=True)
    data=read_raw_csv(source,dest/'raw_cache.bin')
    try:
        if not len(data): raise ValueError(f'Empty source {source}')
        pulse,rfu,mismatch,criteria=masks(data); excluded=pulse|rfu|mismatch
        normal_values=data['raw_ma'][~excluded]
        seconds=(int(data[-1]['elapsed_us'])-int(data[0]['elapsed_us']))/1e6
        indices=np.flatnonzero(excluded); events=[]
        known_normal=set(map(int,np.unique(data['raw24'][~excluded])))
        for index in indices:
            event=event_details(data,index,excluded); event['dataset']=name
            raw=int(data[index]['raw24'])
            event['one_bit_variants_seen_in_normal_data']=[dict(bit=bit,raw24=f'0x{raw^(1<<bit):06X}')
                for bit in range(4,24) if raw^(1<<bit) in known_normal]
            event['one_bit_variant_warning']='Observed elsewhere in same dataset; NOT evidence of the pre-error word'
            event['single_bit_vs_actual_adjacent']=any(event[key] and event[key]['hamming']==1 for key in ('previous','following'))
            event['raw_occurrences_in_dataset']=int(np.sum(data['raw24']==raw))
            events.append(event)
        # Compare event's two actual neighbours to the same strict sign definition
        # at ordinary positions. Zero itself is not assigned a positive sign.
        left=data['code'][:-2]; right=data['code'][2:]
        eligible=~excluded[:-2]&~excluded[2:]
        cross=((left<0)&(right>0))|((left>0)&(right<0))
        ordinary=eligible&~excluded[1:-1]
        event_eligible=eligible&excluded[1:-1]
        gaps=np.diff(data['elapsed_us'].astype(np.int64))
        # Direct per-sample timing comparison, unlike unlocalized budget counters.
        before=gaps[:-1]; after=gaps[1:]
        delayed=(before>90)&(after<75)
        with Path(source).open('rb') as file:
            digest=hashlib.file_digest(file,'sha256').hexdigest()
        result=dict(dataset=name,source=str(Path(source).resolve()),samples=len(data),
            source_sha256=digest,
            first_tim2_us=int(data[0]['tim2_us']),last_tim2_us=int(data[-1]['tim2_us']),
            elapsed_s=seconds,rate_hz=(len(data)-1)/seconds if seconds else None,
            criteria=criteria,full=statistics(data['raw_ma']),normal_subset=statistics(normal_values),
            event_count=len(events),events_per_second=len(events)/seconds if seconds else None,
            events_per_million_samples=len(events)*1e6/len(data),
            single_bit_vs_actual_adjacent=sum(e['single_bit_vs_actual_adjacent'] for e in events),
            sign_cross=dict(definition='Strict opposite nonzero signs at i-1 and i+1; both neighbours non-flagged',
                baseline_count=int(np.sum(cross&ordinary)),baseline_eligible=int(ordinary.sum()),
                event_count=int(np.sum(cross&event_eligible)),event_eligible=int(event_eligible.sum())),
            timing_pattern=dict(definition='dt_before>90 us AND dt_after<75 us; descriptive fixed bins, not a causal test',
                baseline_count=int(np.sum(delayed&ordinary)),baseline_eligible=int(ordinary.sum()),
                event_count=int(np.sum(delayed&event_eligible)),event_eligible=int(event_eligible.sum())),
            intervals_us=dict(min=int(gaps.min()),max=int(gaps.max()),median=float(np.median(gaps)),
                distribution={str(int(k)):int(v) for k,v in zip(*np.unique(gaps,return_counts=True))}) if len(gaps) else {},
            high_byte_events=dict(Counter(e['raw24'][2:4] for e in events)),
            high_byte_normal={f'{int(k):02X}':int(v) for k,v in zip(*np.unique(data['raw24'][~excluded]>>16,return_counts=True))},
            timing_correlation='Unavailable: service-budget counters are session totals, no per-event linkage',
            events=events)
        if verify_directory:
            result['verification']=verify(verify_directory,dest/'verification.json')
        elif verification is not None: result['verification']=verification
        app=None
        for event in events:
            path=dest/f"event_{event['sequence']:09d}.png"
            app=plot_event(data,event,path); event['plot']=str(path)
        (dest/'analysis.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        return result
    finally:
        if isinstance(data,np.memmap): data._mmap.close()


def import_gui(directory,output):
    """Replay original GUI wire archive, compare every tuple with original GUI CSV."""
    from tools.stream_capture import FrameDecoder
    source=next(directory.glob('*.csf4')); gui_csv=source.with_suffix('.csv')
    dest=output/directory.name; dest.mkdir(parents=True,exist_ok=True)
    converted=dest/'replayed_raw.csv'; decoder=FrameDecoder(); count=0; previous=None; elapsed=0
    errors=[]
    with source.open('rb') as binary, gui_csv.open(newline='',encoding='utf8') as table, converted.open('w',newline='',encoding='utf8') as out:
        rows=iter(csv.DictReader(table)); writer=csv.writer(out)
        writer.writerow(('sequence','tim2_us','elapsed_us64','raw24','vshunt_code','current_ma','current_cal_ma'))
        while block:=binary.read(65536):
            for frame in decoder.feed(block):
                if frame['type']!=1: continue
                if frame['protocol_version']!=2: raise ValueError('Expected GUI v2 RAW')
                for (seq,stamp,code),raw in zip(frame['samples'],frame['raw24']):
                    row=next(rows,None)
                    if row is None or (int(row['slot']),int(row['tim2_read_us']),int(row['vshunt_code']))!=(seq,stamp,code):
                        raise ValueError('GUI CSV/wire tuple mismatch')
                    if previous is not None: elapsed+=(stamp-previous)&0xFFFFFFFF
                    if abs(float(row['time_s'])-elapsed/1e6)>0.000001: raise ValueError('GUI CSV/wire time mismatch')
                    previous=stamp; count+=1
                    writer.writerow((seq,stamp,elapsed,f'0x{raw:06X}',code,code/16,code/16))
        if next(rows,None) is not None: errors.append('Extra GUI CSV rows')
    transport={k:getattr(decoder,k) for k in ('crc_errors','format_errors','frame_gaps','slot_gaps','code_errors','resync_bytes')}
    errors.extend(f'{k}={v}' for k,v in transport.items() if v)
    if not decoder.ended: errors.append('END missing from GUI archive')
    return converted,dict(status='FAIL' if errors else 'PASS',errors=errors,transport=transport,
        samples=count,end_received=decoder.ended,csv_wire_tuples_identical=True,
        raw_source=str(source),note='Derived CSV from original CSF4; GUI CSV stores CODE rather than RAW24')


def discover_sources(root):
    sources=[]; seen=set()
    accuracy=root/'accuracy_100ma'
    for name in ('ZERO_BEFORE','LOAD_100','ZERO_AFTER','LOAD_100_LONG'):
        p=accuracy/name
        sources.append((name,p/'raw.csv',p))
    for path in sorted(root.glob('*/summary.json')):
        if path.parent==accuracy or path.parent.name=='raw_forensics': continue
        meta=json.loads(path.read_text(encoding='utf8'))
        if len(meta.get('runs',[]))!=1: continue
        run=meta['runs'][0]; source=Path(run.get('csv',''))
        if not source.is_file(): continue
        firmware=meta.get('identity',{}).get('FW') or run.get('reports',{}).get('stream status',{}).get('FW')
        if firmware!='0.5.0': continue
        with source.open(encoding='utf8') as f: header=f.readline()
        if 'raw24' not in header: continue
        sources.append((path.parent.name,source,path.parent))
    # Deduplicate copies by count + first/last times from the stored CSV summary
    # would depend on metadata correctness. Use the actual whole-file SHA256.
    for name,source,directory in sources:
        with source.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
        if digest in seen: continue
        seen.add(digest); yield name,source,directory


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('results/hil'))
    p.add_argument('--output',type=Path,default=Path('results/hil/raw_forensics'))
    p.add_argument('--include-comparison',action='store_true')
    p.add_argument('--reuse-derived',action='store_true',help='Reuse existing analysis only if source SHA256 matches')
    a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    sources=list(discover_sources(a.root))
    if a.include_comparison:
        directory=a.output/'ZERO_84_300'
        sources.append(('ZERO_84_300',directory/'raw.csv',directory))
    results=[]
    for name,source,directory in sources:
        print('Analyzing/replaying',name,flush=True)
        cache=a.output/name/'analysis.json'
        if a.reuse_derived and cache.exists():
            stored=json.loads(cache.read_text(encoding='utf8'))
            with source.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
            if stored.get('source_sha256')==digest and 'timing_pattern' in stored:
                results.append(stored);continue
        results.append(analyze_dataset(name,source,a.output, directory))
    for directory in sorted(a.root.glob('gui_*')):
        if not list(directory.glob('*.csf4')): continue
        print('Replaying GUI',directory.name,flush=True)
        converted,verification=import_gui(directory,a.output)
        results.append(analyze_dataset(directory.name,converted,a.output,verification=verification))
    events=[e for r in results for e in r['events']]
    counts=Counter(e['raw24'] for e in events)
    for e in events: e['raw_occurrences_across_unique_captures']=counts[e['raw24']]
    keys=('dataset','sequence','tim2_us','elapsed_us64','raw24','raw_binary','code','current_ma',
          'neighbour_median_ma','dt_before_us','dt_after_us','neighbouring_sign_cross','single_bit_vs_actual_adjacent',
          'run_samples','raw_occurrences_across_unique_captures','previous','following','nearest_normal_signed_value',
          'one_bit_variants_seen_in_normal_data','plot')
    with (a.output/'events.csv').open('w',newline='',encoding='utf8') as f:
        writer=csv.DictWriter(f,fieldnames=keys); writer.writeheader()
        for event in events:
            writer.writerow({k:json.dumps(event[k]) if isinstance(event[k],(list,dict)) else event[k] for k in keys})
    summary=dict(datasets=results,total_samples=sum(r['samples'] for r in results),event_count=len(events),
                 repeated_raw=dict(counts),conclusion='D: SPI wire trace required to localize origin; descriptive bit patterns do not prove physical SPI errors')
    summary['classification']=dict(single_bit_vs_actual_adjacent=sum(e['single_bit_vs_actual_adjacent'] for e in events),
        multi_bit_vs_both_actual_adjacent=sum(not e['single_bit_vs_actual_adjacent'] for e in events),
        one_bit_variant_observed_elsewhere=sum(bool(e['one_bit_variants_seen_in_normal_data']) for e in events),
        isolated_single_sample_runs=sum(e['run_samples']==1 for e in events))
    probe=a.output/'profile150_probe.json'
    if probe.exists():
        summary['comparison_150_us']=dict(status='UNAVAILABLE_WITH_EXISTING_FW',
            evidence=json.loads(probe.read_text(encoding='utf8')),
            note='No 150 us Native streaming capture was performed; no rate-dependence conclusion')
    (a.output/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    print('Events',len(events),'samples',summary['total_samples'],flush=True)


if __name__=='__main__': main()
