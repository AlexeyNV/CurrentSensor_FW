# CurrentSensor 1.0.0

USB-измеритель тока на **STM32F411RET6 + INA229** с Python GUI для наблюдения,
записи и анализа исходных измерений. Firmware 1.0.0 и GUI 1.0.0 основаны на
испытанных FW 0.5.0 / GUI 0.6.2a; новых измерительных режимов в релизе нет.

[GitHub Release v1.0.0](https://github.com/AlexeyNV/CurrentSensor_FW/releases/tag/v1.0.0)
· [Результаты нового ELF](docs/RELEASE_VALIDATION_1.0.0.md)
· [Release notes](RELEASE_NOTES_1.0.0.md)

## Аппаратная часть

STM32F411RET6 управляет INA229 по SPI1, mode 1, 6 МГц. Шунт — **5 мОм**.
ALERT сообщает о готовности результата; SPI DMA читает RAW24, TIM2 задаёт
временную шкалу MCU. Через USB CDC проходят команды и бинарные измерения.

В предоставленной схеме USB-изоляция выполнена на **ADUM3160BRWZ**, изолированное
питание — **B0505S-1WR3L**. Это описание реализованной схемы, а не заявление о
проверенной электрической безопасности или категории измерений.
[Схема, Gerber и подключения](docs/HARDWARE.md).

## Программная часть и возможности

- `App/`: acquisition, INA229, кольцевые буферы, transport и Flash-калибровка;
  без RTOS и динамической памяти, совместимо с CubeMX USER CODE.
- `current_sensor/`: один владелец serial, обработка RAW, фоновая запись и Qt GUI.
- `tools/`: CLI, общий CSF4 decoder, HIL, офлайн-анализ и подготовка релиза.
- CSF4 v1 сохраняет CODE и реальные timestamps; Native использует CSF4 v2 с
  полным RAW24, CRC32, последовательностями, CONFIG/STATS и проверяемым END.
- Графики, X/Y курсоры, MIN/MAX/AVG/RMS по полному RAW между X1–X2, GAPS,
  Robust Y / Full RAW, LIVE FOLLOW/MANUAL и Freeze без остановки записи.
- SUSPECT — диагностическая маркировка. RAW не исправляется; Display Clean и
  статистика без SUSPECT отделены от исходного ряда.
- Короткая запись CSV/RAW/CSF4 с JSON metadata; сегментированный архив CSF4 с
  `session.json`, индексом, ротацией и состояниями COMPLETE/PARTIAL/DISK_ERROR.
- Экспорт исходного участка/истории и PNG. Экспорт по курсорам включает все RAW,
  а не прореженные экранные данные.

Настройки теперь всегда находятся **внутри главного окна**: панель над графиком
или страница вместо графика при недостатке высоты. Отдельное окно настроек не
создаётся; основные кнопки доступны, страница и состояние просмотра сохраняются.

## Установка и запуск

Проверенная среда: Windows 10, CPython 3.12, зависимости из lock-файла.
Windows 11 не аттестован отдельно. Нужны доступный USB CDC и свободный COM.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m current_sensor
```

1. Выберите COM прибора и нажмите «Подключить». Само подключение измерение не запускает.
2. Откройте «Дополнительные настройки → Прибор», выберите **Native DMA · 84 us · CSF4 v2**.
3. Нажмите «Применить профиль» или «Измерять». Native preset выбирает FAST,
   DMA, VSHCT=84 мкс, ALATCH=1, VERIFY OFF; фактическая частота около 11912 Гц.
4. Для сохранения выберите каталог/имя в «Запись», при необходимости включите
   «Длительная запись CSF4», затем «Записывать на диск». Запись и измерение —
   отдельные действия; «Пауза графика» не останавливает их.
5. «Остановить» завершает acquisition и проверку END. Дождитесь финализации файлов.
6. «Открыть эксперимент» читает JSON короткой записи либо `session.json` архива.
   Через меню «Файл» сохраняются история, выбранный участок и PNG.

GUI и PuTTY/CLI не должны одновременно владеть одним COM. Ошибка конфигурации
показывается с причиной и не закрывает COM без необходимости. После реального
USB disconnect доступны явное отключение/повторное подключение, без циклов Reset.

DEMO без оборудования: `python -m current_sensor --demo`. Replay существующего
потока: `python -m current_sensor --replay recordings/session.csf4`. DEMO/REPLAY
явно обозначаются; их изображения не выдаются за новое аппаратное испытание.

### Режимы

| GUI preset | Профиль / частота | Протокол и ограничения |
|---|---|---|
| Native DMA 84 us | FAST preset, фактически ≈11912 Гц | v2 RAW24, ALERT-driven, не синхронизирован с TIM3 |
| DMA continuous | FAST 10000 / BALANCED 5000 / PRECISION 2500 / LOW_NOISE 1000 Гц | v1; краткие аппаратные проверки FW 0.5.0 пройдены |
| POLL continuous, диагностика | PRECISION 2500 / LOW_NOISE 1000 Гц | v1; короткие проверки FW 0.5.0 пройдены |
| POLL BALANCED | цель 5000 Гц | Диагностический; в HIL были потери, не полноценный режим 5 кГц |
| POLL FAST | acquisition доступен в CLI | GUI streaming блокируется до команд: известен отказ END |

Ограничения POLL FW 0.5.0 сохранены для 1.0.0; firmware алгоритмы не менялись.
Релизный длительный smoke относится к Native, не ко всем комбинациям.
Другие диагностические capture-тесты прошивки не являются GUI streaming presets.

### Единицы и калибровка

При 5 мОм исходный ток: `I_raw_nA = VSHUNT_CODE * (ADCRANGE ? 15625 : 62500)`.
Программная калибровка: `(I_raw_nA - offset_ua*1000)*gain_ppm/1000000`, с
целочисленным округлением. При disabled результат равен исходному. В `measure`
`CURRENT_mA` остаётся RAW, `CURRENT_CAL_mA` — отдельно откалиброванный ток.
Команды `cal show/set/enable/save/load/reset` сохранены; только явный `cal save`
пишет Flash и запрещён во время acquisition. Автоматической калибровки нет.

## Сборка и безопасная прошивка

Нужны GNU make, GNU Arm toolchain и STM32CubeProgrammer в PATH. Проверены
STM32CubeCLT 1.22.0, GCC for STM32 14.3.1 и CubeProgrammer **2.23.0**.

```powershell
make -j4
# Фиксированная дата и stripped ELF, соответствующий release-процедуре:
.\.venv\Scripts\python.exe tools/release_prepare.py
```

Скрипт использует `SOURCE_DATE_EPOCH=1794614400`, отдельный build-каталог и
`objcopy --strip-debug`; существующий release manifest не перезаписывает.
Повторная чистая сборка этой версии дала тот же ELF SHA-256:

```text
a0017ea7cbbc4419de551c9fd26c165702cf50f12131bc2b91db0fc49971a433
```

Скачанный из Release ELF сверяйте с SHA256SUMS через `Get-FileHash -Algorithm SHA256`.
Обычная сборка без фиксированной даты может отличаться по идентификатору build.

**Сектор 7 (0x08060000–0x0807FFFF) содержит калибровку.** Linker ограничивает
приложение первыми 384 KiB. Не использовать mass/full chip erase, стирание
сектора 7 или изменение option bytes. Локальная резервная копия обязательна.

Переведите плату в системный ROM DFU через BOOT0/RESET. `STM32_Programmer_CLI -l usb`
покажет serial. В следующем примере замените `SERIAL_FROM_DFU_LIST` реальным
серийным номером единственного целевого STM32:

```powershell
.\.venv\Scripts\python.exe tools/hil/run.py --flash --approve-flash --dfu-serial SERIAL_FROM_DFU_LIST --suite smoke --runs 1 --expect-fw 1.0.0 --elf release/v1.0.0/CurrentSensor_FW_1.0.0.elf
```

Проверенная процедура подтверждает ID 0x0431, проверяет адреса ELF, читает весь
сектор 7 и его SHA-256, стирает только пересекаемые секторы приложения,
выполняет `--skipErase -d ... -v`, повторно читает сектор 7 и сравнивает хеши.
Несовпадение/неполный backup блокирует продолжение. Версия programmer ограничена
проверенной; отказ не следует обходить ручным mass erase. После записи вернуть
BOOT0 в нормальное положение перед следующим Reset.

Проверьте `info`, `ina`, `regs`, `measure`, затем Native streaming:

```powershell
.\.venv\Scripts\python.exe tools/hil/run.py --suite stream --seconds 60 --runs 1 --expect-fw 1.0.0 --elf release/v1.0.0/CurrentSensor_FW_1.0.0.elf
```

USB COM и DFU — разные состояния устройства. Полные backup/log/RAW сохраняются
в локальном `results/`; они исключены из Git и не поставляются в Release.

## Результаты испытаний

**Новый ELF 1.0.0:** smoke PASS; за 60 с получено 715505 результатов, 11912.2354 Гц,
END, ноль CRC/frame/sequence/timestamp и SPI/DMA errors, runtime losses и
переполнений. SHA-256 сектора 7 до/после совпал. GUI streaming, запись,
настройки при записи и Disconnect/Reconnect проверены.
**Предупреждения:** 45 превышений номинального service budget и 4 подозрительных
RAW за этот тест; это не скрыто за безусловным PASS.

**Ранее испытанная FW 0.5.0:** 300-секундный Native поток — 3574434 отсчёта,
около 11912 Гц, проверки USB integrity пройдены. Отдельная нагрузочная запись
300 с около 117 мА — 3573759 отсчётов. Fluke 17B+ до/после показывал 0.117 A;
средний RAW 116.078004 мА. Калибровка Fluke/температура не подтверждены.
Это ограниченная проверка одной точки, не точность ±10 мА для каждого отсчёта
или всего диапазона 0–10 А. [Подробности](docs/ACCURACY_100MA.md).

[Технический отчёт 1.0.0](docs/RELEASE_VALIDATION_1.0.0.md) и
[публичные счётчики/хеши](docs/releases/v1.0.0/validation.json).
Регрессия: **130 GUI + 49 CLI/HIL + 16 C тестов PASS**.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests_gui
.\.venv\Scripts\python.exe -m unittest discover -s tools -p "test_*.py"
cmake -S tests -B tests/build
cmake --build tests/build --config Release -j4
ctest --test-dir tests/build -C Release --output-on-failure
```

C-тесты проверены с MSVC / Visual Studio 2022. Шрифтовая Qt-проверка 100/125/150%
на 1366×768, 1694×1188, 1920×1080 охватывает 45 сочетаний настроек без отдельных
окон; не является сертификацией всех вариантов Windows DPI.

## Известные ограничения

- Источник редких RAW-выбросов не локализован; firmware не фильтрует/не исправляет RAW.
- Native имеет предупреждения временного бюджета. Timestamp фиксирует обслуживание
  результата MCU, не точный момент внутренней конверсии. Не гарантируется сохранение
  абсолютно всех внутренних конверсий INA229.
- Архив может консервативно остаться PARTIAL из-за Native STOP-discard при полном
  сохранении принятых RAW и END. Статус и счётчики не подменяются на COMPLETE.
- POLL/FAST streaming запрещён в GUI; POLL/BALANCED имеет потери.
- Метрологическая проверка ограничена: полная калибровка по точкам, большие токи и
  подтверждение точности на 0–10 А — последующая работа. Новых таких исследований
  в 1.0.0 нет. Испытания электрической безопасности не проводились.

## Скриншоты GUI 1.0.0

Главный экран: настоящий аппаратный streaming FW 1.0.0.

![Аппаратный Native streaming](docs/images/v1.0.0/main_live.png)

Настройки только внутри главного окна; ниже DEMO при шрифте 150%:

![Встроенная страница настроек](docs/images/v1.0.0/settings_page.png)

[Панель над графиком — DEMO](docs/images/v1.0.0/settings_inline.png) ·
[Просмотр настоящего архива — REPLAY](docs/images/v1.0.0/archive.png) ·
[График ранее записанного тока — REPLAY](docs/images/v1.0.0/current_plot.png) ·
[Курсоры — REPLAY](docs/images/v1.0.0/cursors.png) ·
[Статистика — REPLAY](docs/images/v1.0.0/statistics.png) ·
[SUSPECT — REPLAY](docs/images/v1.0.0/suspect.png).
[Происхождение изображений](docs/images/v1.0.0/sources.json).

## Лицензия и дальнейшая работа

Собственный код — [MIT](LICENSE), выбранная владельцем проекта.
[Лицензии сторонних компонентов](docs/THIRD_PARTY_NOTICES.md) сохранены отдельно.
Схема и Gerber доступны в [docs/gerber](docs/gerber).
История предыдущих этапов — [архив README](docs/DEVELOPMENT_HISTORY.md).
После публикации v1.0.0 фиксируется тегом; следующие измерительные исследования
выполняются в последующих версиях.
