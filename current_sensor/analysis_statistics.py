"""Exact bounded-memory RAW statistics for the existing background worker."""
from bisect import bisect_left,bisect_right
import numpy as np
from .archive import ArchiveReader
from .data import robust_y_range

LOW=-524288
SIZE=1048576


def overview_robust_bounds(source):
    """Bounded axis estimate only. The detector and statistics are unchanged."""
    if isinstance(source.data,ArchiveReader):
        reader=source.data;entries=reader.data_entries
        chosen=np.unique(np.linspace(0,len(entries)-1,min(32,len(entries)),dtype=int)) if len(entries) else []
        parts=[]
        for i in chosen:
            raw,_=reader._block(entries[i])
            parts.append(raw['vshunt_code'][::max(1,(len(raw)+255)//256)])
        codes=np.concatenate(parts) if parts else np.empty(0,np.int32)
    else:codes=source.raw()['vshunt_code']
    return robust_y_range(codes)


def rolling_statistics(source,left,right):
    """100 ms RAW mean, including every SUSPECT; no transport-thread work."""
    if isinstance(source.data,ArchiveReader):
        chunks=source.data.iter_range(left,right)
        complete=source.data.first_time_s<=left and source.data.last_time_s>=right
    else:
        data=source.raw();lo=bisect_left(data['time_s'],left);hi=bisect_right(data['time_s'],right)
        chunks=(data[lo:hi],)
        complete=bool(len(data)) and float(data['time_s'][0])<=left and float(data['time_s'][-1])>=right
    total=count=0
    for raw in chunks:
        for i in range(0,len(raw),65536):
            codes=raw['vshunt_code'][i:i+65536].astype(np.int64)
            total+=int(codes.sum());count+=len(codes)
    return dict(mean_ma=total/count/16 if count else None,count=count,first_time_s=left,
        last_time_s=right,policy='ALL_RAW_INCLUDING_SUSPECT',requested_window_s=.1,complete=complete)


def histogram_median(hist):
    count=int(hist.sum())
    if not count: return None
    cumulative=np.cumsum(hist)
    a=int(np.searchsorted(cumulative,(count-1)//2+1))+LOW
    b=int(np.searchsorted(cumulative,count//2+1))+LOW
    return (a+b)/32


def panel_statistics(source,left,right):
    left,right=sorted((left,right)); data=source.data
    if isinstance(data,ArchiveReader): chunks=data.iter_range(left,right)
    else:
        data=source.raw();lo=bisect_left(data['time_s'],left);hi=bisect_right(data['time_s'],right)
        chunks=(data[i:min(i+65536,hi)] for i in range(lo,hi,65536))
    hist=np.zeros(SIZE,np.uint64); clean_hist=np.zeros_like(hist)
    totals=[0,0];squares=[0,0];first=last=None;last_value=None;count=0;excluded=0
    analyzer=source.analyzer
    events=[]
    if analyzer is not None:
        with analyzer.lock:
            events=analyzer.events[bisect_left(analyzer.event_times,left):bisect_right(analyzer.event_times,right)]
            analysis_complete=analyzer.complete and analyzer.finished
    else: analysis_complete=False
    event_times=np.array([e['time_s'] for e in events])
    for part in chunks:
        for start in range(0,len(part),65536):
            block=part[start:start+65536];codes=block['vshunt_code'].astype(np.int64)
            if np.any((codes<LOW)|(codes>=LOW+SIZE)): raise ValueError('Code outside signed 20-bit domain')
            if not len(codes): continue
            if first is None: first=float(block['time_s'][0])
            last=float(block['time_s'][-1]);last_value=float(codes[-1]/16);count+=len(codes)
            valid=np.ones(len(codes),bool)
            a=bisect_left(event_times,block['time_s'][0]);b=bisect_right(event_times,block['time_s'][-1])
            for event in events[a:b]:
                i=bisect_left(block['time_s'],event['time_s'])
                if i<len(block) and int(block['slot'][i])==event['slot']: valid[i]=False
            excluded+=int((~valid).sum())
            for k,values in enumerate((codes,codes[valid])):
                h=hist if k==0 else clean_hist
                unique,freq=np.unique(values,return_counts=True)
                h[unique-LOW]+=freq.astype(np.uint64)
                totals[k]+=int(values.sum());squares[k]+=int(np.dot(values,values))
    def finish(h,k):
        n=int(h.sum()); nonzero=np.flatnonzero(h)
        mean=totals[k]/n if n else None
        return dict(count=n,min=(int(nonzero[0])+LOW)/16 if n else None,
            max=(int(nonzero[-1])+LOW)/16 if n else None,mean=mean/16 if n else None,
            median=histogram_median(h),rms=(squares[k]/n)**.5/16 if n else None,
            std=max(0,squares[k]/n-mean*mean)**.5/16 if n else None)
    duration=last-first if first is not None else 0
    return dict(raw=finish(hist,0),excluding_suspect=finish(clean_hist,1),suspects=excluded,
        duration_s=duration,effective_rate_hz=(count-1)/duration if duration else 0,
        first_time_s=first,last_time_s=last,last_value_ma=last_value,
        analysis_complete=analysis_complete,source_kind=source.source_kind,
        history_complete=source.full_available and (isinstance(source.data,ArchiveReader) or source.expected_count is None or len(data)>=source.expected_count),
        note='Statistics are RAW; excluded statistics are descriptive, not corrected data')


def nearest_sample(source,stamp):
    archive=source.data if isinstance(source.data,ArchiveReader) else None
    raw_value=None
    if archive is not None:
        if stamp<archive.first_time_s or stamp>archive.last_time_s:return None
        entries=archive.data_entries
        if not len(entries): return None
        position=min(len(entries)-1,bisect_left(entries['last_time_us'],int(stamp*1e6)))
        candidates=[]; frames=[]
        for i in range(max(0,position-1),min(len(entries),position+2)):
            block,wire=archive._block(entries[i]);candidates.append(block);frames.extend(wire)
        data=np.concatenate(candidates)
    else: data=source.raw()
    if not len(data): return None
    if stamp<float(data['time_s'][0]) or stamp>float(data['time_s'][-1]):return None
    p=bisect_left(data['time_s'],stamp)
    i=min((j for j in (p-1,p) if 0<=j<len(data)),key=lambda j:abs(float(data[j]['time_s'])-stamp))
    row=data[i]; slot=int(row['slot']);tim2=int(row['tim2_read_us'])
    if archive is not None:
        for frame in frames:
            for sample,raw in zip(frame.get('samples',()),frame.get('raw24',())):
                if sample[:2]==(slot,tim2): raw_value=int(raw)
    else:
        cache=source.metadata.get('_raw24_cache')
        if cache and len(data)==Path_size(cache)//4:
            values=np.memmap(cache,dtype='<u4',mode='r',shape=(len(data),))
            value=int(values[i]);raw_value=value if value<=0xFFFFFF else None
            values._mmap.close()
        lookup=getattr(source,'raw24_lookup',None)
        if raw_value is None and lookup is not None: raw_value=lookup(slot,tim2)
    return dict(time_s=float(row['time_s']),current_ma=int(row['vshunt_code'])/16,
        sequence=slot,tim2_us=tim2,raw24=raw_value,code=int(row['vshunt_code']))


def Path_size(path):
    from pathlib import Path
    return Path(path).stat().st_size


def raw24_chunk(source,raw):
    """Return exact saved RAW24 or a sentinel; never reconstruct reserved bits."""
    values=np.full(len(raw),0xFFFFFFFF,np.uint32)
    if not len(raw):return values
    if isinstance(source.data,ArchiveReader):
        archive=source.data;lo,hi=archive._bounds(float(raw['time_s'][0]),float(raw['time_s'][-1]))
        lookup={}
        for entry in archive.data_entries[lo:hi]:
            _,frames=archive._block(entry)
            for frame in frames:
                lookup.update({(s[0],s[1]):v for s,v in zip(frame.get('samples',()),frame.get('raw24',()))})
        for i,row in enumerate(raw):values[i]=lookup.get((int(row['slot']),int(row['tim2_read_us'])),0xFFFFFFFF)
    elif source.metadata.get('_raw24_cache'):
        data=source.raw();path=source.metadata['_raw24_cache']
        if len(data)==Path_size(path)//4:
            cache=np.memmap(path,'<u4',mode='r',shape=(len(data),))
            lo=bisect_left(data['time_s'],raw['time_s'][0])
            values[:]=cache[lo:lo+len(raw)];cache._mmap.close()
    return values
