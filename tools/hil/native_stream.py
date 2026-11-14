"""CSF4 v2 capture with full RAW24 and actual timestamps."""
import csv
import threading
import time
from tools.stream_capture import FrameDecoder

def native_verdict(native):
    from tools.hil.run import ZERO
    errors = [f'Native {key}={native.get(key)}' for key in ZERO
              if key != 'NATIVE_DMA_OVERRUNS' and native.get(key) != '0']
    warnings = []
    if native.get('NATIVE_DMA_OVERRUNS') != '0':
        warnings.append('Service cycles >= nominal VSHCT: '+str(native.get('NATIVE_DMA_OVERRUNS'))+
                        '; this counter is not a physical DMA error or proof of conversion loss')
    if native.get('NATIVE_CNVRF_ZERO') != '0': warnings.append('CNVRF_ZERO='+str(native.get('NATIVE_CNVRF_ZERO')))
    for key, expected in {'SESSION_ADC_CONFIG':'0xAA68','NATIVE_ALATCH':'1','NATIVE_CONVERSION_US':'84',
                          'ACQ_CAPTURE_MODE':'NATIVE','ACQ_MODE':'DMA','STATUS':'OK'}.items():
        if native.get(key) != expected: errors.append(f'{key}: expected {expected}, got {native.get(key)}')
    return errors, warnings

def capture(link, seconds, output):
    calibration = link.command('cal show')
    for command in ('acq mode dma','acq capture native','acq profile set fast',
                    'acq alert adc 84','acq native latch 1','spi verify off'):
        link.command(command)
    port = link.serial; decoder = FrameDecoder(); stop_errors = []
    def send(command):
        link.log.write(f'{time.monotonic():.6f} TX {command}\n'); link.log.flush()
        port.write((command+'\n').encode('ascii'))
    def stop():
        try: send('stream stop')
        except Exception as exc: stop_errors.append(str(exc))
    writer_thread = None
    count = 0; last = first = None; elapsed = 0; timestamp_errors = 0; final = None; error_frames = 0
    suspicious = 0; reserved_nonzero = 0; extremes = [None, None]
    try:
        send('stream start'); deadline = time.monotonic()+3; line = bytearray()
        while time.monotonic() < deadline:
            b = port.read(1); line.extend(b)
            if b == b'\n':
                link.log.write(f'ACK {line!r}\n'); link.log.flush()
                if line == b'STREAM: BINARY V2\r\n': break
                if b'ERR:' in line: raise RuntimeError(f'Native start rejected: {line!r}')
                line.clear()
        else: raise TimeoutError(f'Missing V2 acknowledgement; partial={line!r}')
        send('stream config'); receive_start = time.monotonic(); stop_at = receive_start+seconds
        with output.open('x', newline='', encoding='utf8') as file:
            csvout = csv.writer(file); csvout.writerow(('sequence','tim2_us','elapsed_us64','raw24','vshunt_code','current_ma','current_cal_ma'))
            while time.monotonic() < stop_at+5:
                if writer_thread is None and time.monotonic() >= stop_at:
                    writer_thread = threading.Thread(target=stop, daemon=True); writer_thread.start()
                block = port.read(8192)
                if block: link.log.write(f'{time.monotonic():.6f} BINARY {block.hex()}\n'); link.log.flush()
                for frame in decoder.feed(block):
                    if frame['protocol_version'] != 2: raise ValueError('Expected CSF4 v2')
                    if frame['type'] == 5: error_frames += 1
                    if frame['type'] == 1:
                        for (seq, stamp, code), raw in zip(frame['samples'], frame['raw24']):
                            if last is None: first = stamp
                            else:
                                delta = (stamp-last) & 0xFFFFFFFF
                                if not 0 < delta < 0x80000000: timestamp_errors += 1
                                elapsed += delta
                            last = stamp; count += 1
                            suspicious += int(code < -800 or code > 160000)
                            reserved_nonzero += int(bool(raw & 15))
                            extremes[0] = code if extremes[0] is None else min(extremes[0], code)
                            extremes[1] = code if extremes[1] is None else max(extremes[1], code)
                            calibrated = code * 62500
                            if calibration['ENABLED'] == '1':
                                numerator = (calibrated-int(calibration['OFFSET_UA'])*1000)*int(calibration['GAIN_PPM'])
                                calibrated = ((abs(numerator)+500000)//1000000)*(-1 if numerator < 0 else 1)
                            csvout.writerow((seq, stamp, elapsed, f'0x{raw:06X}', code, code/16, calibrated/1e6))
                    if frame['type'] == 3: final = frame['stats']
                if decoder.ended: break
        if not decoder.ended: raise TimeoutError('Missing END; partial CSV retained')
        receive_end = time.monotonic()
    finally:
        if writer_thread is None:
            writer_thread = threading.Thread(target=stop, daemon=True); writer_thread.start()
        deadline = time.monotonic()+3
        while not decoder.ended and time.monotonic() < deadline:
            block=port.read(8192)
            if block: link.log.write(f'{time.monotonic():.6f} BINARY {block.hex()}\n'); link.log.flush()
            decoder.feed(block)
        writer_thread.join(timeout=1.1)
        link.capture_progress = {'samples':count, 'end_received':decoder.ended,
            'crc_errors':decoder.crc_errors, 'format_errors':decoder.format_errors,
            'frame_gaps':decoder.frame_gaps,'sequence_gaps':decoder.slot_gaps,
            'timestamp_errors':timestamp_errors,'csv':str(output),'stop_errors':stop_errors}
    native = link.command('acq native report')
    diagnostics = {c: link.command(c) for c in ('stream status','stream timing','stream anomaly')}
    errors = list(stop_errors)
    for name, value in {'crc':decoder.crc_errors,'format':decoder.format_errors,'frame_gaps':decoder.frame_gaps,
                        'sequence_gaps':decoder.slot_gaps,'timestamp':timestamp_errors,
                        'code':decoder.code_errors,'resync_bytes':decoder.resync_bytes,'error_frames':error_frames}.items():
        if value: errors.append(f'{name}={value}')
    for key in ('FRAMES_DROPPED','TX_BUFFER_OVERFLOWS','ACQ_BUFFER_OVERFLOWS','SPI_ERRORS'):
        if final.get(key) != 0: errors.append(f'{key}={final.get(key)}')
    native_errors, warnings = native_verdict(native); errors.extend(native_errors)
    if suspicious:
        warnings.append(f'Suspicious measured values outside -50..10000 mA: {suspicious}; RAW preserved; not classified as transport corruption')
    if count != final['ADC_SAMPLES'] or count != final['USB_SAMPLES_SENT']: errors.append('END/CSV count mismatch')
    if decoder.config is None or decoder.config[:2] != (0xAA68,0): errors.append('Missing/inconsistent Native CONFIG')
    rate = (count-1)*1e6/elapsed if elapsed else 0
    if rate < 10000: errors.append(f'payload rate below 10000: {rate}')
    return {'status':'FAIL' if errors else 'PASS', 'errors':errors, 'warnings':warnings,
            'samples':count,'payload_rate_hz':rate,'end':final,'csv':str(output),
            'host_receive_rate_hz':count/(receive_end-receive_start),
            'transport':dict(link.capture_progress, resync_bytes=decoder.resync_bytes),
            'protocol_version':decoder.version, 'elapsed_us64':elapsed,
            'calibration':calibration,'shunt_ohm':0.005,'adcrange':0,
            'suspicious_value_count':suspicious,'reserved_nonzero':reserved_nonzero,'min_max_code':extremes,
            'reports':{'native':native, **diagnostics}}
