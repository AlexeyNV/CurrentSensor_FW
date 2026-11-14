import json
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import numpy as np
from current_sensor.archive import ArchiveOptions, ArchiveRecorder, ArchiveReader, rebuild_archive_index, verify_archive_segments
from current_sensor.data import RAW_DTYPE
from current_sensor.mock_device import encode_frame
from current_sensor.recorder import open_history
from current_sensor.export import ExportSource, export_archive
from current_sensor.anomaly_diagnostics import DetectionWorker
from tools.stream_capture import FrameDecoder


def wait_for(done,timeout=10):
    deadline=time.monotonic()+timeout
    while not done.is_set() and time.monotonic()<deadline:
        time.sleep(.005)
    if not done.is_set():
        raise AssertionError('archive writer did not finish')


def submit_samples(writer,count=256,period=100,first_stamp=0,first_slot=0,with_end=True,detector=None):
    decoder=FrameDecoder()
    samples=0; sequence=0; elapsed=0; previous=None
    step=min(32,65535//period+1)
    for start in range(0,count,step):
        n=min(step,count-start)
        slot=first_slot+start
        stamp=(first_stamp+start*period)&0xffffffff
        codes=[-77]*n
        if start<=count//2<start+n:
            codes[count//2-start]=-8269
        packet=encode_frame(1,sequence,period,slot,stamp,codes)
        frame=decoder.feed(packet)[0]
        raw=np.zeros(n,RAW_DTYPE)
        raw['slot']=np.arange(slot,slot+n,dtype=np.uint32)
        raw['vshunt_code']=codes
        raw['tim2_read_us']=[stamp2 for _,stamp2,_ in frame['samples']]
        for i,clock in enumerate(raw['tim2_read_us']):
            if previous is not None:
                elapsed+=(int(clock)-previous)&0xffffffff
            previous=int(clock)
            raw['time_s'][i]=elapsed/1e6
        writer.submit(raw,(frame,))
        if detector is not None:
            detector.submit(raw)
        samples+=n; sequence+=1
    if with_end:
        end=decoder.feed(encode_frame(3,sequence,period))[0]
        writer.submit(np.empty(0,RAW_DTYPE),(end,))
    return samples


class ArchiveTests(unittest.TestCase):
    def test_candidate_pulse_at_segment_boundary_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            detector=DetectionWorker('pulse',sparse=True)
            writer=ArchiveRecorder(directory,{'source_session':'pulse'},ArchiveOptions(25,0,0,'none'),
                                   detector=detector)
            writer.segment_limit_bytes=900
            writer.block_samples=64
            submit_samples(writer,1024,detector=detector)
            detector.finish()
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            reader=ArchiveReader(writer.manifest_path)
            analyzer=reader.load_analyzer()
            self.assertTrue(analyzer.complete)
            self.assertTrue(any(event['vshunt_code']==-8269 for event in analyzer.events))
            self.assertEqual(reader.range_stats(0,.1023)['min'],-8269/16)
            clean=reader.filtered_range_stats(0,.1023,analyzer)
            self.assertTrue(clean['complete'])
            self.assertEqual(clean['min'],-77/16)
            self.assertEqual(clean['valid_count']+clean['excluded_count'],1024)
            exported=export_archive(ExportSource(reader,{'source_session':'pulse'},True,
                                                  'ARCHIVE',analyzer=analyzer),directory,
                                    'CURSORS',(0,.1023))
            metadata=json.loads(Path(exported['path']).read_text(encoding='utf-8'))
            self.assertEqual(metadata['raw_without_candidates']['min'],-77/16)
            self.assertEqual(metadata['candidate_events_saved'],len(analyzer.events))
            self.assertEqual(len((Path(exported['path']).parent/metadata['files']['events']).read_text(
                encoding='utf-8').splitlines())-1,len(analyzer.events))
            reader.close()

    def test_candidate_index_disambiguates_equal_timestamps_across_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            detector=DetectionWorker('same_time',sparse=True)
            writer=ArchiveRecorder(directory,{'source_session':'same_time'},
                                   ArchiveOptions(25,0,0,'none'),detector=detector)
            writer.block_samples=1
            decoder=FrameDecoder()
            for slot,code in ((0,-77),(1,100000)):
                frame=decoder.feed(encode_frame(1,slot,100,slot,0,[code]))[0]
                raw=np.array([(slot,0,code,0.)],RAW_DTYPE)
                writer.submit(raw,(frame,))
                detector.submit(raw)
            end=decoder.feed(encode_frame(3,2,100))[0]
            writer.submit(np.empty(0,RAW_DTYPE),(end,))
            detector.finish()
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            reader=ArchiveReader(writer.manifest_path)
            analyzer=reader.load_analyzer()
            self.assertEqual(reader.range_stats(0,0)['count'],2)
            clean=reader.filtered_range_stats(0,0,analyzer)
            self.assertEqual(clean['valid_count'],1)
            self.assertEqual(clean['min'],-77/16)
            reader.close()

    def test_checkpoint_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'checkpoint'},ArchiveOptions(25,0,0,'none'))
            submit_samples(writer,96,with_end=False)
            checkpoint=writer.checkpoint()
            self.assertIsNotNone(checkpoint)
            self.assertTrue(checkpoint.wait(10))
            backup=Path(directory)/'crashed'
            shutil.copytree(writer.root,backup)
            self.assertEqual(ArchiveReader(backup).sample_count,96)
            saved=json.loads((backup/'session.json').read_text(encoding='utf-8'))
            saved['owner_thread_id']=4294967295
            (backup/'session.json').write_text(json.dumps(saved),encoding='utf-8')
            recovered=ArchiveReader(backup)
            self.assertTrue(recovered.manifest['recovered'])
            self.assertTrue(recovered.manifest['partial'])
            self.assertEqual(recovered.sample_count,96)
            recovered.close()
            writer.finish()
            wait_for(writer.done)

    def test_disk_reserve_reports_error_without_fabricating_complete_session(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'full'},ArchiveOptions(25,0,1048576,'none'))
            writer.block_samples=32
            submit_samples(writer,64)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            self.assertIn('ARCHIVE_DISK_RESERVE',writer.error)
            reader=ArchiveReader(writer.manifest_path)
            self.assertNotEqual(reader.manifest['status'],'COMPLETE')
            self.assertEqual(reader.sample_count,0)
            reader.close()

    def test_source_slot_loss_remains_partial_after_index_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'loss'},ArchiveOptions(25,0,0,'none'))
            submit_samples(writer,64)
            writer.finish({'session_end_verified':True,'transport':{'slot_gaps':1}})
            wait_for(writer.done)
            self.assertEqual(writer.status,'PARTIAL')
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertEqual(reader.manifest['status'],'PARTIAL')
            self.assertEqual(reader.sample_count,64)
            reader.close()

    def test_verified_flag_without_actual_end_does_not_complete_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'no_end'},ArchiveOptions(25,0,0,'none'))
            submit_samples(writer,64,with_end=False)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            self.assertEqual(writer.status,'PARTIAL')
            self.assertFalse(json.loads(writer.manifest_path.read_text())['archive_end_frame_seen'])
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertEqual(reader.manifest['status'],'PARTIAL')
            reader.close()

    def test_end_without_measurements_is_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'empty'},ArchiveOptions(25,0,0,'none'))
            decoder=FrameDecoder()
            end=decoder.feed(encode_frame(3,0,100))[0]
            writer.submit(np.empty(0,RAW_DTYPE),(end,))
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            self.assertEqual(writer.status,'PARTIAL')
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertEqual(reader.manifest['status'],'PARTIAL')
            self.assertEqual(reader.sample_count,0)
            reader.close()

    def test_slot_gap_and_export_across_segment_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'gap','profile':{'sample_period_us':100}},
                                   ArchiveOptions(25,0,0,'none'))
            writer.segment_limit_bytes=100
            writer.block_samples=2
            decoder=FrameDecoder()
            for sequence,slot,stamp,codes in ((0,0,0,[16,32]),(1,5,500,[800,64])):
                frame=decoder.feed(encode_frame(1,sequence,100,slot,stamp,codes))[0]
                raw=np.empty(2,RAW_DTYPE)
                raw['slot']=[s for s,_,_ in frame['samples']]
                raw['tim2_read_us']=[t for _,t,_ in frame['samples']]
                raw['vshunt_code']=codes
                raw['time_s']=[t/1e6 for _,t,_ in frame['samples']]
                self.assertTrue(writer.submit(raw,(frame,)))
            end=decoder.feed(encode_frame(3,2,100))[0]
            writer.submit(np.empty(0,RAW_DTYPE),(end,))
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            reader=ArchiveReader(writer.manifest_path)
            self.assertGreaterEqual(len(reader.manifest['segments']),2)
            self.assertEqual(reader.range_stats(0,.001)['slot_gaps'],3)
            self.assertFalse(reader.range_stats(.0003,.0006)['slot_gaps_complete'])
            self.assertIn(False,reader.plot_range(0,.001,100).connect.tolist())
            result=export_archive(ExportSource(reader,{'profile':{'sample_period_us':100}},True,'ARCHIVE'),
                                  directory,'CURSORS',(0,.001),0,.001)
            self.assertEqual(result['samples'],4)
            exported=json.loads(Path(result['path']).read_text(encoding='utf-8'))
            self.assertEqual(exported['slot_gaps'],3)
            self.assertEqual(exported['max_ma'],50)
            reader.close()

    def test_legacy_single_csf4_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'legacy.csf4'
            path.write_bytes(encode_frame(1,0,100,0,0,[16,-32,48])+encode_frame(3,1,100))
            data,_=open_history(path)
            self.assertEqual(data['vshunt_code'].tolist(),[16,-32,48])
            self.assertEqual(data['time_s'].tolist(),[0,.0001,.0002])

    def test_size_rotation_preserves_frames_and_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'a','profile':{'sample_rate_hz':10000,'sample_period_us':100}},
                                   ArchiveOptions(25,0,0,'none'))
            writer.segment_limit_bytes=900
            writer.block_samples=64
            submit_samples(writer,1024)
            writer.finish({'acquisition_finished':True,'session_end_verified':True})
            wait_for(writer.done)
            self.assertIsNone(writer.error)
            reader=ArchiveReader(writer.manifest_path)
            self.assertGreater(len(reader.manifest['segments']),2)
            self.assertEqual(reader.sample_count,1024)
            self.assertEqual(reader.manifest['status'],'COMPLETE')
            self.assertEqual(len(reader.raw_range(0,1)),1024)
            self.assertEqual(reader.range_stats(0,.1023)['min'],-8269/16)
            self.assertEqual(reader.range_stats(.045,.06)['count'],151)
            for segment in reader.manifest['segments']:
                self.assertTrue(segment['closed'])
                self.assertEqual(segment['size_bytes'],(reader.root/segment['file']).stat().st_size)
            index_before=writer.index_path.read_bytes()
            audit=verify_archive_segments(writer.manifest_path)
            self.assertTrue(audit['crc_ok'])
            self.assertTrue(audit['session_complete'])
            self.assertEqual(audit['checked_segments'],len(reader.manifest['segments']))
            self.assertEqual(writer.index_path.read_bytes(),index_before)
            reader.close()

    def test_indexed_interval_statistics_match_raw_random_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'ranges'},ArchiveOptions(25,0,0,'none'))
            writer.segment_limit_bytes=900
            writer.block_samples=64
            submit_samples(writer,1024)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            reader=ArchiveReader(writer.manifest_path)
            rng=np.random.default_rng(871)
            for x1,x2 in rng.uniform(-.01,.11,(100,2)):
                raw=reader.raw_range(x1,x2)
                stats=reader.range_stats(x1,x2)
                self.assertEqual(stats['count'],len(raw))
                if len(raw):
                    codes=raw['vshunt_code'].astype(np.int64)
                    self.assertEqual(stats['min'],int(codes.min())/16)
                    self.assertEqual(stats['max'],int(codes.max())/16)
                    self.assertAlmostEqual(stats['mean'],float(codes.mean())/16)
                    self.assertAlmostEqual(stats['rms'],float(np.sqrt(np.mean(codes.astype(float)**2)))/16)
                    self.assertEqual(stats['first_slot'],int(raw['slot'][0]))
                    self.assertEqual(stats['last_slot'],int(raw['slot'][-1]))
            reader.close()

    def test_time_rotation_and_tim2_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'roll','profile':{'sample_rate_hz':10000,'sample_period_us':100}},
                                   ArchiveOptions(25,1,0,'none'))
            writer.block_samples=64
            submit_samples(writer,240,period=10000,first_stamp=0xfffffff0)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            reader=ArchiveReader(writer.manifest_path)
            self.assertGreater(len(reader.manifest['segments']),1)
            data=reader.raw_range(0,3)
            self.assertEqual(len(data),240)
            self.assertEqual(int(data['tim2_read_us'][0]),0xfffffff0)
            self.assertLess(int(data['tim2_read_us'][1]),int(data['tim2_read_us'][0]))
            self.assertAlmostEqual(float(data['time_s'][-1]),2.39)
            self.assertEqual(writer.snapshot()['acquisition_elapsed_us'],2390000)
            self.assertEqual(reader.snapshot()['acquisition_elapsed_us'],2390000)
            self.assertEqual(reader.range_stats(.95,1.05)['count'],11)
            reader.close()

    def test_accelerated_three_hour_timeline_crosses_two_tim2_wraps(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'three_hours'},
                                   ArchiveOptions(25,3600,0,'none'))
            writer.block_samples=1
            decoder=FrameDecoder()
            for slot in range(1081):
                stamp=(slot*10000000)&0xffffffff
                frame=decoder.feed(encode_frame(1,slot,100,slot,stamp,[slot%100]))[0]
                raw=np.array([(slot,stamp,slot%100,slot*10.)],RAW_DTYPE)
                while writer.queue.qsize()>128:
                    time.sleep(.001)
                self.assertTrue(writer.submit(raw,(frame,)))
            end=decoder.feed(encode_frame(3,1081,100))[0]
            writer.submit(np.empty(0,RAW_DTYPE),(end,))
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            reader=ArchiveReader(writer.manifest_path)
            self.assertGreaterEqual(len(reader.manifest['segments']),3)
            self.assertEqual(reader.sample_count,1081)
            self.assertEqual(reader.last_time_s,10800)
            self.assertEqual(reader.snapshot()['acquisition_elapsed_us'],10800000000)
            self.assertEqual(reader.range_stats(3500,3700)['count'],21)
            self.assertLessEqual(reader.locate_slot(720)['first_slot'],720)
            self.assertIsNotNone(reader.locate_time(7200))
            reader.close()
            writer.index_path.unlink()
            rebuilt=ArchiveReader(writer.manifest_path)
            self.assertEqual(rebuilt.last_time_s,10800)
            self.assertEqual(rebuilt.snapshot()['acquisition_elapsed_us'],10800000000)
            rebuilt.close()

    def test_stop_freezes_measurement_and_wall_clocks(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'clock'},ArchiveOptions(25,0,0,'none'),capacity=1024)
            submit_samples(writer,16280)
            writer.finish({'session_end_verified':True})
            stopped=writer.snapshot()
            self.assertEqual(stopped['acquisition_elapsed_us'],1627900)
            wait_for(writer.done)
            first=writer.snapshot()
            time.sleep(.07)
            second=writer.snapshot()
            self.assertEqual(first['status'],'COMPLETE')
            for field in ('acquisition_elapsed_us','recording_wall_elapsed_s','finalization_duration_s',
                          'samples','last_slot','slot_gaps','initial_slot_gaps','elapsed_s','rate_bytes_s'):
                self.assertEqual(first[field],second[field],field)
            self.assertEqual(writer.last_slot,16279)
            self.assertEqual(second['last_slot'],16279)
            manifest=json.loads(writer.manifest_path.read_text(encoding='utf-8'))
            self.assertEqual(manifest['acquisition_elapsed_us'],1627900)
            self.assertEqual(manifest['sample_count'],16280)
            self.assertEqual(manifest['last_sample_time_us'],1627900)
            self.assertLessEqual(manifest['started_utc'],manifest['finished_utc'])
            self.assertLessEqual(manifest['finished_utc'],manifest['finalized_utc'])
            self.assertGreaterEqual(manifest['recording_wall_elapsed_s'],0)
            self.assertGreaterEqual(manifest['finalization_duration_s'],0)
            with ArchiveReader(writer.manifest_path) as reader:
                self.assertEqual(reader.snapshot()['acquisition_elapsed_us'],1627900)
                self.assertEqual(reader.sample_count,16280)

    def test_measurement_freezes_while_archive_is_finalizing(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'finalizing'},ArchiveOptions(25,0,0,'none'))
            entered=threading.Event(); release=threading.Event()
            original=writer._manifest
            def held_manifest():
                if writer.status=='FINALIZING' and not entered.is_set():
                    entered.set()
                    release.wait(3)
                original()
            writer._manifest=held_manifest
            try:
                submit_samples(writer,64)
                writer.finish({'session_end_verified':True})
                self.assertTrue(entered.wait(3))
                before=writer.snapshot()
                time.sleep(.04)
                during=writer.snapshot()
                self.assertEqual(before['status'],'FINALIZING')
                self.assertEqual(before['acquisition_elapsed_us'],6300)
                self.assertEqual(before['acquisition_elapsed_us'],during['acquisition_elapsed_us'])
                self.assertEqual(before['samples'],during['samples'])
                self.assertEqual(before['last_slot'],during['last_slot'])
                self.assertGreater(during['recording_wall_elapsed_s'],before['recording_wall_elapsed_s'])
            finally:
                release.set()
            wait_for(writer.done)
            finished=writer.snapshot()
            time.sleep(.04)
            self.assertEqual(finished,writer.snapshot())

    def test_empty_and_disk_error_clocks_are_stable(self):
        for reserve,expected in ((0,'PARTIAL'),(1048576,'DISK_ERROR')):
            with self.subTest(status=expected),tempfile.TemporaryDirectory() as directory:
                with patch('current_sensor.archive.shutil.disk_usage',return_value=SimpleNamespace(free=0)) if reserve else patch('current_sensor.archive.shutil.disk_usage',wraps=shutil.disk_usage):
                    writer=ArchiveRecorder(directory,{'source_session':'empty'},ArchiveOptions(25,0,reserve,'none'))
                    if reserve:
                        submit_samples(writer,64,with_end=False)
                    else:
                        submit_samples(writer,0)
                    writer.finish({'session_end_verified':not reserve})
                    wait_for(writer.done)
                before=writer.snapshot()
                time.sleep(.03)
                after=writer.snapshot()
                self.assertEqual(before['status'],expected)
                self.assertEqual(before['acquisition_elapsed_us'],0 if not reserve else 6300)
                self.assertEqual(before,after)
                self.assertEqual(json.loads(writer.manifest_path.read_text(encoding='utf-8'))
                                 ['acquisition_elapsed_us'],before['acquisition_elapsed_us'])

    def test_old_manifest_and_recovery_derive_measurement_from_index(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'legacy_duration'},ArchiveOptions(25,0,0,'none'))
            submit_samples(writer,128)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            manifest=json.loads(writer.manifest_path.read_text(encoding='utf-8'))
            for field in ('acquisition_elapsed_us','first_sample_time_us','last_sample_time_us',
                          'recording_wall_elapsed_s','finalization_duration_s','finished_utc','finalized_utc'):
                manifest.pop(field,None)
            writer.manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
            with ArchiveReader(writer.manifest_path) as old:
                self.assertEqual(old.snapshot()['acquisition_elapsed_us'],12700)
                self.assertIsNone(old.snapshot()['recording_wall_elapsed_s'])
            writer.index_path.unlink()
            with ArchiveReader(writer.manifest_path) as recovered:
                self.assertTrue(recovered.manifest['recovered'])
                self.assertEqual(recovered.manifest['acquisition_elapsed_us'],12700)
                self.assertEqual(recovered.snapshot()['acquisition_elapsed_us'],12700)
                self.assertIsNone(recovered.manifest.get('finished_utc'))
                self.assertIsNotNone(recovered.manifest['recovered_utc'])

    def test_compression_and_index_recovery(self):
        for method in ('zlib1','zlib3'):
            with self.subTest(method=method),tempfile.TemporaryDirectory() as directory:
                writer=ArchiveRecorder(directory,{'source_session':'z'},ArchiveOptions(25,0,0,method))
                writer.block_samples=96
                submit_samples(writer,512)
                writer.finish({'session_end_verified':True})
                wait_for(writer.done)
                reader=ArchiveReader(writer.manifest_path)
                self.assertEqual(reader.sample_count,512)
                self.assertEqual(int(reader.raw_range(0,1)['vshunt_code'][256]),-8269)
                reader.close()
                writer.index_path.unlink()
                restored=ArchiveReader(writer.manifest_path)
                self.assertEqual(restored.sample_count,512)
                self.assertTrue(restored.manifest['recovered'])
                self.assertEqual(restored.range_stats(0,.0511)['count'],512)
                restored.close()

    def test_corrupt_index_crc_rebuilds_from_unchanged_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'index_bad'},ArchiveOptions(25,0,0,'none'))
            submit_samples(writer,256)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            segment=writer.root/writer.segments[0]['file']
            before=segment.read_bytes()
            bad=bytearray(writer.index_path.read_bytes())
            bad[8]^=0x80
            writer.index_path.write_bytes(bad)
            reader=ArchiveReader(writer.manifest_path)
            self.assertTrue(reader.manifest['recovered'])
            self.assertEqual(reader.manifest['status'],'COMPLETE')
            self.assertEqual(reader.sample_count,256)
            self.assertEqual(segment.read_bytes(),before)
            reader.close()

    def test_corrupt_last_frame_is_partial_and_earlier_samples_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'bad'},ArchiveOptions(25,0,0,'none'))
            writer.block_samples=32
            submit_samples(writer,128)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            last=writer.root/writer.segments[-1]['file']
            with last.open('ab') as stream:
                stream.write(b'CSF4\x01\x01')
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertTrue(reader.manifest['partial'])
            self.assertTrue(reader.manifest['recovery_errors'])
            self.assertEqual(reader.sample_count,128)
            reader.close()

    def test_corrupt_compressed_tail_keeps_earlier_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'compressed_bad'},ArchiveOptions(25,0,0,'zlib1'))
            writer.block_samples=32
            submit_samples(writer,128)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            segment=writer.root/writer.segments[-1]['file']
            content=bytearray(segment.read_bytes())
            content[-1]^=0x01
            segment.write_bytes(content)
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertTrue(reader.manifest['partial'])
            self.assertEqual(reader.sample_count,128)
            self.assertTrue(any('CRC' in error for error in reader.manifest['recovery_errors']))
            self.assertEqual(segment.read_bytes(),content)
            reader.close()

    def test_segment_checksum_is_checked_when_rebuilding_index(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'checksum'},ArchiveOptions(25,0,0,'none'))
            submit_samples(writer,64)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            manifest=json.loads(writer.manifest_path.read_text(encoding='utf-8'))
            manifest['segments'][0]['checksum_crc32']^=1
            writer.manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
            self.assertFalse(verify_archive_segments(writer.manifest_path)['crc_ok'])
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertTrue(reader.manifest['partial'])
            self.assertTrue(any('SEGMENT_CHECKSUM_MISMATCH' in value
                                for value in reader.manifest['recovery_errors']))
            reader.close()

    def test_missing_middle_segment_keeps_later_time_anchor_and_marks_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=ArchiveRecorder(directory,{'source_session':'missing'},ArchiveOptions(25,0,0,'none'))
            writer.segment_limit_bytes=900
            writer.block_samples=64
            submit_samples(writer,1024)
            writer.finish({'session_end_verified':True})
            wait_for(writer.done)
            missing=writer.segments[1]
            (writer.root/missing['file']).unlink()
            writer.index_path.unlink()
            reader=ArchiveReader(writer.manifest_path)
            self.assertTrue(reader.manifest['time_continuity_uncertain'])
            self.assertTrue(reader.manifest['partial'])
            self.assertLess(reader.sample_count,1024)
            self.assertEqual(reader.last_time_s,.1023)
            self.assertFalse(reader.plot_range(0,.1023,pixels=1).connect.all())
            reader.close()


if __name__=='__main__':
    unittest.main()
