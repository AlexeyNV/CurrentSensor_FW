"""Bounded table over the candidate catalog; no per-sample Qt graphics."""
import json
from PySide6 import QtCore, QtWidgets


class EventsView(QtWidgets.QWidget):
    navigate = QtCore.Signal(object, bool)
    save_range = QtCore.Signal(object, float)
    threshold_changed = QtCore.Signal(float)

    def __init__(self):
        super().__init__()
        self.events = []
        self.selected = 0
        layout = QtWidgets.QVBoxLayout(self)
        line = QtWidgets.QHBoxLayout()
        self.threshold = QtWidgets.QDoubleSpinBox()
        self.threshold.setRange(.0625, 32767)
        self.threshold.setDecimals(4)
        self.threshold.setValue(10)
        self.threshold.setSuffix(' mA min, local MAD')
        self.threshold.valueChanged.connect(self.threshold_changed)
        self.width = QtWidgets.QDoubleSpinBox()
        self.width.setRange(.0001, 60)
        self.width.setDecimals(4)
        self.width.setValue(.005)
        self.width.setSuffix(' s вокруг')
        self.previous = QtWidgets.QPushButton('← Предыдущее')
        self.next = QtWidgets.QPushButton('Следующее →')
        self.go = QtWidgets.QPushButton('Перейти')
        self.save = QtWidgets.QPushButton('Сохранить участок')
        self.previous.clicked.connect(lambda: self.move(-1))
        self.next.clicked.connect(lambda: self.move(1))
        self.go.clicked.connect(self.goto)
        self.save.clicked.connect(lambda: self.save_range.emit(self.current(),self.width.value()) if self.current() else None)
        for widget in (self.threshold,self.width,self.previous,self.next,self.go,self.save):
            line.addWidget(widget)
        layout.addLayout(line)
        self.message = QtWidgets.QLabel('Кандидаты на аномалии ПК; математический признак не доказывает ошибку SPI.')
        layout.addWidget(self.message)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['Время, s','Slot','Ток, mA','Медиана, mA','Отклонение, mA','Правило'])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda row,_: self.select_row(row, True))
        self.table.cellClicked.connect(lambda row,_: self.select_row(row, False))
        layout.addWidget(self.table)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(130)
        layout.addWidget(self.details)
        layout.addWidget(QtWidgets.QLabel('Отчёт STM32 stream anomaly — отдельный источник, без оценки детектором ПК:'))
        self.mcu_report = QtWidgets.QPlainTextEdit()
        self.mcu_report.setReadOnly(True)
        self.mcu_report.setMaximumHeight(150)
        layout.addWidget(self.mcu_report)

    def set_mcu_report(self, report, entries=None):
        self.mcu_report.setPlainText(json.dumps(dict(fields=report, entries=entries) if report else
                                               {'status':'нет завершённого отчёта STM32'},
                                               ensure_ascii=False,indent=2))

    def set_events(self, events, total, complete, error=None):
        self.events = events
        self.selected = min(self.selected, max(0,len(events)-1))
        self.message.setText(f'Кандидаты ПК: {total:,}; в каталоге {len(events):,}; '
                             + ('анализ завершён' if complete else f'АНАЛИЗ НЕПОЛОН {error or "или продолжается"}'))
        self.show_page()

    def show_page(self):
        start = max(0, self.selected-100)
        end = min(len(self.events),start+200)
        self.table.setRowCount(end-start)
        self.table.blockSignals(True)
        for row,event in enumerate(self.events[start:end]):
            for col,value in enumerate((f"{event['time_s']:.6f}",event['slot'],
                f"{event['current_ma']:.4f}",event['local_median_ma'],
                event['deviation_ma'],event['rule'])):
                self.table.setItem(row,col,QtWidgets.QTableWidgetItem('—' if value is None else str(value)))
        self.table.blockSignals(False)
        if self.events:
            self.details.setPlainText(json.dumps(self.events[self.selected],ensure_ascii=False,indent=2))
        else:
            self.details.clear()

    def current(self):
        return self.events[self.selected] if self.events else None

    def select_row(self,row,open_details):
        self.selected = max(0,self.selected-100)+row
        self.show_page()
        if open_details:
            self.goto()

    def move(self,step):
        if self.events:
            self.selected = max(0,min(len(self.events)-1,self.selected+step))
            self.show_page()
            self.goto()

    def goto(self):
        event = self.current()
        if event:
            self.navigate.emit(event,True)
