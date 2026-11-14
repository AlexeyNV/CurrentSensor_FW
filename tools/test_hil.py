import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import zlib
from unittest.mock import patch
from tools.hil.run import discover, validate, ZERO
from tools.hil.flash_safe import inspect_elf, flash
from tools.stream_capture import FrameDecoder

class HilTests(unittest.TestCase):
    def test_service_warning_keeps_runtime_losses_fatal(self):
        from tools.hil.native_stream import native_verdict
        r={key:'0' for key in ZERO}; r['NATIVE_CNVRF_ZERO']='0'; r['NATIVE_DMA_OVERRUNS']='23'
        r.update(SESSION_ADC_CONFIG='0xAA68', NATIVE_ALATCH='1',NATIVE_CONVERSION_US='84',
                 ACQ_CAPTURE_MODE='NATIVE',ACQ_MODE='DMA',STATUS='OK')
        errors,warnings=native_verdict(r); self.assertFalse(errors); self.assertTrue(warnings)
        r['NATIVE_RUNTIME_EVENTS_LOST']='1'; self.assertTrue(native_verdict(r)[0])
        r['DMA_ERRORS']='1'; self.assertTrue(native_verdict(r)[0])
    def test_discovery(self):
        p = SimpleNamespace(device='COM99', vid=0x483, pid=0x5740, serial_number='abc')
        self.assertEqual(discover(ports=[p])['port'], 'COM99')
        for ports in ([], [p,p]):
            with self.assertRaises(RuntimeError): discover(ports=ports)
        with self.assertRaises(RuntimeError): discover(serial_number='wrong', ports=[p])

    def test_smoke_checks(self):
        r = {key:'0' for key in ZERO}
        r.update(RATE_HZ='11911', SAMPLES='59557', STATS_SAMPLES='59557', STATUS='OK',
                 ACQ_CAPTURE_MODE='NATIVE', SESSION_ADC_CONFIG='0xAA68', NATIVE_CONVERSION_US='84', NATIVE_ALATCH='1')
        self.assertEqual(validate(r), [])
        for key, value in [('DMA_ERRORS','1'),('RATE_HZ','9999'),('SAMPLES','0'),('NATIVE_ALATCH','0')]:
            bad=dict(r); bad[key]=value; self.assertTrue(validate(bad))
        del r['SPI_ERRORS']; self.assertTrue(validate(r))

    def test_elf_fail_closed(self):
        data = bytearray(88); data[:7]=b'\x7fELF\x01\x01\x01'
        struct.pack_into('<H',data,18,40); struct.pack_into('<I',data,28,52)
        struct.pack_into('<HH',data,42,32,1)
        struct.pack_into('<8I',data,52,1,84,0x08000000,0x08000000,4,4,5,4)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.elf'; p.write_bytes(data)
            self.assertEqual(inspect_elf(p)['segments'][0]['bytes'],4)
            struct.pack_into('<I',data,64,0x08060000); p.write_bytes(data)
            with self.assertRaises(ValueError): inspect_elf(p)
        with self.assertRaises(RuntimeError): flash()

    def test_v2_raw_offsets(self):
        raw=(0xFFFFF9,0x00001F)
        header=struct.pack('<4sBBHHHIIIHHI', b'CSF4',2,1,32,48,2,0,0,0xFFFFFFF0,0,1,0)
        packet=header+struct.pack('<2I2H',*raw,0,89)
        packet+=struct.pack('<I',zlib.crc32(packet)&0xFFFFFFFF)
        decoder=FrameDecoder(); frames=[]
        for b in packet: frames.extend(decoder.feed(bytes([b])))
        self.assertEqual(frames[0]['raw24'],raw)
        self.assertEqual(frames[0]['samples'],[(0,0xFFFFFFF0,-1),(1,73,1)])
        self.assertEqual(decoder.format_errors,0)

    def test_flash_transaction(self):
        for scenario in ('ok','wrong_target','multiple','changed_sector'):
            data=bytearray(88); data[:7]=b'\x7fELF\x01\x01\x01'
            struct.pack_into('<H',data,18,40); struct.pack_into('<I',data,28,52)
            struct.pack_into('<HH',data,42,32,1)
            struct.pack_into('<8I',data,52,1,84,0x08000000,0x08000000,4,4,5,4)
            commands=[]
            def run(args, **kwargs):
                commands.append(args); text='OK'
                if '-l' in args:
                    text='Device Index : USB1\nSerial number : abc\n'
                    if scenario=='multiple': text+='Device Index : USB2\nSerial number : def\n'
                elif len(args)==4:
                    text=f'Device ID : {"0x999" if scenario=="wrong_target" else "0x431"}\nFlash size : 512 KBytes'
                if '-u' in args:
                    value=b'B' if scenario=='changed_sector' and 'after' in args[-1] else b'A'
                    Path(args[-1]).write_bytes(struct.pack('<H',512) if '0x1FFF7A22' in args else value*0x20000)
                return SimpleNamespace(returncode=0,stdout=text.encode(),stderr=b'')
            with tempfile.TemporaryDirectory() as d:
                path=Path(d); image=path/'input.elf'; image.write_bytes(data)
                with patch('tools.hil.flash_safe.programmer_audit',return_value={'available':True,'path':'fake','help':'v2.23.0 --skipErase'}), patch('tools.hil.flash_safe.subprocess.run',side_effect=run):
                    if scenario=='ok': self.assertTrue(flash(image,path,'abc',True)['sector7_verified'])
                    else:
                        with self.assertRaises(RuntimeError): flash(image,path,'abc',True)
            self.assertFalse(any(x in ('all','-rdu','-ob') for c in commands for x in c))
            if scenario in ('multiple','wrong_target'): self.assertFalse(any('-e' in c for c in commands))
            if scenario=='changed_sector': self.assertFalse(any('-g' in c for c in commands))
            writes=[c for c in commands if '-d' in c]
            self.assertTrue(all('--skipErase' in c and '-v' in c for c in writes))

if __name__=='__main__': unittest.main()
