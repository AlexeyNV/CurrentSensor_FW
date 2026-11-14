# CurrentSensor v1.0.0

Firmware 1.0.0 и Python GUI 1.0.0, на основе FW 0.5.0 / GUI 0.6.2a.

Настройки полностью встроены в главное окно: inline либо страница вместо
графика; отдельного SettingsDialog больше нет. Сохранены streaming, запись,
курсоры, LIVE/MANUAL и выбранная страница. Проверены 45 сочетаний страниц,
размеров окна и увеличения шрифта. Native теперь принимает строку FW 1.0.0;
защита от неподдержанного POLL/FAST сохранена без закрытия COM.

Изменение firmware этого этапа ограничено идентификаторами версии/сборки.
INA229, SPI/DMA, USB CDC, тайминги, Native, CSF4 и Flash layout не менялись.

Проверенный release ELF:

```text
a0017ea7cbbc4419de551c9fd26c165702cf50f12131bc2b91db0fc49971a433
```

Он прошит через ROM DFU с backup сектора 7, стиранием только 0–4, skipErase и
verify. SHA-256 сектора 7 до/после совпал. INFO=1.0.0; smoke-test PASS.
Native 60 с: 715505 отсчётов, 11912.2354 Гц, END, без CRC/sequence/timestamp и
SPI/DMA errors, runtime losses и переполнений. GUI streaming, короткая и
сегментированная запись, настройки во время записи и Disconnect/Reconnect
проверены. Регрессия: 130 GUI + 49 CLI/HIL + 16 C тестов PASS.

**WARNING / известные ограничения:** 45 превышений номинального service budget
и 4 подозрительных RAW в 60-секундном тесте; причина редких выбросов не
локализована. Все RAW сохраняются. PARTIAL архива может консервативно учитывать
Native STOP-discard даже при полном сохранении принятых данных. POLL/FAST
streaming заблокирован; POLL/BALANCED диагностический и имеет потери. Отсутствие
потерь всех внутренних конверсий INA229 и точность ±10 мА во всём диапазоне
0–10 А не гарантируются. Старые проверки Fluke относятся лишь к точке около
117 мА и не заменяют испытания нового ELF.

Запуск GUI из исходников:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m current_sensor
```

Выберите COM, «Подключить», в «Прибор» выберите Native DMA 84 us и нажмите
«Измерять». Полная инструкция — README.md; не открывайте один COM одновременно
в GUI и PuTTY. Собственный код MIT; сторонние условия сохранены.

В GitHub Release публикуются испытанный ELF, BIN/HEX той же сборки,
SHA256SUMS, build_manifest.json, validation.json, эти notes, README и лицензии.
Исходный код/документация доступны по тегу v1.0.0. Большие RAW и Flash-backup
не входят в публичный набор. Воспроизводимость проверена повторной сборкой
при SOURCE_DATE_EPOCH=1794614400 и GCC for STM32 14.3.1.
