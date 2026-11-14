"""Automatic, bounded RAW-only temporary session storage."""
import json
import queue
import shutil
import tempfile
import threading
import time
import uuid
import copy
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import psutil
from .data import RAW_DTYPE
from .range_stats import RawRangeIndex


def spool_directory():
    return Path(tempfile.gettempdir()) / 'CurrentSensorGUI' / 'spool'


@dataclass(frozen=True)
class SpoolSnapshot:
    path: Path
    count: int
    index: RawRangeIndex
    metadata: dict
    finished: bool
    error: str | None

    def raw(self):
        if not self.path.is_file() or self.path.stat().st_size < self.count * RAW_DTYPE.itemsize:
            raise OSError('RAW spool unavailable or shorter than committed count')
        return np.memmap(self.path, RAW_DTYPE, mode='r', shape=(self.count,)) if self.count else np.empty(0, RAW_DTYPE)


class RawSpool(threading.Thread):
    def __init__(self, metadata, directory=None, max_bytes=2 * 1024**3,
                 free_reserve_bytes=256 * 1024**2, queue_capacity=128):
        super().__init__(name='CurrentSensor RAW spool', daemon=True)
        base = Path(directory) if directory else spool_directory()
        self.path = base / ('spool_' + uuid.uuid4().hex + '.raw.bin')
        self.manifest = self.path.with_suffix('.json')
        self.metadata = copy.deepcopy(metadata)
        self.owner_pid = psutil.Process().pid
        self.owner_started_at = psutil.Process().create_time()
        self.max_bytes = max_bytes
        self.free_reserve_bytes = free_reserve_bytes
        self.queue = queue.Queue(queue_capacity)
        self.lock = threading.RLock()
        self.index = RawRangeIndex()
        self.count = 0
        self.received_count = 0
        self.last_time_s = None
        self.saved_count = 0
        self.error = None
        self.stopping = threading.Event()
        self.done = threading.Event()
        self.finished = False
        self.last_manifest = 0.0
        self.write_times = deque(maxlen=2000)

    def submit(self, data):
        self.received_count += len(data)
        if self.error or self.stopping.is_set() or not len(data):
            return not len(data)
        try:
            # The incoming LIVE block is immutable. Conversion is small and
            # deterministic; actual disk I/O runs on this spool's thread.
            raw = np.empty(len(data), RAW_DTYPE)
            for name in RAW_DTYPE.names:
                raw[name] = data[name]
            self.queue.put_nowait(raw)
            return True
        except queue.Full:
            self.error = 'SPOOL_OVERFLOW: disk writer fell behind; session incomplete'
            self.stopping.set()
            return False

    def finish(self, metadata=None):
        with self.lock:
            self.metadata.update(metadata or {})
        self.stopping.set()

    def snapshot(self):
        with self.lock:
            return SpoolSnapshot(self.path, self.count, self.index,
                                 dict(self.metadata), self.finished, self.error)

    def _manifest(self):
        with self.lock:
            values = dict(self.metadata, spool_path=str(self.path),
                          owner_pid=self.owner_pid, owner_started_at=self.owner_started_at,
                          sample_count=self.count, spool_finished=self.finished,
                          spool_received_samples=self.received_count,
                          spool_missing_samples=max(0, self.received_count-self.count),
                          spool_error=self.error, saved_count=self.saved_count,
                          raw_dtype=RAW_DTYPE.descr, raw_itemsize=RAW_DTYPE.itemsize)
        temporary = self.manifest.with_name(self.manifest.name + '.tmp')
        temporary.write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.manifest)
        self.last_manifest = time.monotonic()

    def run(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._manifest()
            next_space_check = 0.0
            remaining_space = 0
            other_bytes = 0
            with self.path.open('xb', buffering=0) as output:
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        data = self.queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    started = time.perf_counter()
                    if time.monotonic() >= next_space_check:
                        remaining_space = shutil.disk_usage(self.path.parent).free - self.free_reserve_bytes
                        other_bytes = sum(path.stat().st_size for path in self.path.parent.glob('spool_*.raw.bin')
                                          if path != self.path)
                        next_space_check = time.monotonic() + 1
                    if other_bytes + (self.count + len(data)) * RAW_DTYPE.itemsize > self.max_bytes:
                        raise OSError('SPOOL_LIMIT: configured maximum exceeded')
                    if remaining_space < data.nbytes:
                        raise OSError('SPOOL_SPACE: free-space reserve reached')
                    blob = memoryview(data).cast('B')
                    while blob:
                        written = output.write(blob)
                        if not written:
                            raise OSError('SPOOL_WRITE: short disk write')
                        blob = blob[written:]
                    remaining_space -= data.nbytes
                    self.index.append(data['vshunt_code'])
                    with self.lock:
                        self.count += len(data)
                        self.last_time_s = float(data['time_s'][-1])
                    if time.monotonic() - self.last_manifest >= 1:
                        self._manifest()
                    self.write_times.append((time.perf_counter()-started)*1000)
        except (OSError, ValueError) as error:
            self.error = str(error)
            self.stopping.set()
        finally:
            with self.lock:
                self.finished = True
            try:
                self._manifest()
            except OSError as error:
                self.error = f'{self.error or ""}; MANIFEST_ERROR: {error}'
            self.done.set()

    def reader(self, count=None):
        return SpoolReader(self, self.count if count is None else count)

    def mark_saved(self, count):
        with self.lock:
            self.saved_count = max(self.saved_count, count)
        if self.done.is_set():
            self._manifest()

    def delete(self):
        if not self.done.is_set():
            raise RuntimeError('cannot delete active spool')
        base = self.path.parent.resolve()
        for path in (self.path, self.manifest):
            if path.resolve().parent != base or not path.name.startswith('spool_'):
                raise RuntimeError('unexpected spool path')
            path.unlink(missing_ok=True)

    @classmethod
    def adopt_recovered(cls, path, count, metadata, index):
        session = cls(metadata, directory=Path(path).parent)
        session.path = Path(path)
        session.manifest = session.path.with_suffix('.json')
        session.count = session.received_count = count
        session.index = index
        session.finished = True
        session.done.set()
        session.error = metadata.get('spool_error')
        session.metadata.update(recovered=True, source_history_complete=False,
                                acquisition_finished=False, session_end_verified=False)
        return session


def recoverable_spools(directory=None, include_live_owner=False):
    base = Path(directory) if directory else spool_directory()
    result = []
    if not base.exists():
        return result
    for manifest in base.glob('spool_*.json'):
        try:
            info = json.loads(manifest.read_text(encoding='utf-8'))
            if not include_live_owner and info.get('owner_pid'):
                try:
                    owner = psutil.Process(info['owner_pid'])
                    if owner.create_time() == info.get('owner_started_at'):
                        continue
                except psutil.AccessDenied:
                    continue
                except psutil.NoSuchProcess:
                    pass
            path = manifest.with_suffix('.bin')
            if not path.exists():
                path = Path(info['spool_path'])
            if path.resolve().parent != base.resolve() or not path.is_file():
                continue
            count = path.stat().st_size // RAW_DTYPE.itemsize
            if count > int(info.get('saved_count', 0)):
                result.append((path, count, info))
        except (OSError, ValueError, KeyError):
            continue
    return result


@dataclass(frozen=True)
class SpoolReader:
    """A request-time prefix; waiting for the disk queue is off the UI thread."""
    spool: RawSpool
    count: int

    def raw(self):
        deadline = time.monotonic() + 2
        while self.spool.count < self.count and not self.spool.done.is_set() and time.monotonic() < deadline:
            time.sleep(.002)
        snap = self.spool.snapshot()
        count = min(self.count, snap.count)
        return SpoolSnapshot(snap.path, count, snap.index, snap.metadata, snap.finished, snap.error).raw()
