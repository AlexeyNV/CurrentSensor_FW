"""Exact RAW range aggregates; block prefixes keep cursor motion O(log N + block)."""
from bisect import bisect_left, bisect_right
from math import sqrt
import threading
import numpy as np


class RawRangeIndex:
    BLOCK = 4096

    def __init__(self):
        self.lock = threading.RLock()
        self.minimum = []
        self.maximum = []
        self.prefix_sum = [0]
        self.prefix_square = [0]
        self.pending = np.empty(0, np.int32)
        self.count = 0

    @classmethod
    def from_data(cls, data):
        index = cls()
        for start in range(0, len(data), 262144):
            index.append(data['vshunt_code'][start:start + 262144])
        return index

    @staticmethod
    def _sum_square(codes):
        values = np.asarray(codes, dtype=np.int64)
        if not len(values):
            return 0, 0
        if np.any((values < -524288) | (values > 524287)):
            return sum(map(int, values)), sum(int(x) ** 2 for x in values)
        return int(values.sum()), int(np.dot(values, values))

    def append(self, codes):
        codes = np.asarray(codes, dtype=np.int32)
        if not len(codes):
            return
        with self.lock:
            pending = np.concatenate((self.pending, codes))
            complete = len(pending) // self.BLOCK
            for block in pending[:complete * self.BLOCK].reshape(complete, self.BLOCK):
                total, square = self._sum_square(block)
                self.minimum.append(int(block.min()))
                self.maximum.append(int(block.max()))
                self.prefix_sum.append(self.prefix_sum[-1] + total)
                self.prefix_square.append(self.prefix_square[-1] + square)
            self.pending = pending[complete * self.BLOCK:].copy()
            self.count += len(codes)

    def query(self, data, x1, x2, max_count=None):
        """Inclusive timestamps; data is immutable or a bounded committed memmap."""
        available = len(data) if max_count is None else min(len(data), max_count)
        left, right = sorted((float(x1), float(x2)))
        result = dict(count=0, min=None, max=None, mean=None, rms=None,
                      slot_gaps=0, dt=abs(float(x2) - float(x1)),
                      slot_gaps_complete=True,
                      interval_start_s=left, interval_end_s=right,
                      available_start_s=None, available_end_s=None,
                      complete=False, statistics_source='RAW')
        if not available:
            return result
        times = data['time_s'][:available]
        result['available_start_s'] = float(times[0])
        result['available_end_s'] = float(times[-1])
        result['complete'] = left >= times[0] and right <= times[-1]
        lo, hi = bisect_left(times, left), bisect_right(times, right)
        # Missing samples have no measured timestamps. If a cursor cuts through
        # a gap, only the fully bracketed gap count is knowable without inventing
        # timestamps for the absent samples.
        for boundary, position in ((left, lo), (right, hi)):
            if 0 < position < available and times[position-1] < boundary < times[position]:
                delta = (int(data['slot'][position])-int(data['slot'][position-1])) & 0xFFFFFFFF
                if delta != 1:
                    result['slot_gaps_complete'] = False
        count = hi - lo
        result['count'] = count
        if not count:
            return result
        block = self.BLOCK
        first_full = min(hi, ((lo + block - 1) // block) * block)
        last_full = max(first_full, (hi // block) * block)
        edges = (data['vshunt_code'][lo:first_full], data['vshunt_code'][last_full:hi])
        with self.lock:
            # A growing spool appends only completed blocks. The caller's
            # snapshot count prevents observing any newer uncommitted bytes.
            if last_full // block > len(self.minimum):
                raise RuntimeError('range index is behind committed RAW data')
            lo_block, hi_block = first_full // block, last_full // block
            middle_sum = self.prefix_sum[hi_block] - self.prefix_sum[lo_block]
            middle_square = self.prefix_square[hi_block] - self.prefix_square[lo_block]
            minimum = min(self.minimum[lo_block:hi_block], default=None)
            maximum = max(self.maximum[lo_block:hi_block], default=None)
        total, square = middle_sum, middle_square
        for edge in edges:
            if len(edge):
                part_sum, part_square = self._sum_square(edge)
                total += part_sum
                square += part_square
                minimum = int(edge.min()) if minimum is None else min(minimum, int(edge.min()))
                maximum = int(edge.max()) if maximum is None else max(maximum, int(edge.max()))
        # Across <2^32 sample slots a modulo difference handles slot rollover.
        # Missing slots are not included in the arithmetic averages.
        span = (int(data['slot'][hi - 1]) - int(data['slot'][lo])) & 0xFFFFFFFF
        result.update(min=minimum / 16, max=maximum / 16,
                      mean=total / count / 16, rms=sqrt(square / count) / 16,
                      slot_gaps=(span - (count - 1)) & 0xFFFFFFFF,
                      first_slot=int(data['slot'][lo]), last_slot=int(data['slot'][hi-1]),
                      first_time_s=float(times[lo]), last_time_s=float(times[hi-1]))
        return result
