import unittest
import numpy as np
from current_sensor.data import (RAW_DTYPE, MedianBank, EnvelopeIndex, with_filters,
                                 statistics, cursor_metrics, anomaly_mask, robust_y_range)
from current_sensor.acquisition_service import AcquisitionService
from current_sensor.trigger import SoftwareTrigger, TriggerConfig
from current_sensor.mock_device import encode_frame
from tools.stream_capture import FrameDecoder


def raw(codes, gap=None, period=.0001):
    data = np.zeros(len(codes), RAW_DTYPE)
    data['slot'] = np.arange(len(data))
    if gap is not None:
        data['slot'][gap:] += 1
    data['time_s'] = data['slot'].astype(float) * period
    data['tim2_read_us'] = (data['time_s'] * 1e6).astype(np.uint32)
    data['vshunt_code'] = codes
    return data


class NumericTests(unittest.TestCase):
    def test_median_chunk_boundaries_and_gap(self):
        codes = np.array([1, 2, 999, 2, 1, -50, -60, 900, -70, -80, -90], np.int32)
        data = raw(codes, gap=5)
        whole = with_filters(data)
        for chunk in (1, 2, 3, 4, 7):
            bank = MedianBank()
            pieces = [bank.process(codes[i:i+chunk], data['slot'][i:i+chunk]) for i in range(0, len(codes), chunk)]
            np.testing.assert_array_equal(np.concatenate([p[0] for p in pieces]), whole['median3'])
            np.testing.assert_array_equal(np.concatenate([p[1] for p in pieces]), whole['median5'])
        self.assertEqual(whole['median3'][2], 2)
        np.testing.assert_array_equal(whole['median5'][5:9], codes[5:9])
        np.testing.assert_array_equal(whole['vshunt_code'], codes)

    def test_pulses_extrema_and_exact_time(self):
        for width in (1, 2, 3):
            data = raw(np.zeros(100000, np.int32))
            data['vshunt_code'][12345:12345+width] = 80000
            data['vshunt_code'][23456] = -90000
            index = EnvelopeIndex(data)
            trace = index.query(0, 10, 500)
            self.assertIn(12345, trace.indices)
            self.assertIn(23456, trace.indices)
            self.assertEqual(trace.y.max(), 5000)
            np.testing.assert_array_equal(trace.x, data['time_s'][trace.indices])
            close = index.query(1.234, 1.235, 500)
            self.assertFalse(close.aggregated)
            self.assertEqual(np.count_nonzero(close.y == 5000), width)

    def test_envelope_edges_and_gaps(self):
        rng = np.random.default_rng(12)
        data = raw(rng.integers(-100, 100, 30000), gap=14211)
        index = EnvelopeIndex(data)
        for lo, hi in ((0, 29999), (50, 2100), (129, 11000), (14200, 15000)):
            trace = index.query(data['time_s'][lo], data['time_s'][hi], 10)
            covered = data['vshunt_code'][max(0, lo-1):min(len(data), hi+2)]
            self.assertEqual(trace.y.max() * 16, covered.max())
            self.assertEqual(trace.y.min() * 16, covered.min())
            for i in np.flatnonzero(trace.connect):
                self.assertFalse(trace.indices[i] < 14211 <= trace.indices[i+1])

    def test_nearby_pulses_zoom_raw(self):
        data = raw([0, 100, 0, 80, 80, 0, 90, 90, 90, 0])
        trace = EnvelopeIndex(data).query(0, 1)
        np.testing.assert_array_equal(trace.y * 16, data['vshunt_code'])

    def test_stats_negative_values_and_cursors(self):
        result = statistics(np.array([-16, 0, 16]))
        self.assertEqual(result['mean'], 0)
        self.assertAlmostEqual(result['rms'], (2/3)**.5)
        self.assertEqual(cursor_metrics(1, 1.5, -5, 10), dict(dt=.5, frequency=2, di=15))
        self.assertIsNone(cursor_metrics(0, 0, 0, 0)['frequency'])
        self.assertFalse(anomaly_mask(raw([-16, 0, 16]), 10000, 1000).any())

    def test_robust_auto_y_does_not_hide_raw(self):
        codes = np.ones(10000, np.int32) * 1600
        codes[500] = 400000
        self.assertLess(robust_y_range(codes)[1], 1000)
        self.assertEqual(EnvelopeIndex(raw(codes)).query(0, 1).y.max(), 25000)

    def test_ring_rollover_statistics_and_reset(self):
        service = AcquisitionService(capacity=5)
        service.reset({'sample_period_us': 100})
        decoder = FrameDecoder()
        for i in range(3):
            service.ingest(decoder.feed(encode_frame(1, i, 100, slot=i*3,
                stamp=(0xffffff00+i*300)&0xffffffff, codes=[-16, 0, 16])))
        snap = service.snapshot()
        self.assertEqual(len(snap), 5)
        np.testing.assert_array_equal(snap['slot'], np.arange(4, 9))
        self.assertAlmostEqual(snap['time_s'][-1], .0008)
        self.assertEqual(service.summary()['rate_hz'], 10000)
        self.assertEqual(service.summary()['period_violations'], 0)
        service.reset({'sample_period_us': 200})
        self.assertEqual(service.summary()['samples'], 0)
        self.assertEqual(len(service.median.codes), 0)

    def test_signed_int32_corruption_cannot_overflow_stats(self):
        service = AcquisitionService()
        service.ingest([dict(type=1, samples=[(0, 0, -2147483648), (1, 100, -2147483648)])])
        self.assertGreater(service.sum_square, 2**63-1)

    def test_slot_rollover_shared_decoder(self):
        decoder = FrameDecoder()
        frames = decoder.feed(encode_frame(1, 0, 100, slot=0xffffffff, codes=[1, 2]))
        decoder.feed(encode_frame(1, 1, 100, slot=1, stamp=200, codes=[3]))
        self.assertEqual(frames[0]['samples'][1][0], 0)
        self.assertEqual(decoder.slot_gaps, 0)

    def test_trigger_pre_post_single_and_raw(self):
        service = AcquisitionService(100)
        service.reset({'sample_period_us': 100})
        service.trigger.configure(TriggerConfig(mode='Single', level_ma=10, hysteresis_ma=1, pre_s=.0002, post_s=.0002))
        codes = [0]*5 + [320]*5
        service.ingest([dict(type=1, samples=[(i, i*100, v) for i,v in enumerate(codes)])])
        event = service.take_trigger()
        self.assertEqual(event['slot'], 5)
        self.assertTrue(event['pre_complete'])
        self.assertGreaterEqual(event['data']['time_s'][-1], .0007)
        self.assertFalse(service.trigger.enabled)

    def test_trigger_gap_holdoff_falling_auto(self):
        trigger = SoftwareTrigger(TriggerConfig(mode='Normal', level_ma=5, hysteresis_ma=1, post_s=0, holdoff_s=1))
        self.assertIsNone(trigger.process(raw([0, 160], gap=1)))
        trigger.configure(TriggerConfig(mode='Single', edge='Falling', level_ma=5, post_s=0))
        self.assertEqual(trigger.process(raw([160, 0]))['slot'], 1)
        trigger.configure(TriggerConfig(mode='Auto', level_ma=1000, post_s=0))
        event = trigger.process(raw([0]*11001))
        self.assertFalse(event['triggered'])
        trigger.configure(TriggerConfig(mode='Normal', level_ma=5, post_s=0, holdoff_s=1))
        self.assertEqual(trigger.process(raw([0,160,0,160]))['slot'], 1)


if __name__ == '__main__':
    unittest.main()
