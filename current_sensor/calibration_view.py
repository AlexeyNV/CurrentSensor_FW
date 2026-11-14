from PySide6 import QtCore, QtWidgets


class CalibrationView(QtWidgets.QWidget):
    command = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QFormLayout(self)
        self.offset = QtWidgets.QSpinBox()
        self.offset.setRange(-100000, 100000)
        self.offset.setSuffix(" µA")
        self.gain = QtWidgets.QSpinBox()
        self.gain.setRange(900000, 1100000)
        self.gain.setValue(1000000)
        self.gain.setSuffix(" ppm")
        self.enabled = QtWidgets.QCheckBox("Коррекция включена в прошивке")
        layout.addRow("Смещение", self.offset)
        layout.addRow("Коэффициент", self.gain)
        layout.addRow(self.enabled)
        self.state = QtWidgets.QLabel("Нет данных")
        layout.addRow("Активные параметры", self.state)
        note = QtWidgets.QLabel("Изменения применяются в RAM. cal save записывает Flash только после подтверждения.\n"
                               "RAW-график и CSV содержат исходный ток; коэффициенты сохраняются в metadata.\n"
                               "Автоматическая калибровка отсутствует.")
        note.setWordWrap(True)
        layout.addRow(note)
        for title, command in (("Прочитать", "cal show"), ("Загрузить Flash", "cal load"),
                               ("Заводские в RAM", "cal reset")):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, c=command: self.command.emit(c))
            layout.addRow(button)
        for title, callback in (("Применить OFFSET в RAM", lambda: f"cal set offset_ua {self.offset.value()}"),
                                ("Применить GAIN в RAM", lambda: f"cal set gain_ppm {self.gain.value()}"),
                                ("Применить ENABLED в RAM", lambda: f"cal enable {int(self.enabled.isChecked())}")):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, cb=callback: self.command.emit(cb()))
            layout.addRow(button)
        self.save_button = QtWidgets.QPushButton("Сохранить коэффициенты во Flash…")
        self.save_button.clicked.connect(self.confirm_save)
        layout.addRow(self.save_button)

    def confirm_save(self):
        answer = QtWidgets.QMessageBox.question(self, "Запись Flash",
            "Выполнить cal save и сохранить активные коэффициенты RAM во Flash прибора?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.command.emit("cal save")

    def update_fields(self, fields):
        if "OFFSET_UA" not in fields:
            return
        self.offset.setValue(int(fields["OFFSET_UA"]))
        self.gain.setValue(int(fields["GAIN_PPM"]))
        self.enabled.setChecked(fields["ENABLED"] == "1")
        dirty = fields.get("DIRTY", "?")
        self.state.setText(f"OFFSET={fields['OFFSET_UA']} µA; GAIN={fields['GAIN_PPM']} ppm; "
                           f"SOURCE={fields.get('SOURCE', '?')}; DIRTY={dirty}\n"
                           + ("Есть несохранённые изменения" if dirty == "1" else "Активные значения совпадают с сохранёнными/заводскими"))
