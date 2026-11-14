"""Streamed candidate analysis and filtered range queries on large RAW histories."""
import argparse
import json
import platform
import tempfile
import time
from pathlib import Path
import numpy as np
import psutil
from current_sensor.anomaly_diagnostics import analyze_history
from current_sensor.data import RAW_DTYPE
from current_sensor import __version__


def run(size, queries=200):
    with tempfile.TemporaryDirectory() as directory:
        data=np.memmap(Path(directory)/'test.raw.bin',RAW_DTYPE,mode='w+',shape=(size,))
        for start in range(0,size,100000):
            end=min(size,start+100000)
            data['slot'][start:end]=np.arange(start,end,dtype=np.uint32)
            data['tim2_read_us'][start:end]=(np.arange(start,end,dtype=np.uint64)*100).astype(np.uint32)
            data['time_s'][start:end]=np.arange(start,end)*.0001
            data['vshunt_code'][start:end]=-77
        data['vshunt_code'][size//2]=-8269
        started=time.perf_counter()
        analyzer=analyze_history(data,'benchmark')
        elapsed=time.perf_counter()-started
        if analyzer.event_count!=1 or len(analyzer.index.mask)!=size:
            raise AssertionError('detector coverage or candidate count differs')
        rng=np.random.default_rng(12)
        durations=[]
        for _ in range(queries):
            a,b=sorted(rng.integers(0,size,2))
            t=time.perf_counter()
            result=analyzer.range_statistics(data,float(data['time_s'][a]),float(data['time_s'][b]))
            durations.append((time.perf_counter()-t)*1000)
            if result['valid_count']+result['excluded_count']!=b-a+1:
                raise AssertionError('filtered range count differs')
        result=dict(samples=size,analysis_seconds=elapsed,throughput_samples_per_second=size/elapsed,
                    candidate_count=analyzer.event_count,complete=analyzer.complete,
                    filtered_query_mean_ms=float(np.mean(durations)),
                    filtered_query_p95_ms=float(np.percentile(durations,95)),
                    rss_mb=psutil.Process().memory_info().rss/1024**2)
        del data,analyzer
        return result


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--output',default='docs/gui_012_anomaly_benchmark.json')
    parser.add_argument('--sizes',nargs='+',type=int,default=[100000,600000,3000000,10000000])
    args=parser.parse_args()
    report=dict(gui_version=__version__,platform=platform.platform(),hardware_test=False,cases=[])
    for size in args.sizes:
        case=run(size)
        report['cases'].append(case)
        print(json.dumps(case),flush=True)
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
