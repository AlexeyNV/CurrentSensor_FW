import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from tools.hil.accuracy_analysis import DTYPE
from tools.hil.raw_forensics import xor_details,event_details,sign_cross,read_raw_csv
from tools.hil.verify_capture import verify


class ForensicsTests(unittest.TestCase):
    def test_xor_positions(self):
        self.assertEqual(xor_details(0xFFFED0,0x7FFED0),dict(xor='0x800000',hamming=1,bits=[23]))
        self.assertEqual(xor_details(0xFFFED0,0xFDFED0)['bits'],[17])
        self.assertEqual(xor_details(0x0003C0,0x0203C0)['hamming'],1)

    def test_real_neighbours_not_hypothetical_words(self):
        data=np.zeros(43,dtype=DTYPE)
        data['sequence']=np.arange(43); data['elapsed_us']=np.arange(43)*84
        data['raw24']=0x10; data['code']=1; data['raw_ma']=.0625
        data['raw24'][21]=0x020810; data['code'][21]=8321; data['raw_ma'][21]=8321/16
        bad=np.zeros(43,dtype=bool);bad[21]=True
        event=event_details(data,21,bad)
        self.assertEqual(event['previous']['raw24'],'0x000010')
        self.assertEqual(event['following']['hamming'],2)
        self.assertEqual(event['previous']['bits'],[11,17])
        self.assertEqual(len(event['neighbours']),40)
        self.assertEqual(event['run_samples'],1)
        self.assertEqual(event['dt_before_us'],84)

    def test_strict_sign_crossing(self):
        self.assertTrue(sign_cross(-1,1)); self.assertTrue(sign_cross(1,-1))
        self.assertFalse(sign_cross(0,1));self.assertFalse(sign_cross(-1,-1))

    def test_early_raw_archive_without_calibrated_column(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'old.csv'
            original='sequence,tim2_us,elapsed_us64,raw24,vshunt_code,current_ma\n0,42,0,0xFFFED0,-19,-1.1875\n'
            source.write_text(original)
            data=read_raw_csv(source,root/'cache.bin')
            try:
                self.assertEqual(data[0]['raw24'],0xFFFED0)
                self.assertEqual(data[0]['raw_ma'],-1.1875)
                self.assertTrue(np.isnan(data[0]['cal_ma']))
            finally: data._mmap.close()
            self.assertEqual(source.read_text(),original)

    def test_replay_output_preserves_sources_and_wrap(self):
        class Decoder:
            ended=True;config=(0xAA68,0)
            crc_errors=format_errors=frame_gaps=slot_gaps=code_errors=resync_bytes=0
            def feed(self,block):
                return [dict(type=1,protocol_version=2,samples=[(0,0xFFFFFFF0,-19),(1,68,-19)],raw24=[0xFFFED0]*2),
                        dict(type=3,stats=dict(ADC_SAMPLES=2,USB_SAMPLES_SENT=2))]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source';source.mkdir(); output=root/'derived/verification.json'
            raw=source/'raw.csv'
            with raw.open('w',newline='') as f:
                w=csv.writer(f);w.writerow(('sequence','tim2_us','elapsed_us64','raw24','vshunt_code','current_ma'))
                for seq,stamp in enumerate((0xFFFFFFF0,68)):w.writerow((seq,stamp,seq*84,'0xFFFED0',-19,-19/16))
            (source/'capture_summary.json').write_text(json.dumps(dict(csv=str(raw),reports=dict(native={}))))
            (source/'cdc.log').write_text('1.0 BINARY 00\n')
            originals={p.name:p.read_bytes() for p in source.iterdir()}
            with patch('tools.hil.verify_capture.FrameDecoder',Decoder),patch('tools.hil.verify_capture.native_verdict',return_value=([],[])):
                result=verify(source,output)
            self.assertEqual(result['errors'],[]);self.assertEqual(result['samples'],2)
            self.assertEqual(originals,{p.name:p.read_bytes() for p in source.iterdir()})
            self.assertTrue(output.exists())


if __name__=='__main__': unittest.main()
