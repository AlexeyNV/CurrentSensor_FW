"""Read-only ELF/programmer audit; programming stays fail-closed."""
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import re
import time
import json

def inspect_elf(path):
    data = Path(path).read_bytes()
    if data[:7] != b'\x7fELF\x01\x01\x01' or len(data) < 52:
        raise ValueError('Expected little-endian ELF32')
    if struct.unpack_from('<H', data, 18)[0] != 40:
        raise ValueError('Expected ARM ELF')
    offset = struct.unpack_from('<I', data, 28)[0]
    size, count = struct.unpack_from('<HH', data, 42)
    if size < 32 or offset + count * size > len(data):
        raise ValueError('Invalid program headers')
    segments = []
    for i in range(count):
        kind, fileoff, virtual, physical, length, memory, flags, align = struct.unpack_from('<8I', data, offset+i*size)
        if kind != 1 or not length:
            continue
        end = physical + length
        if fileoff + length > len(data) or length > memory or end > 2**32:
            raise ValueError('Invalid load segment')
        if not (0x08000000 <= physical < end <= 0x08060000):
            raise ValueError('Load segment outside application Flash; sector 7 protected')
        segments.append({'start': physical, 'end_exclusive': end, 'bytes': length})
    if not segments or min(s['start'] for s in segments) != 0x08000000:
        raise ValueError('Missing vector-table load segment')
    return {'path': str(Path(path).resolve()), 'sha256': hashlib.sha256(data).hexdigest(), 'segments': segments}

def programmer_audit():
    cli = shutil.which('STM32_Programmer_CLI') or shutil.which('STM32_Programmer_CLI.exe')
    if not cli:
        return {'available': False, 'write_enabled': False}
    result = subprocess.run([cli, '--help'], capture_output=True, timeout=15)
    return {'available': True, 'path': cli, 'help': result.stdout.decode(errors='replace'),
            'write_enabled': False, 'reason': 'Explicit operator approval, DFU identity and full sector backup are required'}

def flash(elf=None, output=None, serial_number=None, approved=False, wait_seconds=0):
    if not approved:
        raise RuntimeError('FLASH_BLOCKED: explicit --approve-flash and --dfu-serial required; no write was issued')
    if not serial_number or not re.fullmatch(r'[A-Za-z0-9]+', serial_number):
        raise ValueError('Expected explicit DFU serial number')
    output = Path(output); audit = programmer_audit()
    if not audit['available'] or 'v2.23.0' not in audit['help'] or '--skipErase' not in audit['help']:
        raise RuntimeError('Unverified programmer version/skipErase semantics; write blocked')
    image = output/'firmware.elf'; image.write_bytes(Path(elf).read_bytes()); info = inspect_elf(image)
    def call(args):
        result = subprocess.run([audit['path'], *args], capture_output=True, timeout=120)
        text = (result.stdout+result.stderr).decode(errors='replace')
        with (output/'programmer.log').open('a', encoding='utf8') as log:
            log.write(repr(args)+'\n'+text+'\n')
        if result.returncode or re.search(r'\bError\s*:', text, re.I): raise RuntimeError(text)
        return text
    deadline = time.monotonic()+wait_seconds
    while True:
        listing = call(['-l','usb'])
        ports = re.findall(r'Device Index\s*:\s*(USB\d+)', listing, re.I)
        serials = re.findall(r'Serial number\s*:\s*([A-Za-z0-9]+)', listing, re.I)
        if ports: break
        if time.monotonic() >= deadline:
            raise RuntimeError('No DFU: set BOOT0 for system memory and press RESET; then retry with --wait-dfu 120')
        time.sleep(1)
    if len(ports) != 1 or serials != [serial_number]: raise RuntimeError('Ambiguous/unexpected DFU identity')
    connection = ['-c',f'port={ports[0]}', f'sn={serial_number}']
    identity = call(connection)
    if not re.search(r'Device ID\s*:\s*0x0?431\b', identity, re.I):
        raise RuntimeError('Target is not verified STM32F411')
    # ROM DFU denies factory signature reads on this board. Its default NVM
    # size is not proof. Require successful full sector-7 read below instead;
    # never erase if the protected range is inaccessible or incomplete.
    before, after = output/'sector7_before.bin', output/'sector7_after.bin'
    def backup(path):
        call(connection+['-u','0x08060000','0x20000',str(path.resolve())])
        data=path.read_bytes()
        if len(data)!=0x20000: raise RuntimeError('Incomplete sector 7 read; programming/restart blocked')
        return hashlib.sha256(data).hexdigest()
    before_hash = backup(before)
    (output/'sector7_backup.json').write_text(json.dumps({'sha256':before_hash,'bytes':0x20000,
        'serial':serial_number,'identity':identity,'elf':info}, indent=2), encoding='utf8')
    # Explicit sector erase, followed by skipErase: never implicit mass erase.
    boundaries=(0x08000000,0x08004000,0x08008000,0x0800C000,0x08010000,0x08020000,0x08040000,0x08060000)
    sectors=[str(i) for i in range(7) if any(s['start'] < boundaries[i+1] and s['end_exclusive'] > boundaries[i] for s in info['segments'])]
    call(connection+['-e',*sectors])
    call(connection+['--skipErase','-d',str(image.resolve()),'-v'])
    after_hash=backup(after)
    if before_hash != after_hash: raise RuntimeError('SECTOR7_CHANGED: do not restart or overwrite backup')
    call(connection+['-g','0x08000000'])
    return {'elf':info, 'sector7_sha256':before_hash, 'sector7_verified':True, 'erased_sectors':sectors}
