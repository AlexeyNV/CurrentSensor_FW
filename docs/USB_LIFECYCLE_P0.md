# P0: жизненный цикл USB CDC после STOP

## Подтверждённое по исходникам

В GUI `DeviceWorker._stop_stream()` раньше сначала синхронно писал
`stream stop\n` и только после успешного возврата переводил состояние в
`stopping`. При `Write timeout` состояние оставалось `streaming`, а обработчик
ошибки вызывал `_disconnect()`, который **повторно писал тот же STOP** перед
`serial.close()`. Это подтверждённый дефект host-обработки ошибки и возможный
источник второй блокировки. Он не доказывает, что прежний timeout на COM18
вызван именно GUI: в CLI такой повторной записи не было.

Также прежний `_disconnect()` выставлял `disconnected` после `close()` без
проверки, закрылся ли handle. Если `close()` зависал, worker оставался внутри
системного вызова. Qt event loop не вызывает `serial.close()` напрямую и не
делает бессрочный `join()`; закрытие окна опрашивает состояние worker.
Первичный стек аппаратного зависания не был сохранён, поэтому причина
Windows Error 5/433 остаётся неустановленной.

Теперь `stopping` выставляется до STOP, повторная отправка STOP в аварийном
пути удалена, а закрытие COM выполняется только в serial worker. При закрытии
вызываются доступные `cancel_read()` и `cancel_write()`, затем `close()`;
состояние `disconnected` выставляется лишь после возврата и проверки
`is_open`/`closed`. Неудача даёт `disconnect_failed`, сохраняет ссылку на
порт и позволяет повторить Disconnect. Штатное `idle` после измерения
выставляется только при наличии CSF4 END и текстового подтверждения, после
финальной диагностики. В UI `disconnecting` и `disconnect_failed` различимы.

## Файл трассы

GUI создаёт `logs/current_sensor_lifecycle_<UTC>_<PID>.log` в рабочем каталоге
и показывает путь во вкладке диагностики. Строки содержат monotonic seconds,
PID, thread ID, стадию и состояние serial handle. Включены стадии запроса
STOP, записи STOP, END, финальной диагностики, перехода в idle, Disconnect,
отмены pending read/write, закрытия и повторного открытия COM, завершения
worker. События из Qt-потока ставятся в очередь; запись выполняет отдельный
поток. Если serial-операция длится более 2 с или UI heartbeat прекращается
более чем на 2 с, watchdog сохраняет `WATCHDOG_STACKS` или
`UI_UNRESPONSIVE` со стеками всех Python-потоков. Это позволяет увидеть
последний блокирующий вызов даже при неотзывчивом окне.

## Диагностика FW

`stream status` дополнен накопительными с момента включения MCU полями:
`CDC_RX_PACKETS`, `CDC_RX_BYTES`, `CDC_OUT_REARM_ERRORS`,
`CDC_CONTROL_LINE_EVENTS`, `CDC_LAST_CONTROL_LINE`, `CDC_TX_COMPLETIONS`,
`STREAM_STOP_COMMANDS`, `STREAM_END_COMPLETED`. Счётчики обновляются короткими
операциями, без SPI/USB-передачи из IRQ. CDC OUT после RX по-прежнему
повторно разрешается. DTR/RTS теперь наблюдаются, но не меняют режим работы;
автоматическая остановка по DTR пока не вводилась без аппаратной трассы.
`CDC_RX_BYTES` считает байты, переданные USB callback, даже если RX-кольцо
позднее переполнилось. `CDC_TX_COMPLETIONS` считает все завершённые USB IN
передачи. `STREAM_STOP_COMMANDS` считает только полностью разобранные строки
STOP, а `STREAM_END_COMPLETED` — сеансы, где транспорт завершил END без
ошибки. Все поля 32-битные и могут переполниться при очень долгой работе.

Если `STREAM_STOP_COMMANDS` не вырос после отправки STOP, исследуйте USB OUT
и Windows CDC драйвер. Если STOP получен, но `STREAM_END_COMPLETED` не вырос,
проверяйте `Acq_Stop()`, завершение SPI DMA, очередь CSF4 и USB IN. Если END
подтверждён, а повторное открытие COM не проходит, сохраните GUI stack dump,
Windows ошибку и USB control/close trace: проблема находится после завершения
измерительного протокола или на уровне драйвера/handle.

## Порядок аппаратной проверки

1. Закройте PuTTY и другие приложения, владеющие COM18. Запустите GUI и
   выполните CONNECT → DISCONNECT → RECONNECT без измерения.
2. Выполните CONNECT → START FAST → STOP; убедитесь в `END_RECEIVED`,
   `FINAL_DIAGNOSTICS_FINISHED`, `ACQUISITION_IDLE`. Затем DISCONNECT и
   RECONNECT; проверьте `SERIAL_CLOSE_FINISHED` и
   `SERIAL_REOPEN_FINISHED`.
3. Повторите START → STOP дважды, затем закройте окно. Проверьте
   `WORKER_EXITED`, `CLOSE_WINDOW_FINISHED` и отсутствие
   `WATCHDOG_STACKS`/`UI_UNRESPONSIVE`.
4. Повторите CLI-запись на 1, 5 и 60 с:

```powershell
.\.venv\Scripts\python.exe -u tools\stream_capture.py --port COM18 --profile balanced --acq-mode dma --seconds 1 --output recordings\lifecycle_1s.csv
.\.venv\Scripts\python.exe -u tools\stream_capture.py --port COM18 --profile balanced --acq-mode dma --seconds 5 --output recordings\lifecycle_5s.csv
.\.venv\Scripts\python.exe -u tools\stream_capture.py --port COM18 --profile balanced --acq-mode dma --seconds 60 --output recordings\lifecycle_60s.csv
```

После каждого запуска требуются END, `session_complete=true`,
`csv_matches_end=true`, закрытый COM и успешные `ping`/`stream status` после
повторного открытия PuTTY. При сбое сохраните lifecycle log, stderr CLI,
metadata JSON, значение `stream status` при доступности и точный текст ошибки
Windows. Не запускайте PuTTY одновременно со скриптом: COM-порт эксклюзивен.
Если COM не открывается ещё до START, это отдельная стадия `PORT_OPEN`; в
текущем окружении такая блокировка уже наблюдалась и не позволяет считать
аппаратную проверку завершённой.

В Git нет сохранённого дерева FW 0.4.3: последняя версия в истории —
v0.2.0-calibration. Поэтому точный межверсийный diff 0.4.3 → 0.4.4-debug
невозможен; аудит выполнен по текущим исходникам и известному протоколу.
Аномальные RAW-отсчёты в этой задаче не менялись.
