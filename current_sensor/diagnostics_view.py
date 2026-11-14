import json
from PySide6 import QtCore, QtWidgets


class DiagnosticsView(QtWidgets.QWidget):
    refresh_requested = QtCore.Signal()
    anomaly_requested = QtCore.Signal(float, float)

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        self.refresh = QtWidgets.QPushButton("Обновить ID / регистры / настройки")
        self.refresh.clicked.connect(self.refresh_requested)
        controls.addWidget(self.refresh)
        self.limit = QtWidgets.QDoubleSpinBox()
        self.limit.setRange(0, 32767)
        self.limit.setValue(10000)
        self.limit.setSuffix(" mA |I|")
        self.step = QtWidgets.QDoubleSpinBox()
        self.step.setRange(0, 65536)
        self.step.setValue(1000)
        self.step.setSuffix(" mA скачок; 0=off")
        controls.addWidget(self.limit)
        controls.addWidget(self.step)
        self.apply = QtWidgets.QPushButton("Применить пороги")
        self.apply.clicked.connect(lambda: self.anomaly_requested.emit(self.limit.value(), self.step.value()))
        controls.addWidget(self.apply)
        layout.addLayout(controls)
        self.threshold_warning = QtWidgets.QLabel('')
        self.threshold_warning.setStyleSheet('color:#ffc66d')
        layout.addWidget(self.threshold_warning)
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.addWidget(self.report)
        splitter.addWidget(self.log)
        layout.addWidget(splitter)
        self.fields = {}

    def update_report(self, name, fields):
        self.fields[name] = fields
        self.report.setPlainText(json.dumps(self.fields, ensure_ascii=False, indent=2, default=str))

    def set_idle(self, idle):
        for item in (self.refresh, self.limit, self.step, self.apply):
            item.setEnabled(idle)
