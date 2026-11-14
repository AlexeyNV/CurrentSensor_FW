"""Reproducible host-only CSF4 archive throughput/open/seek benchmark."""
import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
import numpy as np
import psutil
from current_sensor.archive import ArchiveOptions, ArchiveReader, ArchiveRecorder
from current_sensor.data import RAW_DTYPE
from current_sensor.mock_device import encode_frame
from tools.stream_capture import FrameDecoder


def run_case(directory,method,codes):
    process=psutil.Process()
    rss=process.memory_info().rss
    cpu0=process.cpu_times()
    start=time.perf_counter()
    writer=ArchiveRecorder(directory,{'source_session':'benchmark',
        'firmware_version':'MOCK','profile':{'sample_rate_hz':10000,'sample_period_us':100}},
        ArchiveOptions(25,0,0,method))
    decoder=FrameDecoder()
    sequence=0
    config=decoder.feed(encode_frame(4,sequence,100))[0]
    writer.submit(np.empty(0,RAW_DTYPE),(config,))
    sequence+=1
    for slot in range(0,len(codes),32):
        while writer.queue.qsize()>128 and not writer.error:
            rss=max(rss,process.memory_info().rss)
            time.sleep(.001)
        if writer.error:
            raise RuntimeError(writer.error)
        block=codes[slot:slot+32]
        packet=encode_frame(1,sequence,100,slot,(slot*100)&0xffffffff,block)
        frame=decoder.feed(packet)[0]
        raw=np.empty(len(block),RAW_DTYPE)
        raw['slot']=[item[0] for item in frame['samples']]
        raw['tim2_read_us']=[item[1] for item in frame['samples']]
        raw['vshunt_code']=block
        raw['time_s']=np.arange(slot,slot+len(block))*0.0001
        if not writer.submit(raw,(frame,)):
            raise RuntimeError(writer.error or 'ARCHIVE_QUEUE_OVERFLOW')
        sequence+=1
    end=decoder.feed(encode_frame(3,sequence,100))[0]
    writer.submit(np.empty(0,RAW_DTYPE),(end,))
    writer.finish({'session_end_verified':True,'acquisition_finished':True})
    if not writer.done.wait(60):
        raise TimeoutError('archive writer did not finish')
    if writer.error:
        raise RuntimeError(writer.error)
    elapsed=time.perf_counter()-start
    cpu1=process.cpu_times()
    rss=max(rss,process.memory_info().rss)
    opened_at=time.perf_counter()
    reader=ArchiveReader(writer.manifest_path)
    open_ms=(time.perf_counter()-opened_at)*1000
    if reader.sample_count!=len(codes):
        raise AssertionError('archive sample count differs from input')
    overview_at=time.perf_counter()
    overview=reader.plot_range(reader.first_time_s,reader.last_time_s,1200)
    overview_ms=(time.perf_counter()-overview_at)*1000
    if not len(overview.x):
        raise AssertionError('overview is empty')
    rng=np.random.default_rng(319)
    seek=[];stats_times=[]
    for stamp in rng.uniform(0,max(0,reader.last_time_s-.02),100):
        t0=time.perf_counter()
        view=reader.raw_range(float(stamp),float(stamp+.02))
        if not len(view):
            raise AssertionError('indexed seek lost data')
        seek.append((time.perf_counter()-t0)*1000)
        t0=time.perf_counter()
        result=reader.range_stats(float(stamp),float(stamp+.02))
        if result['count']!=len(view):
            raise AssertionError('indexed range stats differ from RAW count')
        stats_times.append((time.perf_counter()-t0)*1000)
    reader.close()
    return dict(method=method,samples=len(codes),archive_bytes=writer.bytes_written,
        segment_count=len(writer.segments),elapsed_s=elapsed,
        throughput_samples_s=len(codes)/elapsed,
        throughput_mib_s=writer.bytes_written/1024**2/elapsed,
        cpu_s=(cpu1.user+cpu1.system)-(cpu0.user+cpu0.system),
        process_rss_peak_mib=rss/1024**2,open_ms=open_ms,overview_ms=overview_ms,
        seek_median_ms=statistics.median(seek),seek_p95_ms=float(np.percentile(seek,95)),
        stats_median_ms=statistics.median(stats_times),stats_p95_ms=float(np.percentile(stats_times,95)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=int,default=200000)
    parser.add_argument('--methods',nargs='+',choices=('none','zlib1','zlib3'),
                        default=('none','zlib1','zlib3'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.samples<1000:
        parser.error('--samples must be at least 1000')
    rng=np.random.default_rng(42)
    codes=rng.normal(-77,48,args.samples).astype(np.int32)
    codes[::10000]=-8269
    rows=[]
    with tempfile.TemporaryDirectory() as root:
        for method in args.methods:
            rows.append(run_case(root,method,codes))
    base=next((row['archive_bytes'] for row in rows if row['method']=='none'),None)
    for row in rows:
        row['size_ratio_to_plain']=row['archive_bytes']/base if base else None
    output=dict(benchmark='host synthetic CSF4, not hardware',
                python=os.sys.version.split()[0],samples=args.samples,results=rows)
    rendered=json.dumps(output,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(rendered+'\n',encoding='utf-8')
    print(rendered)


if __name__=='__main__':
    main()
