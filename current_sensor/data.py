"""Numeric RAW/filter/display operations, independent of Qt and serial I/O."""
from dataclasses import dataclass
from bisect import bisect_left, bisect_right
import numpy as np

RAW_DTYPE = np.dtype([("slot", "<u4"), ("tim2_read_us", "<u4"),
                      ("vshunt_code", "<i4"), ("time_s", "<f8")])
LIVE_DTYPE = np.dtype(RAW_DTYPE.descr + [("median3", "<i4"), ("median5", "<i4")])
FILTERS = ("RAW", "MEDIAN3", "MEDIAN5", "RAW + MEDIAN3")


def discontinuities(slots):
    return ((np.diff(slots.astype(np.int64)) & 0xFFFFFFFF) != 1)


class MedianBank:
    """Causal median, timestamp at window END; incomplete windows stay RAW.

    Four carry samples make chunk boundaries invisible. A slot gap resets the
    window. A new session must use a new bank (AcquisitionService.reset does so).
    """
    def __init__(self):
        self.codes = np.empty(0, dtype=np.int32)
        self.slots = np.empty(0, dtype=np.uint32)

    def process(self, codes, slots):
        if not len(codes):
            return codes.copy(), codes.copy()
        carry = len(self.codes)
        joined = np.concatenate((self.codes, codes))
        joined_slots = np.concatenate((self.slots, slots))
        run_start = np.zeros(len(joined), dtype=np.int64)
        boundaries = np.flatnonzero(discontinuities(joined_slots)) + 1
        run_start[boundaries] = boundaries
        run_start = np.maximum.accumulate(run_start)
        results = []
        for width in (3, 5):
            result = joined.copy()
            if len(joined) >= width:
                windows = np.lib.stride_tricks.sliding_window_view(joined, width)
                medians = np.median(windows, axis=1).astype(np.int32)
                ends = np.arange(width - 1, len(joined))
                valid = ends - run_start[ends] >= width - 1
                result[ends[valid]] = medians[valid]
            results.append(result[carry:])
        self.codes, self.slots = joined[-4:].copy(), joined_slots[-4:].copy()
        return tuple(results)


def with_filters(raw):
    output = np.empty(len(raw), LIVE_DTYPE)
    bank = MedianBank()
    for start in range(0, len(raw), 65536):
        stop = min(len(raw), start + 65536)
        for field in RAW_DTYPE.names:
            output[field][start:stop] = raw[field][start:stop]
        output["median3"][start:stop], output["median5"][start:stop] = bank.process(
            raw["vshunt_code"][start:stop], raw["slot"][start:stop])
    return output


def statistics(codes):
    if not len(codes):
        return dict(count=0, min=None, max=None, mean=None, std=None, rms=None)
    values = codes.astype(np.float64) / 16.0
    return dict(count=len(codes), min=float(values.min()), max=float(values.max()),
                mean=float(values.mean()), std=float(values.std()),
                rms=float(np.sqrt(np.mean(values * values))))


def cursor_metrics(t1, t2, i1, i2):
    delta = t2 - t1
    return dict(dt=delta, frequency=1 / abs(delta) if delta else None, di=i2 - i1)


@dataclass
class DisplayTrace:
    x: np.ndarray
    y: np.ndarray
    connect: np.ndarray
    indices: np.ndarray
    aggregated: bool


class EnvelopeIndex:
    """Lazy multilevel first/min/max/last cache with exact extremum times.

    Immutable data only. Query edge blocks are recomputed so a zoom cannot lose
    an extremum at its boundary. Gap endpoints are always included.
    """
    def __init__(self, data, field="vshunt_code"):
        self.data = data
        self.times = data["time_s"]
        self.codes = data[field]
        self.gaps = np.flatnonzero(discontinuities(data["slot"])) + 1
        self.levels = {}

    def _blocks(self, lo, hi, block):
        length = hi - lo
        full = length // block
        parts = []
        if full:
            groups = self.codes[lo:lo + full * block].reshape(full, block)
            starts = lo + np.arange(full, dtype=np.int64) * block
            parts.append(np.column_stack((starts, starts + groups.argmin(axis=1),
                                          starts + groups.argmax(axis=1), starts + block - 1)).ravel())
        start = lo + full * block
        if start < hi:
            values = self.codes[start:hi]
            parts.append(np.array([start, start + values.argmin(), start + values.argmax(), hi - 1]))
        return np.unique(np.concatenate(parts)) if parts else np.empty(0, dtype=np.int64)

    def query(self, left, right, pixels=1000):
        # np.searchsorted copies this strided structured-array field on each
        # call. Scalar binary search also works on a 10M-point memmap in O(log N).
        lo = bisect_left(self.times, left)
        hi = bisect_right(self.times, right)
        # A neighboring point permits a continuous line to reach the viewport edge.
        lo, hi = max(0, lo - 1), min(len(self.data), hi + 1)
        count = hi - lo
        aggregated = count > max(2, pixels * 2)
        if not aggregated:
            indices = np.arange(lo, hi, dtype=np.int64)
        else:
            block = 8
            while count / block > max(1, pixels):
                block *= 8
            if block not in self.levels:
                self.levels[block] = self._blocks(0, len(self.data), block)
            cached = self.levels[block]
            first_full = min(hi, ((lo + block - 1) // block) * block)
            last_full = max(first_full, (hi // block) * block)
            middle = cached[np.searchsorted(cached, first_full):np.searchsorted(cached, last_full)]
            gaps = self.gaps[(self.gaps > lo) & (self.gaps < hi)]
            indices = np.unique(np.concatenate((self._blocks(lo, first_full, block), middle,
                                                self._blocks(last_full, hi, block), gaps, gaps - 1)))
        connected = np.zeros(len(indices), dtype=bool)
        if len(indices) > 1:
            segment = np.searchsorted(self.gaps, indices, side="right")
            connected[:-1] = segment[:-1] == segment[1:]
        return DisplayTrace(self.times[indices], self.codes[indices].astype(float) / 16,
                            connected, indices, aggregated)

    def region_stats(self, left, right):
        lo, hi = bisect_left(self.times, min(left, right)), bisect_right(self.times, max(left, right))
        return statistics(self.codes[lo:hi])


def anomaly_mask(data, limit_ma, step_ma):
    values = data["vshunt_code"].astype(np.int64)
    mask = np.abs(values) > limit_ma * 16
    if step_ma > 0 and len(values) > 1:
        mask[1:] |= (np.abs(np.diff(values)) > step_ma * 16) & ~discontinuities(data["slot"])
    return mask


def robust_y_range(codes):
    """Keep a single outlier outside auto-Y; render an explicit clipped marker."""
    if not len(codes):
        return -10.0, 10.0
    # Only auto-axis estimation is sampled; RAW curves/extrema and stats aren't.
    low, high = np.percentile(codes[::max(1, (len(codes)+9999)//10000)], [1, 99]) / 16
    span = max(1.0, float(high - low))
    return float(low - span * .2), float(high + span * .2)
