import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import csv
import hashlib
import json
import tempfile
import time
import unittest
import struct
import zlib
from pathlib import Path
import numpy as np
from PySide6 import QtWidgets
from current_sensor.data import RAW_DTYPE,EnvelopeIndex
from current_sensor.export import ExportSource
from current_sensor.export import export_raw
from current_sensor.analysis_statistics import panel_statistics,nearest_sample
from current_sensor.anomaly_diagnostics import CandidateAnalyzer,DetectorRules,analyze_history
from current_sensor.recorder import open_history,Recorder
from current_sensor.main_window import MainWindow
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial


def raw(codes):
    data=np.zeros(len(codes),RAW_DTYPE)
    data['slot']=np.arange(len(codes));data['tim2_read_us']=np.arange(len(codes))*84
    data['time_s']=np.arange(len(codes))*.000084;data['vshunt_code']=codes
    return data


class Analysis060Tests(unittest.TestCase):
    def test_recording_without_end_is_explicitly_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=Recorder(directory,{'profile':{'sample_period_us':84}})
            recorder.start();recorder.submit(raw([1,2,3]));recorder.finish()
            recorder.join(3);self.assertFalse(recorder.is_alive())
            meta=json.loads(recorder.metadata_path.read_text(encoding='utf8'))
            self.assertEqual(meta['recording_integrity'],'PARTIAL')
            self.assertEqual(meta['recorded_samples'],3)
    def test_exact_histogram_negative_dc_and_sine(self):
        data=raw(np.full(1000,-1600))
        result=panel_statistics(ExportSource(data,{},True,'TEST'),0,data['time_s'][-1])
        self.assertEqual(result['raw']['median'],-100)
        self.assertEqual(result['raw']['rms'],100);self.assertEqual(result['raw']['std'],0)
        data=raw(np.rint(160*np.sin(np.arange(10000)*2*np.pi/10000)).astype(int))
        result=panel_statistics(ExportSource(data,{},True,'TEST'),0,data['time_s'][-1])
        self.assertAlmostEqual(result['raw']['rms'],10/2**.5,places=2)
        self.assertAlmostEqual(result['effective_rate_hz'],1e6/84)

    def test_local_mad_impulse_step_and_chunk_boundary(self):
        codes=np.zeros(500,np.int32);codes[100]=8192;codes[250:]=32000
        data=raw(codes);original=data.tobytes()
        analyzer=CandidateAnalyzer('new',DetectorRules(10,10000,1000,True))
        for part in np.array_split(data,13):analyzer.feed(part)
        analyzer.finish()
        self.assertEqual([e['index'] for e in analyzer.events],[100])
        self.assertEqual(analyzer.events[0]['classification'],'SUSPECT')
        self.assertEqual(analyzer.events[0]['threshold_ma'],10)
        self.assertEqual(data.tobytes(),original)

    def test_raw_and_excluded_statistics_remain_separate(self):
        data=raw(np.zeros(1000,np.int32));data['vshunt_code'][500]=8192
        analyzer=analyze_history(data,'test',DetectorRules(10,10000,1000,True))
        source=ExportSource(data,{},True,'TEST',analyzer=analyzer)
        result=panel_statistics(source,0,data['time_s'][-1])
        self.assertEqual(result['raw']['count'],1000)
        self.assertEqual(result['excluding_suspect']['count'],999)
        self.assertEqual(result['raw']['max'],512)
        self.assertEqual(result['excluding_suspect']['max'],0)

    def test_hil_csv_raw24_snap_and_source_immutability(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'hil.csv'
            path.write_text('sequence,tim2_us,elapsed_us64,raw24,vshunt_code,current_ma\n0,4294967280,0,0xFFFED0,-19,-1.1875\n1,68,84,0x66C950,421013,26313.3125\n')
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            data,meta=open_history(path);source=ExportSource(data,meta,True,'HISTORY')
            point=nearest_sample(source,.000080)
            self.assertEqual(point['raw24'],0x66C950);self.assertEqual(point['sequence'],1)
            self.assertEqual(float(data['time_s'][1]),.000084)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            saved=export_raw(source,Path(directory)/'export','CURSORS',(0,.000084),0,.000084)
            reopened,export_metadata=open_history(saved['path'])
            exact=nearest_sample(ExportSource(reopened,export_metadata,True,'HISTORY'),.000084)
            self.assertEqual(exact['raw24'],0x66C950)
            self.assertIsNone(nearest_sample(source,-1))
            self.assertIsNone(nearest_sample(source,1))

    def test_csf4_versions_crc_and_raw24_preserved(self):
        from tools.stream_capture import FrameDecoder
        for version in (1,2):
            raw24=0xFFFED9
            payload=struct.pack('<IH',raw24 if version==2 else (-19&0xFFFFFFFF),0)
            header=struct.pack('<4sBBHHHIIIHHI',b'CSF4',version,1,32,42,1,0,0,42,0 if version==2 else 100,1 if version==2 else 0,0)
            packet=header+payload;packet+=struct.pack('<I',zlib.crc32(packet)&0xFFFFFFFF)
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'source.csf4';path.write_bytes(packet)
                data,meta=open_history(path)
                sample=nearest_sample(ExportSource(data,meta,True,'HISTORY'),0)
                self.assertEqual(sample['code'],-19)
                self.assertEqual(sample['raw24'],raw24 if version==2 else None)
                self.assertEqual(path.read_bytes(),packet)
                broken=bytearray(packet);broken[-1]^=1
                decoder=FrameDecoder();self.assertEqual(decoder.feed(broken),[])
                self.assertGreater(decoder.crc_errors,0)


class Workbench060Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_visual_modes_preserve_raw_and_clean_leaves_gap(self):
        worker=DeviceWorker(lambda *a,**kw:MockSerial())
        window=MainWindow(worker,demo=True)
        try:
            data=raw(np.zeros(1000,np.int32));data['vshunt_code'][500]=8192
            original=data.tobytes();window.source=data;window.follow=False
            window.source_analyzer=analyze_history(data,'test',DetectorRules(10,10000,1000,True))
            trace=EnvelopeIndex(data).query(0,data['time_s'][-1],50)
            self.assertEqual(window.visual_mode.currentText(),'Marked')
            self.assertIn(512,trace.y)
            window.visual_mode.setCurrentText('Display Clean')
            clean,_=window.visual_traces([trace],np.empty(0,RAW_DTYPE))
            self.assertTrue(np.isnan(clean[0].y).any());self.assertTrue((~clean[0].connect).any())
            window.visual_mode.setCurrentText('RAW')
            shown,_=window.visual_traces([trace],np.empty(0,RAW_DTYPE))
            self.assertIn(512,shown[0].y);self.assertEqual(data.tobytes(),original)
            window.units.setCurrentText('A');self.assertEqual(window.unit_factor(),.001)
            self.assertEqual(window.timer.interval(),40)
        finally:
            window.close();deadline=time.monotonic()+5
            while worker.is_alive() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.005)
            self.app.processEvents();window.close()


if __name__=='__main__':unittest.main()
