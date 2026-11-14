"""Read-only revalidation of recorded CDC bytes against Native RAW CSV."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import struct
import sys
ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT))
from tools.stream_capture import FrameDecoder
from tools.hil.native_stream import native_verdict

def verify(directory, output=None):
    directory=Path(directory)
    metadata=directory/'capture_summary.json'
    source=json.loads((metadata if metadata.exists() else directory/'summary.json').read_text())
    run=source if metadata.exists() else source['runs'][0]
    decoder=FrameDecoder(); wire_hash=hashlib.sha256(); count=0; end=None
    with (directory/'cdc.log').open(encoding='utf8') as log:
        for line in log:
            prefix, marker, data=line.partition(' BINARY ')
            if not marker: continue
            if prefix.startswith('ACK '): continue
            float(prefix) # require a timestamped binary RX record
            for frame in decoder.feed(bytes.fromhex(data.strip())):
                if frame['type']==1:
                    if frame['protocol_version']!=2: raise ValueError('Expected RAW24 v2')
                    for (seq, stamp, code), raw in zip(frame['samples'],frame['raw24']):
                        wire_hash.update(struct.pack('<III',seq,stamp,raw)); count+=1
                elif frame['type']==3: end=frame['stats']
    errors=[]
    transport={k:getattr(decoder,k) for k in ('crc_errors','format_errors','frame_gaps','slot_gaps','code_errors','resync_bytes')}
    errors.extend(f'{k}={v}' for k,v in transport.items() if v)
    if not decoder.ended or not end: errors.append('END missing')
    if decoder.config is None or decoder.config[:2]!=(0xAA68,0): errors.append('CONFIG mismatch')
    csv_hash=hashlib.sha256(); rows=0; previous=None; elapsed=0
    csvpath=Path(run['csv'])
    with csvpath.open(newline='',encoding='utf8') as file:
        for row in csv.DictReader(file):
            seq,stamp,raw=int(row['sequence']),int(row['tim2_us']),int(row['raw24'],0)
            code=(raw>>4)-(0x100000 if raw&0x800000 else 0)
            if code!=int(row['vshunt_code']): raise ValueError('CSV RAW/CODE mismatch')
            if float(row['current_ma'])!=code/16: raise ValueError('CSV RAW/current mismatch')
            if previous is not None:
                delta=(stamp-previous)&0xFFFFFFFF
                if not 0<delta<0x80000000: raise ValueError('Nonmonotonic TIM2')
                elapsed+=delta
            if elapsed!=int(row['elapsed_us64']): raise ValueError('CSV timeline mismatch')
            csv_hash.update(struct.pack('<III',seq,stamp,raw)); rows+=1; previous=stamp
    if rows!=count or csv_hash.digest()!=wire_hash.digest(): errors.append('CSV/wire mismatch')
    if end and (end['ADC_SAMPLES']!=count or end['USB_SAMPLES_SENT']!=count): errors.append('END count mismatch')
    native_errors,warnings=native_verdict(run['reports']['native']); errors.extend(native_errors)
    report={'status':'FAIL' if errors else 'WARNING' if warnings else 'PASS','errors':errors,
            'warnings':warnings,'samples':count,'transport':transport,'wire_csv_sha256':wire_hash.hexdigest(),
            'end_received':decoder.ended,'payload_rate_hz':(count-1)*1e6/elapsed if elapsed else 0}
    destination=Path(output) if output is not None else directory/'verification.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,indent=2),encoding='utf8')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('directory'); a=p.parse_args()
    report=verify(a.directory); print(json.dumps(report,indent=2)); raise SystemExit(bool(report['errors']))
