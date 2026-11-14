import json
import queue
import threading
import time
import weakref
from collections import deque
from bisect import bisect_left, bisect_right
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets
from serial.tools import list_ports
from .calibration_view import CalibrationView
from .diagnostics_view import DiagnosticsView
from .renderer import PyQtGraphRenderer
from .data import FILTERS, EnvelopeIndex, anomaly_mask, robust_y_range, statistics, with_filters
from .recorder import open_history
from .trigger import TriggerConfig
from .range_stats import RawRangeIndex
from .session_tools import SessionToolsMixin
from .events_view import EventsView
from .anomaly_diagnostics import DetectorRules, analyze_history
from .data import RAW_DTYPE
from .archive import ArchiveReader
from .workbench import WorkbenchMixin
from . import __version__
from .mode_capabilities import selection_error


def number(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


class MainWindow(WorkbenchMixin, SessionToolsMixin, QtWidgets.QMainWindow):
    def __init__(self, worker, demo=False, auto_port=None, auto_start=False, record_directory=None):
        super().__init__()
        self.worker = worker
        self.demo, self.auto_start = demo, auto_start
        self.follow = True
        self.source = None
        self.source_archive = None
        self.archive_readers = weakref.WeakSet()
        self.next_archive_hover=0.0
        self.indexes = {}
        self.last_revision = -1
        self.needs_redraw = True
        self.next_overview = 0.0
        self.last_recording = None
        self.closing = False
        self.load_events = queue.SimpleQueue()
        self.limit_ma, self.step_ma = 10000.0, 1000.0
        self.latest = {}
        self.draw_count = 0
        self._updating = False
        self.ui_periods = deque(maxlen=2000)
        self.last_ui_start = None
        self.stage_timings = {name: deque(maxlen=2000) for name in
                              ('prepare', 'main_submit', 'overview_submit', 'indicators', 'event_callback')}
        self.setWindowTitle(f"CurrentSensor • измерение и анализ v{__version__}" + (" • DEMO / REPLAY" if demo else ""))
        self.resize(1360, 920)
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        connection = QtWidgets.QHBoxLayout()
        self.ports = QtWidgets.QComboBox()
        self.ports.setEditable(True)
        self.ports.setMinimumWidth(180)
        self.scan = QtWidgets.QPushButton("COM ↻")
        self.scan.clicked.connect(self.refresh_ports)
        self.connect_button = QtWidgets.QPushButton("Подключить")
        self.connect_button.clicked.connect(lambda: worker.request("connect", port=self.ports.currentText().split(" — ")[0]))
        self.disconnect_button = QtWidgets.QPushButton("Отключить")
        self.disconnect_button.clicked.connect(lambda: worker.request("disconnect"))
        self.connection_state = QtWidgets.QLabel("Нет соединения")
        for widget in (self.ports, self.scan, self.connect_button, self.disconnect_button, self.connection_state):
            connection.addWidget(widget)
        connection.addStretch()
        root.addLayout(connection)
        profile_row = QtWidgets.QHBoxLayout()
        self.profiles = QtWidgets.QComboBox()
        self.profiles.setMinimumWidth(210)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["DMA", "POLL (диагностика)"])
        self.mode.addItem('Native DMA · 84 us · CSF4 v2')
        self.mode.setItemText(0, 'DMA · continuous')
        self.mode.setItemText(1, 'POLL · continuous · диагностика')
        self.apply_profile = QtWidgets.QPushButton("Применить профиль")
        self.apply_profile.clicked.connect(lambda: worker.request("profile", name=self.profiles.currentData(), mode=self.selected_mode()))
        self.start_button = QtWidgets.QPushButton("▶ Измерять")
        self.stop_button = QtWidgets.QPushButton("■ Остановить")
        self.start_button.clicked.connect(self.start_measurement)
        self.stop_button.clicked.connect(lambda: worker.request("stop"))
        for widget in (self.profiles, self.mode, self.apply_profile, self.start_button, self.stop_button):
            profile_row.addWidget(widget)
        profile_row.addStretch()
        root.addLayout(profile_row)
        self.profile_label = QtWidgets.QLabel("Параметры профиля будут прочитаны из прибора")
        root.addWidget(self.profile_label)
        self.mode_hint = QtWidgets.QLabel('Выбор не меняет прибор. Примените профиль перед измерением.')
        self.mode_hint.setWordWrap(True)
        self.mode.currentIndexChanged.connect(self.update_selection)
        self.profiles.currentIndexChanged.connect(self.update_selection)
        recording = QtWidgets.QHBoxLayout()
        self.directory = QtWidgets.QLineEdit(record_directory or str(Path.cwd() / "recordings"))
        browse = QtWidgets.QPushButton("Каталог…")
        browse.clicked.connect(self.choose_directory)
        self.record_button = QtWidgets.QPushButton("● Запись RAW")
        self.record_button.setCheckable(True)
        self.record_button.toggled.connect(self.toggle_recording)
        self.record_button.toggled.connect(lambda _: self.refresh_archive_controls())
        open_button = QtWidgets.QPushButton("Открыть эксперимент…")
        open_button.clicked.connect(self.choose_history)
        self.preview = QtWidgets.QPushButton("Последняя запись")
        self.preview.clicked.connect(lambda: self.load_history(self.last_recording) if self.last_recording else None)
        for widget in (self.directory, browse, self.record_button, open_button, self.preview):
            recording.addWidget(widget)
        root.addLayout(recording)
        archive_row=QtWidgets.QHBoxLayout()
        self.long_record=QtWidgets.QCheckBox('Длительная запись CSF4')
        self.long_record.toggled.connect(lambda enabled:
            self.record_button.setText('● Архив CSF4' if enabled else '● Запись RAW'))
        self.segment_size=QtWidgets.QComboBox()
        for size in (25,100,250,500,1024):
            self.segment_size.addItem(f'{size} MiB',size)
        self.segment_size.setCurrentIndex(1)
        self.segment_seconds=QtWidgets.QSpinBox();self.segment_seconds.setRange(0,86400)
        self.segment_seconds.setSuffix(' s/сегм.; 0=без лимита')
        self.disk_reserve=QtWidgets.QSpinBox();self.disk_reserve.setRange(0,1048576)
        self.disk_reserve.setValue(1024);self.disk_reserve.setSuffix(' MiB резерв')
        self.archive_compression=QtWidgets.QCheckBox('zlib1 (экспериментально)')
        for widget in (self.long_record,self.segment_size,self.segment_seconds,
                       self.disk_reserve,self.archive_compression):
            archive_row.addWidget(widget)
        archive_row.addStretch()
        root.addLayout(archive_row)
        self.archive_state=QtWidgets.QLabel('Архив: выключен')
        root.addWidget(self.archive_state)
        self.record_state = QtWidgets.QLabel("Запись выключена; Freeze не влияет на запись")
        self.record_state.setWordWrap(True)
        root.addWidget(self.record_state)
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)
        scope = QtWidgets.QWidget()
        scope_layout = QtWidgets.QVBoxLayout(scope)
        self.current = QtWidgets.QLabel("— mA RAW")
        self.current.setStyleSheet("font-size: 30px; font-weight: 600; color: #52dfc5")
        self.stats = QtWidgets.QLabel("MIN / MAX / AVG / STD")
        self.counters = QtWidgets.QLabel("Частота —; отсчёты —; CRC —; SPI —; DMA —")
        scope_layout.addWidget(self.current)
        scope_layout.addWidget(self.stats)
        scope_layout.addWidget(self.counters)
        controls = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QComboBox()
        self.filter.addItems(FILTERS)
        self.filter.currentIndexChanged.connect(lambda: self.redraw())
        self.time_div = QtWidgets.QComboBox()
        for label, value in (("100 µs/div", .0001), ("1 ms/div", .001), ("10 ms/div", .01),
                             ("100 ms/div", .1), ("1 s/div", 1), ("5 s/div", 5), ("10 s/div", 10), ("60 s/div", 60)):
            self.time_div.addItem(label, value)
        self.time_div.setCurrentIndex(3)
        self.time_div.currentIndexChanged.connect(self.set_time_div)
        self.current_div = QtWidgets.QComboBox()
        for value in (1, 5, 10, 50, 100, 500, 1000, 5000):
            self.current_div.addItem(f"{value} mA/div", value)
        self.current_div.setCurrentIndex(4)
        self.current_div.currentIndexChanged.connect(self.set_current_div)
        self.freeze = QtWidgets.QCheckBox("Freeze")
        self.freeze.toggled.connect(self.freeze_display)
        live = QtWidgets.QPushButton("Live")
        live.clicked.connect(self.return_live)
        auto_x = QtWidgets.QPushButton("Auto X")
        auto_x.clicked.connect(self.auto_x)
        self.auto_y = QtWidgets.QCheckBox("Auto Y (robust)")
        self.auto_y.setChecked(True)
        self.auto_y.toggled.connect(lambda: self.redraw())
        self.cursor_toggle = QtWidgets.QCheckBox("Курсоры")
        self.roi_toggle = QtWidgets.QCheckBox("Область")
        for widget in (self.filter, self.time_div, self.current_div, self.freeze, live, auto_x,
                       self.auto_y, self.cursor_toggle, self.roi_toggle):
            controls.addWidget(widget)
        scope_layout.addLayout(controls)
        bounds = QtWidgets.QHBoxLayout()
        self.y_min, self.y_max = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        for spin, value in ((self.y_min, -10), (self.y_max, 200)):
            spin.setRange(-1e9, 1e9)
            spin.setValue(value)
            spin.setSuffix(" mA")
        bounds.addWidget(QtWidgets.QLabel("Y min / max"))
        bounds.addWidget(self.y_min); bounds.addWidget(self.y_max)
        apply_y = QtWidgets.QPushButton("Установить Y")
        apply_y.clicked.connect(self.set_y_bounds)
        bounds.addWidget(apply_y)
        bounds.addWidget(QtWidgets.QLabel("Колесо: X; Shift+колесо: Y; Ctrl+колесо: X+Y; drag: pan"))
        bounds.addStretch()
        scope_layout.addLayout(bounds)
        self.renderer = PyQtGraphRenderer()
        self.renderer.manual_view.connect(self.manual_view)
        self.renderer.cursor_changed.connect(self.cursor_update)
        self.renderer.region_changed.connect(self.region_update)
        self.renderer.anomaly_clicked.connect(self.jump_to_anomaly)
        self.renderer.view.sigRangeChanged.connect(lambda *_: self.redraw())
        self.hover_proxy = pg.SignalProxy(self.renderer.plot.scene().sigMouseMoved, rateLimit=30, slot=self.hover_point)
        self.cursor_toggle.toggled.connect(self.renderer.show_cursors)
        self.roi_toggle.toggled.connect(self.renderer.roi.setVisible)
        scope_layout.addWidget(self.renderer, 1)
        self.cursor_label = QtWidgets.QLabel("Δt —; 1/Δt —; ΔI —")
        self.region_label = QtWidgets.QLabel("Область: MIN / MAX / AVG / RMS исходного тока")
        self.view_label = QtWidgets.QLabel("RAW • пики сохраняются; длительность субпиксельного события не восстановлена")
        scope_layout.addWidget(self.cursor_label)
        scope_layout.addWidget(self.region_label)
        scope_layout.addWidget(self.view_label)
        self.tabs.addTab(scope, "Осциллограф")
        self.calibration = CalibrationView()
        self.calibration.command.connect(lambda command: worker.request("text", command=command))
        self.tabs.addTab(self.calibration, "Калибровка")
        self.diagnostics = DiagnosticsView()
        self.diagnostics.refresh_requested.connect(self.refresh_diagnostics)
        self.diagnostics.anomaly_requested.connect(lambda limit, step: worker.request("anomaly", limit_ma=limit, step_ma=step))
        self.tabs.addTab(self.diagnostics, "Диагностика")
        self.events_view = EventsView()
        self.events_view.navigate.connect(self.navigate_candidate)
        self.events_view.save_range.connect(self.save_candidate_range)
        self.events_view.threshold_changed.connect(self.set_detector_threshold)
        self.tabs.addTab(self.events_view, 'События')
        self.last_event_view = (None, -1, -1)
        self.next_event_refresh = 0
        self.trigger_panel = self.make_trigger_panel()
        self.tabs.addTab(self.trigger_panel, "Software trigger")
        self.init_session_tools(root, scope_layout, recording, controls)
        self.init_workbench(scope_layout,bounds,root,recording,archive_row,profile_row,controls)
        self.statusBar().showMessage("DEMO / REPLAY: реальные измерения не выполняются" if demo else "Готово")
        self.setStyleSheet("QMainWindow,QWidget {background:#17222d;color:#dce6ee;} "
                           "QPushButton,QComboBox,QSpinBox,QDoubleSpinBox,QLineEdit {padding:5px;} "
                           "QPushButton:disabled {color:#8293a1;} QPushButton:checked {background:#1e665e;color:white;} "
                           "QTabBar::tab {padding:6px 12px;background:#263746;color:#b8c8d4;} "
                           "QTabBar::tab:selected {background:#355467;color:white;} "
                           "QTabWidget::pane {border:1px solid #344757;}")
        self.refresh_ports()
        self.set_connection_state("disconnected")
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.update_ui)
        self.timer.start()
        if auto_port:
            self.ports.setCurrentText(auto_port)
            QtCore.QTimer.singleShot(50, lambda: worker.request("connect", port=auto_port))

    def selected_mode(self):
        if self.mode.currentIndex() == 2: return 'native'
        return "dma" if self.mode.currentIndex() == 0 else "poll"

    def update_selection(self, *_):
        info = self.worker.info or {}
        firmware = info.get('info', {}).get('FW')
        native_error = selection_error('native', firmware)
        item = self.mode.model().item(2)
        item.setEnabled(not native_error)
        item.setToolTip(native_error or 'FAST / DMA / 84 µs / ALATCH 1 / VERIFY OFF; около 11912 Гц, CSF4 v2')
        error = selection_error(self.selected_mode(), firmware, self.profiles.currentData(), info.get('profiles'))
        native = self.selected_mode() == 'native'
        # This is a local preset selection only. No worker requests here.
        if native and not error:
            index = self.profiles.findData('FAST')
            if index >= 0 and self.profiles.currentIndex() != index:
                self.profiles.setCurrentIndex(index)
        self.profiles.setEnabled(self.worker.state == 'idle' and not native)
        hint = error or ('Выбрано Native: FAST / DMA / 84 µs / ALATCH 1 / VERIFY OFF; ≈11912 Гц, не 10000 Гц.'
                         if native else 'Выбран continuous: частота по профилю; CSF4 v1.' +
                         (' POLL — диагностический режим; потери слотов проверяйте отдельно.' if self.selected_mode()=='poll' else ''))
        if error and self.selected_mode()=='poll' and self.profiles.currentData()=='FAST' and firmware in ('0.5.0','1.0.0'):
            hint = 'POLL/FAST: streaming не прошёл проверку STOP (нет END). Выберите DMA/FAST или Native. Диагностика POLL доступна через CLI.'
        self.mode_hint.setText(hint + ('' if error else ' Изменения — только по «Применить» / «Измерять».'))
        self.mode_hint.setToolTip(error or hint)
        self.mode_hint.setStyleSheet('color: #dc8b44' if error else '')
        if hasattr(self, 'advanced_area'):
            self.advanced_area.schedule()
        for button in (self.start_button, self.apply_profile):
            button.setEnabled(self.worker.state == 'idle' and not error)

    def refresh_ports(self):
        old = self.ports.currentText()
        self.ports.clear()
        if self.demo:
            self.ports.addItem("DEMO")
        else:
            for port in list_ports.comports():
                self.ports.addItem(f"{port.device} — {port.description}")
        if old:
            self.ports.setCurrentText(old)

    def set_connection_state(self, state):
        idle = state == "idle"
        labels = {'disconnected': 'DISCONNECTED', 'disconnecting': 'DISCONNECTING',
                  'disconnect_failed': 'DISCONNECT FAILED', 'connecting': 'CONNECTING'}
        self.connection_state.setText(labels.get(state, 'CONNECTED · ' + state.upper()))
        self.connect_button.setEnabled(state == "disconnected")
        self.disconnect_button.setEnabled(state in ('idle', 'streaming', 'stopping',
                                                    'disconnect_failed', 'connecting'))
        self.ports.setEnabled(state == "disconnected")
        self.scan.setEnabled(state == "disconnected")
        for control in (self.profiles, self.apply_profile, self.mode, self.start_button, self.calibration):
            control.setEnabled(idle)
        for control in (self.profiles,self.mode,self.apply_profile):
            control.setToolTip('Параметры прибора можно менять только после остановки acquisition.' if not idle else
                'Выбор поддерживаемого режима; изменения применяются явно при запуске или кнопкой применения.')
        self.stop_button.setEnabled(state == "streaming")
        self.diagnostics.set_idle(idle)
        self.refresh_archive_controls()
        self.events_view.threshold.setEnabled(state in ('idle','disconnected'))
        if state == 'starting':
            self.buffer_saved_count = 0
        self.update_selection()

    def update_profile(self, profile):
        index = self.profiles.findData(profile["name"])
        if index >= 0:
            self.profiles.setCurrentIndex(index)
        self.profile_label.setText(f"{profile['name']} | {profile['sample_rate_hz']} Hz | "
                                  f"период {profile['sample_period_us']} µs | VSHCT {profile['vshct_us']} µs | "
                                  f"AVG {profile['avg']} | ADC_CONFIG 0x{profile['adc_config']:04X} | "
                                  f"{profile.get('acq_mode')} | FW {profile.get('firmware_version')}")
        self.profiles.setToolTip(self.profile_label.text())
        if profile.get('event_driven'):
            self.profile_label.setText('Native DMA | ADC 84 us | реальные timestamps | частота показана по принятым данным')
        self.diagnostics.update_report('active_profile', profile)
        selected_mode = ('native' if profile.get('event_driven') else
                         'poll' if profile.get('acq_mode') == 'POLL' else 'dma')
        self.mode.setCurrentIndex({'dma': 0, 'poll': 1, 'native': 2}[selected_mode])
        self.update_selection()

    def start_measurement(self):
        self.last_recording_result=None
        self.invalidate_panel()
        self.return_live()
        self.record_state.setText('Новая сессия · RAW-история будет начата при старте')
        self.worker.request("start", profile=self.profiles.currentData(), mode=self.selected_mode())

    def choose_directory(self):
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Каталог записи", self.directory.text())
        if selected:
            self.directory.setText(selected)

    def toggle_recording(self, enabled):
        if enabled:
            directory=self.recording_directory()
            if directory is None:
                self.record_button.blockSignals(True);self.record_button.setChecked(False);self.record_button.blockSignals(False)
                return
            if self.long_record.isChecked():
                options=dict(segment_mib=self.segment_size.currentData(),
                             segment_seconds=self.segment_seconds.value(),
                             reserve_mib=self.disk_reserve.value(),
                             compression='zlib1' if self.archive_compression.isChecked() else 'none')
                self.worker.request('record_start',directory=directory,
                                    mode='long',archive_options=options)
            else:
                self.worker.request('record_start',directory=directory,mode='short')
        else:
            self.worker.request('record_stop')

    def refresh_archive_controls(self):
        enabled=(self.worker.state in ('idle','disconnected') and
                 not self.record_button.isChecked())
        for widget in (self.long_record,self.segment_size,self.segment_seconds,
                       self.disk_reserve,self.archive_compression):
            widget.setEnabled(enabled)

    def choose_history(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Открыть эксперимент", self.directory.text(),
                                                           "CurrentSensor (*.json *.csv *.raw.bin *.csf4)")
        if filename:
            self.load_history(filename)

    def load_history(self, filename, metadata_override=None):
        self.invalidate_panel()
        self.view_generation += 1
        generation=self.view_generation
        self.statusBar().showMessage("Чтение истории и подготовка кеша…")
        current_threshold = self.events_view.threshold.value()
        def load():
            archive=None
            try:
                if Path(filename).name=='session.json' or Path(filename).is_dir():
                    archive=ArchiveReader(filename)
                    metadata=dict(archive.manifest.get('source_metadata') or {},
                                  archive_manifest=archive.manifest,
                                  source_session=archive.manifest.get('source_session'))
                    metadata['_source_path']=str(Path(filename).resolve())
                    metadata['_analyzer']=archive.load_analyzer()
                    self.load_events.put((archive,metadata,filename,None,generation))
                    return
                data, metadata = open_history(filename)
                metadata.update(metadata_override or {})
                params = metadata.get('detector_parameters') or {}
                legacy_rules = metadata.get('anomaly_rules') or {}
                rules = DetectorRules(float(params.get('median_ma',current_threshold)),
                                      float(params.get('absolute_ma',legacy_rules.get('limit_ma',10000))),
                                      float(params.get('step_ma',legacy_rules.get('step_ma',1000))),
                                      params.get('method','local_median5_mad8_v1')=='local_median5_mad8_v1')
                metadata['_analyzer'] = analyze_history(data, metadata.get('source_session','legacy'), rules)
                metadata['_range_index'] = RawRangeIndex.from_data(data)
                metadata['_envelope_index']=EnvelopeIndex(data)
                if len(data):metadata['_envelope_index'].query(float(data['time_s'][0]),float(data['time_s'][-1]),1400)
                self.load_events.put((data, metadata, filename, None,generation))
            except Exception as error:
                if archive is not None:
                    archive.close()
                self.load_events.put((None, None, filename, str(error),generation))
        thread = threading.Thread(target=load, name="History loader", daemon=True)
        self.history_threads.append(thread)
        thread.start()

    def refresh_diagnostics(self):
        for command in ("ping", "info", "ina", "regs", "measure", "acq config", "cal show"):
            self.worker.request("text", command=command)

    def hover_point(self, args):
        point = self.renderer.view.mapSceneToView(args[0])
        stamp = point.x()
        if self.source_archive is not None and not self.follow:
            now=time.monotonic()
            if now<self.next_archive_hover:
                return
            self.next_archive_hover=now+.05
            try:
                data=self.source_archive.raw_range(stamp-.001,stamp+.001)
            except (OSError,ValueError) as error:
                self.statusBar().showMessage('Архив: ошибка чтения — '+str(error))
                return
            if not len(data):
                return
            pos=min(len(data)-1,bisect_left(data['time_s'],stamp))
            sample=data[pos]
            self.statusBar().showMessage(f"Архив RAW slot={int(sample['slot'])}; TIM2={int(sample['tim2_read_us'])}; "
                                         f"t={sample['time_s']:.6f} s; I={sample['vshunt_code']/16:.6f} mA")
            return
        data = self.source if self.source is not None else self.worker.service.snapshot(stamp, stamp)
        if data is None or not len(data):
            return
        pos = min(len(data)-1, bisect_left(data['time_s'], stamp))
        if pos and abs(data['time_s'][pos-1]-stamp) < abs(data['time_s'][pos]-stamp):
            pos -= 1
        sample = data[pos]
        self.statusBar().showMessage(f"RAW slot={int(sample['slot'])}; TIM2={int(sample['tim2_read_us'])} µs; "
                                     f"t={sample['time_s']:.6f} s; I={sample['vshunt_code']/16:.6f} mA")

    def redraw(self):
        self.needs_redraw = True

    def freeze_display(self, frozen):
        self.follow = not frozen
        self.renderer.set_follow_mode(self.follow)
        if frozen and self.source is None:
            with self.worker.service.lock:
                self.source = self.worker.service.snapshot()
                self.capture_view_context(self.worker.service.count)
            self.extend_frozen_history()
            # Freeze preserves both axes even if auto-Y was previously enabled.
            self.auto_y.setChecked(False)
        if not frozen:
            self.source = None
        self.indexes.clear()
        self.cursor_dirty = True
        self.redraw()
        self.refresh_view_state()

    def return_live(self):
        self.view_generation += 1
        self.source_archive=None
        self.source = None
        self.indexes.clear()
        self.follow = True
        self.renderer.set_follow_mode(True)
        self.history_loaded = False
        self.source_spool = None
        self.source_analyzer = None
        self.source_range_index = None
        self.cursor_dirty = True
        self.freeze.blockSignals(True); self.freeze.setChecked(False); self.freeze.blockSignals(False)
        self.redraw()
        self.time_window_s=self.follow_window_s
        self.refresh_view_state()

    def manual_view(self):
        if self.follow:
            self.freeze.setChecked(True)
        self.auto_y.setChecked(False)
        x=self.renderer.view.viewRange()[0];self.time_window_s=x[1]-x[0]
        self.redraw()
        self.refresh_view_state()

    def set_time_div(self):
        left, right = self.renderer.view.viewRange()[0]
        if self.time_div.currentData() is None:
            return
        width = self.time_div.currentData() * 10
        self.follow_window_s=float(width)
        self.sync_window_choice(float(width))
        self.renderer.view.setXRange(right - width, right, padding=0)
        self.redraw()

    def set_current_div(self):
        low, high = self.renderer.view.viewRange()[1]
        if self.current_div.currentData() is None:
            return
        half = self.current_div.currentData() * 4 * self.unit_factor()
        self.auto_y.setChecked(False)
        self.renderer.view.setYRange((low + high) / 2 - half, (low + high) / 2 + half, padding=0)

    def set_y_bounds(self):
        if self.y_min.value() >= self.y_max.value():
            self.statusBar().showMessage("Y min должен быть меньше Y max")
            return
        self.auto_y.setChecked(False)
        self.renderer.view.setYRange(self.y_min.value(), self.y_max.value(), padding=0)

    def auto_x(self):
        if self.source_archive is not None and not self.follow:
            self.renderer.view.setXRange(self.source_archive.first_time_s,
                self.source_archive.last_time_s+1e-6,padding=0)
            return
        if self.source is None:
            self.freeze.setChecked(True)
        if self.source is not None and len(self.source):
            self.renderer.view.setXRange(float(self.source["time_s"][0]), float(self.source["time_s"][-1]) + 1e-6, padding=0)

    def cursor_update(self, metrics):
        self.cursor_label.setText(f"КУРСОРЫ: Δt = {abs(metrics['dt']):.9g} с | 1/Δt = {number(metrics['frequency'])} Hz | ΔI(Y1–Y2) = {metrics['di']:.6g} {self.units.currentText() if hasattr(self,'units') else 'mA'}")
        self.cursor_dirty = True

    def region_update(self, left, right):
        self.region_label.setText('ROI RAW: расчёт MIN / MAX / AVG / RMS…')
        self.request_statistics('roi', left, right)

    def jump_to_anomaly(self, stamp, value):
        analyzer = self.current_analyzer()
        if analyzer:
            with analyzer.lock:
                pos = bisect_left(analyzer.event_times, stamp-1e-7)
                nearby = (analyzer.events[pos:pos+1] if pos < len(analyzer.events) and
                          abs(analyzer.event_times[pos]-stamp)<1e-7 else [])
            if nearby:
                self.navigate_candidate(nearby[0], True)
                return
        if self.source is None:
            self.freeze.setChecked(True)
        period = (self.worker.profile.get('nominal_period_us') or self.worker.profile["sample_period_us"]) / 1e6 if self.worker.profile else .0001
        self.renderer.view.setXRange(stamp - 10 * period, stamp + 10 * period, padding=0)
        self.auto_y.setChecked(False)
        span = max(1, abs(value) * .2)
        self.renderer.view.setYRange(value - span, value + span, padding=0)
        self.statusBar().showMessage(f"Аномалия {self.calibrated.currentText()}: t={stamp:.9f} s, I={value:.6f} mA")

    def current_analyzer(self):
        if not self.follow:
            return self.source_analyzer
        return self.worker.detector.analyzer if self.worker.detector else None

    def set_detector_threshold(self, threshold):
        if self.worker.state not in ('idle','disconnected'):
            return
        self.worker.request('detector_rules', median_ma=threshold)
        if self.source_archive is not None:
            self.statusBar().showMessage('Архив показывает сохранённый каталог кандидатов; порог нового анализа применяется к следующей сессии')
            return
        if self.history_loaded and self.source is not None:
            data = self.source
            generation = self.view_generation
            rules = DetectorRules(threshold,self.limit_ma,self.step_ma,True)
            session_id = self.source_metadata.get('source_session','legacy')
            def reanalyze():
                try:
                    analyzer = analyze_history(data,session_id,rules)
                    self.session_events.put(('history_analyzer',(generation,analyzer,None)))
                except Exception as error:
                    self.session_events.put(('history_analyzer',(generation,None,str(error))))
            thread = threading.Thread(target=reanalyze,daemon=True,name='History candidate analyzer')
            self.history_threads.append(thread)
            thread.start()

    def navigate_candidate(self, event, offer_auto_y=True):
        if event is None:
            return
        if self.follow:
            self.freeze.setChecked(True)
        stamp = event['time_s']
        width = self.events_view.width.value()
        self.renderer.view.setXRange(stamp-width,stamp+width,padding=0)
        self.tabs.setCurrentIndex(0)
        self.statusBar().showMessage(f"Кандидат {event['rule']}: slot={event['slot']} TIM2={event['tim2_read_us']} "
                                     f"код={event['vshunt_code']}; физическая причина не установлена")
        if offer_auto_y and self.y_mode.currentIndex() == 2:
            self.y_mode.setCurrentIndex(1)
        self.redraw()

    def save_candidate_range(self,event,width):
        if event is None:
            return
        interval=(event['time_s']-width,event['time_s']+width)
        self.queue_export(self.export_source(interval=interval),'EVENT_RANGE',interval)

    def refresh_candidate_view(self):
        if self.tabs.currentWidget() is not self.events_view:
            return
        if time.monotonic() < self.next_event_refresh:
            return
        self.next_event_refresh = time.monotonic()+.2
        analyzer=self.current_analyzer()
        if analyzer is None:
            return
        with analyzer.lock:
            identity=(id(analyzer),analyzer.event_count,len(analyzer.events),analyzer.finished)
            if identity==self.last_event_view:
                return
            events=list(analyzer.events)
            total=analyzer.event_count
            complete=analyzer.finished and analyzer.complete
            error=analyzer.error
        self.last_event_view=identity
        self.events_view.set_events(events,total,complete,error)

    def update_absolute_warning(self):
        current = abs(self.latest.get('acquisition',{}).get('current_ma') or 0)
        message = (
            'Порог |I| ниже текущего рабочего тока: ожидается много кандидатов.'
            if current and self.limit_ma < current else '')
        if self.diagnostics.threshold_warning.text() != message:
            self.diagnostics.threshold_warning.setText(message)

    def make_trigger_panel(self):
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        self.trigger_mode = QtWidgets.QComboBox(); self.trigger_mode.addItems(["Off", "Auto", "Normal", "Single"])
        self.trigger_edge = QtWidgets.QComboBox(); self.trigger_edge.addItems(["Rising", "Falling", "Level"])
        form.addRow("Режим", self.trigger_mode); form.addRow("Условие", self.trigger_edge)
        self.trigger_values = {}
        for field, label, value, maximum in (("level_ma", "Уровень, mA", 150, 1e6),
                                            ("hysteresis_ma", "Гистерезис, mA", 2, 1e6),
                                            ("pre_s", "Предыстория, s", .1, 30),
                                            ("post_s", "После триггера, s", .2, 30),
                                            ("holdoff_s", "Holdoff, s", .5, 60)):
            spin = QtWidgets.QDoubleSpinBox(); spin.setDecimals(4)
            spin.setRange(-1e6 if field == "level_ma" else 0, maximum); spin.setValue(value)
            self.trigger_values[field] = spin; form.addRow(label, spin)
        arm = QtWidgets.QPushButton("Применить / Arm")
        arm.clicked.connect(lambda: self.worker.request("trigger", config=TriggerConfig(
            mode=self.trigger_mode.currentText(), edge=self.trigger_edge.currentText(),
            **{key: spin.value() for key, spin in self.trigger_values.items()})))
        form.addRow(arm)
        form.addRow(QtWidgets.QLabel("Триггер ПК анализирует полный RAW. Single фиксирует график, но не останавливает USB или запись.\n"
                                    "Auto выдаёт свободную развёртку при отсутствии события. Это не аппаратный триггер INA229."))
        return panel

    def update_ui(self):
        self.worker.trace.heartbeat_ui()
        if self._updating:
            return
        self._updating = True
        started = time.perf_counter()
        if self.last_ui_start is not None:
            self.ui_periods.append((started-self.last_ui_start)*1000)
        self.last_ui_start = started
        try:
            self._update_ui_body()
        finally:
            self.stage_timings['event_callback'].append((time.perf_counter()-started)*1000)
            self._updating = False

    def _update_ui_body(self):
        for _ in range(300):
            try:
                kind, payload = self.worker.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.set_connection_state(payload)
            elif kind == "connected":
                self.update_identity(payload)
                self.profiles.clear()
                for profile in payload["profiles"]:
                    self.profiles.addItem(f"{profile['name']} — {profile['sample_rate_hz']} Hz", profile["name"])
                self.update_profile(payload["profile"])
                self.calibration.update_fields(payload["cal show"])
                self.diagnostics.update_report("connection", payload)
                if self.auto_start:
                    self.auto_start = False
                    if self.directory.text() and self.record_button.isChecked():
                        options=dict(segment_mib=self.segment_size.currentData(),
                                     segment_seconds=self.segment_seconds.value(),
                                     reserve_mib=self.disk_reserve.value(),
                                     compression='zlib1' if self.archive_compression.isChecked() else 'none')
                        self.worker.request("record_start", directory=self.directory.text(),
                                            mode='long' if self.long_record.isChecked() else 'short',
                                            archive_options=options)
                    self.start_measurement()
            elif kind == "profile":
                self.update_profile(payload)
            elif kind == 'configuration_error':
                if not payload.get('confirmed'):
                    self.profile_label.setText('Конфигурация НЕ подтверждена. Повторно примените поддерживаемый профиль.')
                self.diagnostics.update_report('configuration_error', payload)
            elif kind == 'usb_presence':
                self.diagnostics.update_report('usb_presence', payload)
            elif kind == "text":
                self.diagnostics.update_report(payload["command"], payload["fields"])
                if payload["command"] == "cal show":
                    self.calibration.update_fields(payload["fields"])
                    self.update_identity(dict(self.worker.info or {},**{'cal show':payload['fields']}))
                elif payload["command"].startswith("cal "):
                    self.worker.request("text", command="cal show")
            elif kind == "summary":
                self.latest = payload
                self.update_summary(payload)
            elif kind == "diagnostics":
                self.diagnostics.update_report("last_stream", payload)
                self.events_view.set_mcu_report(payload.get('stream anomaly'),
                    self.worker.session_metadata.get('mcu_anomaly_entries'))
                self.calibration.update_fields(payload.get("cal show", {}))
            elif kind == "session_finished":
                self.diagnostics.update_report("session", payload)
                self.freeze.setChecked(True)
            elif kind == "recording":
                self.record_state.setText("Запись: " + payload)
            elif kind == "recorded":
                self.last_recording_result=payload
                self.last_recording = payload["path"]
                self.record_state.setText(f"Сохранено [{payload.get('integrity','PARTIAL')}] {payload['samples']} отсчётов: {payload['path']}" if not payload["error"] else payload["error"])
                self.record_button.blockSignals(True); self.record_button.setChecked(False); self.record_button.blockSignals(False)
                self.refresh_archive_controls()
                self.add_recent(payload['path'])
            elif kind == "anomaly_rules":
                self.limit_ma, self.step_ma = payload["limit_ma"], payload["step_ma"]
                self.update_absolute_warning()
                if self.diagnostics.threshold_warning.text():
                    self.statusBar().showMessage('Предупреждение: абсолютный порог ниже рабочего тока; ожидается много событий')
                self.redraw()
            elif kind == "log":
                self.diagnostics.log.appendPlainText(payload)
            elif kind == "error":
                self.statusBar().showMessage("ОШИБКА: " + payload)
                self.diagnostics.log.appendPlainText("ERROR: " + payload)
        try:
            data, metadata, filename, error, generation = self.load_events.get_nowait()
            if generation!=self.view_generation:
                if isinstance(data,ArchiveReader):
                    data.close()
                return
            if error:
                self.statusBar().showMessage("Ошибка истории: " + error)
            else:
                if isinstance(data,ArchiveReader):
                    self.source_archive=data
                    self.archive_readers.add(data)
                    self.source=np.empty(0,RAW_DTYPE)
                    self.source_range_index=None
                    self.source_analyzer=metadata.pop('_analyzer',None)
                    self.source_metadata=metadata
                    self.history_loaded=True
                    self.source_spool=None
                    self.source_count=data.sample_count
                    self.follow=False
                    self.freeze.blockSignals(True);self.freeze.setChecked(True);self.freeze.blockSignals(False)
                    self.indexes.clear()
                    trace=data.plot_range(data.first_time_s,data.last_time_s,max(300,self.renderer.width()))
                    self.renderer.set_overview_trace(trace,data.first_time_s,data.last_time_s)
                    self.request_statistics('overview',data.first_time_s,data.last_time_s)
                    self.refresh_overview_events()
                    self.renderer.view.setXRange(data.first_time_s,data.last_time_s+1e-6,padding=0)
                    self.diagnostics.update_report('archive_manifest',data.manifest)
                    self.events_view.set_mcu_report(data.manifest.get('mcu_anomaly_report'),
                        data.manifest.get('source_metadata',{}).get('mcu_anomaly_entries'))
                    self.statusBar().showMessage(f'Архив: {data.sample_count:,} RAW; индекс открыт без загрузки отсчётов')
                    self.cursor_dirty=True
                    self.redraw()
                    return
                self.source_archive=None
                self.source = data
                self.source_range_index = metadata.pop('_range_index')
                prepared_index=metadata.pop('_envelope_index',None)
                self.source_analyzer = metadata.pop('_analyzer')
                self.source_metadata = metadata
                self.history_loaded = True
                self.source_spool = None
                self.source_count = len(data)
                self.adopt_loaded_recovery(filename)
                self.cursor_dirty = True
                self.follow = False
                self.freeze.blockSignals(True); self.freeze.setChecked(True); self.freeze.blockSignals(False)
                self.indexes.clear()
                if prepared_index is not None:self.indexes['vshunt_code']=prepared_index
                self.renderer.set_overview(data,prepared_index)
                self.refresh_overview_events()
                self.auto_x()
                self.diagnostics.update_report("history_metadata", metadata)
                self.events_view.set_mcu_report(metadata.get('mcu_anomaly_report',
                    metadata.get('diagnostics',{}).get('stream anomaly')),
                    metadata.get('mcu_anomaly_entries'))
                self.statusBar().showMessage(f"История: {filename}; {len(data):,} точек, memory map")
                self.redraw()
        except queue.Empty:
            pass
        trigger = self.worker.service.take_trigger()
        if trigger:
            self.source = trigger.pop("data")
            self.capture_view_context()
            self.follow = False
            self.freeze.blockSignals(True); self.freeze.setChecked(True); self.freeze.blockSignals(False)
            self.indexes.clear()
            self.renderer.view.setXRange(trigger["time_s"] - trigger["pre_s"], trigger["time_s"] + trigger["post_s"], padding=0)
            self.diagnostics.update_report("software_trigger", trigger)
            self.redraw()
        if self.tabs.currentIndex() == 0:
            self.draw_scope()
        self.process_session_events()
        self.refresh_candidate_view()
        self.update_panel_statistics()
        self.request_average()

    def update_summary(self, report):
        started = time.perf_counter()
        stats = report["acquisition"]
        value = stats["current_ma"]
        scale, shift = 1.,0.
        if value is not None:
            value = value * scale + shift
        label = 'RAW'
        self.current.setText(f"{number(value*self.unit_factor() if value is not None else None,6 if self.unit_factor()!=1 else 4)} {self.units.currentText()} {label} · последний отсчёт")
        self.stats.setText(f"MIN {number(stats['min_ma'])} | MAX {number(stats['max_ma'])} | AVG {number(stats['mean_ma'])} | "
                           f"STD {number(stats['std_ma'])} mA | {stats['time_s']:.3f} s | {stats['rate_hz']:.1f} Hz")
        transport, device = report.get("transport", {}), report.get("device_counters") or {}
        self.counters.setText(f"N {stats['samples']:,} | разрывы старт/поток {stats['initial_slot_gaps']}/{stats['running_slot_gaps']} | "
                              f"период {stats['period_violations']} | CRC {transport.get('crc_errors', 0)} | "
                              f"SPI {device.get('SPI_ERRORS', '—')} | DMA {report.get('dma_errors') if report.get('dma_errors') is not None else '—'} | аномалии {stats['anomalies']}")
        self.counters.setText(self.counters.text()+f" | frame gaps {transport.get('frame_gaps',0)} | OVF acq/USB {device.get('ACQ_BUFFER_OVERFLOWS','—')}/{device.get('TX_BUFFER_OVERFLOWS','—')}")
        if report.get('native'):
            native = report['native']
            self.counters.setText(self.counters.text() +
                f" | Native losses {native.get('NATIVE_RUNTIME_EVENTS_LOST', '?')}"
                f" | Stop tail {native.get('NATIVE_STOP_DISCARDED', '?')}"
                f" | Service warnings {native.get('NATIVE_DMA_OVERRUNS', '?')}")
        if self.history_loaded and report['state']!='streaming':
            panel=self.last_panel_result
            value=panel.get('last_value_ma') if panel else None
            self.current.setText(f"{number(value*self.unit_factor() if value is not None else None,6)} {self.units.currentText()} RAW · последний отсчёт интервала")
            self.stats.setText('Статистика выбранной истории — в панели справа; исходные отсчёты не изменяются')
            self.counters.setText(f"История: {self.source_count:,} отсчётов | SUSPECT — диагностическая маркировка, не ошибка SPI")
        if report["recording"]["error"]:
            self.record_state.setText(report["recording"]["error"])
        elif report['recording']['active']:
            recording = report['recording']
            self.record_state.setText(f"Запись: {recording['elapsed_s']:.1f} s; {recording['samples']:,} точек; "
                                      f"{recording.get('bytes',0)/1024**2:.1f} MiB; очередь {recording['queued']}/{256 if report.get('archive') else 128}; {recording['path']}"
                                      +(f"; свободно {recording['free_bytes']/1024**3:.2f} GiB" if recording.get('free_bytes') is not None else ''))
        else:
            self.record_state.setText('Запись CSV выключена · «Сохранить замер» сохраняет доступную RAW-историю')
        archive=(self.source_archive.snapshot() if self.history_loaded and self.source_archive is not None
                 else report.get('archive'))
        if archive:
            archive_transport=(self.source_archive.manifest.get('transport') or {}
                               if self.history_loaded and self.source_archive is not None else transport)
            free=archive.get('free_bytes')
            elapsed=archive['acquisition_elapsed_s']
            parts=[f"Архив {archive['status']}",f"измерение {elapsed:.3f} с",
                   f"N {archive['samples']:,}",f"{archive['bytes']/1024**2:.1f} MiB",
                   f"сегмент {archive['segment_number']}",
                   f"CRC {archive_transport.get('crc_errors',0)}",
                   f"slots {archive['initial_slot_gaps'] + archive['slot_gaps']}"]
            wall=archive.get('recording_wall_elapsed_s')
            if wall is not None:
                parts.append(f"архивирование {wall:.1f} с")
            finalization=archive.get('finalization_duration_s')
            if finalization is not None and archive['status']!='RECORDING':
                parts.append(f"закрытие {finalization:.1f} с")
            if free is not None:
                parts.append(f"свободно {free/1024**3:.2f} GiB")
            if archive.get('error'):
                parts.append(f"ОШИБКА {archive['error']}")
            self.archive_state.setText(' | '.join(parts))
        else:
            self.archive_state.setText('Архив: выключен')
        self.stage_timings['indicators'].append((time.perf_counter()-started)*1000)
        self.update_absolute_warning()
        self.refresh_compact_status(report)

    def draw_scope(self):
        draw_started = time.perf_counter()
        summary = self.worker.service.summary() if self.follow else self.latest.get("acquisition", {})
        revision = summary.get("revision", -1)
        if not self.needs_redraw and (not self.follow or revision == self.last_revision):
            return
        if not self.follow and self.source_archive is not None:
            self.draw_archive_scope()
            return
        if self.follow:
            right = summary.get("time_s", 0)
            left = right - self.follow_window_s
            self.renderer.view.setXRange(left, right, padding=0)
            data = self.worker.service.snapshot(left, right)
            indexes = {}
        else:
            data = self.source
            indexes = self.indexes
        if data is None or not len(data):
            return
        left, right = self.renderer.view.viewRange()[0]
        fields = {"RAW": ["vshunt_code"], "MEDIAN3": ["median3"], "MEDIAN5": ["median5"],
                  "RAW + MEDIAN3": ["vshunt_code", "median3"]}[self.filter.currentText()]
        pixels = max(100, int(self.renderer.plot.width()))
        traces = []
        raw_trace = None
        for field in fields:
            if field not in indexes:
                indexes[field] = EnvelopeIndex(data, field)
            trace = indexes[field].query(left, right, pixels)
            traces.append(trace)
            if field == 'vshunt_code':
                raw_trace = trace
        if raw_trace is None:
            if "vshunt_code" not in indexes:
                indexes["vshunt_code"] = EnvelopeIndex(data)
            raw_trace = indexes["vshunt_code"].query(left, right, pixels)
        lo, hi = bisect_left(data['time_s'], left), bisect_right(data['time_s'], right)
        visible = data[lo:hi]
        if self.calibrated.currentIndex() and not self.calibration_available():
            self.calibrated.setCurrentIndex(0)
            self.statusBar().showMessage('Коэффициенты этой сессии недоступны; показан RAW')
        scale, shift = self.display_transform()
        if self.auto_y.isChecked() and len(visible):
            if self.y_mode.currentIndex() == 1:
                low, high = raw_trace.y.min(), raw_trace.y.max()
                pad = max(1, high - low) * .1
                bounds = low - pad, high + pad
            else:
                bounds = robust_y_range(visible["vshunt_code"])
            self.renderer.view.setYRange(*(value * scale + shift for value in bounds), padding=0)
        analyzer = self.current_analyzer()
        if analyzer is not None:
            with analyzer.lock:
                visible_events = analyzer.events[bisect_left(analyzer.event_times,left):
                                                 bisect_right(analyzer.event_times,right)][:1000]
            markers = np.empty(len(visible_events), RAW_DTYPE)
            for i,event in enumerate(visible_events):
                markers[i] = (event['slot'],event['tim2_read_us'],event['vshunt_code'],event['time_s'])
        else:
            markers = np.empty(0, RAW_DTYPE)
        self.stage_timings['prepare'].append((time.perf_counter()-draw_started)*1000)
        submitted_at = time.perf_counter()
        # Envelope endpoints include isolated extrema; markers are bounded by pixels.
        names = self.filter.currentText().split(' + ')
        if self.calibrated.currentIndex():
            names = [name + ' CAL' for name in names]
        self.renderer.set_trace_names(names)
        traces,markers=self.visual_traces(traces,markers)
        self.renderer.draw(traces, markers, scale=scale, shift=shift)
        self.stage_timings['main_submit'].append((time.perf_counter()-submitted_at)*1000)
        if time.monotonic() > self.next_overview:
            overview_at = time.perf_counter()
            self.renderer.set_overview(self.worker.service.snapshot() if self.follow else data)
            self.refresh_overview_events()
            self.stage_timings['overview_submit'].append((time.perf_counter()-overview_at)*1000)
            self.next_overview = time.monotonic() + 1
        self.view_label.setText(f"{'LIVE' if self.follow else 'FREEZE / HISTORY'} • {self.filter.currentText()} • "
                                f"{len(raw_trace.x)} элементов • {'min/max envelope; время экстремумов исходное' if raw_trace.aggregated else 'исходные точки'} • "
                                "маркеры за Y-масштабом кликабельны")
        self.needs_redraw = False
        self.last_revision = revision
        self.draw_count += 1
        self.graph_timings.append((time.perf_counter() - draw_started) * 1000)
        if len(self.graph_timings) > 2000:
            del self.graph_timings[:1000]

    def draw_archive_scope(self):
        try:
            self._draw_archive_scope()
        except (OSError,ValueError) as error:
            self.view_label.setText('АРХИВ: повреждение или ошибка чтения — '+str(error))
            self.statusBar().showMessage('Архив: ошибка чтения — '+str(error))
            self.needs_redraw=False

    def _draw_archive_scope(self):
        archive=self.source_archive
        left,right=self.renderer.view.viewRange()[0]
        pixels=max(100,int(self.renderer.plot.width()))
        field=self.filter.currentText()
        estimated=(right-left)*(self.source_metadata.get('profile') or {}).get('sample_rate_hz',10000)
        if field!='RAW' and estimated<=100000:
            period_us=(self.source_metadata.get('profile') or {}).get('sample_period_us',100)
            raw=archive.raw_range(max(archive.first_time_s,left-8*period_us/1e6),right)
            live=with_filters(raw)
            names=['vshunt_code'] if field=='RAW' else ['vshunt_code','median3'] if field=='RAW + MEDIAN3' else [field.lower()]
            traces=[EnvelopeIndex(live,name).query(left,right,pixels) for name in names]
            raw_trace=traces[0] if names[0]=='vshunt_code' else archive.plot_range(left,right,pixels)
            fallback=False
        else:
            traces=[archive.plot_range(left,right,pixels)]
            raw_trace=traces[0]
            fallback=field!='RAW'
        if not len(raw_trace.x):
            return
        if self.auto_y.isChecked():
            low,high=(float(np.min(raw_trace.y)),float(np.max(raw_trace.y)))
            if self.y_mode.currentIndex()==2:
                lo,hi=np.percentile(raw_trace.y,[1,99])
                low,high=float(lo),float(hi)
            pad=max(1,high-low)*.1
            scale,shift=self.display_transform(self.source_metadata)
            self.renderer.view.setYRange(low*scale+shift-pad,high*scale+shift+pad,padding=0)
        scale,shift=self.display_transform(self.source_metadata)
        self.renderer.set_trace_names(field.split(' + ') if not fallback else ['RAW (обзор)'])
        analyzer=self.current_analyzer()
        visible_events=[]
        if analyzer is not None:
            with analyzer.lock:
                visible_events=analyzer.events[bisect_left(analyzer.event_times,left):bisect_right(analyzer.event_times,right)][:1000]
        markers=np.empty(len(visible_events),RAW_DTYPE)
        for i,event in enumerate(visible_events):
            markers[i]=(event['slot'],event['tim2_read_us'],event['vshunt_code'],event['time_s'])
        traces,markers=self.visual_traces(traces,markers)
        self.renderer.draw(traces,markers,scale=scale,shift=shift)
        self.view_label.setText('АРХИВ • индекс min/max; исходные времена экстремумов' +
            (' • MEDIAN доступна после приближения к ≤100000 отсчётов' if fallback else ''))
        self.needs_redraw=False
        self.draw_count+=1

    def closeEvent(self, event):
        if self.close_phase == 'done':
            self.recording_settings.setChecked(False)
            self.worker.trace.disable_ui_watch()
            self.timer.stop()
            for reader in self.archive_readers:
                reader.close()
            self.archive_readers.clear()
            event.accept()
        else:
            self.begin_close()
            event.ignore()
