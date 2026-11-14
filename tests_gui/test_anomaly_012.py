import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
import numpy as np
from current_sensor.anomaly_diagnostics import CandidateAnalyzer, DetectorRules, analyze_history
from current_sensor.data import RAW_DTYPE
from current_sensor.device_worker import DeviceWorker
from current_sensor.export import ExportSource, export_raw
from current_sensor.mock_device import MockSerial
from current_sensor.range_stats import RawRangeIndex
from current_sensor.recorder import open_history


def raw(codes, first=0):
    result=np.zeros(len(codes),RAW_DTYPE)
    result['slot']=np.arange(first,first+len(codes),dtype=np.uint32)
    result['tim2_read_us']=np.arange(len(codes),dtype=np.uint32)*100
    result['time_s']=np.arange(len(codes))*0.0001
    result['vshunt_code']=codes
    return result


def wait_for(predicate, timeout=8):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('timed out')


class CandidateTests(unittest.TestCase):
    def test_mcu_spi_bytes_and_neighbors_stay_separate_from_pc_candidates(self):
        from current_sensor.mcu_anomaly import parse_mcu_anomaly_report
        report={'ANOMALY_EVENTS':'1','ENTRY_0':
                'SLOT=8 TIM2_US=800 SPI_RX_BYTES=??,FD,FB,30 VSHUNT_RAW=0xFDFB30 '
                'VSHUNT_CODE=-8269 CODE_BIT_0x2000=1',
                'ENTRY_1':'SLOT=9 TIM2_US=900 SPI_RX_BYTES=11,FF,FB,30 '
                'VSHUNT_RAW=0xFFFB30 VSHUNT_CODE=-77 CODE_BIT_0x2000=1'}
        entries=parse_mcu_anomaly_report(report)
        self.assertEqual(len(entries),2)
        self.assertEqual(entries[0]['spi_rx_bytes'],[None,0xFD,0xFB,0x30])
        self.assertEqual(entries[0]['vshunt_raw'],0xFDFB30)
        self.assertEqual(entries[0]['next']['vshunt_code'],-77)
        self.assertEqual(entries[1]['previous']['slot'],8)
        self.assertEqual(report['ENTRY_0'].split()[0],'SLOT=8')

    def test_minus_8192_code_change_and_unchanged_raw(self):
        values=np.full(13,-77,np.int32)
        values[6]=-8269
        data=raw(values)
        before=data.tobytes()
        analyzer=analyze_history(data,'case')
        self.assertEqual(data.tobytes(),before)
        self.assertEqual(analyzer.event_count,1)
        event=analyzer.events[0]
        self.assertEqual((event['slot'],event['vshunt_code'],event['tim2_read_us']),(6,-8269,600))
        self.assertEqual(event['local_median_ma'],-77/16)
        self.assertEqual(event['deviation_ma'],-512)
        self.assertIn('MEDIAN5',event['rule'])
        self.assertEqual(len(event['neighbors']),5)
        self.assertEqual(analyzer.range_statistics(data,0,data['time_s'][-1])['valid_count'],12)

    def test_positive_negative_pulse_and_two_consecutive_candidates(self):
        values=np.zeros(30,np.int32)
        values[4]=900
        values[10]=-900
        values[20:22]=900
        analyzer=analyze_history(raw(values),'pulse',DetectorRules(50,10000,1000))
        self.assertEqual([event['slot'] for event in analyzer.events],[4,10,20,21])
        self.assertEqual(analyzer.event_count,4)
        self.assertEqual(analyzer.range_statistics(raw(values),0,.003)['excluded_count'],4)

    def test_packet_and_live_buffer_boundary(self):
        values=np.zeros(40,np.int32)
        values[11]=8192
        data=raw(values)
        analyzer=CandidateAnalyzer('boundary')
        for part in (data[:12],data[12:25],data[25:]):
            analyzer.feed(part)
        analyzer.finish()
        self.assertEqual([event['slot'] for event in analyzer.events],[11])
        # Simulate a live ring retaining only the later 20 samples.
        result=analyzer.range_statistics(data[20:],data['time_s'][20],data['time_s'][-1],base=20)
        self.assertEqual((result['valid_count'],result['excluded_count']),(20,0))

    def test_gap_blocks_median_but_preserves_absolute_rule(self):
        values=np.zeros(10,np.int32)
        values[5]=900
        data=raw(values)
        data['slot'][5:]+=3
        analyzer=analyze_history(data,'gap',DetectorRules(50,10000,1000))
        self.assertEqual(analyzer.event_count,0)
        absolute=analyze_history(data,'gap',DetectorRules(50,10,1000))
        self.assertEqual(absolute.event_count,1)
        self.assertEqual(absolute.events[0]['rule'],'ABSOLUTE')
        self.assertIsNone(absolute.events[0]['local_median_ma'])

    def test_step_rule_across_packets_and_gap_reset(self):
        values=np.zeros(12,np.int32)
        values[6:]=32000
        data=raw(values)
        rules=DetectorRules(5000,10000,100)
        analyzer=CandidateAnalyzer('step',rules)
        analyzer.feed(data[:6]); analyzer.feed(data[6:]); analyzer.finish()
        self.assertEqual([(e['slot'],e['rule']) for e in analyzer.events],[(6,'STEP')])
        data['slot'][6:]+=2
        self.assertEqual(analyze_history(data,'gap',rules).event_count,0)

    def test_raw_and_filtered_range_and_event_export(self):
        values=np.zeros(100,np.int32)
        values[40]=-8269
        values[55]=1600
        data=raw(values)
        analyzer=analyze_history(data,'case')
        with tempfile.TemporaryDirectory() as directory:
            source=ExportSource(data,{'source_session':'case','profile':{'sample_period_us':100}},
                                True,'HISTORY',RawRangeIndex.from_data(data),None,analyzer)
            result=export_raw(source,directory,'CURSORS',(data['time_s'][30],data['time_s'][60]))
            meta=json.loads(Path(result['path']).read_text(encoding='utf-8'))
            self.assertEqual(meta['sample_count'],31)
            self.assertEqual(meta['candidate_count'],2)
            self.assertEqual(meta['raw_without_candidates']['valid_count'],29)
            self.assertEqual(meta['raw_without_candidates']['excluded_count'],2)
            self.assertEqual(meta['raw_without_candidates']['slot_gaps'],meta['slot_gaps'])
            self.assertEqual(meta['min_ma'],-8269/16)
            with (Path(result['path']).parent/meta['files']['events']).open(newline='',encoding='utf-8') as handle:
                events=list(csv.DictReader(handle))
            self.assertEqual(len(events),2)
            self.assertEqual(int(events[0]['vshunt_code']),-8269)
            saved, _=open_history(result['path'])
            np.testing.assert_array_equal(saved['vshunt_code'],values[30:61])

    def test_incomplete_analysis_is_not_zero_events(self):
        data=raw(np.r_[np.zeros(50,np.int32),900,np.zeros(49,np.int32)])
        analyzer=CandidateAnalyzer('partial')
        analyzer.feed(data[:20])
        with tempfile.TemporaryDirectory() as directory:
            source=ExportSource(data,{'profile':{'sample_period_us':100}},True,'HISTORY',
                                RawRangeIndex.from_data(data),None,analyzer)
            # The analyzer is still active, so export waits briefly then marks incomplete.
            result=export_raw(source,directory)
            meta=json.loads(Path(result['path']).read_text(encoding='utf-8'))
            self.assertFalse(meta['detector_analysis_complete'])
            self.assertIsNone(meta['candidate_count'])
            self.assertLess(meta['raw_without_candidates']['analyzed_count'],len(data))

    def test_history_10m_streaming_and_range_index(self):
        with tempfile.TemporaryDirectory() as directory:
            filename=Path(directory)/'large.raw.bin'
            data=np.memmap(filename,RAW_DTYPE,mode='w+',shape=(10_000_000,))
            for start in range(0,len(data),100_000):
                end=min(len(data),start+100_000)
                data['slot'][start:end]=np.arange(start,end,dtype=np.uint32)
                data['time_s'][start:end]=np.arange(start,end)*.0001
                data['vshunt_code'][start:end]=0
            data['vshunt_code'][4_000_000]=8192
            analyzer=analyze_history(data,'large')
            self.assertEqual(analyzer.event_count,1)
            subset=data[3_000_000:5_000_000]
            stats=analyzer.range_statistics(subset,float(subset['time_s'][0]),float(subset['time_s'][-1]),3_000_000)
            self.assertEqual(stats['valid_count'],len(subset)-1)
            self.assertEqual(stats['excluded_count'],1)
            del subset,data


class SessionMetadataTests(unittest.TestCase):
    def test_detector_threshold_can_be_configured_for_next_session_while_disconnected(self):
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        try:
            worker.request('detector_rules',median_ma=75)
            wait_for(lambda:worker.detector_rules.median_ma==75)
            self.assertEqual(worker.state,'disconnected')
        finally:
            worker.request('shutdown')
            worker.join(5)

    def test_fast_balanced_precision_snapshots_are_immutable(self):
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        try:
            worker.request('connect',port='MOCK')
            wait_for(lambda:worker.state=='idle')
            self.assertEqual(worker.connection_profile['name'],'FAST')
            saved=[]
            for name in ('FAST','BALANCED','PRECISION'):
                worker.request('start',profile=name,mode='dma')
                wait_for(lambda:worker.state=='streaming' and worker.service.count>100)
                saved.append(worker.session_metadata)
                worker.request('stop')
                wait_for(lambda:worker.state=='idle')
                wait_for(lambda:worker.detector.done.is_set())
                self.assertEqual(len(worker.detector.analyzer.index.mask),worker.service.count)
            self.assertEqual([m['session_profile']['name'] for m in saved],
                             ['FAST','BALANCED','PRECISION'])
            self.assertEqual([m['connection_profile']['name'] for m in saved],['FAST']*3)
            self.assertEqual([m['device']['profile']['name'] for m in saved],
                             ['FAST','BALANCED','PRECISION'])
            self.assertEqual([m['export_profile']['sample_period_us'] for m in saved],
                             [100,200,400])
            self.assertEqual(len({m['source_session'] for m in saved}),3)
            self.assertEqual([m['mcu_anomaly_report']['SESSION_TYPE'] for m in saved],['STREAM']*3)
        finally:
            worker.request('shutdown')
            worker.join(10)
            for spool in worker.spool_sessions:
                if spool.done.is_set():
                    spool.delete()

    def test_legacy_json_profile_provenance(self):
        from current_sensor.session_metadata import normalize_legacy
        old={'profile':{'name':'FAST','sample_rate_hz':10000},
             'device':{'profile':{'name':'BALANCED','sample_rate_hz':5000}}}
        new=normalize_legacy(old)
        self.assertEqual(new['session_profile']['name'],'FAST')
        self.assertEqual(new['connection_profile']['name'],'BALANCED')
        self.assertIn('legacy',new['profile_semantics'])
        self.assertEqual(old['device']['profile']['name'],'BALANCED')

    def test_old_json_opens_with_annotated_profile_source(self):
        with tempfile.TemporaryDirectory() as directory:
            filename=Path(directory)/'old.raw.bin'
            filename.write_bytes(raw(np.zeros(6,np.int32)).tobytes())
            metadata={'profile':{'name':'FAST','sample_rate_hz':10000},
                      'device':{'profile':{'name':'BALANCED','sample_rate_hz':5000}},
                      'files':{'raw':filename.name}}
            description=Path(directory)/'old.json'
            description.write_text(json.dumps(metadata),encoding='utf-8')
            data,loaded=open_history(description)
            self.assertEqual(len(data),6)
            self.assertEqual(loaded['session_profile']['name'],'FAST')
            self.assertEqual(loaded['connection_profile']['name'],'BALANCED')
            self.assertIn('legacy',loaded['profile_semantics'])

    def test_ten_khz_demo_detector_keeps_full_count(self):
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        try:
            worker.request('connect',port='MOCK')
            wait_for(lambda:worker.state=='idle')
            worker.request('start',profile='FAST',mode='dma')
            wait_for(lambda:worker.service.count>=10000,timeout=5)
            worker.request('stop')
            wait_for(lambda:worker.state=='idle' and worker.detector.done.is_set())
            self.assertEqual(len(worker.detector.analyzer.index.mask),worker.service.count)
            self.assertIsNone(worker.detector.analyzer.error)
            self.assertEqual(worker.decoder.crc_errors,0)
        finally:
            worker.request('shutdown')
            worker.join(8)
            for spool in worker.spool_sessions:
                if spool.done.is_set():
                    spool.delete()


class CandidateUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6 import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self,predicate,timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail('GUI candidate condition timed out')

    def test_live_freeze_history_navigation_and_event_range(self):
        from unittest.mock import patch
        from PySide6 import QtWidgets
        from current_sensor.main_window import MainWindow
        with tempfile.TemporaryDirectory() as directory:
            worker=DeviceWorker(lambda *a,**kw:MockSerial())
            window=MainWindow(worker,demo=True,auto_port='DEMO',auto_start=True)
            window.directory.setText(directory)
            window.show()
            try:
                self.pump(lambda:worker.service.count>4500)
                window.tabs.setCurrentWidget(window.events_view)
                self.pump(lambda:len(window.events_view.events)>0)
                event=window.events_view.events[0]
                self.assertIn('MEDIAN5',event['rule'])
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.No):
                    window.navigate_candidate(event)
                self.assertFalse(window.follow)
                self.assertTrue(window.freeze.isChecked())
                self.assertEqual(worker.state,'streaming')
                window.save_candidate_range(event,.002)
                self.pump(lambda:window.export_jobs==0 and
                          bool(list(Path(directory).glob('capture_export_*.json'))))
                report=json.loads(next(Path(directory).glob('capture_export_*.json')).read_text(encoding='utf-8'))
                self.assertEqual(report['export_type'],'EVENT_RANGE')
                self.assertGreaterEqual(report['candidate_events_saved'],1)
                self.assertIn('mcu_anomaly_report',report)
                self.assertTrue((Path(directory)/report['files']['events']).exists())
                window.load_history(str(next(Path(directory).glob('capture_export_*.json'))))
                self.pump(lambda:window.history_loaded)
                self.assertIsNotNone(window.source_analyzer)
                self.assertGreater(window.source_analyzer.event_count,0)
            finally:
                window.close()
                self.pump(lambda:window.close_phase=='done',timeout=12)


if __name__=='__main__':
    unittest.main()
