import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from current_sensor.data import RAW_DTYPE, with_filters
from current_sensor.range_stats import RawRangeIndex
from current_sensor.spool import RawSpool, recoverable_spools
from current_sensor.export import ExportSource, export_raw
from current_sensor.recorder import Recorder, open_history
from test_numeric import raw


class RangeTests(unittest.TestCase):
    def query(self, codes, x1=None, x2=None):
        data = raw(codes)
        return RawRangeIndex.from_data(data).query(data,
            data['time_s'][0] if x1 is None else x1,
            data['time_s'][-1] if x2 is None else x2)

    def test_constant_positive_negative_rms(self):
        for code in (0, 1600, -1600):
            result = self.query([code]*10000)
            self.assertEqual(result['mean'], code/16)
            self.assertEqual(result['rms'], abs(code/16))
            self.assertEqual(result['count'], 10000)

    def test_sine_and_dc_offset(self):
        codes = np.rint(1600*np.sin(np.arange(10000)*2*np.pi/100)).astype(np.int32)
        for dc in (0, 1600, -1600):
            result = self.query(codes+dc)
            self.assertAlmostEqual(result['mean'], dc/16, places=9)
            self.assertAlmostEqual(result['rms'], math.sqrt((dc/16)**2+100**2/2), delta=.02)
            self.assertAlmostEqual(result['rms'], float(np.sqrt(np.mean(((codes.astype(float)+dc)/16)**2))), places=9)

    def test_single_sample_pulse_and_mixed_signs(self):
        codes = np.zeros(10000, np.int32)
        codes[4096] = 160000
        result = self.query(codes)
        self.assertEqual(result['max'], 10000)
        self.assertEqual(result['mean'], 1)
        self.assertEqual(result['rms'], 100)
        result = self.query([-32, -16, 0, 16, 32])
        self.assertEqual(result['mean'], 0)
        self.assertEqual(result['rms'], math.sqrt(2))

    def test_inclusive_swapped_boundaries_empty_and_gaps(self):
        data = raw(np.arange(10000, dtype=np.int32))
        data['slot'][5000:] += 7
        index = RawRangeIndex.from_data(data)
        x1, x2 = data['time_s'][4000], data['time_s'][6000]
        result = index.query(data, x1, x2)
        self.assertEqual(result, index.query(data, x2, x1))
        self.assertEqual(result['count'], 2001)
        self.assertEqual(result['slot_gaps'], 7)
        self.assertEqual(result['min'], 4000/16)
        self.assertEqual(result['max'], 6000/16)
        empty = index.query(data, 5, 6)
        self.assertEqual(empty['count'], 0)
        self.assertIsNone(empty['rms'])
        self.assertFalse(empty['complete'])
        partial = index.query(data, -1, .5)
        self.assertFalse(partial['complete'])

    def test_slot_rollover_and_incremental_blocks(self):
        data = raw(np.arange(13000, dtype=np.int32)-6000)
        data['slot'] = (np.arange(len(data), dtype=np.uint64)+0xfffffff0).astype(np.uint32)
        index = RawRangeIndex()
        for lo in range(0, len(data), 117):
            index.append(data['vshunt_code'][lo:lo+117])
        for lo, hi in ((0, 1), (1,4095), (4096,4097), (4097,8193), (7,12990)):
            result = index.query(data, data['time_s'][lo], data['time_s'][hi-1])
            values = data['vshunt_code'][lo:hi].astype(float)/16
            self.assertEqual(result['slot_gaps'], 0)
            self.assertAlmostEqual(result['mean'], values.mean(), places=9)
            self.assertAlmostEqual(result['rms'], np.sqrt(np.mean(values**2)), places=9)

    def test_interval_cuts_gap_does_not_invent_missing_timestamps(self):
        data=raw([16,32])
        data['slot'][1]=10
        data['time_s'][1]=.001
        index=RawRangeIndex.from_data(data)
        result=index.query(data,.0002,.0008)
        self.assertEqual(result['count'],0)
        self.assertFalse(result['slot_gaps_complete'])
        full=index.query(data,0,.001)
        self.assertEqual(full['slot_gaps'],9)
        self.assertTrue(full['slot_gaps_complete'])

    def test_long_history_index_queries(self):
        # Full construction is once; repeated queries touch <= 8190 edge codes
        # plus block aggregates, including ranges near the end of a 10M history.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'long.raw.bin'
            data = np.memmap(path, RAW_DTYPE, mode='w+', shape=(10000000,))
            index = RawRangeIndex()
            for lo in range(0, len(data), 65536):
                hi = min(len(data), lo+65536)
                data['slot'][lo:hi] = np.arange(lo,hi,dtype=np.uint32)
                data['time_s'][lo:hi] = np.arange(lo,hi)*.0001
                data['vshunt_code'][lo:hi] = 1600
                index.append(data['vshunt_code'][lo:hi])
            for count in (100000,600000,3000000,10000000):
                result = index.query(data, 0, data['time_s'][count-1])
                self.assertEqual(result['count'], count)
                self.assertEqual(result['mean'], 100)
                self.assertEqual(result['rms'], 100)
            del data


class StorageTests(unittest.TestCase):
    def test_spool_export_without_recording_and_csv_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            data = with_filters(raw(np.array([0,16,320000,-32,48,16]*1000)))
            data['slot'][2500:] += 4
            original = data['vshunt_code'].copy()
            spool = RawSpool({'profile':{'sample_period_us':100}, 'calibration':{'ENABLED':'0'}},
                             directory=Path(directory)/'spool', free_reserve_bytes=0)
            spool.start()
            spool.submit(data)
            spool.finish({'acquisition_finished':True,'session_end_verified':True})
            spool.join(5)
            self.assertIsNone(spool.error)
            self.assertFalse(list(spool.path.parent.glob('*.csv')))
            self.assertFalse(list(spool.path.parent.glob('*.csf4')))
            snap = spool.snapshot()
            source = ExportSource(snap, snap.metadata, True, 'SPOOL', spool.index)
            full = export_raw(source, directory)
            metadata = json.loads(Path(full['path']).read_text(encoding='utf-8'))
            self.assertTrue(metadata['recording_saved'])
            self.assertTrue(metadata['acquisition_finished'])
            self.assertFalse(metadata['session_end_verified'])
            self.assertTrue(metadata['source_session_end_verified'])
            self.assertTrue(metadata['exported_full_available_session'])
            x1, x2 = data['time_s'][4000], data['time_s'][2000]
            result = export_raw(source, directory, 'CURSORS', (x1,x2), x1,x2)
            meta = json.loads(Path(result['path']).read_text(encoding='utf-8'))
            csv_path = Path(result['path']).parent/meta['files']['csv']
            with csv_path.open() as stream:
                rows = list(csv.DictReader(stream))
            values = np.array([int(row['vshunt_code'])/16 for row in rows])
            self.assertEqual(meta['sample_count'], 2001)
            self.assertEqual(meta['slot_gaps'], 4)
            self.assertEqual(meta['statistics_source'], 'RAW')
            self.assertEqual(meta['cursor_x1_s'], x1)
            self.assertEqual(meta['cursor_x2_s'], x2)
            self.assertEqual(meta['min_ma'], values.min())
            self.assertEqual(meta['max_ma'], values.max())
            self.assertAlmostEqual(meta['avg_ma'], values.mean())
            self.assertAlmostEqual(meta['rms_ma'], np.sqrt(np.mean(values**2)))
            self.assertEqual(int(rows[0]['tim2_read_us']), int(data['tim2_read_us'][2000]))
            history, _ = open_history(result['path'])
            np.testing.assert_array_equal(history['vshunt_code'], original[2000:4001])
            np.testing.assert_array_equal(data['vshunt_code'], original)
            del source, snap

    def test_spool_queue_limit_disk_space_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            data = raw([1,2,3,4])
            spool = RawSpool({}, directory=directory, queue_capacity=1, free_reserve_bytes=0)
            self.assertTrue(spool.submit(data))
            self.assertFalse(spool.submit(data))
            spool.start(); spool.join(3)
            self.assertIn('SPOOL_OVERFLOW', spool.error)
            self.assertEqual(spool.count, 4)
            recovered = recoverable_spools(directory, include_live_owner=True)
            self.assertEqual(recovered[0][1], 4)
            spool.delete()
            limited = RawSpool({}, directory=directory, max_bytes=20, free_reserve_bytes=0)
            limited.submit(data); limited.finish(); limited.start(); limited.join(3)
            self.assertIn('SPOOL_LIMIT', limited.error)
            limited.delete()
            empty_disk = RawSpool({}, directory=directory)
            empty_disk.submit(data); empty_disk.finish()
            with patch('current_sensor.spool.shutil.disk_usage', return_value=type('Usage',(),{'free':0})()):
                empty_disk.start(); empty_disk.join(3)
            self.assertIn('SPOOL_SPACE', empty_disk.error)

    def test_recovery_after_abrupt_process_exit(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            script = """import os,sys,time
import numpy as np
from current_sensor.data import RAW_DTYPE
from current_sensor.spool import RawSpool
s=RawSpool({'profile':{'sample_period_us':100}},directory=sys.argv[1],free_reserve_bytes=0)
d=np.zeros(100,RAW_DTYPE); d['slot']=np.arange(100); d['time_s']=np.arange(100)*.0001
s.start(); s.submit(d)
while s.count<100 and not s.error: time.sleep(.001)
os._exit(7)
"""
            process = subprocess.run([sys.executable,'-c',script,directory], timeout=10)
            self.assertEqual(process.returncode,7)
            recovered = recoverable_spools(directory)
            self.assertEqual(recovered[0][1],100)
            self.assertFalse(recovered[0][2]['spool_finished'])
            history, _ = open_history(recovered[0][0])
            np.testing.assert_array_equal(history['slot'], np.arange(100))

    def test_recorder_final_states_are_unambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, {'profile':{'sample_period_us':100}})
            recorder.submit(with_filters(raw([0,16,-16])))
            recorder.finish({'state':'stopping', 'recording':{'active':True},
                             'acquisition_finished':True,'session_end_verified':True})
            recorder.start(); recorder.join(3)
            report = json.loads(recorder.metadata_path.read_text())
            self.assertTrue(report['acquisition_finished'])
            self.assertTrue(report['recording_finished'])
            self.assertTrue(report['recording_saved'])
            self.assertNotIn('state',report)
            self.assertNotIn('recording',report)
            self.assertEqual(report['pre_final_snapshot']['state'],'stopping')

    def test_partial_live_buffer_and_empty_export(self):
        with tempfile.TemporaryDirectory() as directory:
            data=raw([16,-16,320000])
            source=ExportSource(data,{},False,'LIVE_BUFFER')
            result=export_raw(source,directory)
            report=json.loads(Path(result['path']).read_text())
            self.assertEqual(report['export_type'],'FULL_AVAILABLE_PARTIAL')
            self.assertFalse(report['source_history_complete'])
            self.assertFalse(report['exported_full_available_session'])
            with self.assertRaisesRegex(ValueError,'NO_RAW_SAMPLES_IN_INTERVAL'):
                export_raw(source,directory,'ROI',(10,20))


if __name__ == '__main__':
    unittest.main()
