import unittest
from unittest.mock import patch
from current_sensor.device_protocol import DeviceProtocol
from current_sensor.acquisition_service import AcquisitionService
from current_sensor.session_metadata import new_session_metadata
from current_sensor.device_worker import DeviceWorker

class Port:
    def __init__(self): self.rx=bytearray(); self.writes=[]
    def write(self,data):
        self.writes.append(data)
        if data==b'stream start\n': self.rx.extend(b'STREAM: BINARY V2\r\n')
    def read(self,n):
        data=bytes(self.rx[:n]); del self.rx[:n]; return data

class NativeTests(unittest.TestCase):
    def stop_worker(self, worker):
        worker.recorder=None
        worker.request('shutdown'); worker.join(5)
        self.assertFalse(worker.is_alive())
    def test_select_and_start(self):
        port=Port(); protocol=DeviceProtocol(port); commands=[]
        def command(c):
            commands.append(c)
            return {'ACQ_MODE_SELECTED':'DMA','ACQ_CAPTURE_SELECTED':'NATIVE'}
        protocol.command=command
        profile={'name':'FAST','firmware_version':'0.5.0','acq_mode':'DMA'}
        with patch('current_sensor.device_protocol.set_profile',return_value=profile):
            selected=protocol.select('BALANCED','native')
        self.assertTrue(selected['event_driven']); self.assertEqual(selected['sample_period_us'],0)
        self.assertIn('acq native latch 1',commands); self.assertIn('spi verify off',commands)
        protocol.start(); self.assertEqual(port.writes,[b'stream start\n',b'stream config\n'])
        self.assertEqual(new_session_metadata({},selected,{},'test')['protocol_version'],2)
        worker=DeviceWorker(); worker.profile=selected; worker.session_metadata={'protocol_version':2}
        self.addCleanup(self.stop_worker,worker)
        worker.record_directory='unused'
        with patch('current_sensor.device_worker.Recorder') as recorder:
            worker._open_recorder()
            self.assertEqual(recorder.call_args.args[1]['protocol_version'],2)

    def test_event_time_and_stop_tail(self):
        service=AcquisitionService(); service.reset({'event_driven':True,'sample_period_us':0})
        data=service.ingest([{'type':1,'samples':[(0,0xFFFFFFF0,1),(1,65,2),(2,154,-1)]}])
        self.assertEqual(service.summary()['period_violations'],0)
        self.assertAlmostEqual(float(data['time_s'][-1]),.000170)
        worker=DeviceWorker(service=service); worker.profile={'event_driven':True}
        self.addCleanup(self.stop_worker,worker)
        worker.final_stats={'LOST_SAMPLE_SLOTS':1,'ACQ_BUFFER_OVERFLOWS':0}
        worker.diagnostics={'acq native report':{'NATIVE_RUNTIME_EVENTS_LOST':'0','NATIVE_STOP_DISCARDED':'1'}}
        self.assertFalse(worker.snapshot_report()['health']['acquisition_losses'])
        worker.diagnostics['acq native report']['NATIVE_RUNTIME_EVENTS_LOST']='1'
        self.assertTrue(worker.snapshot_report()['health']['acquisition_losses'])
