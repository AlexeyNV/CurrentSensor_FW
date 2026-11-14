"""Parse the firmware's textual capture while retaining the untouched report."""
import re


ENTRY = re.compile(
    r'^SLOT=(\d+) TIM2_US=(\d+) SPI_RX_BYTES=(\?\?|[0-9A-Fa-f]{2}),'
    r'([0-9A-Fa-f]{2}),([0-9A-Fa-f]{2}),([0-9A-Fa-f]{2}) '
    r'VSHUNT_RAW=0x([0-9A-Fa-f]{6}) VSHUNT_CODE=(-?\d+) CODE_BIT_0x2000=([01])$')


def parse_mcu_anomaly_report(fields):
    if not fields:
        return []
    result = []
    for key,value in sorted(fields.items(),key=lambda item: int(item[0][6:])
                            if item[0].startswith('ENTRY_') and item[0][6:].isdigit() else 1<<30):
        if not key.startswith('ENTRY_'):
            continue
        match=ENTRY.fullmatch(value)
        if match is None:
            result.append(dict(entry=key,raw_text=value,parse_status='UNRECOGNIZED'))
            continue
        slot,stamp,first,b1,b2,b3,raw,code,bit=match.groups()
        result.append(dict(entry=key,slot=int(slot),tim2_read_us=int(stamp),
                           spi_rx_bytes=[None if first=='??' else int(first,16),
                                         int(b1,16),int(b2,16),int(b3,16)],
                           vshunt_raw=int(raw,16),vshunt_code=int(code),
                           code_bit_0x2000=bool(int(bit)),raw_text=value,
                           parse_status='OK'))
    for i,event in enumerate(result):
        event['previous'] = (dict(slot=result[i-1].get('slot'),
                                  vshunt_code=result[i-1].get('vshunt_code')) if i else None)
        event['next'] = (dict(slot=result[i+1].get('slot'),
                              vshunt_code=result[i+1].get('vshunt_code')) if i+1<len(result) else None)
    return result
