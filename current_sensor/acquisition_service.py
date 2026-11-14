"""Bounded live storage and full-rate numeric processing, with no Qt calls."""
import threading
from bisect import bisect_left, bisect_right
import numpy as np
from .data import LIVE_DTYPE, MedianBank, anomaly_mask, discontinuities
from .trigger import SoftwareTrigger


class AcquisitionService:
    def __init__(self, capacity=720000):
        self.lock = threading.RLock()
        self.capacity = capacity
        self.buffer = np.empty(capacity, LIVE_DTYPE)
        self.raw24_buffer=np.empty(capacity,np.uint32)
        self.reset({"sample_period_us": 0})

    def reset(self, profile):
        with self.lock:
            self.profile = profile.copy()
            self.head = self.size = self.count = 0
            self.median = MedianBank()
            self.trigger = SoftwareTrigger()
            self.last_stamp = self.last_slot = None
            self.elapsed_us = 0
            self.sum_code = self.sum_square = 0
            self.minimum = self.maximum = self.last_code = None
            self.initial_gaps = self.running_gaps = self.period_violations = 0
            self.limit_ma, self.step_ma = 10000.0, 1000.0
            self.anomalies = 0
            self.trigger_event = None
            self.revision = 0
            self.raw24_buffer.fill(0xFFFFFFFF)

    def ingest(self, frames):
        samples = [item for frame in frames if frame["type"] == 1 for item in frame["samples"]]
        raw24=np.array([raw for frame in frames if frame['type']==1
            for raw in frame.get('raw24',[0xFFFFFFFF]*len(frame['samples']))],np.uint32)
        data = np.empty(len(samples), LIVE_DTYPE)
        if not samples:
            return data
        values = np.asarray(samples, dtype=np.int64)
        data["slot"], data["tim2_read_us"], data["vshunt_code"] = values.T
        stamps = data["tim2_read_us"].astype(np.int64)
        previous = stamps[0] if self.last_stamp is None else self.last_stamp
        deltas = np.diff(np.concatenate(([previous], stamps))) & 0xFFFFFFFF
        data["time_s"] = (self.elapsed_us + np.cumsum(deltas)) / 1000000
        slot_deltas = np.diff(np.concatenate((
            [int(data["slot"][0]) - 1 if self.last_slot is None else self.last_slot], values[:, 0]))) & 0xFFFFFFFF
        if self.last_slot is None:
            self.initial_gaps = int(data["slot"][0])
        self.running_gaps += int(np.maximum(slot_deltas - 1, 0).sum())
        valid_period = slot_deltas == 1
        if self.last_stamp is None:
            valid_period[0] = False
        if not self.profile.get('event_driven'):
            self.period_violations += int(np.count_nonzero(valid_period & (np.abs(deltas - self.profile["sample_period_us"]) > 5)))
        self.elapsed_us += int(deltas.sum())
        self.last_stamp, self.last_slot = int(stamps[-1]), int(data["slot"][-1])
        data["median3"], data["median5"] = self.median.process(data["vshunt_code"], data["slot"])
        anomaly_count = int(anomaly_mask(data, self.limit_ma, self.step_ma).sum())
        if self.last_code is not None and slot_deltas[0] == 1 and self.step_ma > 0:
            boundary_step = abs(int(data["vshunt_code"][0]) - self.last_code) > self.step_ma * 16
            if boundary_step and abs(int(data["vshunt_code"][0])) <= self.limit_ma * 16:
                anomaly_count += 1
        codes = data["vshunt_code"].astype(np.int64)
        event = self.trigger.process(data)
        with self.lock:
            self.anomalies += anomaly_count
            self.count += len(data)
            self.sum_code += int(codes.sum())
            # Even a CRC-valid malformed signed int32 must not overflow int64.
            self.sum_square += (sum(int(value) ** 2 for value in codes) if np.any(
                (codes < -524288) | (codes > 524287)) else int(np.dot(codes, codes)))
            self.minimum = int(codes.min()) if self.minimum is None else min(self.minimum, int(codes.min()))
            self.maximum = int(codes.max()) if self.maximum is None else max(self.maximum, int(codes.max()))
            self.last_code = int(codes[-1])
            portion = data[-self.capacity:]
            n = min(len(portion), self.capacity - self.head)
            self.buffer[self.head:self.head + n] = portion[:n]
            self.buffer[:len(portion) - n] = portion[n:]
            raw_portion=raw24[-self.capacity:]
            self.raw24_buffer[self.head:self.head+n]=raw_portion[:n]
            self.raw24_buffer[:len(portion)-n]=raw_portion[n:]
            self.head = (self.head + len(portion)) % self.capacity
            self.size = min(self.capacity, self.size + len(portion))
            self.revision += 1
            if event:
                event["data"] = self.snapshot(event["time_s"] - event["pre_s"], event["time_s"] + event["post_s"])
                event["pre_complete"] = bool(len(event["data"]) and event["data"]["time_s"][0] <= event["time_s"] - event["pre_s"] + self.profile["sample_period_us"] / 1e6)
                event['contains_gaps'] = bool(discontinuities(event['data']['slot']).any())
                event['pre_complete'] &= not event['contains_gaps']
                self.trigger_event = event
        data.flags.writeable = False
        return data

    def lookup_raw24(self,slot,stamp):
        with self.lock:
            indices=np.flatnonzero((self.buffer['slot']==slot)&(self.buffer['tim2_read_us']==stamp))
            for i in indices:
                distance=(self.head-1-int(i))%self.capacity
                if distance<self.size and int(self.raw24_buffer[i])<=0xFFFFFF:
                    return int(self.raw24_buffer[i])
        return None

    def snapshot(self, left=-np.inf, right=np.inf):
        with self.lock:
            start = (self.head - self.size) % self.capacity
            slices = [self.buffer[start:min(self.capacity, start + self.size)]]
            if start + self.size > self.capacity:
                slices.append(self.buffer[:self.head])
            result = []
            for part in slices:
                lo = bisect_left(part['time_s'], left)
                hi = bisect_right(part['time_s'], right)
                result.append(part[max(0, lo - 1):min(len(part), hi + 1)])
            return np.concatenate(result).copy()

    def summary(self):
        with self.lock:
            mean = self.sum_code / self.count if self.count else 0
            variance = max(0, self.sum_square / self.count - mean * mean) if self.count else 0
            return dict(samples=self.count, time_s=self.elapsed_us / 1e6,
                        rate_hz=(self.count - 1) * 1e6 / self.elapsed_us if self.elapsed_us else 0,
                        current_ma=self.last_code / 16 if self.last_code is not None else None,
                        min_ma=self.minimum / 16 if self.minimum is not None else None,
                        max_ma=self.maximum / 16 if self.maximum is not None else None,
                        mean_ma=mean / 16, std_ma=variance ** .5 / 16,
                        initial_slot_gaps=self.initial_gaps, running_slot_gaps=self.running_gaps,
                        period_violations=self.period_violations, anomalies=self.anomalies,
                        revision=self.revision, live_samples=self.size)

    def take_trigger(self):
        with self.lock:
            event, self.trigger_event = self.trigger_event, None
            return event
