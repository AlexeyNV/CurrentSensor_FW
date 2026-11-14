"""Segmented, frame-preserving CSF4 archive and sparse indexed history."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
import copy
import json
import queue
import shutil
import struct
import threading
import time
import uuid
import zlib
import os
import psutil
import numpy as np
from tools.stream_capture import FrameDecoder, HEADER
from .data import RAW_DTYPE, DisplayTrace
from . import __version__


BLOCK_MAGIC = b'CFB1'
BLOCK_HEADER = struct.Struct('<4sIIIII')  # magic, raw length, stored length, raw CRC, stored CRC, frame count
INDEX_DTYPE = np.dtype([
    ('segment','<u4'),('offset','<u8'),('stored_bytes','<u4'),('raw_bytes','<u4'),
    ('first_time_us','<u8'),('last_time_us','<u8'),('min_time_us','<u8'),('max_time_us','<u8'),
    ('first_slot','<u4'),('last_slot','<u4'),('sample_count','<u4'),('frame_count','<u4'),
    ('first_code','<i4'),('last_code','<i4'),('min_code','<i4'),('max_code','<i4'),
    ('sum_code','<i8'),('sum_square','<i8'),('slot_gaps','<u4'),('entry_crc','<u4')])


@dataclass(frozen=True)
class ArchiveOptions:
    segment_mib: int = 100
    segment_seconds: int = 0
    reserve_mib: int = 1024
    compression: str = 'none'

    def validate(self):
        if self.segment_mib not in (25,100,250,500,1024):
            raise ValueError('segment size must be 25/100/250/500/1024 MiB')
        if not 0 <= self.segment_seconds <= 86400:
            raise ValueError('segment time limit outside range')
        if not 0 <= self.reserve_mib <= 1024*1024:
            raise ValueError('free-space reserve outside range')
        if self.compression not in ('none','zlib1','zlib3'):
            raise ValueError('unsupported compression')


def _atomic_json(path, payload):
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as out:
            json.dump(payload,out,indent=2,ensure_ascii=False,default=str)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt==4:
                    raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


def _write_all(stream,payload):
    remaining=memoryview(payload)
    while remaining:
        written=stream.write(remaining)
        if not written:
            raise OSError('ARCHIVE_SHORT_WRITE')
        remaining=remaining[written:]


def _entry_bytes(record):
    entry=np.zeros(1,INDEX_DTYPE)
    for key,value in record.items():
        entry[key][0]=value
    entry['entry_crc'][0]=zlib.crc32(entry.tobytes()[:-4]) & 0xffffffff
    return entry.tobytes()


def _index_valid(path, count=None):
    needed=None if count is None else count*INDEX_DTYPE.itemsize
    if not path.exists() or (needed is None and path.stat().st_size % INDEX_DTYPE.itemsize) or (needed is not None and path.stat().st_size<needed):
        return False
    with path.open('rb') as source:
        remaining=needed
        while remaining is None or remaining>0:
            chunk=source.read(min(INDEX_DTYPE.itemsize*8192,remaining) if remaining is not None else INDEX_DTYPE.itemsize*8192)
            if not chunk:
                break
            if remaining is not None:
                remaining-=len(chunk)
            if len(chunk)%INDEX_DTYPE.itemsize:
                return False
            for start in range(0,len(chunk),INDEX_DTYPE.itemsize):
                record=chunk[start:start+INDEX_DTYPE.itemsize]
                if zlib.crc32(record[:-4]) & 0xffffffff != int.from_bytes(record[-4:],'little'):
                    return False
    return True


def _block_samples(frames):
    codes=[]; slots=[]; stamps=[]
    for frame in frames:
        for slot,stamp,code in frame.get('samples',()):
            slots.append(slot); stamps.append(stamp); codes.append(code)
    return np.asarray(slots,np.uint32),np.asarray(stamps,np.uint32),np.asarray(codes,np.int32)


def _record(segment, offset, stored_bytes, raw_bytes, frames, times_us):
    slots,stamps,codes=_block_samples(frames)
    n=len(codes)
    if n:
        times=np.asarray(times_us,np.uint64)
        minimum=int(codes.argmin()); maximum=int(codes.argmax())
        values=codes.astype(np.int64)
        gaps=int(((int(slots[-1])-int(slots[0])) & 0xffffffff)-(n-1))
        return dict(segment=segment,offset=offset,stored_bytes=stored_bytes,raw_bytes=raw_bytes,
            first_time_us=int(times[0]),last_time_us=int(times[-1]),
            min_time_us=int(times[minimum]),max_time_us=int(times[maximum]),
            first_slot=int(slots[0]),last_slot=int(slots[-1]),sample_count=n,
            frame_count=len(frames),first_code=int(codes[0]),last_code=int(codes[-1]),
            min_code=int(codes[minimum]),max_code=int(codes[maximum]),
            sum_code=sum(map(int,values)),sum_square=sum(int(x)**2 for x in values),
            slot_gaps=gaps)
    return dict(segment=segment,offset=offset,stored_bytes=stored_bytes,raw_bytes=raw_bytes,
        first_time_us=0,last_time_us=0,min_time_us=0,max_time_us=0,
        first_slot=0,last_slot=0,sample_count=0,frame_count=len(frames),
        first_code=0,last_code=0,min_code=0,max_code=0,sum_code=0,sum_square=0,slot_gaps=0)


def _source_has_losses(document):
    transport=document.get('transport') or {}
    health=document.get('health') or {}
    counters=document.get('device_counters') or {}
    return (bool(document.get('session_errors')) or
            bool(document.get('archive_error')) or
            bool(document.get('recorder_dropped_samples')) or
            bool(document.get('archive_slot_gaps')) or bool(document.get('archive_initial_slot_gaps')) or
            ('received_samples' in document and 'sample_count' in document and
             int(document['received_samples'])!=int(document['sample_count'])) or
            bool((document.get('source_metadata') or {}).get('recording_start',{}).get('samples',0)) or
            bool(health.get('transport_errors') or health.get('acquisition_losses')) or
            any(int(transport.get(key,0) or 0)>0 for key in
                ('crc_errors','format_errors','frame_gaps','slot_gaps','resync_bytes','code_errors')) or
            any(int(counters.get(key,0) or 0)>0 for key in
                ('LOST_SAMPLE_SLOTS','ACQ_BUFFER_OVERFLOWS','FRAMES_DROPPED','TX_BUFFER_OVERFLOWS')))


class ArchiveRecorder(threading.Thread):
    """One asynchronous writer; only the serial owner submits immutable decoded frames."""
    def __init__(self, directory, metadata, options=None, capacity=256, detector=None):
        super().__init__(name='CSF4 archive recorder',daemon=True)
        self.options=options or ArchiveOptions()
        self.options.validate()
        self.segment_limit_bytes=self.options.segment_mib*1024**2
        self.block_samples=1000
        self.root=Path(directory)/('archive_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_')+uuid.uuid4().hex[:8])
        self.manifest_path=self.root/'session.json'
        self.index_path=self.root/'samples.idx'
        self.metadata=copy.deepcopy(metadata)
        self.detector=detector
        self.candidate_events_file=None
        self.candidate_events_saved=0
        self.candidate_events_total=None
        self.detector_analysis_complete=False
        self.candidate_error=None
        self.queue=queue.Queue(capacity)
        self.stopping=threading.Event()
        self.done=threading.Event()
        self.error=None
        self.dropped_samples=0
        self.received_samples=0
        self.samples=0
        self.frames=0
        self.last_slot=None
        self.archive_slot_gaps=0
        self.archive_initial_slot_gaps=0
        self.end_seen=False
        self.bytes_written=0
        self.index_entries=0
        self.final={}
        self.started_monotonic=time.monotonic()
        self.started_utc=datetime.now(timezone.utc).isoformat()
        self.finalization_started_monotonic=None
        self.finished_monotonic=None
        self.finished_utc=None
        self.finalized_utc=None
        self.first_sample_time_us=None
        self.last_sample_time_us=None
        self.accepted_first_time_us=None
        self.accepted_last_time_us=None
        self.frozen_acquisition_elapsed_us=None
        self.owner_pid=os.getpid()
        self.owner_started_at=psutil.Process().create_time()
        self.segments=[]
        self.status='RECORDING'
        self.current_segment_bytes=0
        self.free_bytes=None
        self.lock=threading.RLock()
        self.start()

    @property
    def metadata_path(self):
        return self.manifest_path

    def submit(self, data, frames):
        count=len(data)
        if self.stopping.is_set() or self.error:
            self.received_samples+=count
            self.dropped_samples+=count
            return False
        try:
            self.queue.put_nowait((data,tuple(frames)))
            self.received_samples+=count
            if count:
                with self.lock:
                    if self.accepted_first_time_us is None:
                        self.accepted_first_time_us=int(round(float(data['time_s'][0])*1e6))
                    self.accepted_last_time_us=int(round(float(data['time_s'][-1])*1e6))
            return True
        except queue.Full:
            self.received_samples+=count
            self.dropped_samples+=count
            self.error='ARCHIVE_QUEUE_OVERFLOW'
            self.stopping.set()
            return False

    def finish(self, metadata=None):
        with self.lock:
            self.final.update(metadata or {})
            if self.finished_utc is None:
                self.finished_utc=datetime.now(timezone.utc).isoformat()
                self.frozen_acquisition_elapsed_us=self._acquisition_elapsed_us()
        self.stopping.set()

    def _acquisition_elapsed_us(self):
        if self.frozen_acquisition_elapsed_us is not None:
            return self.frozen_acquisition_elapsed_us
        first=(self.accepted_first_time_us if self.accepted_first_time_us is not None
               else self.first_sample_time_us)
        last=(self.accepted_last_time_us if self.accepted_last_time_us is not None
              else self.last_sample_time_us)
        return max(0,last-first) if first is not None and last is not None else 0

    def checkpoint(self):
        """Queue a durable index/manifest boundary without blocking the receiver."""
        if self.stopping.is_set():
            return self.done
        completed=threading.Event()
        try:
            self.queue.put_nowait(('checkpoint',completed))
        except queue.Full:
            return None
        return completed

    def snapshot(self):
        with self.lock:
            now=self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
            wall_elapsed=max(0.,now-self.started_monotonic)
            acquisition_us=self._acquisition_elapsed_us()
            finalization_s=(max(0.,now-self.finalization_started_monotonic)
                            if self.finalization_started_monotonic is not None else None)
            return dict(status=self.status,root=str(self.root),samples=self.samples,frames=self.frames,
                        last_slot=self.last_slot,
                        bytes=self.bytes_written,segment_number=len(self.segments),
                        segment_bytes=self.current_segment_bytes,queue=self.queue.qsize(),
                        dropped_samples=self.dropped_samples,error=self.error,
                        initial_slot_gaps=self.archive_initial_slot_gaps,
                        slot_gaps=self.archive_slot_gaps,
                        acquisition_elapsed_us=acquisition_us,
                        acquisition_elapsed_s=acquisition_us/1e6,
                        recording_wall_elapsed_s=wall_elapsed,
                        finalization_duration_s=finalization_s,
                        elapsed_s=wall_elapsed,
                        rate_bytes_s=self.bytes_written/max(.001,wall_elapsed),
                        free_bytes=self.free_bytes)

    def _manifest(self):
        with self.lock:
            final=copy.deepcopy(self.final)
            status=self.status
            document=dict(schema_version=1,format='CurrentSensor-CSF4-segmented',
                gui_version=__version__,
                source_session=self.metadata.get('source_session'),
                firmware_version=self.metadata.get('firmware_version'),
                session_profile=self.metadata.get('session_profile',self.metadata.get('profile')),
                sample_rate_hz=self.metadata.get('profile',{}).get('sample_rate_hz'),
                sample_period_us=self.metadata.get('profile',{}).get('sample_period_us'),
                acq_mode=self.metadata.get('acq_mode'),calibration=self.metadata.get('calibration'),
                started_utc=self.started_utc,finished_utc=self.finished_utc,
                finalized_utc=self.finalized_utc,
                first_sample_time_us=self.first_sample_time_us,
                last_sample_time_us=self.last_sample_time_us,
                accepted_first_time_us=self.accepted_first_time_us,
                accepted_last_time_us=self.accepted_last_time_us,
                acquisition_elapsed_us=self._acquisition_elapsed_us(),
                recording_wall_elapsed_s=(max(0.,self.finished_monotonic-self.started_monotonic)
                    if self.finished_monotonic is not None else None),
                finalization_duration_s=(max(0.,self.finished_monotonic-self.finalization_started_monotonic)
                    if self.finished_monotonic is not None and self.finalization_started_monotonic is not None else None),
                options=self.options.__dict__,
                owner_pid=self.owner_pid,owner_started_at=self.owner_started_at,
                owner_thread_id=threading.get_native_id(),
                segments=copy.deepcopy(self.segments),index_file=self.index_path.name,
                sample_count=self.samples,frame_count=self.frames,
                archive_slot_gaps=self.archive_slot_gaps,
                archive_initial_slot_gaps=self.archive_initial_slot_gaps,
                received_samples=self.received_samples,recorder_dropped_samples=self.dropped_samples,
                bytes_written=self.bytes_written,status=status,archive_error=self.error,
                index_entries=self.index_entries,
                candidate_events_file=self.candidate_events_file,
                candidate_events_saved=self.candidate_events_saved,
                candidate_events_total=self.candidate_events_total,
                detector_analysis_complete=self.detector_analysis_complete,
                candidate_error=self.candidate_error,
                detector_parameters=(self.detector.analyzer.rules.as_dict()
                                     if self.detector is not None else None),
                acquisition_finished=bool(final.get('acquisition_finished',False)),
                session_end_verified=bool(final.get('session_end_verified',False)),
                archive_end_frame_seen=self.end_seen,
                complete=(status=='COMPLETE'),partial=(status!='COMPLETE'),
                recovered=False,transport=final.get('transport'),health=final.get('health'),
                device_counters=final.get('device_counters'),session_errors=final.get('session_errors',[]),
                diagnostics=final.get('diagnostics'),mcu_anomaly_report=final.get('mcu_anomaly_report'),
                source_metadata=self.metadata)
        _atomic_json(self.manifest_path,document)

    def _new_segment(self):
        number=len(self.segments)
        suffix='.csf4' if self.options.compression=='none' else '.csfz'
        name=f'segment_{number:05d}{suffix}'
        segment=dict(number=number,file=name,compression=self.options.compression,
                     size_bytes=0,samples=0,frames=0,first_slot=None,last_slot=None,
                     first_time_us=None,last_time_us=None,checksum_crc32=None,closed=False)
        output=(self.root/name).open('xb',buffering=0)
        self.segments.append(segment)
        self.current_segment_bytes=0
        self._manifest()
        return output

    def run(self):
        output=None
        index=None
        block_packets=[]; block_frames=[]; block_times=[]; block_size=0
        last_manifest=time.monotonic()
        try:
            self.root.mkdir(parents=True,exist_ok=False)
            index=self.index_path.open('xb',buffering=0)
            self._manifest()
            output=self._new_segment()
            segment_crc=0

            def close_segment():
                nonlocal output,segment_crc
                if output is None:
                    return
                output.flush(); os.fsync(output.fileno()); output.close(); output=None
                segment=self.segments[-1]
                segment['checksum_crc32']=segment_crc & 0xffffffff
                segment['closed']=True
                self._manifest()

            def flush_block():
                nonlocal output,segment_crc,block_packets,block_frames,block_times,block_size
                if not block_packets:
                    return
                raw=b''.join(block_packets)
                if self.options.compression=='none':
                    stored=raw
                else:
                    level=1 if self.options.compression=='zlib1' else 3
                    compressed=zlib.compress(raw,level)
                    stored=BLOCK_HEADER.pack(BLOCK_MAGIC,len(raw),len(compressed),
                        zlib.crc32(raw)&0xffffffff,zlib.crc32(compressed)&0xffffffff,
                        len(block_frames))+compressed
                segment=self.segments[-1]
                due_size=segment['size_bytes'] and segment['size_bytes']+len(stored)>self.segment_limit_bytes
                due_time=(self.options.segment_seconds and segment['first_time_us'] is not None and
                          block_times and block_times[0]-segment['first_time_us']>=self.options.segment_seconds*1000000)
                if due_size or due_time:
                    close_segment()
                    output=self._new_segment()
                    segment_crc=0
                    segment=self.segments[-1]
                self.free_bytes=shutil.disk_usage(self.root).free
                if self.free_bytes < self.options.reserve_mib*1024**2+len(stored):
                    raise OSError('ARCHIVE_DISK_RESERVE')
                offset=segment['size_bytes']
                _write_all(output,stored)
                output.flush()
                segment_crc=zlib.crc32(stored,segment_crc)
                record=_record(segment['number'],offset,len(stored),len(raw),block_frames,block_times)
                _write_all(index,_entry_bytes(record))
                if record['sample_count']:
                    if self.first_sample_time_us is None:
                        self.first_sample_time_us=record['first_time_us']
                    self.last_sample_time_us=record['last_time_us']
                    self.archive_slot_gaps+=record['slot_gaps']
                    if self.last_slot is None:
                        if not self.metadata.get('recording_start',{}).get('samples',0):
                            self.archive_initial_slot_gaps=record['first_slot']
                    else:
                        delta=(record['first_slot']-self.last_slot)&0xffffffff
                        if delta!=1:
                            self.archive_slot_gaps+=delta-1 if delta<0x80000000 else 1
                    self.last_slot=record['last_slot']
                self.index_entries+=1
                self.bytes_written+=len(stored)
                self.frames+=len(block_frames)
                self.samples+=record['sample_count']
                segment['size_bytes']+=len(stored)
                segment['frames']+=len(block_frames)
                segment['samples']+=record['sample_count']
                if record['sample_count']:
                    if segment['first_slot'] is None:
                        segment['first_slot']=record['first_slot']
                        segment['first_time_us']=record['first_time_us']
                    segment['last_slot']=record['last_slot']
                    segment['last_time_us']=record['last_time_us']
                self.current_segment_bytes=segment['size_bytes']
                block_packets=[]; block_frames=[]; block_times=[]; block_size=0

            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    data,frames=self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                if isinstance(data,str) and data=='checkpoint':
                    flush_block()
                    output.flush(); index.flush()
                    self._manifest()
                    frames.set()
                    continue
                cursor=0
                for frame in frames:
                    if self.end_seen:
                        raise ValueError('ARCHIVE_FRAME_AFTER_END')
                    if frame['type']==3:
                        self.end_seen=True
                    count=len(frame.get('samples',()))
                    part=data[cursor:cursor+count]
                    cursor+=count
                    block_packets.append(frame['packet'])
                    block_frames.append(frame)
                    if count:
                        block_times.extend(np.rint(part['time_s']*1e6).astype(np.uint64).tolist())
                    block_size+=len(frame['packet'])
                    if len(block_times)>=self.block_samples or block_size>=65536:
                        flush_block()
                if cursor!=len(data):
                    raise ValueError('archive frame/sample count mismatch')
                if time.monotonic()-last_manifest>=1:
                    flush_block()
                    self._manifest()
                    last_manifest=time.monotonic()
            with self.lock:
                self.status='FINALIZING'
                self.finalization_started_monotonic=time.monotonic()
            flush_block()
            close_segment()
            if self.detector is not None:
                if self.detector.stopping.is_set() and self.detector.done.wait(10):
                    analyzer=self.detector.analyzer
                    base=int(self.metadata.get('recording_start',{}).get('samples',0))
                    events=[dict(event,index=event['index']-base) for event in analyzer.events
                            if base<=event['index']<base+self.samples]
                    self.candidate_events_total=len(events) if analyzer.complete else None
                    self.detector_analysis_complete=(analyzer.complete and analyzer.error is None and
                        len(analyzer.index.mask)==self.received_samples)
                    self.candidate_error=analyzer.error
                    events_path=self.root/'candidate_events.jsonl'
                    temp=events_path.with_name(events_path.name+'.'+uuid.uuid4().hex+'.tmp')
                    try:
                        with temp.open('x',encoding='utf-8') as output_events:
                            for event in events:
                                output_events.write(json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n')
                        temp.replace(events_path)
                        self.candidate_events_file=events_path.name
                        self.candidate_events_saved=len(events)
                    finally:
                        temp.unlink(missing_ok=True)
                else:
                    self.candidate_error=('DETECTOR_FINALIZE_TIMEOUT' if self.detector.stopping.is_set()
                                          else 'DETECTOR_STILL_RUNNING')
            with self.lock:
                verified=bool(self.final.get('session_end_verified'))
                received=self.received_samples==self.samples
                source_complete=not _source_has_losses(dict(self.final,
                    source_metadata=self.metadata,recorder_dropped_samples=self.dropped_samples))
                terminal_status=('COMPLETE' if verified and self.end_seen and self.samples>0 and received and
                                 source_complete and not self.archive_slot_gaps and
                                 not self.archive_initial_slot_gaps and not self.error else 'PARTIAL')
        except Exception as error:
            self.error=f'{type(error).__name__}: {error}'
            terminal_status='DISK_ERROR' if isinstance(error,OSError) else 'PARTIAL'
            if output is not None:
                output.close()
        finally:
            if index is not None:
                index.close()
            with self.lock:
                self.finished_monotonic=time.monotonic()
                now_utc=datetime.now(timezone.utc).isoformat()
                if self.finished_utc is None:
                    self.finished_utc=now_utc
                    self.frozen_acquisition_elapsed_us=self._acquisition_elapsed_us()
                self.finalized_utc=now_utc
                self.status=terminal_status
            try:
                self._manifest()
            except OSError:
                pass
            self.done.set()


class TimeUnwrapper:
    def __init__(self):
        self.previous=None
        self.elapsed=0

    def times(self,frames):
        stamps=[stamp for frame in frames for _,stamp,_ in frame.get('samples',())]
        result=[]
        for stamp in stamps:
            if self.previous is not None:
                self.elapsed+=(stamp-self.previous)&0xffffffff
            self.previous=stamp
            result.append(self.elapsed)
        return result


def _scan_raw_segment(path):
    with path.open('rb') as source:
        while True:
            offset=source.tell()
            head=source.read(HEADER.size)
            if not head:
                return
            if len(head)<HEADER.size or head[:4]!=b'CSF4':
                raise ValueError(f'TRUNCATED_OR_INVALID_FRAME at {path.name}:{offset}')
            length=HEADER.unpack(head)[4]
            if not HEADER.size+4<=length<=228:
                raise ValueError(f'INVALID_FRAME_LENGTH at {path.name}:{offset}')
            tail=source.read(length-HEADER.size)
            if len(tail)!=length-HEADER.size:
                raise ValueError(f'TRUNCATED_FRAME at {path.name}:{offset}')
            packet=head+tail
            if zlib.crc32(packet[:-4])&0xffffffff!=int.from_bytes(packet[-4:],'little'):
                raise ValueError(f'FRAME_CRC_ERROR at {path.name}:{offset}')
            yield offset,packet


def verify_archive_segments(root):
    """Read-only full-file CRC audit; keep it outside the acquisition path."""
    root=Path(root)
    if root.is_file():
        root=root.parent
    manifest=json.loads((root/'session.json').read_text(encoding='utf-8'))
    errors=[]; checked=0; bytes_checked=0
    for segment in manifest.get('segments',()):
        location=root/segment['file']
        if not location.is_file():
            errors.append(f'{location.name}: MISSING_SEGMENT')
            continue
        actual=location.stat().st_size
        expected=int(segment.get('size_bytes',-1))
        if actual!=expected:
            errors.append(f'{location.name}: SIZE_MISMATCH {actual}/{expected}')
            continue
        checksum=segment.get('checksum_crc32')
        if not segment.get('closed') or checksum is None:
            errors.append(f'{location.name}: SEGMENT_NOT_CLOSED')
            continue
        crc=0
        with location.open('rb') as source:
            while True:
                chunk=source.read(1024*1024)
                if not chunk:
                    break
                crc=zlib.crc32(chunk,crc)
                bytes_checked+=len(chunk)
        if crc&0xffffffff!=int(checksum):
            errors.append(f'{location.name}: SEGMENT_CHECKSUM_MISMATCH')
        else:
            checked+=1
    crc_ok=not errors
    session_complete=(manifest.get('status')=='COMPLETE' and
                      bool(manifest.get('archive_end_frame_seen')))
    return dict(ok=crc_ok and session_complete,crc_ok=crc_ok,
                session_complete=session_complete,
                status=manifest.get('status'),checked_segments=checked,
                segment_count=len(manifest.get('segments',())),
                bytes_checked=bytes_checked,errors=errors)


def rebuild_archive_index(root):
    """Rebuild derived index from validated original frames; originals stay untouched."""
    root=Path(root)
    path=root/'session.json'
    manifest=json.loads(path.read_text(encoding='utf-8'))
    known={item['file'] for item in manifest.get('segments',())}
    for location in sorted(root.glob('segment_*.csf*')):
        if location.name not in known:
            manifest['segments'].append(dict(file=location.name,
                compression='none' if location.suffix=='.csf4' else manifest.get('options',{}).get('compression','zlib1')))
    manifest['segments'].sort(key=lambda item:item['file'])
    decoder=FrameDecoder()
    clock=TimeUnwrapper()
    records=[]; errors=[]; recovered_segments=[]; seen_end=False; time_uncertain=False
    temp=root/('samples.idx.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as out:
            for number,segment in enumerate(manifest['segments']):
                location=root/segment['file']
                if not location.is_file():
                    errors.append(f'{location.name}: MISSING_SEGMENT')
                    clock.previous=None
                    time_uncertain=True
                    recovered_segments.append(dict(segment,number=number,size_bytes=0,samples=0,frames=0,
                        first_slot=None,last_slot=None,first_time_us=None,last_time_us=None,
                        checksum_crc32=None,closed=False))
                    continue
                if clock.previous is None and segment.get('first_time_us') is not None:
                    clock.elapsed=int(segment['first_time_us'])
                good=0; crc=0; sample_count=0; frame_count=0
                first_slot=last_slot=first_time=last_time=None
                packets=[]; frames=[]; times=[]; size=0; start=0
                def emit(offset,raw,stored,block_frames,block_times):
                    nonlocal sample_count,frame_count,first_slot,last_slot,first_time,last_time
                    record=_record(number,offset,stored,raw,block_frames,block_times)
                    _write_all(out,_entry_bytes(record)); records.append(record)
                    sample_count+=record['sample_count']; frame_count+=record['frame_count']
                    if record['sample_count']:
                        if first_slot is None:
                            first_slot=record['first_slot']; first_time=record['first_time_us']
                        last_slot=record['last_slot']; last_time=record['last_time_us']
                prior_errors=len(errors)
                try:
                    if segment['compression']=='none':
                        for offset,packet in _scan_raw_segment(location):
                            if seen_end:
                                raise ValueError('FRAME_AFTER_END')
                            decoded=decoder.feed(packet)
                            if len(decoded)!=1 or decoder.crc_errors or decoder.format_errors:
                                raise ValueError('FRAME_DECODE_ERROR')
                            frame=decoded[0]
                            seen_end=frame['type']==3
                            if not packets:
                                start=offset
                            packets.append(packet); frames.append(frame)
                            times.extend(clock.times((frame,)))
                            size+=len(packet)
                            good=offset+len(packet)
                            crc=zlib.crc32(packet,crc)
                            if len(times)>=1000 or size>=65536:
                                emit(start,size,size,frames,times)
                                packets=[];frames=[];times=[];size=0
                        if packets:
                            emit(start,size,size,frames,times)
                    else:
                        with location.open('rb') as source:
                            while True:
                                offset=source.tell()
                                header=source.read(BLOCK_HEADER.size)
                                if not header:
                                    break
                                if len(header)!=BLOCK_HEADER.size:
                                    raise ValueError('TRUNCATED_BLOCK_HEADER')
                                magic,raw_len,comp_len,raw_crc,comp_crc,frame_total=BLOCK_HEADER.unpack(header)
                                if magic!=BLOCK_MAGIC or raw_len>1024*1024 or comp_len>1024*1024:
                                    raise ValueError('INVALID_BLOCK_HEADER')
                                compressed=source.read(comp_len)
                                if len(compressed)!=comp_len:
                                    raise ValueError('TRUNCATED_BLOCK')
                                if zlib.crc32(compressed)&0xffffffff!=comp_crc:
                                    raise ValueError('BLOCK_CRC_ERROR')
                                raw=zlib.decompress(compressed)
                                if len(raw)!=raw_len or zlib.crc32(raw)&0xffffffff!=raw_crc:
                                    raise ValueError('RAW_BLOCK_CRC_ERROR')
                                block_decoder=FrameDecoder()
                                block_frames=block_decoder.feed(raw)
                                if (len(block_frames)!=frame_total or block_decoder.buffer or
                                        block_decoder.crc_errors or block_decoder.format_errors):
                                    raise ValueError('FRAME_CRC_ERROR_IN_BLOCK')
                                if seen_end:
                                    raise ValueError('BLOCK_AFTER_END')
                                seen_end=any(frame['type']==3 for frame in block_frames)
                                block_times=clock.times(block_frames)
                                emit(offset,raw_len,BLOCK_HEADER.size+comp_len,block_frames,block_times)
                                stored=header+compressed
                                good=source.tell(); crc=zlib.crc32(stored,crc)
                except (ValueError,zlib.error) as error:
                    if segment['compression']=='none' and packets:
                        emit(start,size,size,frames,times)
                    errors.append(f'{location.name}: {error}')
                    clock.previous=None
                    time_uncertain=True
                if location.stat().st_size!=good:
                    errors.append(f'{location.name}: trailing {location.stat().st_size-good} invalid bytes')
                expected_crc=segment.get('checksum_crc32')
                if (expected_crc is not None and good==location.stat().st_size and
                        (crc&0xffffffff)!=int(expected_crc)):
                    errors.append(f'{location.name}: SEGMENT_CHECKSUM_MISMATCH')
                recovered_segments.append(dict(segment,number=number,size_bytes=good,
                    samples=sample_count,frames=frame_count,first_slot=first_slot,last_slot=last_slot,
                    first_time_us=first_time,last_time_us=last_time,
                    checksum_crc32=crc&0xffffffff,
                    closed=good==location.stat().st_size and len(errors)==prior_errors))
        temp.replace(root/'samples.idx')
        source_partial=_source_has_losses(manifest)
        has_samples=any(record['sample_count'] for record in records)
        recovered_gaps=0
        previous_slot=None
        for record in records:
            if not record['sample_count']:
                continue
            recovered_gaps+=record['slot_gaps']
            if previous_slot is None:
                if not (manifest.get('source_metadata') or {}).get('recording_start',{}).get('samples',0):
                    recovered_gaps+=record['first_slot']
            else:
                delta=(record['first_slot']-previous_slot)&0xffffffff
                if delta!=1:
                    recovered_gaps+=delta-1 if delta<0x80000000 else 1
            previous_slot=record['last_slot']
        sampled=[record for record in records if record['sample_count']]
        first_time=sampled[0]['first_time_us'] if sampled else None
        last_time=sampled[-1]['last_time_us'] if sampled else None
        manifest.update(segments=recovered_segments,sample_count=sum(s['samples'] for s in recovered_segments),
            frame_count=sum(s['frames'] for s in recovered_segments),index_entries=len(records),recovered=True,
            recovered_utc=datetime.now(timezone.utc).isoformat(),
            first_sample_time_us=first_time,last_sample_time_us=last_time,
            acquisition_elapsed_us=max(0,last_time-first_time) if sampled else 0,
            recovery_errors=errors,recovered_slot_gaps=recovered_gaps,
            time_continuity_uncertain=time_uncertain,
            partial=bool(errors) or source_partial or recovered_gaps>0 or not seen_end or
                    not has_samples or not manifest.get('session_end_verified'),
            complete=not errors and not source_partial and recovered_gaps==0 and seen_end and
                     has_samples and bool(manifest.get('session_end_verified')),
            archive_end_frame_seen=seen_end)
        manifest['status']=('DISK_ERROR' if manifest.get('archive_error') else
                            'PARTIAL' if manifest['partial'] else 'COMPLETE')
        _atomic_json(path,manifest)
        return manifest
    finally:
        temp.unlink(missing_ok=True)


class ArchiveReader:
    def __init__(self, filename):
        path=Path(filename)
        self.root=path if path.is_dir() else path.parent
        self.manifest_path=self.root/'session.json'
        self.manifest=json.loads(self.manifest_path.read_text(encoding='utf-8'))
        self.index_path=self.root/'samples.idx'
        active=False
        if self.manifest.get('owner_pid'):
            try:
                owner=psutil.Process(self.manifest['owner_pid'])
                active=owner.create_time()==self.manifest.get('owner_started_at')
                thread_id=self.manifest.get('owner_thread_id')
                if active and thread_id is not None:
                    try:
                        active=any(item.id==int(thread_id) for item in owner.threads())
                    except psutil.AccessDenied:
                        pass
            except (psutil.NoSuchProcess,psutil.AccessDenied):
                pass
        expected=self.manifest.get('index_entries',-1)
        valid=_index_valid(self.index_path,expected if active and expected>=0 else None)
        actual=self.index_path.stat().st_size//INDEX_DTYPE.itemsize if valid else 0
        sizes_ok=all((self.root/segment['file']).is_file() and
            ((self.root/segment['file']).stat().st_size>=segment['size_bytes'] if active else
             (self.root/segment['file']).stat().st_size==segment['size_bytes'])
            for segment in self.manifest['segments'])
        if active and self.manifest.get('status')=='RECORDING':
            if not valid or actual<expected or not sizes_ok:
                raise RuntimeError('active archive has no consistent committed index')
            count=expected
        else:
            if (not valid or actual!=expected or not sizes_ok or
                    (self.manifest.get('status')=='COMPLETE' and not self.manifest.get('archive_end_frame_seen')) or
                    self.manifest.get('status') in ('RECORDING','FINALIZING')):
                self.manifest=rebuild_archive_index(self.root)
            count=self.index_path.stat().st_size//INDEX_DTYPE.itemsize
        self.entries=np.memmap(self.index_path,INDEX_DTYPE,mode='r',shape=(count,)) if count else np.empty(0,INDEX_DTYPE)
        nonempty=np.flatnonzero(self.entries['sample_count'])
        if len(nonempty) and nonempty[-1]-nonempty[0]+1==len(nonempty):
            self.data_entries=self.entries[nonempty[0]:nonempty[-1]+1]
        else:
            self.data_entries=self.entries[nonempty]
        self.block_ends=np.cumsum(self.data_entries['sample_count'],dtype=np.uint64)
        self.first_time_s=float(self.data_entries['first_time_us'][0])/1e6 if len(self.data_entries) else 0
        self.last_time_s=float(self.data_entries['last_time_us'][-1])/1e6 if len(self.data_entries) else 0
        self.sample_count=int(self.block_ends[-1]) if len(self.block_ends) else 0

    def snapshot(self):
        """Stable archive summary, including v0.1.3 manifests without duration fields."""
        first=int(self.data_entries['first_time_us'][0]) if self.sample_count else None
        last=int(self.data_entries['last_time_us'][-1]) if self.sample_count else None
        segments=self.manifest['segments']
        wall=self.manifest.get('recording_wall_elapsed_s')
        indexed_elapsed_us=max(0,last-first) if first is not None else 0
        elapsed_us=(int(self.manifest['acquisition_elapsed_us'])
                    if self.manifest.get('acquisition_elapsed_us') is not None and
                    self.manifest['status'] not in ('RECORDING','FINALIZING')
                    else indexed_elapsed_us)
        bytes_total=sum(int(item.get('size_bytes',0)) for item in segments)
        return dict(status=self.manifest['status'],root=str(self.root),samples=self.sample_count,
                    last_slot=(int(self.data_entries['last_slot'][-1]) if self.sample_count else None),
                    frames=int(self.manifest.get('frame_count',0)),
                    bytes=bytes_total,
                    segment_number=len(segments),
                    segment_bytes=int(segments[-1].get('size_bytes',0)) if segments else 0,
                    queue=0,dropped_samples=int(self.manifest.get('recorder_dropped_samples',0)),
                    error=self.manifest.get('archive_error'),
                    initial_slot_gaps=int(self.manifest.get('archive_initial_slot_gaps',0)),
                    slot_gaps=int(self.manifest.get('archive_slot_gaps',0)),
                    acquisition_elapsed_us=elapsed_us,acquisition_elapsed_s=elapsed_us/1e6,
                    recording_wall_elapsed_s=wall,
                    finalization_duration_s=self.manifest.get('finalization_duration_s'),
                    elapsed_s=wall,rate_bytes_s=(bytes_total/wall if wall and wall>0 else None),
                    free_bytes=None)

    def close(self):
        if isinstance(self.entries,np.memmap) and self.entries._mmap is not None:
            self.entries._mmap.close()

    def __del__(self):
        try:
            self.close()
        except (AttributeError,OSError,ValueError):
            pass

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()

    def load_analyzer(self):
        """Load the bounded candidate catalogue; RAW remains on disk."""
        from .anomaly_diagnostics import CandidateAnalyzer, DetectorRules
        params=self.manifest.get('detector_parameters') or {}
        rules=DetectorRules(float(params.get('median_ma',50)),
                            float(params.get('absolute_ma',10000)),
                            float(params.get('step_ma',1000)),params.get('method')=='local_median5_mad8_v1')
        analyzer=CandidateAnalyzer(self.manifest.get('source_session') or 'archive',rules,sparse=True)
        analyzer.index.count=self.sample_count
        name=self.manifest.get('candidate_events_file')
        if name:
            try:
                with (self.root/name).open('r',encoding='utf-8') as source:
                    for line in source:
                        event=json.loads(line)
                        analyzer.events.append(event)
                        analyzer.event_positions.append(int(event['index']))
                        analyzer.event_times.append(float(event['time_s']))
            except (OSError,ValueError) as error:
                analyzer.events.clear();analyzer.event_positions.clear();analyzer.event_times.clear()
                analyzer.error='ARCHIVE_CANDIDATE_CATALOG_UNAVAILABLE: '+str(error)
        analyzer.event_count=int(self.manifest.get('candidate_events_total') or len(analyzer.events))
        analyzer.finished=True
        analyzer.complete=(bool(self.manifest.get('detector_analysis_complete')) and
                           analyzer.event_count==len(analyzer.events) and analyzer.error is None)
        analyzer.error=analyzer.error or self.manifest.get('candidate_error')
        return analyzer

    def _block(self, entry):
        segment=self.manifest['segments'][int(entry['segment'])]
        with (self.root/segment['file']).open('rb') as source:
            source.seek(int(entry['offset']))
            stored=source.read(int(entry['stored_bytes']))
        if len(stored)!=int(entry['stored_bytes']):
            raise ValueError('ARCHIVE_BLOCK_SHORT_READ')
        if segment['compression']=='none':
            raw=stored
        else:
            magic,raw_len,comp_len,raw_crc,comp_crc,frame_count=BLOCK_HEADER.unpack_from(stored)
            compressed=stored[BLOCK_HEADER.size:]
            if magic!=BLOCK_MAGIC or len(compressed)!=comp_len or zlib.crc32(compressed)&0xffffffff!=comp_crc:
                raise ValueError('ARCHIVE_BLOCK_CRC_ERROR')
            raw=zlib.decompress(compressed)
            if len(raw)!=raw_len or zlib.crc32(raw)&0xffffffff!=raw_crc:
                raise ValueError('ARCHIVE_RAW_BLOCK_CRC_ERROR')
        decoder=FrameDecoder()
        frames=decoder.feed(raw)
        if decoder.buffer or decoder.crc_errors or decoder.format_errors or len(frames)!=int(entry['frame_count']):
            raise ValueError('ARCHIVE_FRAME_CRC_ERROR')
        slots,stamps,codes=_block_samples(frames)
        if len(codes)!=int(entry['sample_count']):
            raise ValueError('ARCHIVE_INDEX_SAMPLE_MISMATCH')
        data=np.empty(len(codes),RAW_DTYPE)
        data['slot']=slots;data['tim2_read_us']=stamps;data['vshunt_code']=codes
        if len(data):
            data['time_s']=(int(entry['first_time_us'])+
                            ((stamps.astype(np.uint64)-np.uint64(stamps[0]))&np.uint64(0xffffffff)))/1e6
        return data,frames

    def _bounds(self,left,right):
        entries=self.data_entries
        lo=bisect_left(entries['last_time_us'],int(min(left,right)*1e6))
        hi=bisect_right(entries['first_time_us'],int(max(left,right)*1e6))
        return lo,min(len(entries),hi)

    def locate_time(self,stamp_s):
        """Return the containing indexed block (or next block) without decoding RAW."""
        position=bisect_left(self.data_entries['last_time_us'],int(float(stamp_s)*1e6))
        if position>=len(self.data_entries):
            return None
        entry=self.data_entries[position]
        segment=self.manifest['segments'][int(entry['segment'])]
        return dict(file=str(self.root/segment['file']),offset=int(entry['offset']),
                    first_time_s=int(entry['first_time_us'])/1e6,
                    last_time_s=int(entry['last_time_us'])/1e6)

    def locate_slot(self,slot):
        """Index lookup for sessions shorter than the 32-bit slot rollover."""
        position=bisect_left(self.data_entries['last_slot'],int(slot))
        if position>=len(self.data_entries):
            return None
        entry=self.data_entries[position]
        segment=self.manifest['segments'][int(entry['segment'])]
        return dict(file=str(self.root/segment['file']),offset=int(entry['offset']),
                    first_slot=int(entry['first_slot']),last_slot=int(entry['last_slot']))

    def raw_range(self,left,right):
        chunks=list(self.iter_range(left,right))
        return np.concatenate(chunks) if chunks else np.empty(0,RAW_DTYPE)

    def iter_range(self,left,right):
        lo,hi=self._bounds(left,right)
        for entry in self.data_entries[lo:hi]:
            raw,_=self._block(entry)
            mask=(raw['time_s']>=min(left,right))&(raw['time_s']<=max(left,right))
            if mask.any():
                yield raw[mask]

    def range_stats(self,left,right):
        left,right=sorted((float(left),float(right)))
        lo,hi=self._bounds(left,right)
        count=total=square=0;minimum=maximum=None
        first_slot=last_slot=None
        first_time=last_time=None
        entries=self.data_entries
        first_full=lo+(lo<hi and int(entries[lo]['first_time_us'])/1e6<left)
        last_full=hi-(lo<hi and int(entries[hi-1]['last_time_us'])/1e6>right)
        if first_full<last_full:
            middle=entries[first_full:last_full]
            count=int(middle['sample_count'].astype(np.uint64).sum())
            total=sum(map(int,middle['sum_code']))
            square=sum(map(int,middle['sum_square']))
            minimum=int(middle['min_code'].min());maximum=int(middle['max_code'].max())
            first_slot=int(middle['first_slot'][0]);last_slot=int(middle['last_slot'][-1])
            first_time=int(middle['first_time_us'][0])/1e6
            last_time=int(middle['last_time_us'][-1])/1e6
        for i in sorted(set([lo,hi-1])) if lo<hi else []:
            if first_full<=i<last_full:
                continue
            raw,_=self._block(entries[i])
            raw=raw[(raw['time_s']>=left)&(raw['time_s']<=right)]
            if not len(raw):
                continue
            codes=raw['vshunt_code'].astype(np.int64)
            count+=len(raw);total+=sum(map(int,codes));square+=sum(int(x)**2 for x in codes)
            part_min=int(codes.min());part_max=int(codes.max())
            minimum=part_min if minimum is None else min(minimum,part_min)
            maximum=part_max if maximum is None else max(maximum,part_max)
            if i==lo:
                first_slot=int(raw['slot'][0])
                first_time=float(raw['time_s'][0])
            if i==hi-1:
                last_slot=int(raw['slot'][-1])
                last_time=float(raw['time_s'][-1])
        gaps=((last_slot-first_slot)&0xffffffff)-(count-1) if count else 0
        gap_complete=all(not self._boundary_inside_gap(boundary) for boundary in (left,right))
        return dict(count=count,min=minimum/16 if count else None,max=maximum/16 if count else None,
            mean=total/count/16 if count else None,rms=sqrt(square/count)/16 if count else None,
            sum_code=total,sum_square_code=square,first_slot=first_slot,last_slot=last_slot,
            first_time_s=first_time,last_time_s=last_time,
            slot_gaps=gaps,slot_gaps_complete=gap_complete,dt=right-left,
            interval_start_s=left,interval_end_s=right,
            available_start_s=self.first_time_s if self.sample_count else None,
            available_end_s=self.last_time_s if self.sample_count else None,
            complete=bool(self.sample_count) and left>=self.first_time_s and right<=self.last_time_s,
            statistics_source='RAW')

    def _boundary_inside_gap(self,boundary):
        entries=self.data_entries
        if not len(entries):
            return False
        position=bisect_left(entries['last_time_us'],int(boundary*1e6))
        nearby=entries[max(0,position-1):min(len(entries),position+2)]
        internal_gap=bool(np.any(nearby['slot_gaps']))
        between_gap=any(((int(nearby[i+1]['first_slot'])-int(nearby[i]['last_slot']))&0xffffffff)!=1
                        for i in range(len(nearby)-1))
        if not internal_gap and not between_gap:
            return False
        before=after=None
        for i in range(max(0,position-1),min(len(entries),position+2)):
            raw,_=self._block(entries[i])
            where=bisect_left(raw['time_s'],boundary)
            if where>0:
                sample=raw[where-1]
                if before is None or sample['time_s']>before['time_s']:
                    before=sample
            if where<len(raw):
                sample=raw[where]
                if after is None or sample['time_s']<after['time_s']:
                    after=sample
        return (before is not None and after is not None and
                before['time_s']<boundary<after['time_s'] and
                ((int(after['slot'])-int(before['slot']))&0xffffffff)!=1)

    def filtered_range_stats(self,left,right,analyzer,raw_stats=None):
        """Exact RAW minus the complete sparse candidate catalogue."""
        left,right=sorted((float(left),float(right)))
        raw_stats=raw_stats or self.range_stats(left,right)
        if (not analyzer.complete or not analyzer.finished or
                len(analyzer.index.mask)<self.sample_count):
            return dict(complete=False,valid_count=None,excluded_count=None,
                        min=None,max=None,mean=None,rms=None,
                        analyzed_count=0,statistics_source='RAW_EXCLUDING_CANDIDATES',
                        detector_rules=analyzer.rules.as_dict())
        first=bisect_left(analyzer.event_times,left)
        last=bisect_right(analyzer.event_times,right)
        events=analyzer.events[first:last]
        excluded=len(events)
        count=raw_stats['count']-excluded
        if count<0:
            raise ValueError('ARCHIVE_CANDIDATE_COUNT_EXCEEDS_RAW')
        total=raw_stats['sum_code']-sum(int(event['vshunt_code']) for event in events)
        square=raw_stats['sum_square_code']-sum(int(event['vshunt_code'])**2 for event in events)
        lo,hi=self._bounds(left,right)
        entries=self.data_entries[lo:hi]
        affected={0,len(entries)-1} if len(entries) else set()
        positions_by_block={}
        for event in events:
            position=bisect_right(self.block_ends,int(event['index']))
            if lo<=position<hi:
                local=position-lo
                affected.add(local)
                begin=int(self.block_ends[position-1]) if position else 0
                positions_by_block.setdefault(local,[]).append((int(event['index'])-begin,event))
        minimum=maximum=None
        matched=0
        affected_positions=sorted(position for position in affected if 0<=position<len(entries))
        begin=0
        for boundary in (*affected_positions,len(entries)):
            if begin<boundary:
                untouched=entries[begin:boundary]
                part_min=int(untouched['min_code'].min())
                part_max=int(untouched['max_code'].max())
                minimum=part_min if minimum is None else min(minimum,part_min)
                maximum=part_max if maximum is None else max(maximum,part_max)
            begin=boundary+1
        for position in affected_positions:
            raw,_=self._block(entries[position])
            mask=(raw['time_s']>=left)&(raw['time_s']<=right)
            marked_positions=positions_by_block.get(position)
            if marked_positions:
                marked=np.zeros(len(raw),bool)
                for sample_position,event in marked_positions:
                    if (0<=sample_position<len(raw) and mask[sample_position] and
                            int(raw['slot'][sample_position])==int(event['slot']) and
                            int(raw['vshunt_code'][sample_position])==int(event['vshunt_code'])):
                        marked[sample_position]=True
                matched+=int(marked.sum())
                mask &= ~marked
            codes=raw['vshunt_code'][mask]
            if len(codes):
                part_min=int(codes.min());part_max=int(codes.max())
                minimum=part_min if minimum is None else min(minimum,part_min)
                maximum=part_max if maximum is None else max(maximum,part_max)
        if matched!=excluded or bool(count)!=(minimum is not None):
            raise ValueError('ARCHIVE_CANDIDATE_CATALOG_MISMATCH')
        return dict(complete=True,valid_count=count,excluded_count=excluded,
                    min=minimum/16 if count else None,max=maximum/16 if count else None,
                    mean=total/count/16 if count else None,
                    rms=sqrt(square/count)/16 if count else None,
                    analyzed_count=raw_stats['count'],
                    statistics_source='RAW_EXCLUDING_CANDIDATES',
                    detector_rules=analyzer.rules.as_dict())

    def plot_range(self,left,right,pixels=1000):
        lo,hi=self._bounds(left,right)
        entries=self.data_entries[lo:hi]
        if len(entries) and len(entries)<=pixels*2 and int(entries['sample_count'].astype(np.uint64).sum())<=pixels*2:
            raw=self.raw_range(left,right)
            links=np.ones(len(raw),bool)
            if len(raw)>1:
                links[:-1]=np.diff(raw['slot'].astype(np.uint32))==1
            return DisplayTrace(raw['time_s'],raw['vshunt_code'].astype(float)/16,
                                links,np.arange(len(raw)),False)
        if not len(entries):
            return DisplayTrace(np.empty(0),np.empty(0),np.empty(0,bool),np.empty(0,np.int64),True)
        step=max(1,(len(entries)+pixels-1)//pixels)
        times=[];codes=[];links=[]
        previous_slot=None
        for start in range(0,len(entries),step):
            group=entries[start:start+step]
            min_entry=group[int(group['min_code'].argmin())]
            max_entry=group[int(group['max_code'].argmax())]
            pair=sorted(((int(min_entry['min_time_us']),int(min_entry['min_code'])),
                         (int(max_entry['max_time_us']),int(max_entry['max_code']))))
            gap=(previous_slot is not None and
                 ((int(group[0]['first_slot'])-previous_slot)&0xffffffff)!=1)
            gap=gap or bool(np.any(group['slot_gaps']))
            if len(group)>1:
                gap=gap or bool(np.any(((group['first_slot'][1:].astype(np.uint64)-
                    group['last_slot'][:-1].astype(np.uint64))&0xffffffff)!=1))
            if links and gap:
                links[-1]=False
            for stamp,code in pair:
                times.append(stamp/1e6);codes.append(code/16);links.append(not gap)
            previous_slot=int(group[-1]['last_slot'])
        return DisplayTrace(np.asarray(times),np.asarray(codes),np.asarray(links,bool),
                            np.arange(len(times)),True)

    def iter_raw(self,chunk_blocks=128):
        for start in range(0,len(self.data_entries),chunk_blocks):
            chunks=[self._block(entry)[0] for entry in self.data_entries[start:start+chunk_blocks]]
            if chunks:
                yield np.concatenate(chunks)
