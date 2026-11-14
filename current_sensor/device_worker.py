"""Single serial owner. UI commands are queued; disk I/O has its own thread."""
import queue
import threading
import time
import copy
import uuid
import traceback
import serial
from .acquisition_service import AcquisitionService
from .device_protocol import DeviceProtocol, FrameDecoder, CommandRejected, CommandTimeout, StartTimeout
from .connection_monitor import ConnectionMonitor
from .recorder import Recorder
from .spool import RawSpool
from .trigger import TriggerConfig
from .anomaly_diagnostics import DetectionWorker, DetectorRules
from .session_metadata import new_session_metadata
from .mcu_anomaly import parse_mcu_anomaly_report
from .archive import ArchiveRecorder, ArchiveOptions
from .lifecycle_trace import get_lifecycle_trace


class DeviceWorker(threading.Thread):
    def __init__(self, serial_factory=None, service=None):
        super().__init__(name="CurrentSensor serial", daemon=True)
        self.factory = serial_factory or serial.Serial
        self.monitor_usb = serial_factory is None
        self.presence_monitor = None
        self.trace = get_lifecycle_trace()
        self.service = service or AcquisitionService()
        self.commands = queue.Queue(64)
        self.events = queue.SimpleQueue()
        self.state = "disconnected"
        self.port = self.protocol = self.decoder = self.recorder = None
        self.profile = self.info = self.final_stats = None
        self.connection_profile = None
        self.record_directory = None
        self.record_mode = 'short'
        self.archive_options = ArchiveOptions()
        self.archive = None
        self.spool_enabled = True
        self.spool = None
        self.spool_sessions = []
        self.session_metadata = {}
        self.detector = None
        self.detector_sessions = []
        self.detector_rules = DetectorRules(10.,10000.,1000.,True)
        self.reported_spool_error = None
        self.shutdown_requested = self.disconnect_requested = False
        self.next_stats = self.next_update = 0.0
        self.stop_deadline = 0.0
        self.first_frame_deadline = 0.0
        self.limit_ma, self.step_ma = 10000.0, 1000.0
        self.trigger_config = TriggerConfig()
        self.session_errors = []
        self.diagnostics = {}
        self.config_seen = False
        self.start()
        self.emit('log', f'Lifecycle log: {self.trace.path}')

    def _serial_state(self):
        port = self.port
        if port is None:
            return 'port=None'
        if hasattr(port, 'is_open'):
            return f'port={id(port)} is_open={port.is_open}'
        if hasattr(port, 'closed'):
            return f'port={id(port)} closed={port.closed}'
        return f'port={id(port)} open=unknown'

    def request(self, action, **values):
        stage = {'stop': 'STOP_REQUESTED', 'disconnect': 'DISCONNECT_REQUESTED',
                 'shutdown': 'WORKER_STOP_REQUESTED', 'connect': 'RECONNECT_REQUESTED'}.get(action)
        if stage:
            self.trace.event(stage, f'state={self.state} {self._serial_state()}')
        try:
            self.commands.put_nowait((action, values))
        except queue.Full:
            self.emit("error", "Command queue full")

    def emit(self, kind, payload):
        self.events.put((kind, payload))

    def _log(self, message):
        self.trace.event('PROTOCOL', f'state={self.state} {message}')
        self.emit('log', message)

    def _presence(self, event):
        self.trace.event('USB_PRESENCE', repr(event))
        self.emit('usb_presence', event)

    def _exception(self, error):
        self.trace.event('EXCEPTION', f'state={self.state} type={type(error).__name__} '
                         f'error={error}\n{traceback.format_exc()}')

    def _recover_control(self, error, start=False):
        self._exception(error)
        self.emit('error', f'{type(error).__name__}: {error}')
        if self.port is None:
            return
        if self.state not in ('idle', 'starting'):
            # Preserve the existing partial-recording teardown for failures
            # during END/final diagnostics or connection initialization.
            self._disconnect(str(error))
            return
        if self.detector and self.state == 'starting':
            self.detector.finish()
        if isinstance(error, TimeoutError):
            # A timeout is NOT evidence of USB removal. Probe exactly once,
            # on the owning thread; never reopen/reset/retry the selection.
            try:
                if start:
                    self.protocol.recover_start()
                else:
                    self.protocol.command('ping', timeout=1)
            except (OSError, RuntimeError, TimeoutError) as recovery:
                self._disconnect(f'CONTROL_UNRESPONSIVE: {error}; recovery: {recovery}')
                return
        self.set_state('idle')
        self.emit('configuration_error', dict(error=str(error),
                  confirmed=getattr(self.protocol, 'confirmed', None)))

    def set_state(self, state):
        self.state = state
        self.trace.event('STATE', f'value={state} {self._serial_state()}')
        self.emit("state", state)

    def run(self):
        while True:
            try:
                try:
                    action, args = self.commands.get(timeout=0 if self.state in ("streaming", "stopping") else .02)
                    self._action(action, args)
                except queue.Empty:
                    pass
                if self.state in ("streaming", "stopping"):
                    self._receive()
            except CommandRejected as error:
                self._recover_control(error)
            except (CommandTimeout, StartTimeout) as error:
                self._recover_control(error, start=isinstance(error, StartTimeout))
            except (OSError, serial.SerialException, TimeoutError) as error:
                self._exception(error)
                self._disconnect(str(error))
            except (RuntimeError, ValueError) as error:
                self._exception(error)
                self.emit("error", str(error))
                if self.state == "starting":
                    self._recover_control(error)
                elif self.state == 'idle' and self.protocol:
                    self.emit('configuration_error', dict(error=str(error),
                              confirmed=self.protocol.confirmed))
            except Exception as error:
                # An unexpected host exception must release the COM port too.
                self._exception(error)
                self._disconnect(f"HOST_ERROR {type(error).__name__}: {error}")
            if self.recorder and self.recorder.done.is_set():
                self.emit("recorded", dict(path=str(self.recorder.metadata_path),
                                           integrity=getattr(self.recorder,'integrity_status',getattr(self.recorder,'status','PARTIAL')),
                                           error=self.recorder.error, samples=self.recorder.samples))
                self.recorder = None
                self.record_directory = None
                self.record_mode = 'short'
            if self.spool and self.spool.error and self.spool.error != self.reported_spool_error:
                self.reported_spool_error = self.spool.error
                self.emit('error', 'RAW spool: ' + self.spool.error)
            if time.monotonic() >= self.next_update:
                self.emit("summary", self.snapshot_report())
                self.next_update = time.monotonic() + .1
            if (self.shutdown_requested and self.state == "disconnected" and self.recorder is None
                    and all(session.done.is_set() for session in self.spool_sessions)
                    and all(session.done.is_set() for session in self.detector_sessions)):
                break
        self.trace.event('WORKER_EXITED', f'state={self.state} {self._serial_state()}')
        if self.presence_monitor:
            self.presence_monitor.finish()
            self.presence_monitor.join(1)

    def _action(self, action, args):
        self.trace.event('REQUEST', f'action={action} args={args!r} state={self.state}')
        if action == "connect":
            if self.port is not None:
                raise RuntimeError("A COM connection is already open")
            self.set_state('connecting')
            if self.monitor_usb:
                if self.presence_monitor:
                    self.presence_monitor.finish()
                    self.presence_monitor.join(1)
                self.presence_monitor = ConnectionMonitor(args['port'], self._presence)
            self.trace.begin('serial', 'SERIAL_REOPEN_STARTED', f'port_name={args["port"]}')
            try:
                self.port = self.factory(args["port"], baudrate=115200,
                                         timeout=.01, write_timeout=1)
            except Exception as error:
                self.trace.end('serial', 'SERIAL_REOPEN_FAILED',
                               f'type={type(error).__name__} error={error}')
                raise
            self.trace.end('serial', 'SERIAL_REOPEN_FINISHED', self._serial_state())
            self.protocol = DeviceProtocol(self.port, self._log)
            try:
                self.info = self.protocol.connect_info()
                self.info['connection'] = {'port': args['port'], 'usb_serial_number': None}
                from serial.tools import list_ports
                for candidate in list_ports.comports():
                    if candidate.device == args['port']:
                        self.info['connection'].update(usb_serial_number=candidate.serial_number,
                                                       vid=candidate.vid, pid=candidate.pid)
                        break
                self.profile = self.info["profile"]
                self.connection_profile = copy.deepcopy(self.profile)
            except Exception:
                self._disconnect("Connection initialization failed")
                raise
            self.set_state("idle")
            self.emit("connected", self.info)
        elif action in ("disconnect", "shutdown"):
            self.shutdown_requested |= action == "shutdown"
            self.disconnect_requested = True
            if self.state == "streaming":
                self._stop_stream()
            elif self.state not in ("stopping", "disconnecting"):
                self._disconnect()
        elif action == "start":
            self._require_idle()
            if self.recorder:
                raise RuntimeError("Previous recording is still closing")
            self.profile = self.protocol.select(args.get("profile"), args.get("mode", "dma"))
            self.info["cal show"] = self.protocol.command("cal show")
            self.session_metadata = new_session_metadata(self.info, self.profile,
                self.info.get('cal show'), uuid.uuid4().hex, self.connection_profile)
            self.session_metadata['detector_parameters'] = self.detector_rules.as_dict()
            self.session_metadata['anomaly_rules'] = dict(limit_ma=self.limit_ma,
                                                          step_ma=self.step_ma)
            self.info['profile'] = copy.deepcopy(self.profile)
            self.service.reset(self.profile)
            self.service.limit_ma, self.service.step_ma = self.limit_ma, self.step_ma
            self.service.trigger.configure(self.trigger_config)
            self.decoder = FrameDecoder()
            self.final_stats = None
            self.session_errors = []
            self.diagnostics = {}
            self.config_seen = False
            self.spool = None
            self.archive = None
            self.reported_spool_error = None
            self.detector = DetectionWorker(self.session_metadata['source_session'], self.detector_rules,
                                            sparse=self.record_mode=='long')
            self.detector_sessions.append(self.detector)
            self.set_state("starting")
            self.protocol.start()
            if self.spool_enabled and self.record_mode != 'long':
                self.spool = RawSpool(self.session_metadata)
                self.spool_sessions.append(self.spool)
                self.spool.start()
                self.emit('spool_started', self.spool)
            self.next_stats = time.monotonic() + 1
            if self.record_directory:
                self._open_recorder()
            self.set_state("streaming")
            self.first_frame_deadline = time.monotonic() + 3
            self.emit("profile", self.profile)
        elif action == "stop":
            if self.state == "streaming":
                self._stop_stream()
        elif action == "profile":
            self._require_idle()
            self.profile = self.protocol.select(args["name"], args.get("mode", "dma"))
            self.info['profile'] = copy.deepcopy(self.profile)
            self.emit("profile", self.profile)
        elif action == "text":
            self._require_idle()
            result = self.protocol.command(args["command"])
            self.emit("text", dict(command=args["command"], fields=result))
        elif action == "record_start":
            if self.recorder:
                raise RuntimeError("Previous recording is still closing")
            self.record_directory = args["directory"]
            self.record_mode = args.get('mode','short')
            if self.record_mode not in ('short','long'):
                raise ValueError('Unknown recording mode')
            if self.record_mode == 'short':
                self.archive = None
            if self.record_mode == 'long':
                self.archive_options = ArchiveOptions(**args.get('archive_options',{}))
                self.archive_options.validate()
                if self.detector is not None:
                    self.detector.analyzer.sparsify()
                if self.spool is not None:
                    self.spool.finish(dict(self.snapshot_report(), source_history_complete=False))
                    self.spool = None
            if self.state == "streaming":
                self._open_recorder()
            else:
                self.emit("recording", "armed")
        elif action == "record_stop":
            self.record_directory = None
            if self.recorder:
                self.recorder.finish(self.snapshot_report())
                self.emit("recording", "closing")
            else:
                self.emit("recording", "off")
        elif action == "anomaly":
            self._require_idle()
            limit, step = float(args["limit_ma"]), float(args["step_ma"])
            if not 0 <= limit <= 32767 or not 0 <= step <= 65536:
                raise ValueError("Anomaly thresholds outside range")
            self.protocol.command(f"stream anomaly set {int(limit)}")
            self.limit_ma, self.step_ma = limit, step
            self.detector_rules = DetectorRules(self.detector_rules.median_ma, limit, step,self.detector_rules.robust)
            self.service.limit_ma, self.service.step_ma = limit, step
            self.emit("anomaly_rules", dict(limit_ma=limit, step_ma=step))
        elif action == 'detector_rules':
            if self.state not in ('idle','disconnected'):
                raise RuntimeError('Change detector threshold only while stopped')
            threshold = float(args['median_ma'])
            if not 0 < threshold <= 32767:
                raise ValueError('Median threshold outside range')
            self.detector_rules = DetectorRules(threshold, self.limit_ma, self.step_ma,self.detector_rules.robust)
            self.emit('detector_rules', self.detector_rules)
        elif action == "trigger":
            config = args["config"]
            config.validate()
            if self.profile and config.pre_s + config.post_s > self.service.capacity / self.profile["sample_rate_hz"] - .1:
                raise ValueError("Trigger window exceeds live buffer")
            self.trigger_config = config
            self.service.trigger.configure(config)
        elif action == 'spool_enabled':
            if self.state in ('streaming', 'starting', 'stopping'):
                raise RuntimeError('Change automatic spool only while stopped')
            self.spool_enabled = bool(args['enabled'])

    def _require_idle(self):
        if self.state != "idle":
            raise RuntimeError("Command requires a connected, stopped device")

    def _open_recorder(self):
        metadata=dict(self.session_metadata,
                      anomaly_rules=dict(limit_ma=self.limit_ma, step_ma=self.step_ma),
                      recording_start=self.service.summary(), protocol_version=self.profile.get('protocol_version',1))
        if self.record_mode=='long':
            self.recorder=ArchiveRecorder(self.record_directory,metadata,self.archive_options,
                                          detector=self.detector)
            self.archive=self.recorder
            self.emit('recording',str(self.recorder.root))
        else:
            self.recorder=Recorder(self.record_directory,metadata,detector=self.detector)
            self.recorder.start()
            self.emit("recording", str(self.recorder.base))

    def _stop_stream(self):
        self.set_state("stopping")
        self.trace.begin('serial', 'STOP_WRITE_STARTED', self._serial_state())
        try:
            self.protocol.stop()
        except Exception as error:
            self.trace.end('serial', 'STOP_WRITE_FAILED',
                           f'type={type(error).__name__} error={error} {self._serial_state()}')
            raise
        self.trace.end('serial', 'STOP_WRITE_FINISHED', self._serial_state())
        self.stop_deadline = time.monotonic() + 5

    def _receive(self):
        wire = self.port.read(4096)
        frames = self.decoder.feed(wire)
        data = self.service.ingest(frames)
        if self.detector and len(data):
            self.detector.submit(data)
        if self.spool and len(data):
            self.spool.submit(data)
        if self.recorder and frames:
            if isinstance(self.recorder,ArchiveRecorder):
                self.recorder.submit(data,frames)
            else:
                self.recorder.submit(data, b"".join(frame["packet"] for frame in frames))
        for frame in frames:
            if frame["type"] in (2, 3):
                self.final_stats = frame["stats"]
            elif frame["type"] == 4:
                self.config_seen = True
                if frame["config"][:2] != (self.profile["adc_config"], self.profile["sample_period_us"]):
                    self.session_errors.append("BINARY_PROFILE_MISMATCH")
            elif frame["type"] == 5:
                self.session_errors.append(f"DEVICE_ERROR_{frame['error_code']}")
        if self.decoder.ended and (b"STATUS: OK\r\n" in self.decoder.text_tail or b"ERR:" in self.decoder.text_tail):
            self.trace.event('END_RECEIVED',
                             f'samples={self.service.count} {self._serial_state()}')
            if b"ERR:" in self.decoder.text_tail:
                self.session_errors.append(self.decoder.text_tail.decode("ascii", errors="replace"))
            self.trace.begin('serial', 'FINAL_DIAGNOSTICS_STARTED', self._serial_state())
            try:
                reports = self.protocol.diagnostics()
            except Exception as error:
                self.trace.end('serial', 'FINAL_DIAGNOSTICS_FAILED',
                               f'type={type(error).__name__} error={error}')
                raise
            self.trace.end('serial', 'FINAL_DIAGNOSTICS_FINISHED', self._serial_state())
            self.diagnostics = reports
            if not self.config_seen:
                self.session_errors.append("MISSING_BINARY_CONFIG")
            if self.final_stats.get("USB_SAMPLES_SENT") != self.service.count:
                self.session_errors.append("USB_SAMPLE_COUNT_MISMATCH")
            status = reports["stream status"]
            if status.get("PROFILE") != self.profile["name"] or status.get("ACQ_MODE") != self.profile.get("acq_mode"):
                self.session_errors.append("FINAL_PROFILE_OR_MODE_MISMATCH")
            if any(reports[key].get("SESSION_ID") != status.get("SESSION_ID")
                   for key in ("stream timing", "stream anomaly")) or status.get("SESSION_ID") is None:
                self.session_errors.append("DIAGNOSTIC_SESSION_MISMATCH")
            self.emit("diagnostics", reports)
            mcu = reports.get('stream anomaly', {})
            mcu_entries = parse_mcu_anomaly_report(mcu)
            self.session_metadata.update(acquisition_finished=True, session_end_verified=True,
                                         diagnostics=reports, mcu_anomaly_report=mcu,
                                         mcu_anomaly_entries=mcu_entries)
            if self.detector:
                self.detector.finish()
            if self.recorder:
                self.recorder.finish(dict(self.snapshot_report(), diagnostics=reports,
                                          mcu_anomaly_report=mcu,
                                          mcu_anomaly_entries=mcu_entries,
                                          session_end_verified=True, acquisition_finished=True))
            if self.spool:
                self.spool.finish(dict(self.snapshot_report(), diagnostics=reports,
                                       mcu_anomaly_report=mcu,
                                       mcu_anomaly_entries=mcu_entries,
                                       session_end_verified=True, acquisition_finished=True))
            self.emit("session_finished", dict(self.snapshot_report(), diagnostics=reports))
            self.set_state("idle")
            self.trace.event('ACQUISITION_IDLE', self._serial_state())
            if self.disconnect_requested:
                self._disconnect()
        elif self.state == "stopping" and time.monotonic() > self.stop_deadline:
            raise TimeoutError("No complete END/text transition")
        elif (self.state == 'streaming' and self.service.count == 0 and
              time.monotonic() > self.first_frame_deadline):
            self.session_errors.append('FIRST_SAMPLE_TIMEOUT')
            self.emit('error', 'FIRST_SAMPLE_TIMEOUT: stopping once; waiting for END')
            self._stop_stream()
        elif self.state == "streaming" and time.monotonic() > self.next_stats:
            self.port.write(b"stream status\n")
            self.next_stats = time.monotonic() + 1

    def snapshot_report(self):
        report = dict(state=self.state, acquisition=self.service.summary(),
                      device_counters=self.final_stats, session_errors=list(self.session_errors))
        archive_snapshot=self.archive.snapshot() if self.archive else None
        if self.decoder:
            report["transport"] = {name: getattr(self.decoder, name) for name in
                                   ("crc_errors", "format_errors", "code_errors", "frame_gaps", "slot_gaps", "resync_bytes", "ended")}
        report["recording"] = dict(active=self.recorder is not None,
                                   bytes=getattr(self.recorder,'bytes_written',0),
                                   free_bytes=getattr(self.recorder,'free_bytes',None),
                                   queued=self.recorder.queue.qsize() if self.recorder else 0,
                                   samples=(self.recorder.samples if hasattr(self.recorder,'samples') else 0) if self.recorder else 0,
                                   elapsed_s=(archive_snapshot['recording_wall_elapsed_s']
                                              if isinstance(self.recorder,ArchiveRecorder) else
                                              time.monotonic()-self.recorder.started_monotonic if self.recorder else 0),
                                   path=str(self.recorder.root if isinstance(self.recorder,ArchiveRecorder) else self.recorder.base) if self.recorder else None,
                                   error=self.recorder.error if self.recorder else None)
        report['archive']=archive_snapshot
        report['spool'] = (dict(enabled=self.spool_enabled, count=self.spool.count,
                                saved_count=self.spool.saved_count, error=self.spool.error,
                                queued=self.spool.queue.qsize(), path=str(self.spool.path))
                           if self.spool else dict(enabled=self.spool_enabled, count=0,
                                                   saved_count=0, error=None, queued=0, path=None))
        transport = report.get("transport", {})
        counters = self.final_stats or {}
        status = self.diagnostics.get("stream status", {})
        report["dma_errors"] = status.get("DMA_ERRORS")
        report["health"] = dict(
            transport_errors=any(transport.get(k, 0) for k in
                                 ("crc_errors", "format_errors", "frame_gaps", "resync_bytes")) or
                             any(counters.get(k, 0) for k in ("FRAMES_DROPPED", "TX_BUFFER_OVERFLOWS")) or
                             'USB_SAMPLE_COUNT_MISMATCH' in self.session_errors,
            acquisition_losses=any(counters.get(k, 0) for k in ("LOST_SAMPLE_SLOTS", "ACQ_BUFFER_OVERFLOWS")),
            measured_anomalies=self.service.anomalies,
            code_errors=transport.get("code_errors", 0),
            session_verified=bool(self.decoder and self.decoder.ended and self.diagnostics and not self.session_errors))
        if self.profile and self.profile.get('event_driven'):
            native = self.diagnostics.get('acq native report', {})
            report['native'] = native
            if native:
                report['health']['acquisition_losses'] = bool(
                    int(native.get('NATIVE_RUNTIME_EVENTS_LOST', '0')) or
                    int(native.get('NATIVE_QUEUE_DROPS', '0')) or counters.get('ACQ_BUFFER_OVERFLOWS', 0))
        return report

    def _disconnect(self, reason=None):
        presence = self.presence_monitor.latest if self.presence_monitor else None
        classification = ('B_USB_ABSENT' if presence and not presence.get('present', True) else
                          'C_PRESENT_UNRESPONSIVE' if reason and 'CONTROL_UNRESPONSIVE' in reason else
                          'A_HOST_CLOSE_DEVICE_PRESENT' if presence and presence.get('present') else
                          'USB_PRESENCE_UNKNOWN')
        self.trace.event('CLOSE_REASON', f'reason={reason!r} class={classification} presence={presence!r}')
        if reason:
            self.session_errors.append(reason)
            self.emit("error", reason)
        self.set_state('disconnecting')
        close_error = None
        if self.port is not None:
            for method_name, stage in (('cancel_read', 'SERIAL_CANCEL_READ'),
                                       ('cancel_write', 'SERIAL_CANCEL_WRITE')):
                method = getattr(self.port, method_name, None)
                if method is not None:
                    self.trace.event(stage, self._serial_state())
                    try:
                        method()
                    except Exception as error:
                        self.trace.event(stage + '_FAILED',
                                         f'type={type(error).__name__} error={error}')
            self.trace.begin('serial', 'SERIAL_CLOSE_STARTED', self._serial_state())
            try:
                self.port.close()
                still_open = (self.port.is_open if hasattr(self.port, 'is_open') else
                              not self.port.closed if hasattr(self.port, 'closed') else False)
                if still_open:
                    raise RuntimeError('serial.close returned while port remains open')
            except Exception as error:
                close_error = error
                self.trace.end('serial', 'SERIAL_CLOSE_FAILED',
                               f'type={type(error).__name__} error={error} {self._serial_state()}')
                self.emit('error', f'Serial close failed: {type(error).__name__}: {error}')
            else:
                self.trace.end('serial', 'SERIAL_CLOSE_FINISHED', self._serial_state())
                self.port = self.protocol = None
        if self.recorder:
            self.recorder.finish(dict(self.snapshot_report(), disconnect_reason=reason,
                                      session_end_verified=bool(self.decoder and self.decoder.ended),
                                      acquisition_finished=bool(self.decoder and self.decoder.ended)))
        if self.spool:
            self.spool.finish(dict(self.snapshot_report(), disconnect_reason=reason,
                                   session_end_verified=bool(self.decoder and self.decoder.ended),
                                   acquisition_finished=bool(self.decoder and self.decoder.ended)))
        if self.detector:
            self.detector.finish()
        self.record_directory = None
        self.disconnect_requested = False
        self.set_state('disconnect_failed' if close_error else 'disconnected')
        if close_error is None:
            self.trace.event('DISCONNECTED', self._serial_state())
