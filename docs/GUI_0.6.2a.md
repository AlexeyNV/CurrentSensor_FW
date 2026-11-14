# CurrentSensor GUI 0.6.2a — переключение acquisition без ложного отключения USB

Проверено 14 ноября 2026 на COM18, STM32F411 / INA229, FW 0.5.0,
SPI 6 МГц. Прошивка, Native DMA, CSF4 v1/v2, SUSPECT, RAW и Flash не изменялись.
Это короткая проверка переключения/START/STOP, не длительная аттестация режимов.

## Подтверждённая первопричина

В GUI 0.6.2 `DeviceProtocol.native` устанавливался в False при создании
соединения. Выбор capture mode в MCU при закрытии COM сохраняется. После
предыдущего Native новый Python-сеанс мог выбрать BALANCED/DMA, оставив
`ACQ_CAPTURE_SELECTED: NATIVE`. Ветка возврата в continuous выполнялась только
при локальном `was_native=True`, а для POLL команда смены SPI mode отправлялась
ещё до выхода из DMA-only capture.

В воспроизведении BALANCED/DMA прибор вернул
`ERR: REPEAT_REQUIRES_DMA_NON_FAST` на `stream start`: Native streaming в FW
допускает только FAST / DMA / 84 мкс / ALATCH=1 / VERIFY OFF. Строка ошибки
прошивки общая для нескольких несовместимых комбинаций; она не доказывает,
что был включён repeat. Общий legacy start helper запоминал ERR, но продолжал
ждать ACK и выдавал TimeoutError через 3 секунды. Worker закрывал COM на любом
TimeoutError. Это ситуация **A: порт закрывал Python**, а не доказанное
исчезновение USB. Независимый монитор не зарегистрировал исчезновения COM18;
после освобождения порта единственный `ping` дал `pong`, без Reset.

VID/PID: 0483:5740; USB serial: 3282346D3032. Монитор опрашивает Windows каждые
100 мс: это журнал наблюдаемой enumeration, не доказательство отсутствия
событий короче периода опроса.

## Исправление и обработка ошибок

Перед мутациями проверяются mode, объявленный прошивкой профиль и известные
несовместимости. В FW 0.5.x штатная последовательность из IDLE:

```text
spi verify off
acq capture continuous
acq mode dma                 # либо poll
acq config                   # подтверждение ACQ_MODE_SELECTED
acq profile set balanced     # выбранный профиль; затем show/readback
acq config                   # подтверждение CONTINUOUS
```

Для Native после базового выбора DMA/FAST:

```text
acq capture native
acq alert adc 84
acq native latch 1
spi verify off
acq config                   # подтверждение NATIVE
```

Шаги 84 мкс / latch / verify подтверждаются ответами команд; idle ADC_CONFIG
остаётся диагностическим 0xFB68. Подтверждение конфигурации сессии выполняется
также по бинарному CONFIG и заключительным диагностическим отчётам. Native
не объявляется таймерным FAST/10000 Гц: номинал 1e6/84, фактическая частота
считается по принятым данным и timestamps.

Python публикует применённый профиль только после полного подтверждения.
При частичном отказе новая конфигурация не объявляется применённой. После
полного ERR с прочитанной границей CLI возможен один откат к ранее
подтверждённому preset; после timeout автоматического отката/повторного Start
нет. Следующий явный Start заново применяет полный preset.

`CommandRejected` содержит имя команды и исходный ERR. Отказ START немедленно
возвращает worker в IDLE, сохраняя serial и возможность повторного запуска.
Для timeout отдельной CLI-команды выполняется один ограниченный ping; для
неопределённого START — один STOP, ограниченное чтение END и проверка CLI.
Если связь не подтверждена, порт освобождается с точной причиной
`CONTROL_UNRESPONSIVE`; это не называется исчезновением USB. Нет циклов
переподключения, Reset или перепрошивки. Ошибки END/final diagnostics сохраняют
прежний путь завершения PARTIAL. Если после ACK за 3 секунды не пришёл ни один
отсчёт, worker один раз отправляет STOP; отсутствие END остаётся ошибкой.

В lifecycle log сохраняются запрос/preset, TX/RX CLI, traceback, timeout,
состояние worker, причина close и snapshot USB enumeration. Отдельный поток
enumeration продолжает наблюдение после close до завершения worker.
`B_USB_ABSENT` обозначает наблюдаемое отсутствие, `C_PRESENT_UNRESPONSIVE` —
неуспех контрольного обмена при присутствии, `A_HOST_CLOSE_DEVICE_PRESENT` —
закрытие host при присутствующем устройстве; исходная причина хранится рядом.
`SerialException` сама по себе не доказывает USB reset.

Изменение ComboBox — только локальный выбор. Native preset локально выбирает
FAST; реальная конфигурация меняется по «Применить» или «Измерять».
Пояснение выбранного режима и отдельно подтверждённый профиль находятся в
«Дополнительные настройки → Прибор». Native отключён для FW до 0.5.x.
Список профилей по-прежнему получается из FW, включая будущие профили.

## Матрица GUI для FW 0.5.0

| GUI / identifier | Профиль | VSHCT / ADC_CONFIG сессии | Acquisition | Streaming | Частота / ограничения |
|---|---|---|---|---|---|
| DMA continuous / dma | FAST | 50 мкс / 0xAA28 | Да | CSF4 v1 | Цель 10000 Гц, проверено коротким HIL |
| DMA continuous / dma | BALANCED | 150 мкс / 0xAAA8 | Да | CSF4 v1 | 5000 Гц |
| DMA continuous / dma | PRECISION | 280 мкс / 0xAAE8 | Да | CSF4 v1 | 2500 Гц |
| DMA continuous / dma | LOW_NOISE | 540 мкс / 0xAB28 | Да | CSF4 v1 | 1000 Гц |
| POLL continuous, диагностика / poll | FAST | 50 мкс / 0xAA28 | CLI поддерживает | **GUI блокирует до команд** | HIL FW 0.5.0: потери и нет END; использовать DMA |
| POLL continuous, диагностика / poll | BALANCED | 150 мкс / 0xAAA8 | Да | v1, диагностически | Цель 5000 Гц; HIL ≈2949.52 Гц с потерями, не полноценный режим 5 кГц |
| POLL continuous, диагностика / poll | PRECISION | 280 мкс / 0xAAE8 | Да | CSF4 v1 | 2500 Гц, короткая проверка успешна |
| POLL continuous, диагностика / poll | LOW_NOISE | 540 мкс / 0xAB28 | Да | CSF4 v1 | 1000 Гц, короткая проверка успешна |
| Native DMA 84 us / native | FAST preset | 84 мкс / 0xAA68 | Да | CSF4 v2 / RAW24 | ALATCH=1, VERIFY OFF, около 11912 Гц по ALERT, не синхронно TIM3 |

Для continuous GUI выставляет VERIFY OFF; ALATCH не является условием
таймерного continuous. Профиль определяет период 100/200/400/1000 мкс.
Native всегда требует DMA; выбор BALANCED и Native не является отдельным
поддержанным сочетанием: preset локально выбирает FAST. ADCRANGE=0, AVG=1.

Другие возможности FW не добавляются в ComboBox автоматически:

| Capture / инструмент FW | Назначение и ограничения |
|---|---|
| ALERT_MONITOR | Диагностика ALERT при таймерном continuous; отдельного GUI preset нет. Не объявляется новым гарантированным режимом streaming. |
| SINGLE_SHOT | DMA-only диагностические одиночные преобразования; reject допускается только здесь. Отдельного GUI streaming preset нет, 10 кГц не обещаются. |
| ALERT_DRIVEN | Диагностика синхронизации; FW явно запрещает `stream start` для этого capture. |
| spi repeat | DMA, не FAST; несовместим с Native и verify policy. Не штатный GUI preset. |
| spi freeze / spi pair / id | Самостоятельные диагностические тесты, не USB streaming preset. |
| Старые FW / DEMO | Прежний continuous CSF4 v1 путь сохраняется; Native v2 недоступен. |

## HIL и оставшаяся проблема POLL/FAST

Сначала проверен Native, затем после явного Disconnect/Connect — остальные
режимы. При обнаружении отсутствия END матрица была остановлена; повторных
запусков POLL/FAST не было. После анализа read-only отчётов эта комбинация
заблокирована в GUI и worker/protocol, без изменения FW. Затем отдельным
ограниченным прогоном проверены остальные POLL-профили и возврат в Native.

| Проверка | N | Частота по timestamps, Гц | Пропуски внутри ряда | END / ошибки SPI,DMA,CRC |
|---|---:|---:|---:|---|
| Native до исправления, ~2 с | 24193 | 11912.013 | 0 | Да / 0 |
| Native после исправления, ~2 с | 23967 | 11912.188 | 0 | Да / 0 |
| DMA FAST, ~1 с | 10273 | 10000.000 | 0 | Да / 0 |
| DMA BALANCED, ~1 с | 5000 | 5000.000 | 0 | Да / 0 |
| DMA PRECISION, ~1 с | 2560 | 2500.000 | 0 | Да / 0 |
| DMA LOW_NOISE, ~1 с | 1023 | 1000.000 | 0 | Да / 0 |
| POLL BALANCED, ~1 с | 3003 | 2949.519 | 2087 | Да / 0 |
| POLL PRECISION, ~1 с | 2569 | 2500.015 | 0 | Да / 0 |
| POLL LOW_NOISE, ~1 с | 1019 | 1000.003 | 0 | Да / 0 |
| Native после POLL, ~1 с | 12291 | 11912.216 | 0 | Да / 0 |

В continuous-сессиях отмечен также один начальный slot gap: он НЕ исключён из
health/счётчиков и сохранён в `quality_warnings`. COMPLETE у HIL означает
завершение процедуры переключения, а не отсутствие всех потерь измерений.
SUSPECT/аномалии не исправлялись и не исследовались.

Отдельная неустранённая неисправность POLL/FAST: MCU принял START V1 и STOP,
но END не сформирован. Последующая read-only диагностика показала
ADC_SAMPLES=5881, USB_SAMPLES_SENT=4160, FRAMES_DROPPED=1,
LOST_SAMPLE_SLOTS=4194; STOP_RECEIVED=STOP_PROCESSED=9,
END_QUEUED=END_TX_COMPLETED=8 (накопительные счётчики; предыдущие 8 сессий
завершились). SPI_OWNER_CONFLICTS=0, SPI_ERRORS=0. Это подтверждает отказ
завершения с потерями, но не устанавливает внутреннюю причину в FW/transport.
USB оставался перечислен; после close `ping` и восстановление Native прошли.
Исправление FW потребует отдельной задачи/согласования. Произвольные другие
POLL-комбинации не скрыты ради успешного результата.

Во всех сохранённых прогонах enumeration не отмечал исчезновения USB;
калибровка до/после совпала: offset=0, gain=1000000, enabled=0, SOURCE=FLASH,
DIRTY=0. В конце восстановлены FAST / DMA / NATIVE / 84 / latch1 / verifyOFF,
acquisition остановлен. Перепрошивки и записи Flash не было.

## Доказательства и проверки

- [До исправления: команды и ответы](../results/gui_062a/before/commands.log), [summary](../results/gui_062a/before/summary.json).
- [После исправления: DMA и отдельный отказ POLL/FAST](../results/gui_062a/after/commands.log), [summary](../results/gui_062a/after/summary.json).
- [Read-only диагностика отказа POLL/FAST](../results/gui_062a/after/poll_failure_diagnostics.json).
- [POLL follow-up и финальный Native](../results/gui_062a/poll_followup/commands.log), [summary](../results/gui_062a/poll_followup/summary.json).
- [Поддержанный выбор Native](../results/gui_062a/screenshots/supported_native.png), [заблокированный POLL/FAST](../results/gui_062a/screenshots/unsupported_poll_fast.png).

Скриншоты — реальная Qt-отрисовка в DEMO с capability metadata FW 0.5.0,
не фотографии аппаратного теста. [Проверка отсутствия IO при выборе](../results/gui_062a/screenshots/proof.json).

128 GUI-тестов и 49 CLI/HIL/analysis-тестов прошли. Новые тесты покрывают
все предлагаемые presets/профили и блокируемый POLL/FAST, Native и старую FW,
сохранённый capture после подключения, порядок команд, ERR/откат, command
timeout, ACK timeout, отсутствие первых samples, SerialException, отсутствие
COM, enumeration removal/reappearance, явный reconnect и retry, ComboBox без IO.
Существующие тесты покрывают потерю USB при записи, Stop/Disconnect, RAW,
архивы, timestamps/rollover, курсоры и SUSPECT.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests_gui
.\.venv\Scripts\python.exe -m unittest discover -s tools -p "test_*.py"
.\.venv\Scripts\python.exe -m current_sensor --port COM18
# Только при закрытых GUI/PuTTY и готовой плате; новое имя не затирает доказательства:
.\.venv\Scripts\python.exe tools/hil/mode_switch_hil.py --label manual_verify_1
```

Изменённые исходники этого этапа: `current_sensor/__init__.py`,
`device_protocol.py`, `device_worker.py`, `main_window.py`, `layout_controls.py`;
добавлены `mode_capabilities.py`, `connection_monitor.py`,
`tests_gui/test_mode_switch_062a.py`, `tools/hil/mode_switch_hil.py`,
`tools/gui_modes062a.py`; обновлены README и этот отчёт. Остальные ранее
существовавшие изменения рабочей копии к этому исправлению не относятся.
Commit/push не выполнялись.
