"""Write selected RAW samples from a fixed source snapshot on a separate thread."""
import csv
import json
import queue
import threading
import uuid
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from .data import RAW_DTYPE
from .range_stats import RawRangeIndex
from .spool import SpoolSnapshot
from .recorder import write_raw_rows
from .anomaly_diagnostics import analyze_history
from .session_metadata import normalize_legacy
from .mcu_anomaly import parse_mcu_anomaly_report
from .archive import ArchiveReader
from . import __version__
from .analysis_statistics import raw24_chunk


@dataclass
class ExportSource:
    data: object  # immutable ndarray/memmap or committed SpoolSnapshot
    metadata: dict
    full_available: bool
    source_kind: str
    index: RawRangeIndex | None = None
    expected_count: int | None = None
    analyzer: object | None = None
    base_index: int = 0

    def raw(self):
        return self.data.raw() if hasattr(self.data, 'raw') else self.data


def export_raw(source, directory, selection='FULL_SESSION', interval=None,
               cursor_x1_s=None, cursor_x2_s=None):
    """Synchronous core, called by ExportWorker; returns JSON path and sample count."""
    if isinstance(source.data,ArchiveReader):
        return export_archive(source,directory,selection,interval,cursor_x1_s,cursor_x2_s)
    started = time.perf_counter()
    data = source.raw()
    if not len(data):
        raise ValueError('NO_RAW_SAMPLES_AVAILABLE')
    index = source.index or (source.data.index if isinstance(source.data, SpoolSnapshot)
                             else RawRangeIndex.from_data(data))
    if interval is None:
        lo, hi = 0, len(data)
        left, right = float(data['time_s'][lo]), float(data['time_s'][hi-1])
    else:
        left, right = sorted(map(float, interval))
        lo, hi = bisect_left(data['time_s'], left), bisect_right(data['time_s'], right)
    if hi <= lo:
        raise ValueError('NO_RAW_SAMPLES_IN_INTERVAL')
    stats = index.query(data, left, right)
    base = Path(directory) / ('capture_export_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_')
                              + uuid.uuid4().hex[:8])
    base.parent.mkdir(parents=True, exist_ok=True)
    paths = [base.with_suffix(ext) for ext in ('.csv', '.raw.bin', '.json', '.events.csv')]
    period = source.metadata.get('profile', {}).get('sample_period_us')
    try:
        analyzer = source.analyzer
        if analyzer is None:
            analyzer = analyze_history(data, source.metadata.get('source_session', 'legacy'),
                                       source.metadata.get('_detector_rules'))
        else:
            deadline = time.monotonic()+2
            while len(analyzer.index.mask) < source.base_index+hi and not analyzer.finished and time.monotonic()<deadline:
                time.sleep(.005)
        filtered = analyzer.range_statistics(data, left, right, source.base_index)
        filtered['slot_gaps'] = stats['slot_gaps']
        filtered['slot_gaps_complete'] = stats['slot_gaps_complete']
        events = analyzer.events_between(source.base_index+lo, source.base_index+hi)
        detail_complete = filtered['complete'] and analyzer.complete
        with paths[0].open('x', newline='', encoding='utf-8') as csv_output, paths[1].open('xb') as raw_output:
            writer = csv.writer(csv_output)
            writer.writerow(('slot', 'tim2_read_us', 'vshunt_code', 'sample_period_us', 'time_s','raw24'))
            for start in range(lo, hi, 8192):
                chunk = data[start:min(hi, start + 8192)]
                raw = np.empty(len(chunk), RAW_DTYPE)
                for name in RAW_DTYPE.names:
                    raw[name] = chunk[name]
                raw_output.write(raw.tobytes())
                write_raw_rows(writer, raw, period if period is not None else '',raw24_chunk(source,raw))
                time.sleep(0)  # Yield between bounded CSV chunks to receiver/UI.
        with paths[3].open('x', newline='', encoding='utf-8') as event_output:
            event_writer = csv.writer(event_output)
            event_writer.writerow(('session_id', 'slot', 'tim2_read_us', 'time_s', 'vshunt_code',
                                   'current_ma', 'local_median_ma', 'deviation_ma', 'rule', 'neighbors_json'))
            for event in events:
                event_writer.writerow((event['session_id'], event['slot'], event['tim2_read_us'],
                    event['time_s'], event['vshunt_code'], event['current_ma'],
                    event['local_median_ma'], event['deviation_ma'], event['rule'],
                    json.dumps(event['neighbors'], separators=(',',':'))))
        fields = normalize_legacy(source.metadata)
        fields.pop('_detector_rules', None)
        acquisition_finished = bool(fields.get('acquisition_finished', False))
        available_complete = source.full_available and (source.expected_count is None or len(data) == source.expected_count)
        full = selection == 'FULL_SESSION' and lo == 0 and hi == len(data) and available_complete
        pre_final = {key: fields.pop(key) for key in ('state', 'recording') if key in fields}
        record = dict(fields, gui_version=__version__, raw24_csv=True,export_type=selection,
                      export_source=source.source_kind, statistics_source='RAW',
                      source_history_complete=available_complete,
                      interval_data_complete=bool(stats['complete']),
                      pre_final_snapshot=pre_final,
                      exported_full_available_session=full,
                      acquisition_finished=acquisition_finished,
                      recording_finished=True, recording_saved=True,
                      session_end_verified=False, is_csf4_session=False,
                      source_session_end_verified=bool(fields.get('session_end_verified', False)),
                      recorder_error=None,
                      cursor_x1_s=cursor_x1_s, cursor_x2_s=cursor_x2_s,
                      interval_start_s=left, interval_end_s=right,
                      sample_count=hi-lo, first_slot=int(data['slot'][lo]),
                      last_slot=int(data['slot'][hi-1]),
                      first_time_s=float(data['time_s'][lo]),
                      last_time_s=float(data['time_s'][hi-1]),
                      min_ma=stats['min'], max_ma=stats['max'],
                      avg_ma=stats['mean'], rms_ma=stats['rms'],
                      slot_gaps=stats['slot_gaps'],
                      slot_gaps_complete=stats['slot_gaps_complete'],
                      raw_statistics=dict(stats),
                      raw_without_candidates=filtered,
                      candidate_count=len(events) if detail_complete else None,
                      candidate_events_saved=len(events),
                      detector_parameters=analyzer.rules.as_dict(),
                      detector_analysis_complete=detail_complete,
                      detector_error=analyzer.error,
                      mcu_anomaly_report=fields.get('mcu_anomaly_report',
                                                    fields.get('diagnostics',{}).get('stream anomaly')),
                      mcu_anomaly_entries=fields.get('mcu_anomaly_entries',
                          parse_mcu_anomaly_report(fields.get('mcu_anomaly_report',
                                                    fields.get('diagnostics',{}).get('stream anomaly')))),
                      raw_dtype=RAW_DTYPE.descr, raw_itemsize=RAW_DTYPE.itemsize,
                      files={'csv': paths[0].name, 'raw': paths[1].name,
                             'events': paths[3].name})
        if not available_complete and selection == 'FULL_SESSION':
            record['export_type'] = 'FULL_AVAILABLE_PARTIAL'
        temporary = paths[2].with_name(paths[2].name + '.tmp')
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        temporary.replace(paths[2])
        return {'path': str(paths[2]), 'samples': hi-lo, 'interval': (left, right),
                'full': full, 'source': source, 'selection': selection,
                'snapshot_count': len(data), 'partial': not available_complete or not bool(stats['complete']),
                'elapsed_ms': (time.perf_counter()-started)*1000}
    except Exception:
        # These names were uniquely generated by this invocation and only
        # this invocation may remove them after a failed export.
        for path in paths + [paths[2].with_name(paths[2].name + '.tmp')]:
            path.unlink(missing_ok=True)
        raise


def export_archive(source,directory,selection='FULL_SESSION',interval=None,
                   cursor_x1_s=None,cursor_x2_s=None):
    """Stream explicit archive exports in bounded decoded blocks; no full RAM copy."""
    started=time.perf_counter()
    archive=source.data
    left,right=(archive.first_time_s,archive.last_time_s) if interval is None else sorted(map(float,interval))
    stats=archive.range_stats(left,right)
    if not stats['count']:
        raise ValueError('NO_RAW_SAMPLES_IN_INTERVAL')
    base=Path(directory)/('capture_export_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_')+uuid.uuid4().hex[:8])
    base.parent.mkdir(parents=True,exist_ok=True)
    paths=[base.with_suffix(ext) for ext in ('.csv','.raw.bin','.json','.events.csv')]
    count=0;first_slot=last_slot=None
    first_time=last_time=None
    period=source.metadata.get('profile',{}).get('sample_period_us','')
    try:
        with paths[0].open('x',newline='',encoding='utf-8') as csv_output,paths[1].open('xb') as raw_output:
            writer=csv.writer(csv_output)
            writer.writerow(('slot','tim2_read_us','vshunt_code','sample_period_us','time_s','raw24'))
            for chunk in archive.iter_range(left,right):
                raw_output.write(chunk.tobytes())
                write_raw_rows(writer,chunk,period,raw24_chunk(source,chunk))
                if first_slot is None:
                    first_slot=int(chunk['slot'][0]);first_time=float(chunk['time_s'][0])
                last_slot=int(chunk['slot'][-1]);last_time=float(chunk['time_s'][-1])
                count+=len(chunk)
                time.sleep(0)
        if count!=stats['count']:
            raise ValueError('archive index/export sample count mismatch')
        analyzer=source.analyzer
        events=[]
        if analyzer is not None:
            events=analyzer.events[bisect_left(analyzer.event_times,left):
                                   bisect_right(analyzer.event_times,right)]
        filtered=(archive.filtered_range_stats(left,right,analyzer,stats)
                  if analyzer is not None else None)
        detail_complete=bool(analyzer is not None and analyzer.complete and analyzer.finished and
                             len(analyzer.index.mask)>=archive.sample_count)
        with paths[3].open('x',newline='',encoding='utf-8') as event_output:
            event_writer=csv.writer(event_output)
            event_writer.writerow(('session_id','slot','tim2_read_us','time_s',
                'vshunt_code','current_ma','local_median_ma','deviation_ma','rule','neighbors_json'))
            for event in events:
                event_writer.writerow((event['session_id'],event['slot'],event['tim2_read_us'],
                    event['time_s'],event['vshunt_code'],event['current_ma'],
                    event['local_median_ma'],event['deviation_ma'],event['rule'],
                    json.dumps(event['neighbors'],separators=(',',':'))))
        metadata=normalize_legacy(source.metadata)
        full=(selection=='FULL_SESSION' and count==archive.sample_count and archive.manifest.get('complete'))
        pre_final={key:metadata.pop(key) for key in ('state','recording') if key in metadata}
        record=dict(metadata,gui_version=__version__,raw24_csv=True,export_type=selection if full or selection!='FULL_SESSION' else 'FULL_AVAILABLE_PARTIAL',
            export_source='ARCHIVE',statistics_source='RAW',source_history_complete=bool(archive.manifest.get('complete')),
            interval_data_complete=bool(stats['complete']),pre_final_snapshot=pre_final,
            exported_full_available_session=bool(full),acquisition_finished=bool(archive.manifest.get('acquisition_finished')),
            recording_finished=True,recording_saved=True,session_end_verified=False,is_csf4_session=False,
            source_session_end_verified=bool(archive.manifest.get('session_end_verified')),
            recorder_error=None,cursor_x1_s=cursor_x1_s,cursor_x2_s=cursor_x2_s,
            interval_start_s=left,interval_end_s=right,sample_count=count,
            first_slot=first_slot,last_slot=last_slot,first_time_s=first_time,last_time_s=last_time,
            min_ma=stats['min'],max_ma=stats['max'],avg_ma=stats['mean'],rms_ma=stats['rms'],
            slot_gaps=stats['slot_gaps'],slot_gaps_complete=stats['slot_gaps_complete'],
            raw_statistics=stats,raw_without_candidates=filtered,
            candidate_count=len(events) if detail_complete else None,
            candidate_events_saved=len(events),
            detector_analysis_complete=detail_complete,
            detector_error=(analyzer.error or (None if detail_complete else 'ARCHIVE_CANDIDATES_INCOMPLETE')
                            if analyzer is not None else 'ARCHIVE_CANDIDATES_NOT_ANALYZED'),
            detector_parameters=analyzer.rules.as_dict() if analyzer is not None else metadata.get('detector_parameters'),
            mcu_anomaly_report=archive.manifest.get('mcu_anomaly_report'),
            files={'csv':paths[0].name,'raw':paths[1].name,'events':paths[3].name},
            raw_dtype=RAW_DTYPE.descr,raw_itemsize=RAW_DTYPE.itemsize)
        temp=paths[2].with_name(paths[2].name+'.tmp')
        temp.write_text(json.dumps(record,indent=2,ensure_ascii=False,default=str),encoding='utf-8')
        temp.replace(paths[2])
        return dict(path=str(paths[2]),samples=count,interval=(left,right),full=bool(full),
                    source=source,selection=selection,snapshot_count=count,
                    partial=not full if selection=='FULL_SESSION' else not stats['complete'],
                    elapsed_ms=(time.perf_counter()-started)*1000)
    except Exception:
        for path in paths+[paths[2].with_name(paths[2].name+'.tmp')]:
            path.unlink(missing_ok=True)
        raise


class ExportWorker(threading.Thread):
    def __init__(self, events):
        super().__init__(name='CurrentSensor export', daemon=True)
        self.jobs = queue.Queue(16)
        self.events = events
        self.stopping = threading.Event()
        self.start()

    def submit(self, source, directory, selection='FULL_SESSION', interval=None,
               cursor_x1_s=None, cursor_x2_s=None):
        try:
            self.jobs.put_nowait((source, directory, selection, interval, cursor_x1_s, cursor_x2_s))
            return True
        except queue.Full:
            return False

    def run(self):
        while not self.stopping.is_set() or not self.jobs.empty():
            try:
                args = self.jobs.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                result = export_raw(*args)
                self.events.put(('exported', result))
            except Exception as error:
                self.events.put(('export_error', str(error)))

    def finish(self):
        self.stopping.set()
