"""python tools/hil/run.py --port auto --suite smoke"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.hil.flash_safe import inspect_elf, programmer_audit, flash

ZERO = ('SPI_ERRORS', 'DMA_ERRORS', 'DMA_TIMEOUTS', 'BUFFER_OVERFLOWS',
        'NATIVE_QUEUE_DROPS', 'NATIVE_RUNTIME_EVENTS_LOST',
        'NATIVE_COUNTER_INVARIANT_ERRORS', 'NATIVE_DMA_OVERRUNS',
        'NATIVE_INVALID_CALLBACKS', 'NATIVE_DMA_FAILURES')

def fields(text):
    return {
        line.split(':', 1)[0].strip(): line.split(':', 1)[1].strip()
        for line in text.splitlines() if ':' in line}

def validate(report):
    errors = []
    for key in ZERO:
        if key == 'NATIVE_DMA_OVERRUNS':continue  # service-budget warning; not a DMA failure
        if report.get(key) != '0': errors.append(f'{key}: {report.get(key, "MISSING")}')
    try:
        if int(report['RATE_HZ']) < 10000: errors.append('rate below 10000 Hz')
        if int(report['SAMPLES']) <= 0 or report['SAMPLES'] != report['STATS_SAMPLES']:
            errors.append('sample/statistics mismatch')
    except (KeyError, ValueError): errors.append('missing/invalid rate or samples')
    for key, expected in {'ACQ_CAPTURE_MODE': 'NATIVE', 'SESSION_ADC_CONFIG': '0xAA68',
                           'NATIVE_CONVERSION_US': '84', 'NATIVE_ALATCH': '1'}.items():
        if report.get(key) != expected: errors.append(f'{key}: expected {expected}, got {report.get(key)}')
    if report.get('STATUS') != 'OK': errors.append('report STATUS not OK')
    return errors

class Link:
    def __init__(self, port, log):
        import serial
        self.serial = serial.Serial(port, 115200, timeout=.05, write_timeout=1, exclusive=True)
        self.log = log
    def command(self, command, timeout=3, terminal=False):
        self.log.write(f'\n{time.monotonic():.6f} TX {command}\n'); self.log.flush()
        self.serial.write((command+'\n').encode('ascii'))
        end = time.monotonic()+timeout
        last = time.monotonic(); data = bytearray()
        while time.monotonic() < end:
            block = self.serial.read(4096)
            if block:
                data.extend(block); last = time.monotonic()
                self.log.write(f'{time.monotonic():.6f} RX {block!r}\n'); self.log.flush()
                if terminal and (b'\nSTATUS:' in data or data.startswith(b'STATUS:') or b'ERR:' in data) and data.endswith(b'\n'): break
            elif data and not terminal and time.monotonic()-last > .15: break
            elif not command and time.monotonic()-last > .2: break
        else: raise TimeoutError(f'CLI timeout: {command}; partial={data!r}')
        text = data.decode('ascii', errors='replace')
        if command == 'acq stop' and text.strip() == 'ERR: NOT_RUNNING':
            return {'STATUS': 'OK', 'ALREADY_STOPPED': '1'}
        if 'ERR:' in text or ('STATUS:' in text and fields(text).get('STATUS') != 'OK'):
            raise RuntimeError(f'{command}: {text}')
        return fields(text)
    def close(self): self.serial.close()

def discover(port='auto', serial_number=None, ports=None):
    if ports is None:
        from serial.tools.list_ports import comports
        ports = list(comports())
    candidates = [p for p in ports if p.vid == 0x0483 and p.pid == 0x5740
                  and (serial_number is None or p.serial_number == serial_number)
                  and (port == 'auto' or p.device == port)]
    if len(candidates) != 1: raise RuntimeError(f'Expected exactly one CDC candidate; found {[p.device for p in candidates]}')
    p = candidates[0]
    return {'port': p.device, 'vid': p.vid, 'pid': p.pid, 'serial_number': p.serial_number}

def smoke(link, expected):
    reports = {}
    for command in ('info', 'cal show', 'spi config', 'ina', 'regs', 'measure'):
        reports[command] = link.command(command)
    if reports['info'].get('MCU') != 'STM32F411RET6' or reports['info'].get('FW') != expected:
        raise RuntimeError('INFO identity/version mismatch')
    if reports['spi config'].get('SPI_HZ') != '6000000': raise RuntimeError('SPI is not 6 MHz')
    if not reports['ina'].get('MANUFACTURER_ID', '').startswith('0x5449') or reports['ina'].get('DEVICE_ID') != '0x2291':
        raise RuntimeError('INA229 identification failed')
    for command in ('acq mode dma', 'acq capture native', 'acq profile set fast',
                    'acq alert adc 84', 'acq native latch 1', 'spi verify off'):
        reports[command] = link.command(command)
    reports['test'] = link.command('acq test 5', timeout=9, terminal=True)
    reports['native'] = link.command('acq native report')
    reports['dma'] = link.command('acq dma report')
    errors = validate(reports['test']) + validate(reports['native'])
    reports['stop'] = link.command('acq stop')
    restored = link.command('regs'); reports['restored'] = restored
    if restored.get('ADC_CONFIG') != reports['regs'].get('ADC_CONFIG'): errors.append('ADC restore mismatch')
    reports['post_measure'] = link.command('measure')
    warnings=[]
    for key in ('NATIVE_CNVRF_ZERO','NATIVE_DMA_OVERRUNS'):
        if reports['native'].get(key) != '0':warnings.append(f'{key}={reports["native"].get(key,"MISSING")}')
    return {'reports': reports, 'errors': errors, 'warnings':warnings,
            'status': 'FAIL' if errors else 'PASS'}

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', default='auto'); p.add_argument('--serial-number')
    p.add_argument('--suite', choices=['smoke', 'stream'], default='smoke')
    p.add_argument('--seconds', type=int, choices=[5,60,300], default=5)
    p.add_argument('--runs', type=int, default=3); p.add_argument('--expect-fw', default='1.0.0')
    p.add_argument('--elf', type=Path, default=ROOT/'build/CurrentSensor_FW.elf'); p.add_argument('--flash', action='store_true')
    p.add_argument('--approve-flash', action='store_true'); p.add_argument('--dfu-serial')
    p.add_argument('--wait-dfu', type=int, default=0)
    a = p.parse_args(argv)
    if not 1 <= a.runs <= 100: p.error('--runs must be 1..100')
    if not 0 <= a.wait_dfu <= 300: p.error('--wait-dfu must be 0..300')
    dest = ROOT/'results/hil'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]); dest.mkdir(parents=True)
    result = {'utc': datetime.now(timezone.utc).isoformat(), 'status': 'FAIL', 'runs': [], 'hardware_pass': False}
    link = None
    with (dest/'cdc.log').open('w', encoding='utf8') as log:
        try:
            result['elf'] = inspect_elf(a.elf)
            result['git_revision'] = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
            result['git_status'] = subprocess.check_output(['git','status','--porcelain'], cwd=ROOT, text=True)
            if a.flash:
                result['programmer'] = programmer_audit()
                result['flash'] = flash(a.elf, dest, a.dfu_serial, a.approve_flash, a.wait_dfu)
                deadline = time.monotonic()+20
                while True:
                    try: discover(a.port, a.serial_number); break
                    except RuntimeError:
                        if time.monotonic() >= deadline: raise
                        time.sleep(.5)
            result['device'] = discover(a.port, a.serial_number)
            link = Link(result['device']['port'], log)
            link.command('')
            identity = link.command('info')
            result['identity'] = identity
            if identity.get('MCU') != 'STM32F411RET6': raise RuntimeError('Unexpected INFO identity')
            link.command('acq stop')
            if a.flash and a.suite == 'stream':
                result['post_flash_smoke'] = []
                for _ in range(3):
                    check = smoke(link, a.expect_fw); result['post_flash_smoke'].append(check)
                    if check['errors']: raise RuntimeError('Post-flash smoke failed; streaming blocked')
            for _ in range(a.runs):
                if a.suite == 'smoke': run = smoke(link, a.expect_fw)
                else:
                    from tools.hil.native_stream import capture
                    if identity.get('FW') != a.expect_fw: raise RuntimeError('FW version mismatch')
                    run = capture(link, a.seconds, dest/f'stream_{len(result["runs"])+1}.csv')
                result['runs'].append(run)
                print(f'run {len(result["runs"])}: {run["status"]}', flush=True)
                if run['errors']: break
            result['hardware_pass'] = len(result['runs']) == a.runs and all(r['status']=='PASS' for r in result['runs'])
            result['status'] = ('WARNING' if any(r['warnings'] for r in result['runs']) else 'PASS') if result['hardware_pass'] else 'FAIL'
        except Exception as exc:
            result['error'] = str(exc); result['traceback'] = traceback.format_exc(); print(result['traceback'], file=sys.stderr)
            result['capture_progress'] = getattr(link, 'capture_progress', None)
        finally:
            if link:
                try:
                    result['cleanup'] = link.command('acq stop')
                    result['cleanup_capture'] = link.command('acq capture continuous')
                except Exception as exc: result['cleanup_error'] = str(exc); result['status']='FAIL'; result['hardware_pass']=False
                finally: link.close()
    (dest/'summary.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    (dest/'report.md').write_text('# CurrentSensor HIL\n\n'+result['status']+'\n\n'+
        f"UTC: {result['utc']}\n\nFW: {result.get('identity',{}).get('FW','UNKNOWN')}\n\n"
        f"Git: {result.get('git_revision','UNKNOWN')} (dirty={bool(result.get('git_status'))})\n\n"
        f"ELF SHA256: {result.get('elf',{}).get('sha256','UNKNOWN')}\n\n"+
        '\n'.join(f'- Run {i+1}: {r["status"]}; rate {r["reports"]["native"].get("RATE_HZ")}; samples {r["reports"]["native"].get("SAMPLES")}; errors {r["errors"]}; warnings {r["warnings"]}' for i,r in enumerate(result['runs']))+
        '\n\n'+result.get('error','')+'\n\nNo metrological accuracy certification. See summary.json and cdc.log.\n', encoding='utf8')
    print(f'{result["status"]}: {dest}')
    return 0 if result['hardware_pass'] else 1

if __name__ == '__main__': raise SystemExit(main())
