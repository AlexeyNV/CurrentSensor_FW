"""RAW current accuracy statistics; no acquisition or calibration writes."""
import csv
import json
import math
from pathlib import Path
import numpy as np

DTYPE=np.dtype([('sequence','<u4'),('tim2_us','<u4'),('elapsed_us','<u8'),('raw24','<u4'),
                ('code','<i4'),('raw_ma','<f8'),('cal_ma','<f8')])
_PLOT_APP=None

def statistics(values):
    values=np.asarray(values,dtype=np.float64)
    if not len(values): return {'n':0}
    return dict(n=len(values),median_ma=float(np.median(values)),mean_ma=float(np.mean(values)),
                min_ma=float(np.min(values)),max_ma=float(np.max(values)),
                rms_ma=float(np.sqrt(np.mean(values*values))),std_ma=float(np.std(values)),
                p99_ma=float(np.percentile(values,99)),p999_ma=float(np.percentile(values,99.9)))

def masks(data, min_deviation_ma=10., sigma_multiplier=8.):
    values=data['raw_ma']; median=float(np.median(values))
    mad=float(np.median(np.abs(values-median))); threshold=max(min_deviation_ma,sigma_multiplier*1.4826*mad)
    decoded=(data['raw24'].astype(np.int64)>>4)
    decoded-=((data['raw24']&0x800000)!=0)*0x100000
    pulse=np.abs(values-median)>threshold
    rfu=(data['raw24']&15)!=0
    mismatch=(decoded!=data['code']) | (values!=data['code']/16)
    return pulse,rfu,mismatch,dict(center_median_ma=median,mad_ma=mad,threshold_ma=threshold,
        criterion='abs(I_raw-median)>max(10mA,8*1.4826*MAD) OR RFU!=0 OR RAW/CODE/value mismatch',
        min_deviation_ma=min_deviation_ma,sigma_multiplier=sigma_multiplier,
        warning='Bias relative to Fluke is NOT an exclusion criterion; stable bias is retained')

def compare(stats, before, after, resolution_ma, reference_conditions_confirmed=False):
    ref=(before+after)/2
    bound=max(abs(before),abs(after))*.015+3*resolution_ma+abs(after-before)/2
    error=stats['mean_ma']-ref
    worst=max(abs(stats['min_ma']-ref),abs(stats['max_ma']-ref))
    def verdict(deviation):
        if deviation+bound<=10: return 'WITHIN_BOUND' if reference_conditions_confirmed else 'CONDITIONAL_WITHIN_BOUND'
        if deviation-bound>10: return 'OUTSIDE_BOUND' if reference_conditions_confirmed else 'CONDITIONAL_OUTSIDE_BOUND'
        return 'INCONCLUSIVE'
    return dict(reference_ma=ref,reference_before_ma=before,reference_after_ma=after,
        reference_endpoint_change_ma=after-before,reference_error_bound_ma=bound,
        bound_model='max(abs(endpoints))*1.5% + 3*resolution + abs(after-before)/2',
        mean_difference_ma=error,median_difference_ma=stats['median_ma']-ref,
        mean_difference_percent=100*error/abs(ref) if ref else None,
        mean_verdict=verdict(abs(error)),all_samples_verdict=verdict(worst),
        worst_observed_deviation_ma=worst,scope='This point and recording only; endpoint readings do not bound unobserved load drift',
        reference_conditions_confirmed=reference_conditions_confirmed)

def read_csv(source, binary):
    n=0; batch=[]
    with Path(source).open(newline='',encoding='utf8') as file, Path(binary).open('wb') as out:
        for row in csv.DictReader(file):
            batch.append((int(row['sequence']),int(row['tim2_us']),int(row['elapsed_us64']),
                int(row['raw24'],0),int(row['vshunt_code']),float(row['current_ma']),float(row['current_cal_ma'])))
            if len(batch)>=32768:
                np.array(batch,dtype=DTYPE).tofile(out); n+=len(batch); batch.clear()
        if batch: np.array(batch,dtype=DTYPE).tofile(out); n+=len(batch)
    return np.memmap(binary,dtype=DTYPE,mode='r',shape=(n,)) if n else np.empty(0,dtype=DTYPE)

def plots(data, output):
    global _PLOT_APP
    # Existing plotting dependency; export standalone plots without AI images.
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtWidgets,QtGui
    import pyqtgraph as pg
    import pyqtgraph.exporters
    _PLOT_APP=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/segoeui.ttf'
    if font.exists(): QtGui.QFontDatabase.addApplicationFont(str(font))
    values=data['raw_ma']; times=data['elapsed_us']/1e6
    plot=pg.PlotWidget(title='RAW current: exact min/max envelope (statistics use all samples)')
    plot.setLabel('left','Current',units='mA'); plot.setLabel('bottom','Elapsed',units='s')
    blocks=np.array_split(np.arange(len(values)),min(2500,len(values)))
    chosen=np.sort(np.concatenate([np.array([b[np.argmin(values[b])],b[np.argmax(values[b])]]) for b in blocks]))
    plot.plot(times[chosen],values[chosen],pen='c'); plot.resize(1400,650)
    exporter=pg.exporters.ImageExporter(plot.plotItem); exporter.export(str(Path(output)/'raw_timeline.png'))
    median=float(np.median(values)); sigma=1.4826*float(np.median(np.abs(values-median)))
    plot.setYRange(median-max(10,8*sigma),median+max(10,8*sigma),padding=0)
    plot.setTitle('RAW zoom around median; outliers remain in full timeline and source')
    pg.exporters.ImageExporter(plot.plotItem).export(str(Path(output)/'raw_zoom.png'))
    plot.close()
    hist=pg.PlotWidget(title='Full RAW distribution'); hist.setLabel('bottom','Current',units='mA')
    counts,bins=np.histogram(values,bins=200)
    hist.plot((bins[:-1]+bins[1:])/2,counts,pen='y'); hist.resize(1400,650)
    pg.exporters.ImageExporter(hist.plotItem).export(str(Path(output)/'raw_histogram.png')); hist.close()

def analyze(source, output, reference=None):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    data=read_csv(source,output/'analysis_cache.bin')
    try:
        return _analyze_data(data,source,output,reference)
    finally:
        if isinstance(data,np.memmap): data._mmap.close()

def write_context(data, index, contexts):
    lo=max(0,int(index)-200); hi=min(len(data),int(index)+201)
    name=f'context_{int(index):09d}.csv'
    with (contexts/name).open('w',newline='',encoding='utf8') as context:
        rows=csv.writer(context); rows.writerow(('relative_index',*DTYPE.names))
        for j in range(lo,hi): rows.writerow((j-int(index),*(data[j][k].item() for k in DTYPE.names)))
    return name,lo,hi

def _analyze_data(data,source,output,reference):
    if not len(data): raise ValueError('Empty capture: no accuracy verdict')
    pulse,rfu,mismatch,criteria=masks(data); excluded=pulse|rfu|mismatch
    indices=np.flatnonzero(excluded)
    durations=(int(data['elapsed_us'][-1])-int(data['elapsed_us'][0]))/1e6
    max_run=run=0
    for i,index in enumerate(indices):
        run=run+1 if i and index==indices[i-1]+1 else 1
        max_run=max(max_run,run)
    full=statistics(data['raw_ma']); robust=statistics(data['raw_ma'][~excluded])
    result=dict(full=full,robust=robust,calibrated_full=statistics(data['cal_ma']),criteria=criteria,
        exclusions=dict(total=int(excluded.sum()),pulse=int(pulse.sum()),rfu=int(rfu.sum()),
                        raw_code_mismatch=int(mismatch.sum()),fraction=float(excluded.mean())),
        source=str(Path(source).resolve()),statistics_source='ALL_RAW_SAMPLES',
        robust_warning='Descriptive statistics only; excluded samples remain in originals and prevent an all-sample accuracy claim')
    result['anomaly_statistics']=dict(count=len(indices),rate_per_second=len(indices)/durations if durations else None,
        clusters=int(np.sum(np.diff(indices)!=1)+1) if len(indices) else 0,longest_run=int(max_run),
        positive_deviations=int(np.sum(data['raw_ma'][excluded]>criteria['center_median_ma'])),
        negative_deviations=int(np.sum(data['raw_ma'][excluded]<criteria['center_median_ma'])),
        max_abs_deviation_ma=float(np.max(np.abs(data['raw_ma'][excluded]-criteria['center_median_ma']))) if len(indices) else None,
        statistics=statistics(data['raw_ma'][excluded]))
    contexts=output/'contexts'; contexts.mkdir(exist_ok=True)
    with (output/'anomalies.csv').open('w',newline='',encoding='utf8') as file:
        writer=csv.writer(file); writer.writerow(('index','sequence','tim2_us','raw24','current_ma','deviation_from_median_ma','pulse','rfu','mismatch','previous_available','following_available','context'))
        for index in np.flatnonzero(excluded):
            name,lo,hi=write_context(data,index,contexts)
            row=data[index]; writer.writerow((index,row['sequence'],row['tim2_us'],f'0x{int(row["raw24"]):06X}',row['raw_ma'],row['raw_ma']-criteria['center_median_ma'],int(pulse[index]),int(rfu[index]),int(mismatch[index]),index-lo,hi-index-1,f'contexts/{name}'))
    if reference:
        result['comparison_full']=compare(full,**reference)
        result['comparison_robust']=compare(robust,**reference) if robust['n'] else None
        result['comparison_calibrated_full']=compare(result['calibrated_full'],**reference)
        # Requirement exceedances are reported independently of the statistical
        # exclusion mask: do not hide a stable bias by excluding it as noise.
        for key,values in (('comparison_full',data['raw_ma']),
                           ('comparison_calibrated_full',data['cal_ma'])):
            ref=result[key]['reference_ma']; bound=result[key]['reference_error_bound_ma']
            deviation=np.abs(values-ref)
            result[key]['observed_samples_outside_reference_10ma']=int(np.sum(deviation>10))
            result[key]['samples_not_guaranteed_within_10ma_given_bound']=int(np.sum(deviation+bound>10))
        # Preserve contexts for reference exceedances too, without excluding them
        # from robust statistics or calling them corrupted transport samples.
        ref=result['comparison_full']['reference_ma']
        with (output/'reference_exceedances.csv').open('w',newline='',encoding='utf8') as file:
            writer=csv.writer(file); writer.writerow(('index','sequence','current_ma','reference_ma','deviation_ma','excluded_from_robust','context'))
            for index in np.flatnonzero(np.abs(data['raw_ma']-ref)>10):
                name,_,_=write_context(data,index,contexts)
                row=data[index]
                writer.writerow((index,row['sequence'],row['raw_ma'],ref,row['raw_ma']-ref,int(excluded[index]),f'contexts/{name}'))
    plots(data,output)
    (output/'analysis.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result
