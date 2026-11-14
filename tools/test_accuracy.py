import unittest
import csv
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
from tools.hil.accuracy_analysis import statistics,masks,compare,DTYPE,analyze
from tools.hil.accuracy_suite import run_stage,parse_current

class AccuracyTests(unittest.TestCase):
    def test_operator_units(self):
        self.assertEqual(parse_current('0,117А'),117)
        self.assertEqual(parse_current('117 mA'),117)
        with self.assertRaises(ValueError): parse_current('0.117')
    def test_dc_and_rms(self):
        result=statistics(np.full(100,-100.))
        self.assertEqual(result['mean_ma'],-100); self.assertEqual(result['rms_ma'],100)
        self.assertEqual(result['std_ma'],0)
    def test_sine_dc(self):
        x=100+10*np.sin(np.linspace(0,2*np.pi,10000,endpoint=False))
        self.assertAlmostEqual(statistics(x)['rms_ma'],np.sqrt(10000+50))
    def test_spike_and_bias(self):
        a=np.zeros(1001,dtype=DTYPE); a['code']=1840; a['raw24']=1840<<4; a['raw_ma']=115
        a['code'][500]=8000; a['raw24'][500]=8000<<4; a['raw_ma'][500]=500
        pulse,rfu,mismatch,criteria=masks(a)
        self.assertEqual(np.flatnonzero(pulse).tolist(),[500]); self.assertFalse(rfu.any() or mismatch.any())
        result=compare(statistics(a['raw_ma'][~pulse]),100,100,1,True)
        self.assertEqual(result['mean_verdict'],'OUTSIDE_BOUND')
        a['raw24'][0]|=1; self.assertTrue(masks(a)[1][0])
    def test_reference_limit_and_resolution(self):
        stats=statistics(np.full(100,100.))
        self.assertEqual(compare(stats,100,100,1)['mean_verdict'],'CONDITIONAL_WITHIN_BOUND')
        self.assertEqual(compare(stats,100,100,10)['mean_verdict'],'INCONCLUSIVE')
    def test_raw_context_and_full_statistics_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'raw.csv'
            with source.open('w',newline='') as file:
                writer=csv.writer(file); writer.writerow(('sequence','tim2_us','elapsed_us64','raw24','vshunt_code','current_ma','current_cal_ma'))
                for i in range(1000):
                    code=8000 if i==500 else 1600
                    writer.writerow((i,(0xFFFFFF00+i*84)&0xFFFFFFFF,i*84,hex(code<<4),code,code/16,code/16))
            with patch('tools.hil.accuracy_analysis.plots'):
                result=analyze(source,root/'analysis')
            self.assertEqual(result['full']['n'],1000); self.assertEqual(result['robust']['n'],999)
            self.assertEqual(result['full']['max_ma'],500); self.assertEqual(result['robust']['mean_ma'],100)
            context=next((root/'analysis/contexts').glob('*.csv'))
            with context.open() as file: rows=list(csv.DictReader(file))
            self.assertEqual(len(rows),401); self.assertEqual(rows[0]['relative_index'],'-200')
            self.assertEqual(rows[-1]['relative_index'],'200'); self.assertEqual(rows[200]['sequence'],'500')
            self.assertEqual(int(rows[200]['raw24']),8000<<4)
    def test_confirmation_gate(self):
        with self.assertRaises(ValueError): run_stage('unused','ZERO_BEFORE',None,before=float('nan'))
    def test_reference_exceedance_is_not_statistical_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); data=np.zeros(1000,dtype=DTYPE)
            data['sequence']=np.arange(1000); data['elapsed_us']=np.arange(1000)*84
            data['code']=1600; data['code'][500]=1744
            data['raw24']=data['code']<<4; data['raw_ma']=data['code']/16; data['cal_ma']=data['raw_ma']
            from tools.hil.accuracy_analysis import _analyze_data
            with patch('tools.hil.accuracy_analysis.plots'):
                result=_analyze_data(data,'test.csv',root,dict(before=98,after=98,resolution_ma=1))
            self.assertEqual(result['exclusions']['total'],0)
            self.assertEqual(result['comparison_full']['observed_samples_outside_reference_10ma'],1)
            with (root/'reference_exceedances.csv').open() as file: rows=list(csv.DictReader(file))
            self.assertEqual(rows[0]['sequence'],'500')
    def test_offline_reanalysis_never_starts_new_capture(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.hil.accuracy_suite.discover') as discover:
            with self.assertRaisesRegex(ValueError,'existing stage'):
                run_stage(directory,'ZERO_BEFORE','ZERO_BEFORE',reanalyze=True)
            discover.assert_not_called()
