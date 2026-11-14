"""Session saving and asynchronous RAW interval measurements for the Qt window."""
import copy
import queue
import time
import threading
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets
from .export import ExportSource, ExportWorker
from .spool import RawSpool, SpoolReader, recoverable_spools
from .statistics_worker import StatisticsWorker
from .recorder import open_spool_history
from .data import EnvelopeIndex
from .archive import ArchiveReader
from .anomaly_diagnostics import DetectorRules


def fmt(value):
    return '—' if value is None else f'{value:.6g}'


class SessionToolsMixin:
    def init_session_tools(self, root, scope_layout, recording, controls):
        self.session_events = queue.SimpleQueue()
        self.exporter = ExportWorker(self.session_events)
        self.statistics_worker = StatisticsWorker(self.session_events)
        self.export_jobs = 0
        self.export_timings = []
        self.buffer_saved_count = 0
        self.range_tokens = {'cursor': 0, 'roi': 0, 'panel':0}
        self.range_pending = {'cursor': False, 'roi': False, 'panel':False}
        self.last_cursor_stats = None
        self.cursor_dirty = False
        self.next_cursor_update = 0.0
        self.next_status_update = 0.0
        self.next_scale_update = 0.0
        self.history_threads = []
        self.source_spool = None
        self.source_analyzer = None
        self.source_count = 0
        self.source_metadata = {}
        self.source_range_index = None
        self.history_loaded = False
        self.pending_recovery = {}
        self.view_generation = 0
        self.close_phase = None
        self.close_discard = False
        self.settings = QtCore.QSettings('CurrentSensor', 'GUI')
        self.time_window_s = self.time_div.currentData() * 10
        self.graph_timings = []
        self.profile_label.hide()
        self.auto_spool = QtWidgets.QCheckBox('Временная RAW-история')
        enabled = True if self.demo else self.settings.value('auto_spool', True, type=bool)
        self.auto_spool.setChecked(enabled)
        self.auto_spool.setToolTip('Полная сессия на диске: лимит 2 GiB, резерв свободного места 256 MiB. При отключении остаётся только live-кольцо.')
        self.worker.request('spool_enabled', enabled=enabled)
        self.auto_spool.toggled.connect(self.set_spool_enabled)
        row = QtWidgets.QHBoxLayout()
        self.save_full_button = QtWidgets.QPushButton('Сохранить замер')
        self.save_range_button = QtWidgets.QPushButton('Сохранить участок')
        self.save_png_button = QtWidgets.QPushButton('Экспорт PNG')
        self.save_full_button.clicked.connect(self.save_session)
        self.save_range_button.clicked.connect(self.save_interval)
        self.save_png_button.clicked.connect(self.save_png)
        self.export_mode = QtWidgets.QComboBox()
        self.export_mode.addItems(['Видимый X', 'ROI', 'Авто (ROI / X)'])
        self.export_mode.setToolTip('Включённые X1–X2 всегда имеют приоритет. Иначе — выбранный источник участка.')
        recover = QtWidgets.QPushButton('Восстановить…')
        recover.clicked.connect(self.recover_session)
        for widget in (self.save_full_button, self.save_range_button, self.export_mode,
                       self.save_png_button, self.auto_spool, recover):
            row.addWidget(widget)
        row.addStretch()
        root.insertLayout(4, row)
        self.session_state = QtWidgets.QLabel('IDLE · NOT RECORDING · UNSAVED')
        root.insertWidget(6, self.session_state)
        self.recent = QtWidgets.QComboBox()
        self.recent.setMinimumWidth(180)
        self.recent.setMaximumWidth(330)
        self.recent.addItem('Последние эксперименты', None)
        for path in ([] if self.demo else self.settings.value('recent', [], type=list)):
            self.recent.addItem(Path(path).name, path)
        self.recent.activated.connect(lambda i: self.load_history(self.recent.itemData(i)) if self.recent.itemData(i) else None)
        recording.removeWidget(self.preview)
        self.preview.hide()
        recording.addWidget(self.recent)
        self.shortcuts = []
        for key, callback in (('Ctrl+S', self.save_session), ('Ctrl+Shift+S', self.save_interval)):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self.calibrated = QtWidgets.QComboBox()
        self.calibrated.addItems(['RAW', 'CALIBRATED'])
        self.calibrated.setToolTip('Коррекция только отображения. Статистика X1–X2 и экспорт всегда RAW.')
        self.calibrated.currentIndexChanged.connect(self.redraw)
        controls.insertWidget(1, self.calibrated)
        self.y_mode = QtWidgets.QComboBox()
        self.y_mode.addItems(['Y: Manual', 'Y: Auto', 'Y: Robust'])
        self.y_mode.setCurrentIndex(2)
        self.auto_y.hide()
        controls.insertWidget(7, self.y_mode)
        self.y_mode.currentIndexChanged.connect(self.set_y_mode)
        self.auto_y.toggled.connect(self.sync_y_mode)
        for combo in (self.time_div, self.current_div):
            combo.setEditable(True)
            combo.lineEdit().setReadOnly(True)
        self.renderer.view.sigRangeChanged.connect(self.sync_scales)
        frame = QtWidgets.QFrame()
        frame.setObjectName('measurementBlock')
        frame.setStyleSheet('#measurementBlock {border:1px solid #416073; border-radius:3px; background:#10212d;}')
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(2)
        self.cursor_stats_label = QtWidgets.QLabel('X1–X2: включите курсоры для измерения RAW')
        self.clean_stats_label = QtWidgets.QLabel('RAW без кандидатов: включите курсоры')
        self.interval_label = QtWidgets.QLabel('ИНТЕРВАЛ: N = — | Δt = — с | GAPS = —')
        for widget in (self.cursor_label, self.region_label):
            scope_layout.removeWidget(widget)
        for widget in (self.cursor_label, self.cursor_stats_label, self.clean_stats_label,
                       self.interval_label, self.region_label):
            layout.addWidget(widget)
        scope_layout.insertWidget(scope_layout.count() - 1, frame)
        self.cursor_toggle.toggled.connect(self.cursor_enabled)
        self.roi_toggle.toggled.connect(self.roi_enabled)
        if not self.demo:
            QtCore.QTimer.singleShot(200, self.notice_recovery)

    def set_spool_enabled(self, enabled):
        self.worker.request('spool_enabled', enabled=enabled)
        if not self.demo:
            self.settings.setValue('auto_spool', enabled)

    def capture_view_context(self, count=None):
        self.view_generation += 1
        self.source_spool = self.worker.spool
        self.source_count = self.worker.service.summary()['samples'] if count is None else count
        self.source_metadata = copy.deepcopy(self.worker.session_metadata)
        self.source_analyzer = self.worker.detector.analyzer if self.worker.detector else None
        self.source_range_index = None
        self.history_loaded = False

    def extend_frozen_history(self):
        if (self.source_spool is None and self.source is not None and
                self.source_count > len(self.source) and self.worker.archive is not None):
            writer=self.worker.archive
            generation=self.view_generation
            target=max(0,self.source_count-int(writer.metadata.get('recording_start',{}).get('samples',0)))
            def open_frozen_archive():
                reader=None
                try:
                    deadline=time.monotonic()+10
                    while writer.received_samples<target and not writer.done.is_set() and not writer.error:
                        if time.monotonic()>=deadline:
                            raise RuntimeError('Архив: ожидается запись префикса Freeze')
                        time.sleep(.005)
                    if writer.error:
                        raise RuntimeError('Архив: '+str(writer.error))
                    checkpoint=(writer.done if writer.stopping.is_set() or writer.done.is_set()
                                else writer.checkpoint())
                    if checkpoint is None or not checkpoint.wait(10):
                        raise RuntimeError('Архив: снимок индекса не готов')
                    reader=ArchiveReader(writer.manifest_path)
                    if reader.sample_count<target:
                        raise RuntimeError('Архив: часть Freeze-префикса ещё не сохранена')
                    metadata=dict(reader.manifest.get('source_metadata') or {},
                                  archive_manifest=reader.manifest,
                                  source_session=reader.manifest.get('source_session'))
                    metadata['_analyzer']=(self.worker.detector.analyzer
                                           if self.worker.detector is not None else reader.load_analyzer())
                    self.session_events.put(('freeze_archive',(generation,reader,metadata,None)))
                except Exception as error:
                    if reader is not None:
                        reader.close()
                    self.session_events.put(('freeze_archive',(generation,None,None,str(error))))
            thread=threading.Thread(target=open_frozen_archive,name='Freeze archive',daemon=True)
            self.history_threads.append(thread)
            thread.start()
            return
        if self.source_spool is None or self.source is None or self.source_count <= len(self.source):
            return
        reader = self.source_spool.reader(self.source_count)
        generation = self.view_generation
        def load():
            try:
                data = open_spool_history(reader)
                index = EnvelopeIndex(data)
                if len(data):
                    index.query(data['time_s'][0], data['time_s'][-1], 1000)
                self.session_events.put(('freeze_history', (generation, data, index, None)))
            except Exception as error:
                self.session_events.put(('freeze_history', (generation, None, None, str(error))))
        thread = threading.Thread(target=load, name='Freeze history', daemon=True)
        self.history_threads.append(thread)
        thread.start()

    def export_source(self, full=False, interval=None):
        if self.history_loaded and getattr(self,'source_archive',None) is not None:
            archive=self.source_archive
            return ExportSource(archive,copy.deepcopy(self.source_metadata),
                bool(archive.manifest.get('complete')), 'ARCHIVE',None,archive.sample_count,
                self.source_analyzer,0)
        analyzer = (self.worker.detector.analyzer if self.worker.detector else None) if full or self.follow else self.source_analyzer
        if self.history_loaded and self.source_spool is not None:
            spool = self.source_spool
            return ExportSource(spool.reader(self.source_count), copy.deepcopy(spool.metadata),
                                False, 'SPOOL', spool.index, self.source_count,
                                self.source_analyzer, 0)
        if self.history_loaded and self.source is not None:
            return ExportSource(self.source, copy.deepcopy(self.source_metadata),
                bool(self.source_metadata.get('exported_full_available_session',
                    self.source_metadata.get('source_history_complete',
                        not self.source_metadata.get('recording_start', {}).get('samples', 0)))),
                'HISTORY', self.source_range_index, None, self.source_analyzer, 0)
        spool = self.worker.spool if full or self.follow else self.source_spool
        target = self.worker.service.summary()['samples'] if full or self.follow else self.source_count
        if spool is not None and not (spool.error and (not spool.count or
                (interval is not None and max(interval) > (spool.last_time_s or 0)))):
            metadata = copy.deepcopy(spool.snapshot().metadata)
            metadata['source_snapshot_count'] = target
            return ExportSource(spool.reader(target), metadata, not bool(spool.error),
                                'SPOOL', spool.index, target, analyzer, 0)
        data = self.worker.service.snapshot() if full or self.follow else self.source
        if data is None:
            data = self.worker.service.snapshot()
        metadata = copy.deepcopy(self.worker.session_metadata if full or self.follow else self.source_metadata)
        metadata['source_snapshot_count'] = target
        return ExportSource(data, metadata, len(data) == target,
                            'LIVE_BUFFER' if self.follow or full else 'FREEZE',
                            None, target, analyzer, max(0,target-len(data)))

    def request_statistics(self, role, x1, x2):
        self.range_tokens[role] += 1
        token = self.range_tokens[role]
        self.range_pending[role] = True
        source=self.export_source(interval=(x1, x2))
        if source.source_kind in ('LIVE_BUFFER','FREEZE') or (source.source_kind=='SPOOL' and
                source.metadata.get('source_session')==self.worker.session_metadata.get('source_session')):
            source.raw24_lookup=self.worker.service.lookup_raw24
        self.statistics_worker.submit(role, token, source, x1, x2)

    def cursor_enabled(self, enabled):
        self.cursor_dirty = enabled
        if not enabled:
            self.range_tokens['cursor'] += 1
            self.range_pending['cursor'] = False
            self.cursor_stats_label.setText('X1–X2: курсоры выключены')
            self.clean_stats_label.setText('RAW без кандидатов: курсоры выключены')
            self.interval_label.setText('ИНТЕРВАЛ: N = — | Δt = — с | GAPS = —')

    def roi_enabled(self, enabled):
        if enabled:
            self.export_mode.setCurrentIndex(2)
            self.region_update(*self.renderer.roi.getRegion())

    def update_cursor_statistics(self):
        if not self.cursor_toggle.isChecked():
            return
        self.request_statistics('cursor', *(line.value() for line in self.renderer.x_cursors))
        self.cursor_dirty = False

    def apply_range_result(self, role, token, result, error):
        if token != self.range_tokens[role]:
            return
        self.range_pending[role] = False
        if role=='overview':
            if not error:self.renderer.set_overview_bounds(result)
            else:self.statusBar().showMessage('Обзор: оценка диапазона RAW недоступна — '+error)
            return
        if role=='rolling':
            self.apply_average(result,error)
            return
        if role=='panel':
            self.apply_panel_statistics(result,error)
            return
        if role=='cursor' and not error and result.get('cursor_samples'):
            self.apply_sample_cursors(result['cursor_samples'])
        if error:
            label = self.cursor_stats_label if role == 'cursor' else self.region_label
            label.setText('RAW: данные недоступны — ' + error)
            if role == 'cursor':
                self.interval_label.setText('ИНТЕРВАЛ: расчёт недоступен')
                self.clean_stats_label.setText('RAW без кандидатов: расчёт недоступен')
            return
        prefix = 'X1–X2' if role == 'cursor' else 'ROI RAW'
        text = (f"{prefix}: MIN = {fmt(result['min'])} мА | MAX = {fmt(result['max'])} мА | "
                f"AVG = {fmt(result['mean'])} мА | RMS = {fmt(result['rms'])} мА")
        availability = '' if result['complete'] else (' | ЧАСТИЧНО: доступно ' +
            f"{fmt(result['available_start_s'])}–{fmt(result['available_end_s'])} с")
        if not result['count']:
            availability = ' | НЕТ ОТСЧЁТОВ'
        if role == 'cursor':
            self.last_cursor_stats = result
            self.cursor_stats_label.setText(text)
            clean = result.get('filtered')
            if clean and clean.get('valid_count') is not None:
                self.clean_stats_label.setText(
                    f"ОБРАБОТАНО, RAW без кандидатов: MIN {fmt(clean['min'])} | MAX {fmt(clean['max'])} | "
                    f"AVG {fmt(clean['mean'])} | RMS {fmt(clean['rms'])} мА | "
                    f"N valid {clean['valid_count']:,} | N excluded {clean['excluded_count']:,} | "
                    f"GAPS {result['slot_gaps']}" + ('' if clean['complete'] else ' | АНАЛИЗ НЕПОЛОН'))
            else:
                self.clean_stats_label.setText('RAW без кандидатов: анализ ещё недоступен или каталог неполон')
            self.interval_label.setText(f"ИНТЕРВАЛ: N = {result['count']:,} | Δt = {fmt(result['dt'])} с | "
                                        f"GAPS {'=' if result['slot_gaps_complete'] else '≥'} {result['slot_gaps']}" +
                                        ('' if result['slot_gaps_complete'] else ' (граница внутри разрыва)') + availability)
        else:
            self.region_label.setText(text + f" | N = {result['count']:,}{availability}")

    def save_session(self):
        self.queue_export(self.export_source(full=True), 'FULL_SESSION')

    def save_interval(self):
        x1 = x2 = None
        if self.cursor_toggle.isChecked():
            x1, x2 = (line.value() for line in self.renderer.x_cursors)
            interval, selection = (x1, x2), 'CURSORS'
        elif self.export_mode.currentIndex() == 1 or (self.export_mode.currentIndex() == 2 and self.roi_toggle.isChecked()):
            if not self.roi_toggle.isChecked():
                self.statusBar().showMessage('Включите ROI или выберите видимый диапазон')
                return
            interval, selection = self.renderer.roi.getRegion(), 'ROI'
        else:
            interval = self.renderer.view.viewRange()[0]
            selection = 'FREEZE' if self.freeze.isChecked() else 'VISIBLE_RANGE'
        self.queue_export(self.export_source(interval=interval), selection, interval, x1, x2)

    def queue_export(self, source, selection, interval=None, x1=None, x2=None):
        if self.exporter.submit(source, self.directory.text(), selection, interval, x1, x2):
            self.export_jobs += 1
            self.statusBar().showMessage('Сохранение RAW в фоне…')
        else:
            self.statusBar().showMessage('Очередь экспорта заполнена; повторите сохранение после завершения')

    def save_png(self, filename=None):
        if not isinstance(filename, str):
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Экспорт осциллограммы',
                str(Path(self.directory.text()) / 'oscillogram.png'), 'PNG (*.png)')
        if filename:
            # Grab preserves current axes, cursor lines, highlight and ROI.
            if not self.renderer.grab().save(filename, 'PNG'):
                self.statusBar().showMessage('Не удалось сохранить PNG')
            else:
                self.statusBar().showMessage('PNG: ' + filename)

    def add_recent(self, path):
        old = self.recent.findData(path)
        if old >= 0:
            self.recent.removeItem(old)
        self.recent.insertItem(1, Path(path).name, path)
        while self.recent.count() > 13:
            self.recent.removeItem(self.recent.count()-1)
        if not self.demo:
            self.settings.setValue('recent', [self.recent.itemData(i) for i in range(1, self.recent.count())])

    def notice_recovery(self):
        count = len(recoverable_spools())
        if count:
            self.statusBar().showMessage(f'Найдены временные сессии: {count}. Нажмите «Восстановить…».')

    def recover_session(self):
        entries = recoverable_spools()
        if not entries:
            self.statusBar().showMessage('Временные несохранённые сессии не найдены')
            return
        labels = [f'{path.name} — {count:,} точек' for path, count, _ in entries]
        label, ok = QtWidgets.QInputDialog.getItem(self, 'Восстановление RAW', 'Сессия', labels, 0, False)
        if ok:
            path, count, info = entries[labels.index(label)]
            info.update(recovered=True, source_history_complete=False,
                        acquisition_finished=False, session_end_verified=False,
                        recovery_sample_count=count)
            self.pending_recovery[str(path)] = (path, count, info)
            self.load_history(str(path), metadata_override=info)

    def adopt_loaded_recovery(self, filename):
        entry = self.pending_recovery.pop(str(filename), None)
        if entry is not None:
            path, count, info = entry
            session = RawSpool.adopt_recovered(path, count, info, self.source_range_index)
            self.worker.spool_sessions.append(session)
            self.source_spool = session

    def set_y_mode(self):
        self.auto_y.blockSignals(True)
        self.auto_y.setChecked(self.y_mode.currentIndex() != 0)
        self.auto_y.blockSignals(False)
        self.redraw()

    def sync_y_mode(self, enabled):
        if not enabled:
            self.y_mode.setCurrentIndex(0)
        elif self.y_mode.currentIndex() == 0:
            self.y_mode.setCurrentIndex(2)

    def sync_scales(self, *_):
        x, y = self.renderer.view.viewRange()
        self.time_window_s = (getattr(self,'follow_window_s',x[1]-x[0]) if self.follow else x[1]-x[0])
        now = time.monotonic()
        if self.follow and now < self.next_scale_update:
            return
        self.next_scale_update = now + .1
        for combo, text in ((self.time_div, f'{self.time_window_s/10:.6g} s/div'),
                            (self.current_div, f'{(y[1]-y[0])/8:.6g} {self.units.currentText() if hasattr(self,"units") else "mA"}/div')):
            if combo.currentText() != text:
                combo.blockSignals(True)
                combo.setEditText(text)
                combo.blockSignals(False)
        for spin, value in ((self.y_min, y[0]), (self.y_max, y[1])):
            if not spin.hasFocus():
                spin.blockSignals(True); spin.setValue(value); spin.blockSignals(False)

    def display_calibration(self, metadata=None):
        if self.calibrated.currentIndex() == 0:
            return 1.0, 0.0
        if metadata is None:
            metadata = self.worker.session_metadata if self.follow else self.source_metadata
        cal = metadata.get('calibration') or metadata.get('device', {}).get('cal show', {})
        if str(cal.get('ENABLED', '0')) != '1':
            return 1.0, 0.0
        gain = int(cal['GAIN_PPM']) / 1000000
        return gain, -int(cal['OFFSET_UA']) / 1000 * gain

    def calibration_available(self):
        metadata = self.worker.session_metadata if self.follow else self.source_metadata
        cal = metadata.get('calibration') or metadata.get('device', {}).get('cal show', {})
        return all(name in cal for name in ('ENABLED', 'GAIN_PPM', 'OFFSET_UA'))

    def process_session_events(self):
        for _ in range(50):
            try:
                kind, payload = self.session_events.get_nowait()
            except queue.Empty:
                break
            if kind == 'range_stats':
                self.apply_range_result(*payload)
            elif kind == 'history_analyzer':
                generation,analyzer,error=payload
                if generation == self.view_generation:
                    if error:
                        self.statusBar().showMessage('Ошибка анализа истории: '+error)
                    else:
                        self.source_analyzer=analyzer
                        self.cursor_dirty=True
                        self.redraw()
            elif kind == 'freeze_history':
                generation, data, index, error = payload
                if generation == self.view_generation and not self.follow and not self.history_loaded:
                    if error:
                        self.statusBar().showMessage(error)
                    else:
                        self.source = data
                        self.indexes = {'vshunt_code': index}
                        self.cursor_dirty = True
                        self.redraw()
            elif kind == 'freeze_archive':
                generation,reader,metadata,error=payload
                if generation == self.view_generation and not self.follow and not self.history_loaded:
                    if error:
                        self.statusBar().showMessage(error)
                    else:
                        self.load_events.put((reader,metadata,str(reader.manifest_path),None,generation))
                elif reader is not None:
                    reader.close()
            elif kind == 'exported':
                self.export_jobs -= 1
                self.export_timings.append(payload['elapsed_ms'])
                self.last_recording = payload['path']
                self.add_recent(payload['path'])
                if payload['selection'] == 'FULL_SESSION' and isinstance(payload['source'].data, SpoolReader):
                    try:
                        payload['source'].data.spool.mark_saved(payload['snapshot_count'])
                    except OSError as error:
                        self.worker.events.put(('error', f'Export saved; spool manifest: {error}'))
                elif payload['selection'] == 'FULL_SESSION' and payload['source'].source_kind == 'LIVE_BUFFER':
                    self.buffer_saved_count = payload['source'].metadata.get('source_snapshot_count', payload['snapshot_count'])
                self.statusBar().showMessage(f"Сохранено {payload['samples']:,} RAW: {payload['interval'][0]:.6f}–"
                    f"{payload['interval'][1]:.6f} с; {payload['path']}" +
                    (' · ЧАСТИЧНЫЕ ДАННЫЕ' if payload['partial'] else ''))
            elif kind == 'export_error':
                self.export_jobs = max(0, self.export_jobs - 1)
                self.statusBar().showMessage('Ошибка экспорта: ' + payload)
                if self.close_phase == 'saving':
                    self.close_phase = None
                    self.closing = False
        now = time.monotonic()
        if (self.cursor_toggle.isChecked() and not self.range_pending['cursor'] and
                now >= self.next_cursor_update and (self.cursor_dirty or self.follow)):
            self.update_cursor_statistics()
            self.next_cursor_update = now + .1
        if now >= self.next_status_update:
            spool = self.worker.spool
            archive=self.worker.archive
            saved = bool((spool and spool.count > 0 and spool.saved_count >= self.worker.service.count) or
                         (archive and archive.done.is_set() and archive.status=='COMPLETE'))
            self.session_state.setText(f"{'ACQUIRING' if self.worker.state == 'streaming' else 'IDLE'} · "
                f"{'LIVE' if self.follow else 'FREEZE / HISTORY'} · "
                f"{'RECORDING' if self.worker.recorder else 'NOT RECORDING'} · "
                f"{'SAVED' if saved else 'UNSAVED'}" +
                (f" · архив {archive.samples:,} RAW" if archive else
                 f" · spool {spool.count:,} RAW" if spool else ' · только live-буфер') +
                (f" · ОШИБКА: {archive.error}" if archive and archive.error else
                 f" · ОШИБКА: {spool.error}" if spool and spool.error else ''))
            if self.history_loaded:
                self.session_state.setText('HISTORY · исходный файл открыт для чтения' +
                    (' · acquisition и запись продолжаются' if self.worker.state=='streaming' else ' · acquisition остановлен'))
            self.auto_spool.setEnabled(self.worker.state in ('idle', 'disconnected'))
            self.next_status_update = now + .2
        self.advance_close()

    def begin_close(self):
        if self.close_phase is None:
            self.closing = True
            self.close_phase = 'stopping'
            self.worker.trace.begin('window_close', 'CLOSE_WINDOW_REQUESTED',
                                    f'worker_state={self.worker.state}')
            self.worker.request('stop')
            self.statusBar().showMessage('Завершение измерения и записи…')

    def advance_close(self):
        archive=self.worker.archive
        archived=bool(archive is not None and archive.done.is_set() and
                      archive.error is None and archive.samples==self.worker.service.count)
        if self.close_phase == 'stopping':
            if self.worker.state == 'disconnect_failed':
                self.statusBar().showMessage('Отключение COM не подтверждено; см. lifecycle log')
                return
            if self.worker.state in ('streaming', 'starting', 'stopping') or self.worker.recorder or self.export_jobs:
                return
            if any(not item.done.is_set() for item in self.worker.spool_sessions):
                return
            if any(not item.done.is_set() for item in self.worker.detector_sessions):
                return
            unsaved = [item for item in self.worker.spool_sessions if item.count > item.saved_count]
            live_unsaved = (self.worker.spool is None and not archived and
                            self.worker.service.count > self.buffer_saved_count)
            choice = QtWidgets.QMessageBox.Discard
            if (unsaved or live_unsaved) and not self.demo:
                self.close_phase = 'prompting'
                choice = QtWidgets.QMessageBox.question(self, 'Несохранённые измерения',
                    f'Сохранить {len(unsaved) + int(live_unsaved)} сессий в выбранный каталог?\n'
                    '«Не сохранять» удалит временные RAW-файлы после закрытия.',
                    QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
                    QtWidgets.QMessageBox.Save)
            if choice == QtWidgets.QMessageBox.Cancel:
                self.worker.trace.end('window_close', 'CLOSE_WINDOW_CANCELLED')
                self.close_phase = None; self.closing = False
                return
            if choice == QtWidgets.QMessageBox.Save:
                self.close_phase = 'saving'
                for spool in unsaved:
                    snap = spool.snapshot()
                    self.queue_export(ExportSource(spool.reader(snap.count), snap.metadata,
                        not bool(snap.error) and snap.metadata.get('source_history_complete', True),
                        'SPOOL', spool.index, snap.count,
                        next((d.analyzer for d in self.worker.detector_sessions
                              if d.analyzer.session_id == snap.metadata.get('source_session')), None), 0),
                        'FULL_SESSION')
                if live_unsaved:
                    self.queue_export(self.export_source(full=True), 'FULL_SESSION')
                return
            self.close_discard = True
            self.close_phase = 'saving'
        if self.close_phase == 'saving' and not self.export_jobs:
            if not self.close_discard and (any(s.count > s.saved_count for s in self.worker.spool_sessions) or
                    (self.worker.spool is None and not archived and
                     self.worker.service.count > self.buffer_saved_count)):
                self.close_phase = None
                self.closing = False
                self.worker.trace.end('window_close', 'CLOSE_WINDOW_CANCELLED',
                                      'unsaved RAW data')
                self.statusBar().showMessage('Есть несохранённые RAW-данные; закрытие отменено')
                return
            self.worker.request('shutdown')
            self.statistics_worker.finish()
            self.exporter.finish()
            self.close_phase = 'shutdown'
        if self.close_phase == 'shutdown' and self.worker.state == 'disconnect_failed':
            self.statusBar().showMessage('Закрытие COM не подтверждено; повторите «Отключить» и проверьте lifecycle log')
            return
        if self.close_phase == 'shutdown' and not any(thread.is_alive() for thread in
                (self.worker, self.statistics_worker, self.exporter, *self.history_threads)):
            for spool in self.worker.spool_sessions:
                try:
                    spool.delete()
                except OSError:
                    pass  # A remaining file remains recoverable on next launch.
            self.close_phase = 'done'
            self.worker.trace.end('window_close', 'CLOSE_WINDOW_FINISHED')
            self.close()
