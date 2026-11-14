"""Incremental RAW candidate detection. Events are hypotheses, never SPI verdicts."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from math import sqrt
import queue
import threading
import time
import numpy as np


@dataclass(frozen=True)
class DetectorRules:
    median_ma: float = 50.0
    absolute_ma: float = 10000.0
    step_ma: float = 1000.0
    robust: bool = False

    def as_dict(self):
        return dict(method='local_median5_mad8_v1' if self.robust else 'centered_median5',
                    algorithm_version=1,window_samples=5,mad_scale=1.4826,sigma_multiplier=8,
                    label='SUSPECT',median_ma=self.median_ma,
                    absolute_ma=self.absolute_ma, step_ma=self.step_ma,
                    current_lsb_ma=0.0625)


class CandidateIndex:
    """Block aggregates of unmarked codes; one byte of validity per RAW sample."""
    BLOCK = 4096

    def __init__(self):
        self.lock = threading.RLock()
        self.mask = bytearray()
        self.pending_codes = np.empty(0, np.int32)
        self.pending_valid = np.empty(0, np.uint8)
        self.minima, self.maxima = [], []
        self.counts, self.sums, self.squares = [0], [0], [0]

    def append(self, codes, valid):
        codes = np.asarray(codes, np.int32)
        valid = np.asarray(valid, np.uint8)
        if len(codes) != len(valid):
            raise ValueError('validity length differs from codes')
        with self.lock:
            self.mask.extend(valid.tobytes())
            all_codes = np.concatenate((self.pending_codes, codes))
            all_valid = np.concatenate((self.pending_valid, valid))
            complete = len(all_codes) // self.BLOCK
            for code_block, valid_block in zip(all_codes[:complete*self.BLOCK].reshape(complete, self.BLOCK),
                                               all_valid[:complete*self.BLOCK].reshape(complete, self.BLOCK)):
                selected = code_block[valid_block.astype(bool)].astype(np.int64)
                self.minima.append(int(selected.min()) if len(selected) else None)
                self.maxima.append(int(selected.max()) if len(selected) else None)
                self.counts.append(self.counts[-1] + len(selected))
                self.sums.append(self.sums[-1] + sum(map(int, selected)))
                self.squares.append(self.squares[-1] + sum(int(x)**2 for x in selected))
            self.pending_codes = all_codes[complete*self.BLOCK:].copy()
            self.pending_valid = all_valid[complete*self.BLOCK:].copy()

    def query_positions(self, data, lo, hi, base=0):
        global_lo, global_hi = base+lo, base+hi
        with self.lock:
            ready = len(self.mask)
            hi = min(hi, max(0, ready-base))
            if hi <= lo:
                return dict(valid_count=0, excluded_count=0, min=None, max=None,
                            mean=None, rms=None, analyzed_count=0)
            global_hi = base+hi
            first = min(global_hi, ((global_lo+self.BLOCK-1)//self.BLOCK)*self.BLOCK)
            last = max(first, (global_hi//self.BLOCK)*self.BLOCK)
            a, b = first//self.BLOCK, last//self.BLOCK
            count = self.counts[b]-self.counts[a]
            total = self.sums[b]-self.sums[a]
            square = self.squares[b]-self.squares[a]
            mins = [x for x in self.minima[a:b] if x is not None]
            maxs = [x for x in self.maxima[a:b] if x is not None]
            minimum, maximum = (min(mins) if mins else None), (max(maxs) if maxs else None)
            for begin, end in ((global_lo, first), (last, global_hi)):
                if begin == end:
                    continue
                valid = np.frombuffer(bytes(self.mask[begin:end]), np.uint8).astype(bool)
                codes = np.asarray(data['vshunt_code'][begin-base:end-base], np.int64)[valid]
                if len(codes):
                    count += len(codes)
                    total += sum(map(int, codes))
                    square += sum(int(x)**2 for x in codes)
                    minimum = int(codes.min()) if minimum is None else min(minimum, int(codes.min()))
                    maximum = int(codes.max()) if maximum is None else max(maximum, int(codes.max()))
        return dict(valid_count=count, excluded_count=max(0, hi-lo-count),
                    min=minimum/16 if minimum is not None else None,
                    max=maximum/16 if maximum is not None else None,
                    mean=total/count/16 if count else None,
                    rms=sqrt(square/count)/16 if count else None,
                    analyzed_count=max(0,hi-lo))


class CoverageIndex:
    """Long archives retain only candidate events, not one validity byte per sample."""
    def __init__(self,count=0):
        self.count=count
        self.mask=self

    def __len__(self):
        return self.count

    def append(self,codes,valid):
        self.count+=len(codes)


class CandidateAnalyzer:
    MAX_EVENTS = 100000

    def __init__(self, session_id, rules=None, sparse=False):
        self.session_id = session_id
        self.rules = rules or DetectorRules()
        self.index = CoverageIndex() if sparse else CandidateIndex()
        self.sparse=sparse
        self.events = []
        self.event_positions = []
        self.event_times = []
        self.event_count = 0
        self.total_received = 0
        self.carry = None
        self.finished = False
        self.complete = True
        self.error = None
        self.lock = threading.RLock()

    def sparsify(self):
        with self.lock:
            if not self.sparse:
                self.index=CoverageIndex(len(self.index.mask))
                self.sparse=True

    def feed(self, data):
        if not len(data):
            return
        with self.lock:
            carry = self.carry
            carry_n = len(carry) if carry is not None else 0
            joined = np.concatenate((carry, data)) if carry_n else data
            start = max(0, carry_n-2)
            end = max(start, len(joined)-2)
            self._process(joined, start, end, self.total_received-carry_n)
            self.total_received += len(data)
            self.carry = joined[-4:].copy()

    def finish(self):
        with self.lock:
            if self.finished:
                return
            carry = self.carry
            if carry is not None:
                start = max(0, len(carry)-2)
                # The first batch may contain fewer than five samples.
                if self.total_received <= 4:
                    start = max(0, len(carry)-min(2, self.total_received))
                self._process(carry, start, len(carry), self.total_received-len(carry))
            self.finished = True

    def _process(self, joined, start, end, origin):
        if end <= start:
            return
        codes = joined['vshunt_code'].astype(np.int64)
        slots = joined['slot']
        positions = np.arange(start, end)
        median = np.full(len(positions), np.nan)
        thresholds=np.full(len(positions),self.rules.median_ma*16,dtype=float)
        can_median = (positions >= 2) & (positions+2 < len(joined))
        if np.any(can_median):
            windows = np.lib.stride_tricks.sliding_window_view(codes, 5)
            candidates = positions[can_median]
            contiguous = np.diff(slots.astype(np.uint32)) == 1
            valid = (contiguous[candidates-2] & contiguous[candidates-1] &
                     contiguous[candidates] & contiguous[candidates+1])
            median[can_median] = np.where(valid, np.median(windows[candidates-2], axis=1), np.nan)
            if self.rules.robust:
                centered=windows[candidates-2]
                local_mad=np.median(np.abs(centered-median[can_median,None]),axis=1)
                thresholds[can_median]=np.maximum(self.rules.median_ma*16,8*1.4826*local_mad)
        median_hit = np.isfinite(median) & (np.abs(codes[positions]-median) > thresholds)
        absolute_hit = np.abs(codes[positions]) > self.rules.absolute_ma*16
        step_hit = np.zeros(len(positions), bool)
        previous = positions > 0
        if not self.rules.robust and self.rules.step_ma > 0 and np.any(previous):
            p = positions[previous]
            step_hit[previous] = ((slots[p]-slots[p-1]).astype(np.uint32) == 1) & (
                np.abs(codes[p]-codes[p-1]) > self.rules.step_ma*16)
        marked = median_hit | absolute_hit | step_hit
        self.index.append(joined['vshunt_code'][start:end], ~marked)
        self.event_count += int(marked.sum())
        if self.event_count > self.MAX_EVENTS:
            self.complete = False
        for local in np.flatnonzero(marked):
            if len(self.events) >= self.MAX_EVENTS:
                break
            p = positions[local]
            rules = [name for name, hit in (('MEDIAN5', median_hit[local]),
                   ('ABSOLUTE', absolute_hit[local]), ('STEP', step_hit[local])) if hit]
            neighbors = []
            for q in range(max(0,p-2),min(len(joined),p+3)):
                neighbors.append(dict(slot=int(slots[q]), tim2_read_us=int(joined['tim2_read_us'][q]),
                                      code=int(codes[q]), current_ma=int(codes[q])/16))
            event = dict(session_id=self.session_id, index=origin+int(p), slot=int(slots[p]),
                         classification='SUSPECT',algorithm=self.rules.as_dict()['method'],
                         threshold_ma=float(thresholds[local]/16),
                         tim2_read_us=int(joined['tim2_read_us'][p]), time_s=float(joined['time_s'][p]),
                         vshunt_code=int(codes[p]), current_ma=int(codes[p])/16,
                         local_median_ma=float(median[local]/16) if np.isfinite(median[local]) else None,
                         deviation_ma=float((codes[p]-median[local])/16) if np.isfinite(median[local]) else None,
                         rule='+'.join(rules), neighbors=neighbors)
            self.events.append(event)
            self.event_positions.append(event['index'])
            self.event_times.append(event['time_s'])

    def events_between(self, lo, hi):
        with self.lock:
            return list(self.events[bisect_left(self.event_positions,lo):bisect_left(self.event_positions,hi)])

    def range_statistics(self, data, x1, x2, base=0):
        left, right = sorted((float(x1),float(x2)))
        lo, hi = bisect_left(data['time_s'],left), bisect_right(data['time_s'],right)
        with self.lock:
            complete = self.complete and base+hi <= len(self.index.mask)
            if self.sparse:
                available=max(lo,min(hi,len(self.index.mask)-base))
                codes=np.asarray(data['vshunt_code'][lo:available],np.int64)
                events=self.events_between(base+lo,base+available)
                if events:
                    valid=np.ones(len(codes),bool)
                    valid[[event['index']-base-lo for event in events]]=False
                    codes=codes[valid]
                from .range_stats import RawRangeIndex
                total,square=RawRangeIndex._sum_square(codes)
                result=dict(valid_count=len(codes),excluded_count=available-lo-len(codes),
                    min=int(codes.min())/16 if len(codes) else None,
                    max=int(codes.max())/16 if len(codes) else None,
                    mean=total/len(codes)/16 if len(codes) else None,
                    rms=sqrt(square/len(codes))/16 if len(codes) else None,
                    analyzed_count=available-lo)
            else:
                result = self.index.query_positions(data, lo, hi, base)
        result.update(complete=complete, statistics_source='RAW_EXCLUDING_CANDIDATES',
                      detector_rules=self.rules.as_dict())
        return result


class DetectionWorker(threading.Thread):
    def __init__(self, session_id, rules=None, capacity=128, sparse=False):
        super().__init__(name='CurrentSensor candidate detector', daemon=True)
        self.analyzer = CandidateAnalyzer(session_id, rules, sparse=sparse)
        self.queue = queue.Queue(capacity)
        self.stopping = threading.Event()
        self.done = threading.Event()
        self.start()

    def submit(self, data):
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            self.analyzer.complete = False
            self.analyzer.error = 'DETECTOR_QUEUE_OVERFLOW'
            # Continue with later blocks; absolute positions become unreliable.
            self.stopping.set()

    def finish(self):
        self.stopping.set()

    def run(self):
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    first = self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                batch = [first]
                size = len(first)
                deadline = time.monotonic()+.015
                while size < 512 and not self.stopping.is_set():
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        part = self.queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    batch.append(part)
                    size += len(part)
                if self.analyzer.error:
                    continue
                self.analyzer.feed(np.concatenate(batch) if len(batch)>1 else first)
            if not self.analyzer.error:
                self.analyzer.finish()
        except Exception as error:
            self.analyzer.complete = False
            self.analyzer.error = f'{type(error).__name__}: {error}'
        finally:
            self.done.set()


def analyze_history(data, session_id, rules=None, chunk=65536):
    analyzer = CandidateAnalyzer(session_id, rules)
    for start in range(0, len(data), chunk):
        analyzer.feed(data[start:start+chunk])
    analyzer.finish()
    return analyzer
