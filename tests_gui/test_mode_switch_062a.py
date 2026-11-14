import os,time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest.mock import patch
import serial
from current_sensor.device_protocol import DeviceProtocol, CommandRejected, CommandTimeout, StartTimeout
from current_sensor.device_worker import DeviceWorker
from current_sensor.mock_device import MockSerial, MOCK_PROFILES
from current_sensor.connection_monitor import ConnectionMonitor
from test_integration import wait_for


class ModernSerial(MockSerial):
    """FW 0.5 restrictions relevant to GUI selection, not a Native emulator."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.capture = 'NATIVE'  # persists after another host closes COM
        self.reject_start = False
        self.reject_mode_once = False

    def _profile_fields(self):
        return dict(super()._profile_fields(), FW='0.5.0')

    def write(self, data):
        for command in data.decode('ascii').splitlines():
            if command == 'info':
                self.writes.append(command); self._text(dict(FW='0.5.0'))
            elif command.startswith('spi verify ') or command in ('acq alert adc 84','acq native latch 1'):
                self.writes.append(command); self._text()
            elif command.startswith('acq capture '):
                self.writes.append(command); self.capture=command.split()[-1].upper(); self._text()
            elif command == 'acq config':
                self.writes.append(command)
                self._text(dict(self._profile_fields(), ACQ_MODE_SELECTED=self.mode, ACQ_CAPTURE_SELECTED=self.capture))
            elif command.startswith('acq mode ') and (self.reject_mode_once or
                    (self.capture=='NATIVE' and command.endswith('poll'))):
                self.reject_mode_once=False; self.writes.append(command); self._text(error='ACQ_ACTIVE')
            elif command == 'stream start' and (self.reject_start or self.capture=='NATIVE'):
                self.writes.append(command); self._text(error='REPEAT_REQUIRES_DMA_NON_FAST')
            else:
                super().write((command+'\n').encode())
        return len(data)


class ModeSwitchTests(unittest.TestCase):
    def worker(self):
        ports=[]
        def factory(*a, **kw):
            p=ModernSerial(); ports.append(p); return p
        w=DeviceWorker(factory); w.spool_enabled=False
        w.request('connect',port='MOCK'); wait_for(lambda:w.state=='idle')
        def cleanup():
            w.request('shutdown'); w.join(8); self.assertFalse(w.is_alive())
        self.addCleanup(cleanup)
        return w,ports

    def test_every_continuous_profile_poll_dma_after_stale_native(self):
        for mode in ('poll','dma'):
            for name in MOCK_PROFILES:
                with self.subTest(mode=mode,profile=name):
                    p=ModernSerial(); protocol=DeviceProtocol(p); protocol.connect_info()
                    if mode=='poll' and name=='FAST':
                        count=len(p.writes)
                        with self.assertRaisesRegex(ValueError,'missing END'):protocol.select(name,mode)
                        self.assertEqual(len(p.writes),count)
                        continue
                    selected=protocol.select(name,mode)
                    self.assertEqual(selected['acq_mode'],mode.upper())
                    self.assertEqual(selected['capture_mode'],'CONTINUOUS')
                    self.assertLess(p.writes.index('acq capture continuous'),p.writes.index('acq mode '+mode))
                    protocol.start(); self.assertTrue(p.streaming)
                    p.close()

    def test_unsupported_selection_no_writes(self):
        protocol=DeviceProtocol(ModernSerial()); protocol.connect_info()
        for mode in ('alert_driven','single_shot','typo'):
            count=len(protocol.port.writes)
            with self.assertRaises(ValueError):protocol.select('FAST',mode)
            self.assertEqual(count,len(protocol.port.writes))
        protocol.firmware='0.4.3'
        count=len(protocol.port.writes)
        with self.assertRaises(ValueError):protocol.select('FAST','native')
        self.assertEqual(count,len(protocol.port.writes))

    def test_config_rejected_once_rolls_back_without_close(self):
        w,ports=self.worker(); w.request('profile',name='FAST',mode='dma')
        wait_for(lambda:w.protocol.confirmed==('FAST','dma'))
        ports[0].reject_mode_once=True
        w.request('profile',name='BALANCED',mode='poll')
        wait_for(lambda:ports[0].writes.count('acq mode dma')==2)
        self.assertEqual(w.state,'idle'); self.assertFalse(ports[0].closed)
        self.assertEqual(w.profile['name'],'FAST')
        self.assertEqual(w.protocol.confirmed,('FAST','dma'))
        w.request('start',profile='BALANCED',mode='poll')
        wait_for(lambda:w.service.count>100)
        w.request('stop'); wait_for(lambda:w.state=='idle')
        self.assertTrue(w.decoder.ended)

    def test_actual_start_ack_timeout_is_typed(self):
        port=ModernSerial();protocol=DeviceProtocol(port)
        with patch.object(port,'write',return_value=13), patch.object(port,'read',return_value=b''), \
                patch('current_sensor.device_protocol.time.monotonic',side_effect=[0,1,4]):
            with self.assertRaises(StartTimeout):protocol.start()

    def test_actual_command_timeout_names_command(self):
        protocol=DeviceProtocol(ModernSerial())
        with patch.object(protocol.port,'write',return_value=1),patch.object(protocol.port,'readline',return_value=b''), \
                patch('current_sensor.device_protocol.time.monotonic',side_effect=[0,1,4]):
            with self.assertRaisesRegex(CommandTimeout,'acq config'):protocol.command('acq config')

    def test_start_err_is_immediate_keeps_port_and_allows_retry(self):
        w,ports=self.worker(); ports[0].reject_start=True
        w.request('start',profile='BALANCED',mode='dma')
        wait_for(lambda:'stream start' in ports[0].writes and w.state=='idle')
        self.assertFalse(ports[0].closed); self.assertTrue(w.is_alive())
        ports[0].reject_start=False
        w.request('start',profile='BALANCED',mode='dma'); wait_for(lambda:w.service.count>100)
        w.request('stop'); wait_for(lambda:w.state=='idle')

    def test_command_timeout_one_probe_keeps_responsive_port(self):
        w,ports=self.worker(); original=w.protocol.command
        def command(c,**kw):
            if c=='acq capture continuous':raise CommandTimeout(c)
            return original(c,**kw)
        with patch.object(w.protocol,'command',side_effect=command):
            count=ports[0].writes.count('ping')
            w.request('profile',name='BALANCED',mode='poll')
            wait_for(lambda:ports[0].writes.count('ping')>count)
            self.assertEqual(w.state,'idle'); self.assertFalse(ports[0].closed)
            self.assertIsNone(w.protocol.confirmed)

    def test_start_timeout_bounded_recovery_and_retry(self):
        w,ports=self.worker()
        with patch.object(w.protocol,'start',side_effect=StartTimeout('stream start: no acknowledgement')):
            w.request('start',profile='FAST',mode='dma')
            wait_for(lambda:'stream stop' in ports[0].writes and w.state=='idle')
        self.assertFalse(ports[0].closed)
        self.assertEqual(ports[0].writes.count('stream stop'),1)
        w.request('start',profile='FAST',mode='dma'); wait_for(lambda:w.service.count>100)
        w.request('stop'); wait_for(lambda:w.state=='idle')

    def test_unresponsive_command_timeout_closes_with_reason(self):
        w,ports=self.worker()
        with patch.object(w.protocol,'command',side_effect=CommandTimeout('ping')):
            w.request('text',command='info'); wait_for(lambda:w.state=='disconnected')
        self.assertTrue(ports[0].closed)
        self.assertIn('CONTROL_UNRESPONSIVE',w.session_errors[-1])

    def test_serial_exception_and_explicit_reconnect(self):
        w,ports=self.worker()
        with patch.object(w.protocol,'command',side_effect=serial.SerialException('USB removed')):
            w.request('text',command='info'); wait_for(lambda:w.state=='disconnected')
        self.assertTrue(ports[0].closed)
        w.request('connect',port='MOCK'); wait_for(lambda:len(ports)==2 and w.state=='idle')

    def test_absent_device(self):
        w=DeviceWorker(lambda *a,**kw: (_ for _ in ()).throw(serial.SerialException('COM absent')))
        self.addCleanup(lambda:(w.request('shutdown'),w.join(5)))
        w.request('connect',port='ABSENT');wait_for(lambda:bool(w.session_errors))
        self.assertEqual(w.state,'disconnected');self.assertTrue(w.is_alive())

    def test_presence_removal_and_reappearance_are_separate_events(self):
        class Port:
            device='COM18';vid=0x483;pid=0x5740;serial_number='abc'
        events=[];ports=[Port()]
        with patch('current_sensor.connection_monitor.list_ports.comports',side_effect=lambda:list(ports)):
            m=ConnectionMonitor('COM18',events.append,interval=.005)
            try:
                wait_for(lambda:len(events)==1)
                ports.clear();wait_for(lambda:len(events)==2)
                ports.append(Port());wait_for(lambda:len(events)==3)
            finally:m.finish();m.join(1)
        self.assertEqual([e['present'] for e in events],[True,False,True])
        self.assertEqual(events[-1]['devices'][0]['serial'],'abc')

    def test_no_first_sample_requests_one_stop(self):
        w,ports=self.worker()
        with patch.object(ports[0],'_produce',return_value=None):
            w.request('start',profile='FAST',mode='dma')
            wait_for(lambda:w.state=='streaming')
            w.first_frame_deadline=0
            wait_for(lambda:w.state=='idle')
        self.assertIn('FIRST_SAMPLE_TIMEOUT',w.session_errors)
        self.assertEqual(ports[0].writes.count('stream stop'),1)
        self.assertFalse(ports[0].closed)


class ModeSelectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_combobox_only_no_io_and_unsupported_start_disabled(self):
        from current_sensor.main_window import MainWindow
        worker=DeviceWorker(lambda *a,**kw:ModernSerial())
        window=MainWindow(worker,demo=True,auto_port='DEMO')
        def pump(predicate,seconds=5):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                self.app.processEvents()
                if predicate():return
                time.sleep(.005)
            self.fail('Qt condition timed out')
        try:
            pump(lambda:window.profiles.count()==4 and worker.state=='idle')
            count=len(worker.port.writes)
            for mode in (1,0,2,0,1):
                window.mode.setCurrentIndex(mode)
                self.app.processEvents()
                self.assertEqual(len(worker.port.writes),count)
            self.assertFalse(window.start_button.isEnabled())
            self.assertFalse(window.apply_profile.isEnabled())
            self.assertIn('нет END',window.mode_hint.text())
            window.profiles.setCurrentIndex(window.profiles.findData('BALANCED'))
            self.assertTrue(window.start_button.isEnabled())
            self.assertEqual(len(worker.port.writes),count)
            worker.info['info']['FW']='0.4.3'
            window.mode.setCurrentIndex(2)
            self.assertFalse(window.start_button.isEnabled())
            self.assertFalse(window.mode.model().item(2).isEnabled())
            self.assertEqual(len(worker.port.writes),count)
        finally:
            window.close();pump(lambda:not worker.is_alive(),8)
            self.app.processEvents();window.close()


if __name__=='__main__':unittest.main()
