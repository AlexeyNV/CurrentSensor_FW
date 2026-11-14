import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
import numpy as np
import serial
from current_sensor.device_worker import DeviceWorker
from current_sensor.device_protocol import DeviceProtocol
from current_sensor.mock_device import MockSerial
from current_sensor.recorder import Recorder, open_history
from current_sensor.data import with_filters
from current_sensor.archive import ArchiveReader
from test_numeric import raw


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('condition timed out')


class IntegrationTests(unittest.TestCase):
    def worker(self, **options):
        ports = []
        mock_class = options.pop('mock_class', MockSerial)
        def factory(*a, **kw):
            port = mock_class(**options)
            ports.append(port)
            return port
        worker = DeviceWorker(factory)
        def cleanup():
            worker.request('shutdown')
            worker.join(8)
            self.assertFalse(worker.is_alive())
            self.assertTrue(all(p.closed for p in ports))
            for spool in worker.spool_sessions:
                spool.delete()
        self.addCleanup(cleanup)
        worker.request('connect', port='MOCK')
        wait_for(lambda: worker.state == 'idle')
        return worker, ports

    def start(self, worker, profile='FAST'):
        worker.request('start', profile=profile, mode='dma')
        wait_for(lambda: worker.service.count > 100)

    def stop(self, worker):
        worker.request('stop')
        wait_for(lambda: worker.state == 'idle')
        wait_for(lambda: worker.recorder is None)

    def test_profiles_recording_diagnostics_and_reconnect(self):
        worker, ports = self.worker()
        self.assertEqual(len(worker.info['profiles']), 4)
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start', directory=directory)
            self.start(worker, 'BALANCED')
            wait_for(lambda: worker.service.count > 800)
            self.stop(worker)
            metadata = json.loads(next(Path(directory).glob('*.json')).read_text())
            self.assertTrue(metadata['completed'])
            self.assertTrue(metadata['health']['session_verified'])
            self.assertEqual(metadata['recorded_samples'], worker.service.count)
            self.assertEqual(metadata['profile']['acq_mode'], 'DMA')
            self.assertIn('stream anomaly', metadata['diagnostics'])
            history, _ = open_history(next(Path(directory).glob('*.json')))
            self.assertIsInstance(history, np.memmap)
            np.testing.assert_array_equal(history['vshunt_code'], worker.service.snapshot()['vshunt_code'])
        worker.request('disconnect')
        wait_for(lambda: worker.state == 'disconnected')
        worker.request('connect', port='MOCK')
        wait_for(lambda: len(ports) == 2 and worker.state == 'idle')
        self.assertTrue(ports[0].closed)

    def test_fw_044_debug_gui_start_stop_protocol(self):
        class DebugMockSerial(MockSerial):
            def _profile_fields(self):
                fields = super()._profile_fields()
                fields['FW'] = '0.4.4-debug'
                return fields

        worker, ports = self.worker(mock_class=DebugMockSerial)
        self.assertEqual(worker.info['profile']['firmware_version'], '0.4.4-debug')
        self.start(worker, 'BALANCED')
        self.stop(worker)
        self.assertTrue(worker.decoder.ended)
        self.assertEqual(worker.final_stats['USB_SAMPLES_SENT'], worker.service.count)
        self.assertEqual(ports[0].writes.count('stream stop'), 1)
        self.assertEqual(worker.state, 'idle')

    def test_stop_write_timeout_does_not_retry_during_disconnect(self):
        class StopTimeoutMock(MockSerial):
            def write(self, data):
                if data == b'stream stop\n':
                    self.writes.append('stream stop')
                    raise serial.SerialTimeoutException('Write timeout')
                return super().write(data)

        worker, ports = self.worker(mock_class=StopTimeoutMock)
        self.start(worker)
        worker.request('stop')
        wait_for(lambda: worker.state == 'disconnected')
        self.assertTrue(ports[0].closed)
        self.assertEqual(ports[0].writes.count('stream stop'), 1)
        self.assertFalse(worker.decoder.ended)
        worker.request('connect', port='MOCK')
        wait_for(lambda: len(ports) == 2 and worker.state == 'idle')
        self.assertTrue(ports[0].closed)

    def test_close_failure_is_not_reported_as_disconnected(self):
        class CloseOnceMock(MockSerial):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise OSError('close blocked')
                super().close()

        worker, ports = self.worker(mock_class=CloseOnceMock)
        worker.request('disconnect')
        wait_for(lambda: worker.state == 'disconnect_failed')
        self.assertFalse(ports[0].closed)
        self.assertIs(worker.port, ports[0])
        worker.request('disconnect')
        wait_for(lambda: worker.state == 'disconnected')
        self.assertTrue(ports[0].closed)
        self.assertEqual(ports[0].close_calls, 2)

    def test_repeated_start_stop_then_disconnect(self):
        worker, ports = self.worker()
        for _ in range(2):
            self.start(worker)
            self.stop(worker)
            self.assertTrue(worker.decoder.ended)
        worker.request('disconnect')
        wait_for(lambda: worker.state == 'disconnected')
        self.assertTrue(ports[0].closed)

    def test_blocked_serial_close_produces_watchdog_stack(self):
        release = threading.Event()

        class BlockedCloseMock(MockSerial):
            def close(self):
                release.wait(4)
                super().close()

        worker, ports = self.worker(mock_class=BlockedCloseMock)
        try:
            worker.request('disconnect')
            wait_for(lambda: worker.state == 'disconnecting')
            wait_for(lambda: 'blocked_stage=SERIAL_CLOSE_STARTED' in
                     worker.trace.path.read_text(encoding='utf-8'), timeout=4)
            self.assertFalse(ports[0].closed)
        finally:
            release.set()
        wait_for(lambda: worker.state == 'disconnected')
        self.assertTrue(ports[0].closed)

    def test_long_archive_uses_single_wire_store_and_preserves_session(self):
        worker,_=self.worker()
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start',directory=directory,mode='long',
                           archive_options={'segment_mib':25,'reserve_mib':0})
            wait_for(lambda:worker.record_directory==directory)
            time.sleep(.04)
            self.assertIsNone(worker.archive)
            self.start(worker)
            self.assertIsNone(worker.spool)
            self.assertTrue(worker.detector.analyzer.sparse)
            wait_for(lambda: worker.service.count>700)
            self.stop(worker)
            archive=worker.archive
            self.assertIsNotNone(archive)
            self.assertIsNone(archive.error)
            reader=ArchiveReader(archive.manifest_path)
            self.assertEqual(reader.sample_count,worker.service.count)
            self.assertAlmostEqual(reader.snapshot()['acquisition_elapsed_s'],
                                   worker.service.summary()['time_s'],places=6)
            self.assertEqual(reader.manifest['status'],'COMPLETE')
            self.assertEqual(reader.manifest['source_session'],worker.session_metadata['source_session'])
            self.assertTrue(reader.manifest['candidate_events_file'])
            np.testing.assert_array_equal(reader.raw_range(0,reader.last_time_s)['vshunt_code'],
                                          worker.service.snapshot()['vshunt_code'])
            self.assertFalse(list(Path(directory).glob('*.raw.bin')))
            self.assertFalse(list(Path(directory).glob('*.csv')))
            reader.close()

    def test_archive_disk_failure_does_not_stop_live_acquisition(self):
        worker,_=self.worker()
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start',directory=directory,mode='long',
                           archive_options={'segment_mib':25,'reserve_mib':1048576})
            self.start(worker)
            wait_for(lambda:worker.archive is not None and worker.archive.done.is_set())
            self.assertIn('ARCHIVE_DISK_RESERVE',worker.archive.error)
            self.assertEqual(worker.state,'streaming')
            before=worker.service.count
            wait_for(lambda:worker.service.count>before+100)
            self.assertGreater(len(worker.service.snapshot()),0)
            self.stop(worker)
            reader=ArchiveReader(worker.archive.manifest_path)
            self.assertEqual(reader.manifest['status'],'DISK_ERROR')
            reader.close()

    def test_usb_disconnect_finalizes_archive_with_fixed_elapsed(self):
        worker,ports=self.worker()
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start',directory=directory,mode='long',
                           archive_options={'segment_mib':25,'reserve_mib':0})
            self.start(worker)
            wait_for(lambda:worker.service.count>500)
            archive=worker.archive
            ports[0].closed=True
            wait_for(lambda:worker.state=='disconnected' and archive.done.is_set())
            before=archive.snapshot()
            time.sleep(.05)
            after=archive.snapshot()
            self.assertEqual(before['status'],'PARTIAL')
            self.assertEqual(before['acquisition_elapsed_us'],after['acquisition_elapsed_us'])
            self.assertEqual(before['recording_wall_elapsed_s'],after['recording_wall_elapsed_s'])
            self.assertEqual(before['samples'],after['samples'])
            with ArchiveReader(archive.manifest_path) as reader:
                self.assertEqual(reader.snapshot()['acquisition_elapsed_us'],before['acquisition_elapsed_us'])
                self.assertEqual(reader.sample_count,before['samples'])

    def test_long_archive_marks_usb_crc_loss_as_partial(self):
        worker,_=self.worker(corrupt_at=2)
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start',directory=directory,mode='long',
                           archive_options={'segment_mib':25,'reserve_mib':0})
            self.start(worker)
            self.stop(worker)
            reader=ArchiveReader(worker.archive.manifest_path)
            self.assertEqual(reader.sample_count,worker.service.count)
            self.assertEqual(reader.manifest['status'],'PARTIAL')
            self.assertGreater(reader.manifest['transport']['crc_errors'],0)
            reader.close()

    def test_midstream_long_archive_stop_is_prompt_and_does_not_stop_mcu(self):
        worker,_=self.worker()
        self.start(worker)
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start',directory=directory,mode='long',
                           archive_options={'segment_mib':25,'reserve_mib':0})
            wait_for(lambda:worker.archive is not None and worker.archive.samples>100)
            started=time.monotonic()
            worker.request('record_stop')
            wait_for(lambda:worker.recorder is None,timeout=3)
            self.assertLess(time.monotonic()-started,3)
            self.assertEqual(worker.state,'streaming')
            self.assertEqual(worker.archive.status,'PARTIAL')
        self.stop(worker)

    def test_midstream_record_and_stop_independent(self):
        worker, _ = self.worker()
        self.start(worker)
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start', directory=directory)
            wait_for(lambda: worker.recorder is not None and worker.recorder.samples > 100)
            worker.request('record_stop')
            wait_for(lambda: worker.recorder is None)
            self.assertEqual(worker.state, 'streaming')
            metadata = json.loads(next(Path(directory).glob('*.json')).read_text())
            self.assertGreater(metadata['first_recorded_slot'], 0)
            self.assertLess(metadata['recorded_samples'], worker.service.count)
        self.stop(worker)

    def test_crc_loss_is_transport_error(self):
        worker, _ = self.worker(corrupt_at=2)
        self.start(worker)
        self.stop(worker)
        self.assertEqual(worker.decoder.crc_errors, 1)
        self.assertTrue(worker.snapshot_report()['health']['transport_errors'])
        self.assertFalse(worker.snapshot_report()['health']['acquisition_losses'])

    def test_missing_slot_is_acquisition_loss(self):
        worker, _ = self.worker(skip_at=100)
        self.start(worker)
        self.stop(worker)
        self.assertTrue(worker.snapshot_report()['health']['acquisition_losses'])
        self.assertFalse(worker.snapshot_report()['health']['transport_errors'])
        self.assertEqual(worker.service.running_gaps, 1)

    def test_usb_unplug_closes_com_and_recording(self):
        worker, ports = self.worker(drop_after=300)
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start', directory=directory)
            self.start(worker)
            wait_for(lambda: worker.state == 'disconnected' and worker.recorder is None)
            self.assertTrue(ports[0].closed)
            metadata = json.loads(next(Path(directory).glob('*.json')).read_text())
            self.assertFalse(metadata['session_end_verified'])
            self.assertIn('unplug', metadata['disconnect_reason'])

    def test_cal_errors_and_stream_command_guard(self):
        port = MockSerial()
        protocol = DeviceProtocol(port)
        self.assertEqual(protocol.command('ping'), {'PING': 'pong'})
        self.assertIn('FW', protocol.command('info'))
        with self.assertRaisesRegex(RuntimeError, 'OUT_OF_RANGE'):
            protocol.command('cal set offset_ua 100001')
        self.assertEqual(protocol.command('cal show')['OFFSET_UA'], '0')
        with self.assertRaises(ValueError):
            protocol.command('cal show\ncal save')
        worker, ports = self.worker()
        self.start(worker)
        worker.request('text', command='cal save')
        time.sleep(.03)
        self.assertNotIn('cal save', ports[0].writes)
        self.stop(worker)

    def test_future_profile_is_discovered_not_hardcoded(self):
        from unittest.mock import patch
        from current_sensor.mock_device import MOCK_PROFILES
        with patch.dict(MOCK_PROFILES, {'FUTURE_MODE': (2000, 500, 280, 0xAAE8)}):
            protocol = DeviceProtocol(MockSerial())
            self.assertIn('FUTURE_MODE', [p['name'] for p in protocol.connect_info()['profiles']])
            self.assertEqual(protocol.select('FUTURE_MODE')['sample_period_us'], 500)

    def test_disk_failure_and_bounded_recorder_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory)/'file'
            bad.write_text('not a directory')
            recorder = Recorder(bad, {'profile': {'sample_period_us':100}})
            recorder.start(); recorder.join(3)
            self.assertTrue(recorder.done.is_set())
            self.assertIn('ERROR', recorder.error)
            recorder = Recorder(directory, {'profile': {'sample_period_us':100}}, capacity=1)
            data = with_filters(raw([0,1,500000,-1]))
            self.assertTrue(recorder.submit(data))
            self.assertFalse(recorder.submit(data))
            recorder.start(); recorder.join(3)
            self.assertEqual(recorder.dropped_samples, 4)
            self.assertFalse(json.loads(recorder.metadata_path.read_text())['completed'])
            original = recorder.base.with_suffix('.csv').read_bytes()
            history, _ = open_history(recorder.metadata_path)
            np.testing.assert_array_equal(history['vshunt_code'], data['vshunt_code'])
            self.assertEqual(original, recorder.base.with_suffix('.csv').read_bytes())

    def test_replay_preserves_frames(self):
        worker, _ = self.worker()
        with tempfile.TemporaryDirectory() as directory:
            worker.request('record_start', directory=directory)
            self.start(worker)
            self.stop(worker)
            expected = worker.service.snapshot()['vshunt_code'].copy()
            replay, _ = self.worker(replay=next(Path(directory).glob('*.csf4')))
            replay.request('start', profile='FAST', mode='dma')
            wait_for(lambda: replay.service.count > 0 and replay.state == 'idle')
            self.assertEqual(replay.decoder.frame_gaps, 0)
            self.assertEqual(replay.decoder.crc_errors, 0)
            np.testing.assert_array_equal(replay.service.snapshot()['vshunt_code'], expected)


if __name__ == '__main__':
    unittest.main()
