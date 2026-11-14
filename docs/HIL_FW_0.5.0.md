# HIL and FW 0.5.0

The main Native preset is DMA, VSHCT=84 us, one conversion/result (AVG field=0),
ADC_CONFIG=0xAA68, ALATCH=1, SPI_VERIFY=OFF, SPI 6 MHz, shunt 5 mOhm. Frozen,
phase and double-read commands remain available but their development is frozen.
No automatic calibration or power-load control is implemented.

See [actual hardware results and artifact links](HIL_RESULTS_0.5.0.md).
Integrity criteria passed for streaming; the overall verdict remains WARNING.
No absolute 10 mA accuracy or capture of every internal conversion is certified.

## Commands

Close PuTTY/GUI before opening a serial owner. VID/PID 0483:5740, USB serial and
INFO identify the device; ambiguous candidates are rejected before opening.
Every run creates results/hil/<UTC>_<unique-id>/ with CDC bytes, summary.json,
report.md, Git revision/dirty state, host ELF SHA256/ranges and device identity.

```powershell
.\.venv\Scripts\python.exe tools/hil/run.py --port auto --suite smoke --expect-fw 0.5.0
.\.venv\Scripts\python.exe tools/hil/run.py --suite stream --expect-fw 0.5.0 --runs 1 --seconds 5
```

Smoke defaults to three runs, each using supported `acq test 5`. Stream durations
are explicitly 5, 60 or 300 seconds; run the shorter test first. Optional
--serial-number pins a known CDC serial. --expect-fw defaults to baseline
0.4.8-debug.2a for baseline comparison; specify 0.5.0 for the new firmware.
No cal-save commands are issued. Cleanup stops acquisition, selects continuous
capture for legacy clients and closes COM in finally. Partial CSV is retained.

Smoke checks configuration, INA identity, >=10000 Hz, SAMPLES=STATS_SAMPLES,
zero SPI/DMA/timeout/overflow/runtime loss/queue/invariant/overrun counters, and
restored registers plus ordinary measurement. CNVRF_ZERO is reported separately.
Streaming checks END counts against CSV, framing/CRC, sequence, timestamps,
configuration, Native runtime losses and bounded acquisition/USB queue errors.
NATIVE_DMA_OVERRUNS means complete service cycles >=nominal ADC period, not
physical DMA errors; streaming records it as WARNING while smoke requires zero.
Suspicious measured values are separate warnings, not automatically corrupt
transport. Exit 0 includes WARNING; callers must inspect summary.status.

Full binary RX is logged as timestamped hex, allowing offline verification:

```powershell
.\.venv\Scripts\python.exe tools/hil/verify_capture.py results/hil/RUN_DIRECTORY
```

This rechecks frame CRC/CONFIG/END, reconstructs every sequence/TIM2/RAW tuple,
compares a rolling SHA256 with CSV, validates rollover-safe elapsed time and
writes verification.json without rewriting the original summary.

## CSF4 v2

Explicitly approved in this session because v1's signed CODE20 loses reserved
RAW bits. Native acknowledges `STREAM: BINARY V2`. Header version=2, flags=1
(event-driven), period_us=0 (no fixed output period). Other header fields,
frame types, lengths and CRC32 rules retain their v1 layout. DATA stores uint32
little-endian RAW24 (upper byte zero), then actual uint16 TIM2 offsets relative
to the first sample timestamp. A sequence gap or offset >65535 starts a new
frame. CONFIG is 16 bytes, ADC_CONFIG=0xAA68 and period=0. No artificial samples,
replacement RAW or timestamp interpolation are used.

Sequence counts accepted Native results; it is not an internal ADC conversion
identifier. END retains its 19-word layout. LOST_SAMPLE_SLOTS in v2 reports
known omitted notifications, including intentional STOP tail. Native post-report
separates runtime losses from STOP tail. Every accepted result still has uncertain
conversion association. The existing million-sample diagnostic statistics cap
does not cap transmission; long tests compare full END/CSV counts.

At ~11912 samples/s, 32-sample DATA frames use about 84.9 kB/s before USB overhead.
The existing bounded transport retains immutable in-flight storage and BUSY
handling. For v2, frame building uses smaller steps and Native is serviced before
the next transport step. Startup now resets ring metadata without clearing its
large unpublished payload; a reproduced initial 2.5 ms delay was removed.
The DMA watchdog remains 80 us; ADC/ALERT algorithms are unchanged.

Continuous streaming remains v1. Native v2 is accepted only for DMA/FAST/explicit
ADC84/latch1/verify-off; other combinations are refused. Power-on continuous mode
is retained for existing clients. Python FrameDecoder supports both versions;
use HIL --suite stream for v2 (legacy stream_capture CLI profile logic remains
v1). Full RAW is saved in HIL CSV; current_mA is uncalibrated CODE/16. Calibration
state is saved and current_cal_ma uses integer nanoamp scaling/rounding.

## Existing GUI

```powershell
.\.venv\Scripts\python.exe -m current_sensor --port COM18
```

Select **Native DMA · 84 us · FW 0.5** and press Measure. The preset confirms DMA,
FAST, ADC84, latch1 and verify-off. Charts use actual TIM2 timestamps, not 84 us
synthetic spacing; live rate is calculated from received samples. Service-cycle
warnings, runtime losses and intentional STOP tail remain distinct. Calibration
display uses the existing saved coefficients. Native recording metadata declares
protocol_version=2; original RAW24 is retained in the wire .csf4 file, while the
existing numeric RAW-history/CSV remains CODE20 and actual timestamps. No archive
schema was changed. Short record/stop/reconnect/reopen has real hardware coverage;
long Native GUI archives still need a dedicated endurance test.

The reproducible headless test exercises the existing Qt window and serial
worker on real hardware, saves a live plot PNG and records/reconnects:

```powershell
.\.venv\Scripts\python.exe tools/hil/gui_smoke.py
```

## Safe programming

Installed programmer 2.23.0 supports explicit sector erase and --skipErase,
verified against local --help and [ST's version-matched command reference](https://dev.st.com/stm32cube-docs/prog/2.23.0/en/docs/markup/CubeProg_Command_Lines.html).
Ordinary download implicitly erases; checking only ELF ranges is insufficient.

The safe module validates ARM little-endian ELF32 and every file-backed PT_LOAD
physical address against 0x08000000..0x0805FFFF. It preserves an immutable image
copy. Actual writing requires --approve-flash plus an explicit DFU serial and
exactly one matching device, Device ID 0x431, complete successful sector-7 read
and a SHA256 manifest saved before erase. ROM DFU on this board refused factory
size-register reads and reports NVM size as a default; that default is not used
as proof of physical capacity. Full 128KB access at 0x08060000 is mandatory.

Only ELF-overlapping sectors 0..6 can be explicitly erased. Download uses
--skipErase and -v, then sector 7 is read again and hashed. Failure/mismatch
blocks restart. Mass erase, option bytes, read-unprotect and bootloader writes
are never issued. A blocked check never silently falls back to unsafe download.

After explicit approval and physical BOOT0/RESET entry to system DFU:

```powershell
.\.venv\Scripts\python.exe tools/hil/run.py --flash --approve-flash --dfu-serial 3282346D3032 --wait-dfu 120 --expect-fw 0.5.0 --suite smoke
```

GO starts 0x08000000 after verification; CDC re-enumeration is bounded, then
INFO/smoke run. Return BOOT0 low before ordinary reset if it was held high.
This exact procedure was exercised with operator approval; see the result report
for sector hash and erased sectors. No further permission is inferred for load
switching or calibration writes.
