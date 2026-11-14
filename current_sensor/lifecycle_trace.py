"""Persistent lifecycle trace and independent Python stack watchdog."""

import faulthandler
import os
import queue
import threading
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class LifecycleTrace:
    def __init__(self, directory=None, threshold_s=2.0):
        root = Path(directory) if directory is not None else Path.cwd() / 'logs'
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            root = Path(tempfile.gettempdir()) / 'CurrentSensor' / 'logs'
            root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.path = root / f'current_sensor_lifecycle_{stamp}_{os.getpid()}.log'
        try:
            self.file = self.path.open('a', encoding='utf-8', buffering=1)
        except OSError:
            root = Path(tempfile.gettempdir()) / 'CurrentSensor' / 'logs'
            root.mkdir(parents=True, exist_ok=True)
            self.path = root / self.path.name
            self.file = self.path.open('a', encoding='utf-8', buffering=1)
        self.lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.pending = queue.SimpleQueue()
        self.active = {}
        self.threshold_s = threshold_s
        self.ui_last = None
        self.ui_dumped = False
        self.stop_event = threading.Event()
        self.writer = threading.Thread(target=self._write, name='CurrentSensor trace writer', daemon=True)
        self.thread = threading.Thread(target=self._watch, name='CurrentSensor watchdog', daemon=True)
        self.writer.start()
        self.thread.start()

    def _line(self, stage, details=''):
        return (f'{time.monotonic():.6f} pid={os.getpid()} '
                f'tid={threading.get_ident()} stage={stage} {details}\n')

    def event(self, stage, details=''):
        self.pending.put(self._line(stage, details))

    def begin(self, owner, stage, details=''):
        with self.lock:
            self.active[owner] = (time.monotonic(), stage, threading.get_ident(), details, False)
        self.event(stage, details)

    def end(self, owner, stage, details=''):
        with self.lock:
            self.active.pop(owner, None)
        self.event(stage, details)

    def _write(self):
        while not self.stop_event.is_set() or not self.pending.empty():
            try:
                line = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            with self.file_lock:
                self.file.write(line)
                self.file.flush()

    def heartbeat_ui(self):
        self.ui_last = time.monotonic()
        self.ui_dumped = False

    def disable_ui_watch(self):
        self.ui_last = None

    def _watch(self):
        while not self.stop_event.wait(0.25):
            now = time.monotonic()
            expired = []
            with self.lock:
                for owner, (started, stage, thread_id, details, dumped) in list(self.active.items()):
                    if dumped or now - started < self.threshold_s:
                        continue
                    self.active[owner] = (started, stage, thread_id, details, True)
                    expired.append((owner, started, stage, thread_id, details))
            for owner, started, stage, thread_id, details in expired:
                with self.file_lock:
                    self.file.write(self._line('WATCHDOG_STACKS',
                                               f'owner={owner} blocked_stage={stage} '
                                               f'blocked_tid={thread_id} elapsed_s={now-started:.3f} '
                                               f'{details}'))
                    self.file.flush()
                    faulthandler.dump_traceback(file=self.file, all_threads=True)
                    self.file.flush()
            ui_last = self.ui_last
            if (ui_last is not None and not self.ui_dumped and
                    now - ui_last >= self.threshold_s):
                self.ui_dumped = True
                with self.file_lock:
                    self.file.write(self._line('UI_UNRESPONSIVE',
                                               f'heartbeat_age_s={now-ui_last:.3f}'))
                    faulthandler.dump_traceback(file=self.file, all_threads=True)
                    self.file.flush()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1.0)
        self.writer.join(timeout=1.0)
        with self.file_lock:
            self.file.flush()
            self.file.close()


_trace = None
_trace_lock = threading.Lock()


def get_lifecycle_trace():
    global _trace
    with _trace_lock:
        if _trace is None:
            _trace = LifecycleTrace()
        return _trace


def close_lifecycle_trace():
    global _trace
    with _trace_lock:
        trace, _trace = _trace, None
    if trace is not None:
        trace.close()
