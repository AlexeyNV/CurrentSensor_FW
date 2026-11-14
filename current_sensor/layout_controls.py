"""Compact Qt presentation; measurement, recording and transport stay separate."""
import time
from PySide6 import QtCore,QtWidgets
from .settings_panel import SettingsPage,SettingsTabs,SettingsPanel


def take_widgets(layout):
    widgets=[]
    while layout.count():
        item=layout.takeAt(0)
        if item.widget() is not None:
            item.widget().hide();widgets.append(item.widget())
    return widgets


def grid_page(widgets,columns=3):
    page=QtWidgets.QWidget();grid=QtWidgets.QGridLayout(page)
    for i,widget in enumerate(widgets):
        grid.addWidget(widget,i//columns,i%columns);widget.show()
    for col in range(columns):grid.setColumnStretch(col,1)
    return page


class CompactLayoutMixin:
    @staticmethod
    def group_count(value):return f'{int(value):,}'.replace(',',' ')

    def compact_layout(self,root,scope,controls,bounds,profile,window_row,recording,archive):
        connection=root.itemAt(0).layout()
        export_row=next(root.itemAt(i).layout() for i in range(root.count())
            if root.itemAt(i).layout() is not None and root.itemAt(i).layout().indexOf(self.save_full_button)>=0)
        export_widgets=take_widgets(export_row)
        profile_widgets=take_widgets(profile)
        graph_widgets=take_widgets(controls)
        bounds_widgets=take_widgets(bounds)
        take_widgets(window_row)
        self._old_root_items=[]
        while root.count():self._old_root_items.append(root.takeAt(0))
        for label in (self.profile_label,self.archive_state,self.session_state,self.stats):label.hide()
        self.recording_settings.toggled.disconnect()
        self.recording_settings.setText('Дополнительные настройки')
        self.advanced_tabs=SettingsTabs()
        graph=[('Режим отображения',self.visual_mode),('Фильтр графика',self.filter),('Ток на графике',self.calibrated),
               ('Масштаб X, s/div',self.time_div),('Масштаб Y, mA/div',self.current_div),('Единицы',self.units),
               ('Ручной Y: минимум',self.y_min),('Ручной Y: максимум',self.y_max)]
        graph.extend(('',w) for w in bounds_widgets if isinstance(w,QtWidgets.QPushButton))
        self.advanced_tabs.addTab(SettingsPage(graph),'График')
        self.advanced_tabs.addTab(SettingsPage([('X1/X2 и Y1/Y2 · привязка к RAW',self.cursor_toggle),
            ('Область исследования',self.roi_toggle),('',self.zoom_selection)]),'Курсоры')
        self.save_full_button.setText('Сохранить доступную историю')
        self.save_range_button.setText('Экспорт выделенного участка')
        self.advanced_tabs.addTab(SettingsPage([('',w) for w in export_widgets]),'История')
        self.advanced_tabs.addTab(SettingsPage([('Профиль',self.profiles),('Режим сбора',self.mode),
            ('',self.apply_profile),('Подтверждённая конфигурация',self.profile_label),
            ('Выбранный режим / ограничения',self.mode_hint)]),'Прибор')
        recording_widgets=take_widgets(recording)+take_widgets(archive)
        labels={self.directory:'Каталог записи',self.recording_name:'Имя записи',self.segment_size:'Размер сегмента',
            self.segment_seconds:'Длительность сегмента',self.disk_reserve:'Резерв места'}
        self.advanced_tabs.addTab(SettingsPage([(labels.get(w,''),w) for w in recording_widgets]),'Запись')
        self.recording_details.hide()
        self.advanced_area=SettingsPanel(self,self.advanced_tabs,self.recording_settings)
        self.recording_settings.toggled.connect(self.advanced_area.set_expanded)
        self.freeze.setText('Пауза графика')
        self.start_button.setText('Измерять');self.stop_button.setText('Остановить')
        self.record_button.setText('Записывать на диск')
        self.live_button=next(w for w in graph_widgets if isinstance(w,QtWidgets.QPushButton) and w.text()=='Live')
        self.live_button.setText('К текущему времени')
        auto_x=next(w for w in graph_widgets if isinstance(w,QtWidgets.QPushButton) and w.text()=='Auto X')
        auto_x.setText('Вся история по X')
        self.advanced_tabs.widget(0).add_control('',auto_x)
        auto_x.show()
        self.view_state=QtWidgets.QLabel()
        self.view_state.setWordWrap(True);self.view_state.setMaximumWidth(240)
        for widget in (self.start_button,self.stop_button,self.record_button,self.freeze,self.live_button,self.view_state):
            profile.addWidget(widget);widget.show()
        profile.addStretch()
        self.connection_caption=QtWidgets.QLabel()
        connection.addWidget(self.connection_caption)
        self.connection_state.hide()
        root.addLayout(connection);root.addLayout(profile)
        root.addWidget(self.recording_settings);root.addWidget(self.advanced_area)
        root.addWidget(self.record_state);root.addWidget(self.tabs,1)
        # Hidden diagnostic pages must not impose their wide form's minimum
        # size on the oscilloscope. Preserve each page's identity/signals.
        for i in range(1,self.tabs.count()):
            page=self.tabs.widget(i);content=QtWidgets.QWidget();content.setLayout(page.layout())
            area=QtWidgets.QScrollArea();area.setWidgetResizable(True);area.setWidget(content)
            outer=QtWidgets.QVBoxLayout(page);outer.setContentsMargins(0,0,0,0);outer.addWidget(area)
        self.record_state.setToolTip('Пользовательская запись отделена от временной RAW-истории spool. Пауза графика не останавливает ни одну из них.')
        for widget in (QtWidgets.QLabel('Окно LIVE'),self.window_seconds,self.custom_seconds,
                       QtWidgets.QLabel('Автомасштаб Y'),self.y_mode,self.fit_button):
            window_row.addWidget(widget);widget.show()
        window_row.addStretch()
        self.fit_button.setText('Весь график')
        self.y_mode.setItemText(0,'Ручной');self.y_mode.setItemText(1,'Все значения');self.y_mode.setItemText(2,'Устойчивый')
        self.follow_window_s=float(self.window_seconds.currentData() or self.custom_seconds.value())
        self.time_window_s=self.follow_window_s
        self.custom_seconds.setVisible(self.window_seconds.currentData() is None)
        self.window_seconds.currentIndexChanged.connect(lambda:self.custom_seconds.setVisible(self.window_seconds.currentData() is None))
        frame=self.cursor_label.parentWidget()
        frame.setVisible(self.cursor_toggle.isChecked() or self.roi_toggle.isChecked())
        for toggle in (self.cursor_toggle,self.roi_toggle):
            toggle.toggled.connect(lambda _:frame.setVisible(self.cursor_toggle.isChecked() or self.roi_toggle.isChecked()))
        self.view_label.hide()
        self.current.setStyleSheet('font-size:28px;font-weight:600;color:#52dfc5')
        self.current.setWordWrap(True)
        self.average_label=QtWidgets.QLabel('Средний RAW за 100 мс: —')
        self.average_label.setWordWrap(True);self.average_label.setMaximumWidth(350)
        self.average_label.setStyleSheet('font-size:14px;color:#b6d8e4')
        self.average_label.setToolTip('Все исходные значения, включая SUSPECT. Нет исключения, замены медианой или интерполяции.')
        self.average_policy='Все RAW, включая SUSPECT'
        scope.removeWidget(self.current)
        hero=QtWidgets.QHBoxLayout();hero.addWidget(self.current,2);hero.addWidget(self.average_label,1)
        scope.insertLayout(0,hero)
        self.next_average_update=0.;self.range_tokens['rolling']=0;self.range_pending['rolling']=False
        self.last_recording_result=None
        statistics_layout=self.panel_values.parentWidget().layout()
        for widget in (self.exclude_suspect,self.save_marks):statistics_layout.removeWidget(widget)
        self.statistics_asof=QtWidgets.QLabel();self.statistics_asof.setWordWrap(True)
        self.statistics_details=QtWidgets.QWidget();detail_layout=QtWidgets.QVBoxLayout(self.statistics_details)
        self.statistics_details_label=QtWidgets.QLabel();self.statistics_details_label.setWordWrap(True)
        for widget in (self.exclude_suspect,self.save_marks,self.statistics_details_label):detail_layout.addWidget(widget)
        self.statistics_details.hide()
        self.exclude_suspect.hide()
        self.statistics_mode=QtWidgets.QComboBox()
        self.statistics_mode.addItems(['Полный RAW','Диагностика без SUSPECT','Сравнить RAW / без SUSPECT'])
        self._sync_statistics_mode=False
        self.statistics_mode.currentIndexChanged.connect(self.select_statistics_mode)
        self.exclude_suspect.toggled.connect(self.reflect_statistics_mode)
        self.statistics_policy=QtWidgets.QLabel('');self.statistics_policy.setWordWrap(True)
        self.statistics_details_toggle=QtWidgets.QPushButton('Подробности статистики')
        self.statistics_details_toggle.setCheckable(True)
        self.statistics_details_toggle.toggled.connect(self.statistics_details.setVisible)
        statistics_layout.insertWidget(1,self.statistics_mode)
        statistics_layout.insertWidget(3,self.statistics_policy)
        statistics_layout.insertWidget(4,self.statistics_asof)
        statistics_layout.insertWidget(5,self.statistics_details_toggle)
        statistics_layout.insertWidget(6,self.statistics_details)
        statistics_panel=self.panel_values.parentWidget()
        dock=next(w for w in self.findChildren(QtWidgets.QDockWidget) if w.widget() is statistics_panel)
        scroll=QtWidgets.QScrollArea();scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(statistics_panel);dock.setWidget(scroll);dock.setMinimumWidth(240)
        files=self.menuBar().addMenu('Файл')
        for text,callback in [('Открыть эксперимент…',self.choose_history),
                ('Сохранить доступную историю',self.save_session),('Экспорт выделенного участка',self.save_interval),
                ('Экспорт PNG',self.save_png)]:files.addAction(text).triggered.connect(callback)
        splitter=self.renderer.splitter
        state=self.settings.value('layout061/plot_splitter') if not self.demo else None
        restored=splitter.restoreState(state) if isinstance(state,QtCore.QByteArray) else False
        self.splitter_restored=restored
        splitter.splitterMoved.connect(self.save_splitter)
        if not restored:QtCore.QTimer.singleShot(0,lambda:splitter.setSizes([420,180]))
        for widget in (self.start_button,self.stop_button,self.record_button,self.live_button,self.fit_button,self.recording_settings):
            widget.setMinimumHeight(28)
        self.identity_label.setMaximumWidth(620)
        self.refresh_view_state()

    def save_splitter(self,*_):
        if not self.demo:self.settings.setValue('layout061/plot_splitter',self.renderer.splitter.saveState())

    def refresh_view_state(self):
        if not hasattr(self,'view_state'):return
        span=self.renderer.view.viewRange()[0]
        self.view_state.setText('LIVE FOLLOW · за отсчётами' if self.follow else f'MANUAL · X {span[1]-span[0]:.6g} с')
        self.view_state.setStyleSheet('color:#52dfc5' if self.follow else 'color:#ffc66d')

    def sync_window_choice(self,seconds):
        index=self.window_seconds.findData(seconds)
        self.window_seconds.blockSignals(True)
        self.window_seconds.setCurrentIndex(index if index>=0 else self.window_seconds.count()-1)
        self.window_seconds.blockSignals(False)
        if index<0:self.custom_seconds.setValue(seconds)
        self.custom_seconds.setVisible(index<0)

    def refresh_compact_status(self,report):
        if not hasattr(self,'view_state'):return
        state=report['state'];record=report['recording']
        captions={'idle':'Подключено','streaming':'Подключено · измерение','disconnected':'Отключено',
            'connecting':'Подключение…','disconnecting':'Отключение…','disconnect_failed':'Ошибка отключения',
            'starting':'Запуск…','stopping':'Остановка…'}
        self.connection_caption.setText(captions.get(state,state))
        measurement={'streaming':'Измерение выполняется','starting':'Запуск измерения…',
            'stopping':'Остановка измерения…'}.get(state,'Измерение остановлено')
        armed=bool(self.worker.record_directory) and not record['active']
        if record['error']:record_text='Ошибка записи: '+str(record['error'])
        elif record['active']:
            seconds=max(0,int(record['elapsed_s']));hours,seconds=divmod(seconds,3600);minutes,seconds=divmod(seconds,60)
            closing=bool(self.worker.recorder and self.worker.recorder.stopping.is_set())
            record_text=(('Завершение записи' if closing else 'Запись выполняется')+
                f" · {hours:02d}:{minutes:02d}:{seconds:02d} · {self.group_count(record['samples'])} отсчётов · {record.get('bytes',0)/1024**2:.1f} MiB")
        elif armed:record_text='Запись подготовлена · начнётся при измерении'
        elif self.last_recording_result:
            outcome=self.last_recording_result
            record_text=('Ошибка записи: '+str(outcome['error']) if outcome.get('error') else
                'Запись завершена' if outcome.get('integrity')=='COMPLETE' else 'Неполная запись (PARTIAL)')
        else:record_text='Запись на диск выключена'
        self.record_state.setText(measurement+' · '+record_text)
        color=('#ff647c' if 'Ошибка' in record_text else '#ffc66d' if 'PARTIAL' in record_text else
               '#52dfc5' if record['active'] else '#dce6ee')
        style='color:'+color
        if self.record_state.styleSheet()!=style:self.record_state.setStyleSheet(style)
        self.record_button.setText('Остановить запись' if record['active'] or armed else 'Записывать на диск')
        stats=report['acquisition'];transport=report.get('transport',{})
        if not self.history_loaded or state=='streaming':
            self.counters.setText(f"Принято сейчас: N {self.group_count(stats['samples'])} · {stats['rate_hz']:.1f} Гц · CRC {transport.get('crc_errors',0)} · подробности в «Диагностика»")
        self.refresh_view_state()
        self.refresh_statistics_asof()

    def render_statistics(self,result):
        if getattr(self,'statistics_result_identity',None)!=id(result):
            self.statistics_result_identity=id(result);self.statistics_received_at=time.monotonic()
        stats=result['excluding_suspect' if self.exclude_suspect.isChecked() else 'raw']
        factor=self.unit_factor();unit=self.units.currentText()
        def value(key):return '—' if stats[key] is None else f'{stats[key]*factor:.{6 if unit=="A" else 4}f} {unit}'
        rows=[('AVG',value('mean')),('MEDIAN',value('median')),('MIN',value('min')),('MAX',value('max')),
              ('RMS',value('rms')),('STD',value('std')),('Частота',f"{result['effective_rate_hz']:.2f} Гц"),
              ('Длительность',f"{result['duration_s']:.3f} с"),('В расчёте',self.group_count(stats['count'])),
              ('Исходных',self.group_count(result['raw']['count'])),
              ('SUSPECT',f"{result['suspects']:,}".replace(',',' '))]
        title='RAW без SUSPECT' if self.exclude_suspect.isChecked() else 'Полный RAW'
        self.panel_values.setText('<b>'+title+'</b><br><br><table width="100%" cellspacing="6">'+
            ''.join(f'<tr><td>{name}</td><td align="right">{text}</td></tr>' for name,text in rows)+'</table>')
        mode=self.statistics_mode.currentIndex()
        if mode==2:
            raw=result['raw'];clean=result['excluding_suspect']
            def compact(s,key):return '—' if s[key] is None else f'{s[key]*factor:.{6 if unit=="A" else 4}f}'
            body=''.join(f'<tr><td>{label}</td><td align="right">{compact(raw,key)}</td><td align="right">{compact(clean,key)}</td></tr>'
                for label,key in [('AVG','mean'),('MEDIAN','median'),('MIN','min'),('MAX','max'),('RMS','rms'),('STD','std')])
            self.panel_values.setText(f'<table width="100%" cellspacing="4"><tr><th>{unit}</th><th>RAW</th><th>Без SUSPECT</th></tr>'+body+
                f"<tr><td>В расчёте</td><td>{self.group_count(raw['count'])}</td><td>{self.group_count(clean['count'])}</td></tr></table>"
                f"<p>Исходных: {self.group_count(raw['count'])}<br>SUSPECT: {self.group_count(result['suspects'])}<br>"
                f"Частота: {result['effective_rate_hz']:.2f} Гц<br>Длительность: {result['duration_s']:.3f} с</p>")
        self.statistics_policy.setText('SUSPECT исключены только из расчёта. Это не доказанные ошибки; исходный RAW сохранён.' if mode else '')
        self.statistics_details_label.setText('Маркировка: '+('завершена' if result['analysis_complete'] else 'продолжается / неполна')+
            '\nИсточник: '+result['source_kind']+('' if result['history_complete'] else ' · доступная часть истории'))
        self.refresh_statistics_asof()

    def select_statistics_mode(self,index):
        self._sync_statistics_mode=True
        self.exclude_suspect.setChecked(index==1)
        self._sync_statistics_mode=False
        self.apply_panel_statistics(self.last_panel_result,None)

    def reflect_statistics_mode(self,checked):
        if self._sync_statistics_mode:return
        self.statistics_mode.blockSignals(True);self.statistics_mode.setCurrentIndex(1 if checked else 0);self.statistics_mode.blockSignals(False)
        self.apply_panel_statistics(self.last_panel_result,None)

    def refresh_statistics_asof(self):
        if not hasattr(self,'statistics_asof') or self.last_panel_result is None:return
        result=self.last_panel_result;first=result['first_time_s'];last=result['last_time_s']
        age=max(0,time.monotonic()-getattr(self,'statistics_received_at',time.monotonic()))
        interval='нет данных' if first is None else f't={first:.3f}–{last:.3f} с'
        self.statistics_asof.setText(f"Срез расчёта: {interval}\nN {self.group_count(result['raw']['count'])} исходных отсчётов\nРезультат получен {age:.1f} с назад")

    def request_average(self):
        if time.monotonic()<self.next_average_update or self.range_pending['rolling']:return
        self.next_average_update=time.monotonic()+.1
        from .export import ExportSource
        if self.history_loaded and self.worker.state!='streaming':
            if self.last_panel_result is None:return
            right=self.last_panel_result['last_time_s']
            if right is None:return
            source=self.export_source(interval=(right-.1,right))
        else:
            summary=self.worker.service.summary()
            if not summary['samples']:return
            right=summary['time_s'];data=self.worker.service.snapshot(right-.1,right)
            source=ExportSource(data,{},False,'LIVE_BUFFER')
        self.range_tokens['rolling']+=1;self.range_pending['rolling']=True
        self.statistics_worker.submit('rolling',self.range_tokens['rolling'],source,right-.1,right)

    def apply_average(self,result,error):
        self.last_average_result=result
        if error or result is None or result['mean_ma'] is None:
            self.average_label.setText('Средний RAW за 100 мс: недоступен');return
        self.average_label.setText(f"Средний RAW за 100 мс: {result['mean_ma']*self.unit_factor():.{6 if self.units.currentText()=='A' else 4}f} {self.units.currentText()}"
            f"\nВсе RAW, включая SUSPECT · N {self.group_count(result['count'])}"+(' · доступная часть' if not result.get('complete',True) else ''))
