"""One shared CSF4 decoder and profile API for CLI and GUI."""
import time
from .mode_capabilities import selection_error, native_capable
from tools.stream_capture import (FrameDecoder, get_available_profiles, get_active_profile,
                                  set_profile, stop_stream, synchronize_cli)


class CommandRejected(RuntimeError):
    """A complete ERR response, with the text boundary consumed."""
    def __init__(self, command, response):
        self.command = command
        super().__init__(f'{command}: {response}')


class CommandTimeout(TimeoutError):
    def __init__(self, command):
        self.command = command
        super().__init__(f'no command boundary: {command}')


class StartTimeout(TimeoutError):
    pass


class _LoggedPort:
    """Log shared CLI helpers too; binary reads remain untouched and unlogged."""
    def __init__(self, port, log):
        self.port, self.log = port, log

    def __getattr__(self, name):
        return getattr(self.port, name)

    def write(self, data):
        for line in data.decode('ascii', errors='replace').splitlines():
            if line:
                self.log('> ' + line)
        return self.port.write(data)

    def readline(self):
        data = self.port.readline()
        line = data.decode('ascii', errors='replace').strip()
        if line:
            self.log('< ' + line)
        return data


class DeviceProtocol:
    def __init__(self, port, log=lambda message: None):
        self.port = _LoggedPort(port, log)
        self.log = log
        self.native = False
        self.firmware = None
        self.profiles = None
        self.confirmed = None

    def command(self, command, timeout=3.0):
        if "\n" in command or "\r" in command:
            raise ValueError("one ASCII command required")
        # info/regs have no STATUS terminator. A queued ping is an unambiguous
        # boundary for all text commands, including error responses.
        self.port.write(command.encode("ascii") + (b"\n" if command == 'ping' else b"\nping\n"))
        lines, fields = [], {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.port.readline().decode("ascii", errors="replace").strip()
            if line == "pong":
                if command == 'ping':
                    return {'PING': 'pong'}
                errors = [entry for entry in lines if entry.startswith("ERR:")]
                if errors or fields.get("STATUS", "OK") != "OK":
                    raise CommandRejected(command, "; ".join(errors) or fields["STATUS"])
                return fields
            if line:
                lines.append(line)
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip()
        raise CommandTimeout(command)

    def connect_info(self):
        synchronize_cli(self.port)
        info = {command: self.command(command) for command in ("info", "ina", "regs", "cal show")}
        info["profiles"] = get_available_profiles(self.port)
        info["profile"] = get_active_profile(self.port)
        self.firmware = info['info'].get('FW')
        self.profiles = info['profiles']
        info['acq config'] = self.command('acq config')
        # Selection persists in the MCU across serial close/open.
        self.native = info['acq config'].get('ACQ_CAPTURE_SELECTED') == 'NATIVE'
        self.log("< " + repr(info))
        return info

    def select(self, name, mode="dma"):
        if not name and mode in ('dma', 'poll', 'native'):
            active = get_active_profile(self.port)
            name = active['name']
            self.firmware = active.get('firmware_version') or self.firmware
        error = selection_error(mode, self.firmware, name, self.profiles)
        if error:
            raise ValueError(error)
        previous = self.confirmed
        self.confirmed = None
        try:
            result = self._select(name, mode)
        except CommandRejected:
            # Only a fully acknowledged rejection is safe for one rollback.
            # Never retry timeouts or transport failures automatically.
            if previous:
                try:
                    self.log('ROLLBACK ' + repr(previous))
                    self._select(*previous)
                    self.confirmed = previous
                except (RuntimeError, TimeoutError, OSError) as error:
                    self.log('ROLLBACK_FAILED ' + repr(error))
            raise
        self.confirmed = (name, mode)
        return result

    def _select(self, name, requested_mode):
        native = requested_mode == 'native'
        mode = 'dma' if native else requested_mode
        if native:
            name = 'FAST'
        if self.firmware is None:
            self.firmware = self.command('info').get('FW')
        error = selection_error(requested_mode, self.firmware, name, self.profiles)
        if error:
            raise ValueError(error)
        modern = native_capable(self.firmware)
        if modern:
            # Clear DMA-only capture/verify restrictions BEFORE selecting POLL.
            # Do this regardless of the host's previous connection/session.
            self.command('spi verify off')
            self.command('acq capture continuous')
        self.command(f"acq mode {mode}")
        selected = self.command("acq config").get("ACQ_MODE_SELECTED")
        if selected != mode.upper():
            raise RuntimeError(f"mode not confirmed: {selected}")
        result = set_profile(self.port, name) if name else get_active_profile(self.port)
        if native:
            if not native_capable(result['firmware_version']):
                raise RuntimeError('Native streaming requires FW 0.5.x or 1.0.0')
            for command in ('acq capture native','acq alert adc 84','acq native latch 1','spi verify off'):
                self.command(command)
            if self.command('acq config').get('ACQ_CAPTURE_SELECTED') != 'NATIVE':
                raise RuntimeError('Native selection not confirmed')
            result.update(adc_config=0xAA68, sample_period_us=0, sample_rate_hz=1000000/84,
                          nominal_period_us=84, vshct_us=84, event_driven=True,
                          protocol_version=2, capture_mode='NATIVE')
        elif modern:
            if self.command('acq config').get('ACQ_CAPTURE_SELECTED') != 'CONTINUOUS':
                raise RuntimeError('Continuous selection not confirmed; apply again before Start')
            result.update(capture_mode='CONTINUOUS', protocol_version=1, event_driven=False)
        result['acq_mode'] = mode.upper()
        self.native = native
        self.log("< PROFILE " + repr(result))
        return result

    def start(self):
        self.port.write(b'stream start\n')
        deadline = time.monotonic()+3
        line = bytearray()
        expected = b'STREAM: BINARY V2\r\n' if self.native else b'STREAM: BINARY V1\r\n'
        while time.monotonic() < deadline:
            b = self.port.read(1)
            line.extend(b)
            if b == b'\n':
                self.log('< ' + line.decode('ascii', errors='replace').strip())
                if line == expected:
                    break
                if line.startswith(b'ERR:'):
                    raise CommandRejected('stream start', line.decode(errors='replace').strip())
                line.clear()
            if len(line) > 4096:
                raise StartTimeout('stream start: unexpected binary/overlong acknowledgement')
        else:
            raise StartTimeout('stream start: no acknowledgement')
        self.port.write(b"stream config\n")

    def recover_start(self, timeout=2):
        """One STOP, then drain through END before attempting text resync."""
        self.stop()
        decoder = FrameDecoder()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            decoder.feed(self.port.read(4096))
            if decoder.ended and b'STATUS: OK\r\n' in decoder.text_tail:
                return self.command('ping', timeout=1)
        # No END may mean Start was rejected silently; only a verified CLI
        # boundary can return the worker to IDLE. No reconnect is attempted.
        return self.command('ping', timeout=1)

    def stop(self):
        stop_stream(self.port)

    def diagnostics(self):
        reports = {}
        for command in ("stream status", "stream timing", "stream anomaly", "regs", "cal show"):
            try:
                reports[command] = self.command(command)
            except RuntimeError as error:
                reports[command] = {"ERROR": str(error)}
        if self.native: reports['acq native report'] = self.command('acq native report')
        return reports
