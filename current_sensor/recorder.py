"""Nonblocking acquisition-to-disk queue; CSV is always unfiltered RAW."""
import csv
import hashlib
import json
import queue
import tempfile
import threading
import time
import uuid
import copy
import shutil
from . import __version__
from itertools import repeat
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from .data import RAW_DTYPE, LIVE_DTYPE, MedianBank
from .archive import TimeUnwrapper, _scan_raw_segment
from tools.stream_capture import FrameDecoder
from .session_metadata import normalize_legacy
from .mcu_anomaly import parse_mcu_anomaly_report


def write_raw_rows(writer, raw, period, raw24=None):
    """Bounded column conversion avoids a Python structured-row lookup/field."""
    rows=zip(raw['slot'].tolist(), raw['tim2_read_us'].tolist(),
                         raw['vshunt_code'].tolist(), repeat(period),
                         map('{:.6f}'.format, raw['time_s'].tolist()))
    writer.writerows(rows if raw24 is None else ((*row,f'0x{int(value):06X}' if int(value)<=0xFFFFFF else '') for row,value in zip(rows,raw24)))


class Recorder(threading.Thread):
    def __init__(self, directory, metadata, capacity=128, detector=None):
        super().__init__(name="CurrentSensor recorder", daemon=True)
        stem = datetime.now(timezone.utc).strftime("capture_%Y%m%dT%H%M%S_") + uuid.uuid4().hex[:8]
        self.base = Path(directory) / stem
        self.metadata = copy.deepcopy(metadata)
        self.queue = queue.Queue(capacity)
        self.stopping = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.dropped_samples = 0
        self.samples = 0
        self.final = {}
        self.first_slot = self.last_slot = None
        self.started_monotonic = time.monotonic()
        self.bytes_written=0;self.free_bytes=None
        self.detector=detector
        self.first_time=self.last_time=None

    @property
    def metadata_path(self):
        return self.base.with_suffix(".json")

    def submit(self, data, wire=b""):
        if self.stopping.is_set() or self.error:
            return False
        try:
            self.queue.put_nowait((data, wire))
            return True
        except queue.Full:
            self.dropped_samples += len(data)
            self.error = "RECORDER_OVERFLOW: disk cannot keep up; recording stopped"
            self.stopping.set()
            return False

    def finish(self, metadata=None):
        self.final.update(metadata or {})
        self.stopping.set()

    def _write_metadata(self, complete):
        report = normalize_legacy(dict(self.metadata, **self.final))
        self.integrity_status=('DISK_ERROR' if self.error else 'COMPLETE' if complete and
            report.get('session_end_verified') and not report.get('session_errors') else
            'PARTIAL' if self.stopping.is_set() else 'RECORDING')
        report.setdefault('mcu_anomaly_report', report.get('diagnostics',{}).get('stream anomaly'))
        report.setdefault('mcu_anomaly_entries', parse_mcu_anomaly_report(report.get('mcu_anomaly_report')))
        snapshot = {key: report.pop(key) for key in ('state', 'recording') if key in report}
        report.update(gui_version=__version__,recording_integrity=self.integrity_status, completed=complete, recording_closed=self.stopping.is_set(), recorder_error=self.error,
                      pre_final_snapshot=snapshot,
                      acquisition_finished=bool(report.get('acquisition_finished', False)),
                      recording_finished=self.stopping.is_set(), recording_saved=complete,
                      session_end_verified=bool(report.get('session_end_verified', False)),
                      recorded_samples=self.samples, recorder_dropped_samples=self.dropped_samples,
                      first_recorded_slot=self.first_slot, last_recorded_slot=self.last_slot,
                      raw_dtype=RAW_DTYPE.descr, raw_itemsize=RAW_DTYPE.itemsize,
                      files={"csv": self.base.with_suffix(".csv").name,
                             "raw": self.base.with_suffix(".raw.bin").name,
                             "wire": self.base.with_suffix(".csf4").name,
                             "suspects":self.base.with_suffix('.suspects.json').name if self.detector else None},
                      csv_is_raw=True, wire_may_be_session_fragment=True)
        temp = self.base.with_suffix(".json.tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.metadata_path)

    def run(self):
        try:
            self.base.parent.mkdir(parents=True, exist_ok=True)
            self._write_metadata(False)
            with self.base.with_suffix(".csv").open("x", newline="", encoding="utf-8") as csv_file, \
                    self.base.with_suffix(".raw.bin").open("xb") as raw_file, \
                    self.base.with_suffix(".csf4").open("xb") as wire_file:
                writer = csv.writer(csv_file)
                writer.writerow(("slot", "tim2_read_us", "vshunt_code", "sample_period_us", "time_s"))
                last_flush = time.monotonic()
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        data, wire = self.queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    raw = np.empty(len(data), RAW_DTYPE)
                    for field in RAW_DTYPE.names:
                        raw[field] = data[field]
                    if len(raw):
                        period = self.metadata["profile"]["sample_period_us"]
                        write_raw_rows(writer, raw, period)
                        raw_file.write(raw.tobytes())
                        self.first_slot = int(raw["slot"][0]) if self.first_slot is None else self.first_slot
                        self.last_slot = int(raw["slot"][-1])
                        self.first_time=float(raw['time_s'][0]) if self.first_time is None else self.first_time
                        self.last_time=float(raw['time_s'][-1])
                        self.samples += len(raw)
                    wire_file.write(wire)
                    if time.monotonic() - last_flush > .5:
                        csv_file.flush(); raw_file.flush(); wire_file.flush()
                        self.bytes_written=csv_file.tell()+raw_file.tell()+wire_file.tell()
                        try:self.free_bytes=shutil.disk_usage(self.base.parent).free
                        except OSError:self.free_bytes=None
                        last_flush = time.monotonic()
        except (OSError, ValueError) as error:
            self.error = f"DISK_ERROR: {error}"
            self.stopping.set()
        finally:
            try:
                if self.detector is not None:
                    if self.detector.stopping.is_set():self.detector.done.wait(2)
                    analyzer=self.detector.analyzer
                    with analyzer.lock:
                        sidecar=dict(schema='CurrentSensor.suspect.v1',classification='SUSPECT',
                            parameters=analyzer.rules.as_dict(),source_wire=self.base.with_suffix('.csf4').name,
                            source_session=self.metadata.get('source_session'),
                            complete=analyzer.complete and analyzer.finished,
                            events=[event for event in analyzer.events if self.first_time is not None and self.first_time<=event['time_s']<=self.last_time])
                    self.base.with_suffix('.suspects.json').write_text(json.dumps(sidecar,ensure_ascii=False,indent=2),encoding='utf8')
                self._write_metadata(self.error is None)
            except OSError as error:
                self.error = f"METADATA_WRITE_ERROR: {error}; previous error: {self.error}"
            self.done.set()


def open_history(filename):
    """Load/import in a background thread. Large histories remain memory-mapped.

    CSV imports and causal filter caches live in the OS temp directory; the
    user's original CSV and binary RAW files are never modified.
    """
    path = Path(filename)
    metadata = {}
    if path.suffix.lower()=='.csv' and (path.parent/'capture_summary.json').is_file():
        metadata=json.loads((path.parent/'capture_summary.json').read_text(encoding='utf8'))
    if path.suffix.lower() == ".json":
        metadata = normalize_legacy(json.loads(path.read_text(encoding="utf-8")))
        files = metadata.get("files", {})
        path = path.parent / files.get("raw", files.get("samples", files.get("csv", "")))
    if not path.is_file():
        raise ValueError("recording data file not found")
    identity = f"v060:{path.resolve()}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
    cache = Path(tempfile.gettempdir()) / "CurrentSensorGUI"
    cache.mkdir(exist_ok=True)
    target = cache / (hashlib.sha256(identity.encode()).hexdigest()[:24] + ".live.bin")
    if not target.exists():
        temporary = target.with_suffix(".tmp." + uuid.uuid4().hex)
        try:
            bank = MedianBank()
            with temporary.open("xb") as destination:
                if path.suffix.lower() == ".csv":
                    with path.open(newline="", encoding="utf-8-sig") as source:
                        reader = csv.DictReader(source)
                        rows, elapsed, previous = [], 0, None
                        for row in reader:
                            stamp = int(row.get('tim2_read_us',row.get('tim2_us')))
                            if previous is not None:
                                elapsed += (stamp - previous) & 0xFFFFFFFF
                            code=int(row['vshunt_code'])
                            if row.get('raw24'):
                                raw24=int(row['raw24'],0)
                                if raw24>0xFFFFFF or raw24<0 or (raw24>>4)-(0x100000 if raw24&0x800000 else 0)!=code:
                                    raise ValueError('CSV RAW24/CODE mismatch')
                            rows.append((int(row.get('slot',row.get('sequence'))), stamp, code,
                                         float(row.get('time_s',float(row.get('elapsed_us64',elapsed))/1e6))))
                            previous = stamp
                            if len(rows) == 65536:
                                _cache_chunk(np.array(rows, RAW_DTYPE), bank, destination)
                                rows.clear()
                        if rows:
                            _cache_chunk(np.array(rows, RAW_DTYPE), bank, destination)
                elif path.suffix.lower() == '.csf4':
                    decoder=FrameDecoder()
                    clock=TimeUnwrapper()
                    rows=[]
                    for _,packet in _scan_raw_segment(path):
                        if decoder.ended:
                            raise ValueError('CSF4_FRAME_AFTER_END')
                        frames=decoder.feed(packet)
                        if len(frames)!=1 or decoder.crc_errors or decoder.format_errors:
                            raise ValueError('CSF4_FRAME_INVALID')
                        frame=frames[0]
                        for (slot,stamp,code),elapsed in zip(frame.get('samples',()),clock.times((frame,))):
                            rows.append((slot,stamp,code,elapsed/1e6))
                        if len(rows)>=65536:
                            _cache_chunk(np.array(rows,RAW_DTYPE),bank,destination)
                            rows.clear()
                    if rows:
                        _cache_chunk(np.array(rows,RAW_DTYPE),bank,destination)
                else:
                    count = path.stat().st_size // RAW_DTYPE.itemsize
                    if count:
                        source = np.memmap(path, RAW_DTYPE, mode="r", shape=(count,))
                        for start in range(0, count, 65536):
                            _cache_chunk(source[start:start + 65536], bank, destination)
                        del source
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    count = target.stat().st_size // LIVE_DTYPE.itemsize
    data = np.memmap(target, LIVE_DTYPE, mode="r", shape=(count,)) if count else np.empty(0, LIVE_DTYPE)
    raw24_target=target.with_suffix('.raw24.bin')
    if not raw24_target.exists():
        wire_path=path if path.suffix.lower()=='.csf4' else path.parent/metadata.get('files',{}).get('wire','')
        csv_path=path if path.suffix.lower()=='.csv' else path.parent/metadata.get('files',{}).get('csv','')
        csv_has_raw=path.suffix.lower()=='.csv' or metadata.get('raw24_csv',False)
        if csv_has_raw or wire_path.is_file():
            raw_temp=raw24_target.with_name(raw24_target.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                written=0
                with raw_temp.open('wb') as raw_out:
                    if csv_has_raw:
                        with csv_path.open(newline='',encoding='utf-8-sig') as f:
                            batch=[]
                            for row in csv.DictReader(f):
                                batch.append(int(row['raw24'],0) if row.get('raw24') else 0xFFFFFFFF)
                                if len(batch)>=65536:
                                    np.array(batch,'<u4').tofile(raw_out);written+=len(batch);batch.clear()
                            np.array(batch,'<u4').tofile(raw_out);written+=len(batch)
                    else:
                        decoder=FrameDecoder()
                        for _,packet in _scan_raw_segment(wire_path):
                            for frame in decoder.feed(packet):
                                if frame['type']!=1:continue
                                samples=frame['samples']; raw_values=frame.get('raw24',[0xFFFFFFFF]*len(samples))
                                for slot,stamp,code in samples:
                                    if written>=count or (int(data[written]['slot']),int(data[written]['tim2_read_us']),int(data[written]['vshunt_code']))!=(slot,stamp,code):
                                        raise ValueError('RAW24 wire/cache association mismatch')
                                    written+=1
                                np.array(raw_values,'<u4').tofile(raw_out)
                if written!=count:raise ValueError('RAW24 cache count mismatch')
                raw_temp.replace(raw24_target)
            except (ValueError,OSError) as error:
                if path.suffix.lower() in ('.csv','.csf4'):raise
                metadata['raw24_unavailable']=str(error)
            finally:
                if raw_temp.exists():raw_temp.unlink()
    if raw24_target.exists():metadata['_raw24_cache']=str(raw24_target)
    metadata['_source_path']=str(Path(filename).resolve())
    return data, metadata


def _cache_chunk(raw, bank, destination):
    output = np.empty(len(raw), LIVE_DTYPE)
    for field in RAW_DTYPE.names:
        output[field] = raw[field]
    output["median3"], output["median5"] = bank.process(raw["vshunt_code"], raw["slot"])
    destination.write(output.tobytes())


def open_spool_history(reader):
    """Pin exactly a freeze-time prefix, never the later growing file suffix."""
    import shutil
    raw = reader.raw()
    cache = Path(tempfile.gettempdir()) / 'CurrentSensorGUI'
    cache.mkdir(exist_ok=True)
    identity = f'{reader.spool.path.resolve()}:prefix:{len(raw)}'
    target = cache / (hashlib.sha256(identity.encode()).hexdigest()[:24] + '.live.bin')
    if not target.exists():
        if shutil.disk_usage(cache).free < len(raw)*LIVE_DTYPE.itemsize + 256*1024**2:
            raise OSError('Недостаточно места для кеша Freeze; закреплённый live-фрагмент сохранён')
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            bank = MedianBank()
            with temporary.open('xb') as destination:
                for lo in range(0, len(raw), 65536):
                    _cache_chunk(raw[lo:lo+65536], bank, destination)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return np.memmap(target, LIVE_DTYPE, mode='r', shape=(len(raw),)) if len(raw) else np.empty(0,LIVE_DTYPE)
