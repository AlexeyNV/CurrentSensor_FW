"""Presentation controls built on the existing acquisition/history services."""
from pathlib import Path
import time
from bisect import bisect_left,bisect_right
import numpy as np
from .data import DisplayTrace
from PySide6 import QtWidgets,QtCore
from .layout_controls import CompactLayoutMixin


class WorkbenchMixin(CompactLayoutMixin):
    def init_workbench(self, scope_layout, bounds, root,recording,archive_row,profile_row,controls):
        self.units = QtWidgets.QComboBox()
        self.units.addItems(['mA', 'A'])
        self.units.currentIndexChanged.connect(self.change_units)
        self.window_seconds = QtWidgets.QComboBox()
        for value in (1,5,10,30,60): self.window_seconds.addItem(f'{value} s',value)
        self.window_seconds.addItem('Свой интервал',None)
        self.custom_seconds = QtWidgets.QDoubleSpinBox()
        self.custom_seconds.setRange(.0001,86400); self.custom_seconds.setDecimals(4)
        self.custom_seconds.setValue(1); self.custom_seconds.setSuffix(' s')
        self.window_seconds.currentIndexChanged.connect(self.apply_window_seconds)
        self.custom_seconds.editingFinished.connect(self.apply_window_seconds)
        self.fit_button=QtWidgets.QPushButton('Zoom to fit / сброс')
        self.fit_button.clicked.connect(self.zoom_to_fit)
        self.zoom_selection=QtWidgets.QPushButton('Zoom X1–X2 / ROI')
        self.zoom_selection.clicked.connect(self.zoom_interval)
        row=QtWidgets.QHBoxLayout()
        for widget in (QtWidgets.QLabel('Окно'),self.window_seconds,self.custom_seconds,
                       QtWidgets.QLabel('Ток'),self.units,self.fit_button,self.zoom_selection): row.addWidget(widget)
        row.addStretch(); scope_layout.insertLayout(3,row)
        self.identity_label=QtWidgets.QLabel('Устройство не подключено')
        self.identity_label.setWordWrap(True)
        self.statusBar().addPermanentWidget(self.identity_label,1)
        self.profile_label.show()
        for label in (self.profile_label,self.stats,self.counters,self.cursor_label):label.setWordWrap(True)
        self.current.setStyleSheet('font-size:38px;font-weight:600;color:#52dfc5')
        self.freeze.setText('Пауза графика / Freeze')
        self.freeze.setToolTip('Останавливает только просмотр; USB acquisition и запись продолжаются')
        self.recording_name=QtWidgets.QLineEdit()
        self.recording_name.setPlaceholderText('Имя записи (необязательно)')
        self.recording_name.setMaximumWidth(220)
        recording.insertWidget(1,self.recording_name)
        recording.removeWidget(self.record_button);profile_row.addWidget(self.record_button)
        details=QtWidgets.QWidget();details_layout=QtWidgets.QVBoxLayout(details)
        self.recording_details=details
        root.removeItem(recording);root.removeItem(archive_row)
        details_layout.addLayout(recording);details_layout.addLayout(archive_row)
        details.hide()
        self.recording_settings=QtWidgets.QPushButton('Каталог, имя записи и настройки архива ▾')
        self.recording_settings.setCheckable(True);self.recording_settings.toggled.connect(details.setVisible)
        root.insertWidget(3,self.recording_settings);root.insertWidget(4,details)
        dock=QtWidgets.QDockWidget('Статистика RAW',self)
        panel=QtWidgets.QWidget(); layout=QtWidgets.QVBoxLayout(panel)
        self.statistics_scope=QtWidgets.QComboBox()
        self.statistics_scope.addItems(['Вся доступная запись','Видимый интервал','X1–X2'])
        self.exclude_suspect=QtWidgets.QCheckBox('Исключить SUSPECT из статистики')
        self.exclude_suspect.setToolTip('Только отдельный расчёт; оригиналы не изменяются')
        self.panel_values=QtWidgets.QLabel('Нет исходных отсчётов')
        self.panel_values.setWordWrap(True)
        self.panel_values.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.statistics_scope);layout.addWidget(self.exclude_suspect)
        layout.addWidget(self.panel_values);layout.addStretch()
        dock.setWidget(panel);self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea,dock)
        self.next_panel_statistics=0.;self.last_panel_result=None
        self.visual_mode=QtWidgets.QComboBox()
        self.visual_mode.addItems(['RAW','Marked','Display Clean'])
        self.visual_mode.setCurrentIndex(1)
        self.visual_mode.setToolTip('SUSPECT — диагностическая гипотеза. Clean скрывает только на графике; реальные импульсы тоже могут быть помечены.')
        self.visual_mode.currentIndexChanged.connect(self.redraw)
        row.insertWidget(0,self.visual_mode)
        self.save_marks=QtWidgets.QPushButton('Сохранить маркировку…')
        self.save_marks.clicked.connect(self.save_marking_sidecar)
        layout.insertWidget(2,self.save_marks)
        self.statistics_scope.currentIndexChanged.connect(self.invalidate_panel)
        self.exclude_suspect.toggled.connect(lambda:self.apply_panel_statistics(self.last_panel_result,None))
        self.compact_layout(root,scope_layout,controls,bounds,profile_row,row,recording,archive_row)
        self.range_tokens['overview']=0;self.range_pending['overview']=False
        self.overview_scale=QtWidgets.QComboBox();self.overview_scale.addItems(['Robust Y','Full RAW'])
        self.overview_scale.setToolTip('Меняется только Y обзора. Все RAW сохранены; SUSPECT может быть реальным импульсом.')
        self.overview_scale.currentTextChanged.connect(self.renderer.set_overview_mode)
        self.renderer.overview_controls.addWidget(QtWidgets.QLabel('Обзор Y'))
        self.renderer.overview_controls.addWidget(self.overview_scale)
        hint=QtWidgets.QLabel('SUSPECT: маркер ведёт к исходному RAW');hint.setWordWrap(True)
        self.overview_scale.setMaximumWidth(140)
        self.renderer.overview_controls.addWidget(hint,1)
        self.renderer.overview_event_clicked.connect(self.navigate_overview_event)

    def recording_directory(self):
        import re
        name=self.recording_name.text().strip()
        if not name:return self.directory.text()
        if name in ('.','..') or re.search(r'[\x00-\x1f<>:"/\\|?*]',name) or name.endswith(('.', ' ')):
            self.statusBar().showMessage('Недопустимое имя записи');return None
        if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
            self.statusBar().showMessage('Имя зарезервировано системой');return None
        return str(Path(self.directory.text())/name)

    def invalidate_panel(self):
        self.next_panel_statistics=0.
        self.range_tokens['panel']+=1;self.range_pending['panel']=False
        self.last_panel_signature=None
        if 'rolling' in self.range_tokens:
            self.range_tokens['rolling']+=1;self.range_pending['rolling']=False
        if 'overview' in self.range_tokens:
            self.range_tokens['overview']+=1;self.range_pending['overview']=False

    def update_panel_statistics(self):
        if time.monotonic()<self.next_panel_statistics or self.range_pending['panel']: return
        self.next_panel_statistics=time.monotonic()+1.
        mode=self.statistics_scope.currentIndex()
        if mode==2:
            if not self.cursor_toggle.isChecked():
                self.panel_values.setText('Включите X1–X2');return
            left,right=sorted(c.value() for c in self.renderer.x_cursors)
        elif mode==1: left,right=self.renderer.view.viewRange()[0]
        else:
            if self.history_loaded and self.source_archive is not None:
                left,right=self.source_archive.first_time_s,self.source_archive.last_time_s
            elif self.history_loaded and self.source is not None and len(self.source):
                left,right=float(self.source['time_s'][0]),float(self.source['time_s'][-1])
            else: left,right=0.,self.worker.service.summary()['time_s']
        analyzer=self.current_analyzer()
        signature=(self.view_generation,self.worker.service.summary()['revision'] if self.follow else self.source_count,
            mode,left,right,id(analyzer),analyzer.event_count if analyzer else None,analyzer.finished if analyzer else None)
        if signature==getattr(self,'last_panel_signature',None):return
        self.last_panel_signature=signature
        self.request_statistics('panel',left,right)

    def refresh_overview_events(self):
        analyzer=self.current_analyzer()
        if analyzer is not None:
            with analyzer.lock:events=list(analyzer.events)
        else:events=[]
        self.renderer.set_overview_events(events)

    def navigate_overview_event(self,event):
        self.navigate_candidate(event,True)
        self.cursor_toggle.setChecked(True)
        for cursor in self.renderer.x_cursors:cursor.setValue(event['time_s'])
        self.cursor_dirty=True

    def apply_panel_statistics(self,result,error):
        if error:
            self.panel_values.setText('Расчёт недоступен: '+error);return
        if result is None:return
        self.last_panel_result=result
        stats=result['excluding_suspect' if self.exclude_suspect.isChecked() else 'raw']
        unit=self.units.currentText();factor=self.unit_factor()
        def val(key): return '—' if stats[key] is None else f'{stats[key]*factor:.6g} {unit}'
        lines=['RAW без SUSPECT (описательно)' if self.exclude_suspect.isChecked() else 'Полный исходный RAW']
        for label,key in [('AVG','mean'),('MEDIAN','median'),('MIN','min'),('MAX','max'),('RMS','rms'),('STD','std')]:
            lines.append(f'{label}: {val(key)}')
        lines += [f"N: {stats['count']:,}",f"Частота: {result['effective_rate_hz']:.3f} Hz",
            f"Длительность: {result['duration_s']:.6f} s",f"SUSPECT: {result['suspects']:,}",
            'Маркировка: '+('завершена' if result['analysis_complete'] else 'продолжается / неполна'),
            'Источник: '+result['source_kind']+('' if result['history_complete'] else ' · доступная часть истории')]
        self.panel_values.setText('\n\n'.join(lines))
        self.render_statistics(result)
        if self.history_loaded and self.worker.state!='streaming' and result['last_value_ma'] is not None:
            self.current.setText(f"{result['last_value_ma']*factor:.6g} {unit} RAW · последний отсчёт интервала")

    def apply_sample_cursors(self,samples):
        if not all(samples):
            self.cursor_label.setText('X1 / X2: часть выбранного интервала вне доступной RAW-истории; значения отсчёта недоступны')
            return
        for cursor,sample in zip(self.renderer.x_cursors,samples):
            cursor.blockSignals(True);cursor.setValue(sample['time_s']);cursor.blockSignals(False)
        self.renderer.cursor_region.setRegion(sorted(s['time_s'] for s in samples))
        first,second=samples;dt=second['time_s']-first['time_s'];factor=self.unit_factor();unit=self.units.currentText()
        def describe(name,s):
            raw='недоступен' if s['raw24'] is None else f"0x{s['raw24']:06X}"
            return f"{name}: t={s['time_s']:.9g} s I={s['current_ma']*factor:.6g} {unit} seq={s['sequence']} RAW24={raw}"
        self.cursor_label.setText(describe('X1',first)+' | '+describe('X2',second)+
            f"\nΔt={dt:.9g} s | ΔI={(second['current_ma']-first['current_ma'])*factor:.6g} {unit} | "+
            (f'1/Δt={1/abs(dt):.6g} Hz' if dt else '1/Δt=—'))

    def visual_traces(self,traces,markers):
        mode=self.visual_mode.currentText()
        if mode=='RAW':return traces,np.empty(0,markers.dtype)
        if mode!='Display Clean':return traces,markers
        analyzer=self.current_analyzer()
        if analyzer is None:return traces,np.empty(0,markers.dtype)
        with analyzer.lock: times=np.asarray(analyzer.event_times)
        cleaned=[]
        for trace in traces:
            y=trace.y.copy();connect=trace.connect.copy()
            if len(trace.x):
                at=np.searchsorted(times,trace.x)
                exact=np.zeros(len(trace.x),bool)
                valid=at<len(times)
                exact[valid]=times[at[valid]]==trace.x[valid]
                y[exact]=np.nan
                if len(trace.x)>1:
                    # Break even if a marked sample was not an envelope extremum.
                    a=np.searchsorted(times,trace.x[:-1],side='left')
                    b=np.searchsorted(times,trace.x[1:],side='right')
                    connect[:-1]&=(a==b)
            cleaned.append(DisplayTrace(trace.x,y,connect,trace.indices,trace.aggregated))
        return cleaned,np.empty(0,markers.dtype)

    def save_marking_sidecar(self):
        import json,threading,uuid
        analyzer=self.current_analyzer()
        if analyzer is None:return
        filename,_=QtWidgets.QFileDialog.getSaveFileName(self,'Сохранить SUSPECT отдельно от RAW',
            str(Path(self.directory.text())/'suspects.sidecar.json'),'JSON (*.json)')
        if not filename:return
        forbidden={Path(self.source_metadata.get('_source_path','__none__')).resolve()}
        if self.source_archive is not None:forbidden.add(self.source_archive.manifest_path.resolve())
        if Path(filename).resolve() in forbidden:
            self.statusBar().showMessage('Нельзя перезаписать исходную запись');return
        def save():
            try:
                with analyzer.lock:
                    document=dict(schema='CurrentSensor.suspect.v1',classification='SUSPECT',
                        source=self.source_metadata.get('_source_path'),parameters=analyzer.rules.as_dict(),
                        complete=analyzer.complete and analyzer.finished,events=list(analyzer.events))
                path=Path(filename);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
                temp.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf8');temp.replace(path)
                self.worker.events.put(('log','SUSPECT sidecar сохранён: '+filename))
            except Exception as error:self.worker.events.put(('error','Sidecar: '+str(error)))
        thread=__import__('threading').Thread(target=save,daemon=True,name='SUSPECT sidecar')
        self.history_threads.append(thread);thread.start()

    def unit_factor(self):
        return .001 if hasattr(self,'units') and self.units.currentText()=='A' else 1.

    def display_transform(self, metadata=None):
        gain,offset=self.display_calibration(metadata)
        return gain*self.unit_factor(),offset*self.unit_factor()

    def change_units(self):
        unit=self.units.currentText(); old=getattr(self,'last_display_unit','mA')
        factor=.001 if unit=='A' else 1000.
        if old!=unit:
            y=self.renderer.view.viewRange()[1]
            self.renderer.view.setYRange(y[0]*factor,y[1]*factor,padding=0)
            for cursor in self.renderer.y_cursors: cursor.setValue(cursor.value()*factor)
        self.last_display_unit=unit
        self.renderer.current_unit=unit
        self.renderer.set_overview_mode(self.renderer.overview_mode)
        self.renderer.overview_index=None
        for spin in (self.y_min,self.y_max): spin.setSuffix(' '+unit)
        self.renderer.plot.setLabel('left','Ток',units=unit)
        self.redraw()
        if self.latest: self.update_summary(self.latest)
        if self.last_panel_result:self.apply_panel_statistics(self.last_panel_result,None)

    def apply_window_seconds(self):
        seconds=self.window_seconds.currentData()
        seconds=self.custom_seconds.value() if seconds is None else seconds
        self.time_window_s=float(seconds)
        self.follow_window_s=float(seconds)
        if not self.follow:
            left=self.renderer.view.viewRange()[0][0]
            self.renderer.view.setXRange(left,left+seconds,padding=0)
        self.redraw()

    def zoom_interval(self):
        if self.cursor_toggle.isChecked():
            left,right=sorted(c.value() for c in self.renderer.x_cursors)
        elif self.roi_toggle.isChecked(): left,right=self.renderer.roi.getRegion()
        else:
            self.statusBar().showMessage('Включите X1–X2 или ROI для выбора интервала');return
        if right>left:
            self.manual_view();self.renderer.view.setXRange(left,right,padding=0);self.redraw()

    def zoom_to_fit(self):
        self.y_mode.setCurrentIndex(1)
        self.auto_x();self.redraw()

    def update_identity(self,payload):
        connection=payload.get('connection',{}); info=payload.get('info',{})
        def hexid(value): return f'{value:04X}' if isinstance(value,int) else '—'
        cal=payload.get('cal show',{})
        self.identity_label.setText(f"{connection.get('port','—')} | USB {hexid(connection.get('vid'))}:{hexid(connection.get('pid'))}"
            f" | SN {connection.get('usb_serial_number') or '—'} | FW {info.get('FW','—')}"
            f" | CAL {'ON' if cal.get('ENABLED')=='1' else 'OFF'} / {cal.get('SOURCE','—')}")
