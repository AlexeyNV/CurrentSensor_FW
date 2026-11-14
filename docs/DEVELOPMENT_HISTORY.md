# ????? README ?? v1.0.0

???????????? ??????? ? ??????, ?? ?????????? ?????? 1.0.0. ??????? ?????? results/ ???????? ??????????.

# CurrentSensor_FW

FW **0.5.0** adds Native CSF4 v2 RAW24 streaming, HIL automation and Native GUI
mode. Operator-approved flashing preserved sector 7. Three smoke runs passed;
5/60/300-second streaming and GUI recording/reconnect verified integrity near
11912 Hz. Overall streaming status is **WARNING**: service-cycle overruns and
rare suspicious RAW values remain visible; accuracy is not certified.
See [hardware results](HIL_RESULTS_0.5.0.md) and
[commands, protocol and safe flashing](HIL_FW_0.5.0.md).

FW **0.4.8-debug.2a** fixes phase-experiment notification rearming after busy
conversions, preserves IRQs across ACK foreground dispatch, and adds
`spi pair wait` with a pre-cleanup WAIT_TIMEOUT snapshot. See the
[cause, timing analysis and repeat-test procedure](FW_0.4.8_DEBUG_2A.md).
Native acquisition and the DMA watchdog are unchanged. Board validation pending.

FW **0.4.8-debug.2** records the first sixteen **mismatching** Native diagnostic
pairs with immutable DMA RX snapshots, ALERT/DIAG/VSHUNT timing, GPIO/pending
state and RAW-to-byte consistency checks. `spi pair` adds a separate continuous
phase experiment with nonblocking first-read/gap delays and timing histograms.
The [technical audit and board/logic-analyzer procedure](FW_0.4.8_DEBUG_2.md)
separate observed evidence from unconfirmed causes. The optional PA8 mismatch
marker is disabled unless explicitly confirmed at build time and enabled by CLI.
No FW 0.4.8-debug.2 hardware result or root cause is claimed.

FW **0.4.8-debug.1** adds `spi freeze`: one triggered conversion followed by
10,000 or 100,000 pairs of exact VSHUNT reads through the existing SPI1 DMA
backend at 6 MHz. An independent repeated ID-register test, ADC restart audit,
first-16-error trace and verified configuration restoration help separate
continuous-register updates from read-path faults. See the [freeze test guide,
counter definitions, host results and safe board procedure](FW_0.4.8_DEBUG_1.md).
The owner subsequently reported clean frozen-register and ID board tests; see
the debug.2 report for those inputs. Native/ALERT state machines, CSF4,
calibration and sector 7 are unchanged by the freeze addition.

FW **0.4.8-debug** adds experimental `acq capture native`: no TIM3 slot decimation,
separate accepted-result numbering, bounded IRQ queue, explicit uncertainty and
latency diagnostics. Native streaming remains blocked. See the [datasheet audit,
state machine, counter definitions and staged board checks](FW_0.4.8_DEBUG.md).
No Native 20 kHz hardware result is claimed.

FW **0.4.7-debug.2** repairs DMA watchdog/callback ownership and prevents double
primary commits. [Audit, BALANCED model and board procedure](FW_0.4.7_DEBUG_2.md)
distinguish the reproduced timeout race from the still-unconfirmed physical
CNVRF service pattern behind BALANCED losses. New stopped-state diagnostics:
`acq dma report`, `acq event report`. [Native feasibility](NATIVE_FEASIBILITY.md)
is an analysis only; hardware validation is pending.

FW **0.4.7-debug.1** fixes the ALERT-driven clock-snapshot race and commits output
slots only after successful sample receipt. See [root cause, state transitions,
regression results and board checks](FW_0.4.7_DEBUG_1.md).
ALERT-driven USB streaming is blocked in this debug build pending board validation.

FW **0.4.7-debug** adds the explicit continuous `acq capture alert_driven`
experiment. See [architecture audit, timing budget, counters and board procedure](FW_0.4.7_DEBUG.md).
Default acquisition remains TIM3/DMA. ALERT-driven has host coverage and awaits
hardware validation; no 10 kHz result is claimed.

Firmware 0.4.6-debug for STM32F411RET6 + INA229AIDGST, SPI1 and USB CDC. Four RAM-selected measurement profiles share the existing TIM3/SPI DMA acquisition path and CSF4 v1 transport. FAST, DMA and SPI 6 MHz remain the power-on defaults. The owner reports approximately 10,000 samples/s without registered losses during a 90-minute FW 0.4.3 run; rare implausible VSHUNT codes remain under investigation. The new FW 0.4.6-debug ALERT/single-shot paths have **not** been tested on hardware. See the [earlier debug audit](FW_0.4.4_DEBUG.md).

## FW 0.4.6-debug: ALERT and read verification experiment

The default `acq capture continuous` path is the existing TIM3 + continuous INA229 + SPI DMA acquisition. No new setting is saved to Flash. `acq capture alert` enables `DIAG_ALRT.CNVR` (bit 14), clears `ALATCH` and `APOL`, and observes falling edges on PB1/EXTI1 while retaining continuous acquisition and its original RAW values. `acq capture single` writes a profile-specific triggered-shunt ADC configuration on each eligible TIM3 slot: FAST `0x2A28`, BALANCED `0x2AA8`. It waits for ALERT, reads `DIAG_ALRT.CNVRF` once, then performs one or two separate DMA VSHUNT transactions. A busy conversion is never restarted on the next tick. A missing ALERT times out after `VSHCT_US + 500 us`; elapsed slots are counted as missed. Stop, USB disconnect and test completion stop DMA and restore the previous ADC_CONFIG and DIAG_ALRT control bits. `CONFIG.ADCRANGE` must remain zero. This experiment does not write Flash or hardware current-calibration registers.

The [TI INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf), sections 7.3.4, 7.3.7, 7.4.1, and register tables 7-6/7-16, states that triggered conversion returns to shutdown, writing ADC_CONFIG restarts conversion, and reading DIAG_ALRT clears CNVRF. `CNVR=1` routes ready to ALERT. `APOL=0` is active-low open-drain and needs a pull-up; `ALATCH=0` is transparent. CubeMX configures PB1 as falling-edge EXTI1 with no internal pull-up and IRQ priority 5. TIM3 is priority 5, SPI DMA/TIM2 priority 4, USB FS priority 6. The repository contains no PCB schematic or R22 BOM, so the stated INA229 pin 3 → PB1 and 10 kΩ to 3.3 V must be confirmed on the actual board before relying on ALERT.

At 6 MHz, one 16-bit register transaction takes 4 us on the wire and each 24-bit VSHUNT read takes 5.33 us. The single-shot sequence uses two DIAG_ALRT reads, one ADC_CONFIG write and two VSHUNT reads: at least 22.7 us of SPI clocks, plus CS/IRQ/software time. The datasheet specifies a **typical**, not worst-case, 60 us wake from shutdown. With a 50 us FAST conversion, the nominal sum before software overhead is about 133 us against a 100 us slot; BALANCED is about 233 us against 200 us. Earlier FW 0.3.2 hardware data measured about 156 us to ready for one FAST single-shot. Thus single-shot cannot be assumed to sustain 10 kHz FAST or 5 kHz BALANCED. Its actual rate, lost slots and verification effects require board measurements; `acq test` reports a rate error when the target is missed. The continuous baseline remains available.

| Profile | TIM3 ticks | Indicative triggered sequence, before software overhead |
| --- | --- | --- |
| FAST | 0, 100, 200 us | Trigger at 0 → typical wake + conversion ready near 110 us → DIAG + two DMA reads; the 100 us tick is already busy. |
| BALANCED | 0, 200, 400 us | Trigger at 0 → typical wake + conversion ready near 210 us → DIAG + two DMA reads; the 200 us tick is already busy. |

At a hypothetical target of 10 kHz, this path would add about 10,000 EXTI callbacks/s, 20,000 VSHUNT DMA completions/s, and 30,000 foreground register transactions/s to the 10,000 TIM3 IRQ/s. These are event-count estimates, not a measured CPU-utilization or worst-case latency bound. The ALERT IRQ only stores a TIM2 timestamp and counters; the timestamp marks IRQ service, not the exact electrical edge. No SPI, USB, or text formatting runs there. DMA completion closes CS after SPI BSY clears. Actual DWT/TIM2 timing and interaction with USB must be checked on the board.

CLI commands (when acquisition is stopped):

```text
acq capture continuous|alert|single
acq alert report
spi verify off|diagnostic|reject|show|report
```

`off` reads VSHUNT once. `diagnostic` performs a second DMA read and keeps the first RAW value for CSF4, even on a mismatch; a failed second read is reported separately. In continuous mode the two reads can straddle an ADC conversion, so disagreement is **not** proof of an SPI error. `reject` is accepted only with `acq capture single`; after an ALERT-confirmed triggered conversion, a 20-bit code mismatch or failed read omits that slot. Slot numbering is preserved, so the existing CSF4 sequence-gap handling shows the omission in CSV/GUI. The two complete 24-bit words, signed codes, command/data RX bytes, slot, ALERT/read times and outcome of the first 16 differing or failed pairs are kept in RAM. `VERIFY_RAW_MISMATCH` also counts differences confined to the four reserved low bits. Status codes in `spi verify report`: 1 match, 2 code mismatch, 3 first read error, 4 second read error, 5 stopped during pair, 6 low-bit-only mismatch. `spi repeat` and `spi verify` cannot be active together. `spi verify report` and `acq alert report` are queried after Stop; no USB output is performed in EXTI or DMA callbacks.

`ALERT_IRQ_COUNT` counts observed low-going edges, `ALERT_RATE_HZ` and `TIM3_RATE_HZ` compare their rates, `ALERT_MIN/MAX_PERIOD_US` are TIM2 edge intervals, and `ALERT_SPURIOUS` counts high-level or out-of-state events. `ALERT_MISSED` / `TRIGGER_TIMEOUTS` count single-shot triggers without an ALERT before timeout. `ALERT_BUSY_SLOTS` counts timer ticks while a shot is pending, waiting, or being read. `ALERT_TO_READ_US` is the maximum measured edge-to-first VSHUNT-start delay; `TRIGGER_TO_ALERT_US` is the maximum write-completion-to-edge delay. `ALERT_EVENTS_PER_SAMPLE_LAST/MAX` are observed edges between accepted samples. In continuous mode an active-low ALERT can coalesce conversion events between reads, so these edge counts cannot prove how many internal ADC conversions occurred. `MISSED_SAMPLE_SLOTS` retains the existing timer-slot semantics; DMA/SPI errors and verification mismatches have separate counters. CSF4 timestamps still mark receipt of the **first** VSHUNT value, including when a second verification read follows.

Board check at SPI 6 MHz, one stage at a time (close PuTTY before opening COM in Python):

```text
info
spi config
acq capture continuous
spi verify off
acq profile set balanced
acq test 5
acq capture alert
acq test 5
acq alert report
acq capture single
spi verify off
acq test 5
acq alert report
spi verify diagnostic
acq test 5
spi verify report
acq stop
measure
regs
```

First confirm the board's 3.3 V ALERT pull-up and falling edges with a scope/logic analyzer. Compare baseline and ALERT-monitor counts and slot losses. Then compare single-shot `TRIGGER_TO_ALERT_US`, `ALERT_BUSY_SLOTS`, rate, `VERIFY_*`, SPI/DMA errors and timestamps; expect possible rate loss from the budget above. For streaming, set BALANCED + single-shot + diagnostic in PuTTY, close it, then run `python tools/stream_capture.py --port COM18 --profile balanced --acq-mode dma --seconds 5 --output recordings/single_alert_5s.csv`; confirm `ACQ_CAPTURE_MODE: SINGLE_SHOT`, END, CRC, slot gaps, and return to `measure` after Stop. Repeat FAST only after BALANCED behavior is understood. Enable `spi verify reject` only after confirming reliable ALERT timing and pair semantics; mismatches should create visible slot gaps without replacement values. Finally restore `spi verify off` and `acq capture continuous`. No hardware result is claimed here.

For the FW 0.4.4-debug USB CDC `Write timeout` observed while ending a 60-second BALANCED capture, see [stream stop diagnostics and board checks](STREAM_STOP_DEBUG.md). The capture tool now reports the failing command and stage with a traceback, preserves partial CSV/metadata, drains CSF4 while sending `stream stop`, and explicitly marks sessions without END as incomplete. The original timeout stage remains unknown until the new diagnostic run.

For the GUI STOP → Disconnect → Reconnect investigation, see the [USB lifecycle P0 guide](USB_LIFECYCLE_P0.md). The GUI writes a persistent `logs/current_sensor_lifecycle_*.log` with serial stages and watchdog stack dumps. Firmware `stream status` includes cumulative CDC RX/OUT, control line, STOP and END counters. Hardware confirmation is pending.

## Build and test

Windows desktop **CurrentSensor GUI v0.1.3.1** (PySide6, RAW scope, profiles,
recording, calibration controls, diagnostics): [launch and validation guide](GUI.md).
Automatic RAW history, save/freeze and cursor interval statistics: [v0.1.1 guide](GUI_0.1.1.md), [measured performance](GUI_0.1.1_BENCHMARK.md).
Candidate anomaly diagnostics, profile provenance, separate STM32 trace and processed interval statistics: [v0.1.2 guide](GUI_0.1.2.md), [benchmark](GUI_0.1.2_BENCHMARK.md).
Segmented CSF4 long-term recording with a sparse index and crash recovery: [v0.1.3 guide](GUI_0.1.3.md), [host benchmarks](GUI_0.1.3_BENCHMARK.md). Arm “Длительная запись CSF4” before starting acquisition; plain CSF4 is the default, optional zlib is experimental. GUI v0.1.3 has not been tested with real hardware.
GUI v0.1.3.1 fixes the long-recording timer after Stop: the archive status shows acquisition duration from unwrapped RAW timestamps, while archive wall time and finalization are separate and stop advancing after completion. Existing v0.1.3 archives remain readable. See [timer semantics and verification](GUI_0.1.3.1.md). This GUI fix has host-test coverage but has not been tested with hardware.
After a long recording, `python -m tools.verify_archive_013 recordings/archive_NAME/session.json` audits all closed segment checksums without changing the archive.
Run `python -m current_sensor --demo` to try it without hardware after installing
`requirements.txt`. GUI development does not change the STM32 firmware or CSF4 wire format.

Use the STM32 GNU Arm toolchain:

```sh
make -j4
```

The firmware is in `build/CurrentSensor_FW.{elf,hex,bin}`. Host-side C tests use CMake and a native compiler:

```sh
cmake -S tests -B tests/build
cmake --build tests/build --config Release
ctest --test-dir tests/build --build-config Release --output-on-failure
python -m unittest discover -s tools -p "test_*.py"
```

Connect a terminal to the USB CDC port and send ASCII commands terminated by CR, LF, or CRLF:

| Command | Action |
| --- | --- |
| `help` | List commands |
| `ping` | Return `pong` |
| `info` | Show firmware, MCU, USB, and SPI information |
| `ina` | Read manufacturer and device IDs |
| `regs` | Read CONFIG, ADC_CONFIG, and DIAG_ALRT |
| `measure` | Read one set of VSHUNT, VBUS, and DIETEMP values |
| `spi config` | Show PCLK2, SPI divider, actual SCLK and ownership conflicts |
| `spi speed 6000000\|3000000\|1500000` | Select SPI speed in RAM while acquisition and DMA are stopped |
| `spi repeat show` | Show bounded diagnostic repeat-read results after acquisition |
| `spi repeat threshold_ma N` | Set the 1–32767 mA threshold for repeat diagnostics; default 50 mA |
| `spi repeat enable 0\|1` | Disable/enable repeat diagnostics in DMA mode on non-FAST profiles |
| `cal show` | Show active software calibration |
| `cal set offset_ua <signed_integer>` | Set offset from -100000 to +100000 uA in RAM |
| `cal set gain_ppm <unsigned_integer>` | Set gain from 900000 to 1100000 ppm in RAM |
| `cal enable <0|1>` | Disable or enable correction in RAM |
| `cal save` | Append the current parameters to Flash |
| `cal load` | Reload the newest valid Flash record, or factory defaults if none exists |
| `cal reset` | Restore factory values in RAM; use `cal save` to make the reset persistent |
| `acq start` | Start timer-driven VSHUNT acquisition until stopped |
| `acq stop` | Stop acquisition and restore the previous ADC_CONFIG |
| `acq status` | Show sample rate, counts, timing violations, and errors |
| `acq test 1` | Acquire for one second and report actual independent samples |
| `acq test 5` | Run a five-second acquisition and then report statistics |
| `acq config` | Read CONFIG and ADC_CONFIG and show timer and conversion settings |
| `acq mode dma` | Select SPI1 DMA for the next acquisition; this is the power-on default |
| `acq mode poll` | Select the foreground SPI polling mode for comparison |
| `acq profile list` | List stable profile IDs and all conversion/timer parameters |
| `acq profile show` | Show the selected profile, acquisition mode and firmware version |
| `acq profile set <name>` | Select fast, balanced, precision or low_noise while stopped; names are case-insensitive |
| `acq probe` | With acquisition stopped, trigger one shunt conversion and inspect CNVRF after 200 us |
| `acq probe <us>` | Repeat with a fresh trigger and a 1–1000 us wait; suggested values: 50, 75, 100, 150, 200, 1000 |
| `stream start` | Start acquisition and switch output to CSF4 v1 binary frames after a text acknowledgement |
| `stream stop` | Stop acquisition, drain queued data, send an END frame, and return to text mode |
| `stream status` | Show transport counters in text mode; request a binary STATS frame while streaming |
| `stream config` | Show protocol settings in text mode; request a binary CONFIG frame while streaming |
| `stream stats` | Show distribution summary in text mode; request a binary STATS frame while streaming |
| `stream timing` | After stopping, show measured build, USB submit, USB IRQ, RX and forwarding durations |
| `stream anomaly` | After stopping, show a bounded 24-bit VSHUNT trace around the first threshold crossing |
| `stream anomaly set 50` | Set the trace threshold to 50 mA before streaming |
| `stream anomaly limit_ma N` | Set the trace magnitude threshold (0..32767 mA) before streaming; default 10000 mA |

`cal show` after a first boot with an erased storage sector:

```text
CAL:
OFFSET_UA: 0
GAIN_PPM: 1000000
ENABLED: 0
SOURCE: DEFAULT
DIRTY: 0
STATUS: OK
```

`SOURCE` describes the last operation that established the active parameters: `DEFAULT` at factory startup, after `cal reset`, or after `cal load` with no valid record; `FLASH` after loading or saving a valid record; `RAM` after any `cal set` or `cal enable`, even if the value is unchanged. `DIRTY=1` means the active parameters differ from the most recently saved Flash values. When Flash has no valid record, the comparison is against factory defaults. Thus `SOURCE: DEFAULT` can have `DIRTY: 1` after `cal reset` if Flash contains other values. A successful `cal save` or `cal load` clears `DIRTY`.

Valid state-changing commands return `STATUS: OK`. Invalid input returns `ERR: BAD_NUMBER`, `ERR: NUMBER_OVERFLOW`, `ERR: OUT_OF_RANGE`, or `ERR: UNKNOWN_COMMAND`, without partially applying a value. Storage failures return `ERR: STORAGE_UNAVAILABLE`, `ERR: STORAGE_FULL`, or `ERR: STORAGE_ERROR`. `cal save` during acquisition returns `ERR: BUSY_STREAMING`. Commands that require INA229 SPI access, including `measure`, `regs`, and `ina`, return `ERR: ACQ_ACTIVE` until acquisition stops. The command parser accepts exact command spelling and decimal integers; extra characters and trailing spaces are rejected. An overlong input line returns `ERR: LINE_TOO_LONG`.

## Measurement and correction

`measure` prints the full 24-bit `VSHUNT_RAW` and `VBUS_RAW`, signed 20-bit `VSHUNT_CODE`, 16-bit `DIETEMP_RAW`, `VSHUNT_mV`, original `CURRENT_mA`, corrected `CURRENT_CAL_mA`, `CAL_ENABLED`, `VBUS_V`, `DIETEMP_C`, and `STATUS`. The shunt is 0.005 Ohm. The firmware reads `CONFIG.ADCRANGE` for every measurement and uses these equations:

```text
VSHUNT_mV = VSHUNT_CODE * (ADCRANGE ? 0.000078125 : 0.0003125)
I_raw_nA = VSHUNT_CODE * (ADCRANGE ? 15625 : 62500)
I_cal_nA = enabled ? round_away_from_zero((I_raw_nA - offset_ua * 1000) * gain_ppm / 1000000) : I_raw_nA
VBUS_V = (VBUS_RAW >> 4) * 0.0001953125
DIETEMP_C = signed16(DIETEMP_RAW) * 0.0078125
```

The integer calculation rounds to the nearest nA, with half values away from zero, and checks for overflow. `CURRENT_mA` remains the original uncorrected current. With `enabled=0`, `CURRENT_CAL_mA` exactly equals `CURRENT_mA`. The displayed current has six decimal places in mA. The command only reads already converted registers; successive register reads are not an atomic ADC snapshot. An SPI error prints `STATUS: SPI_ERROR` or `STATUS: SPI_TIMEOUT` without measurement values. The INA229 `CURRENT` and `SHUNT_CAL` registers are not used for this correction. The INA229 register scales are from the [INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf).

## Measurement profiles in FW 0.4.3

The single firmware table is `App/Src/measurement_profile.c`. Profile IDs and names are stable. Frequencies are STM32 reading-slot targets; INA229 conversion timing remains independent.

| ID | PROFILE | SAMPLE_RATE_HZ | SAMPLE_PERIOD_US | VSHCT_US | AVG count | ADC_CONFIG | TIM3 PSC / ARR |
| ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| 0 | FAST | 10000 | 100 | 50 | 1 | 0xAA28 | 95 / 99 |
| 1 | BALANCED | 5000 | 200 | 150 | 1 | 0xAAA8 | 95 / 199 |
| 2 | PRECISION | 2500 | 400 | 280 | 1 | 0xAAE8 | 95 / 399 |
| 3 | LOW_NOISE | 1000 | 1000 | 540 | 1 | 0xAB28 | 95 / 999 |

These settings follow the [TI INA229 datasheet, sections 7.3.4 and 7.6.1.2](https://www.ti.com/lit/ds/symlink/ina229.pdf): MODE bits 15:12 are A (continuous shunt only), VSHCT bits 8:6 are respectively 0, 2, 3, 4, and AVG bits 2:0 are zero (one conversion). Inactive VBUSCT/VTCT fields retain their previous values. `CONFIG.ADCRANGE=0` is required and checked on start. Hardware current-calibration registers are untouched.

`acq profile list` returns one line per ID: `PROFILE_<id>: NAME,rate_hz,period_us,vshct_us,averaging_count,adc_config_hex`, followed by `PROFILE_COUNT: 4` and `STATUS: OK`. For example, `PROFILE_1: BALANCED,5000,200,150,1,0xAAA8`. Show/set responses use stable individual fields:

```text
PROFILE_ID: 1
PROFILE: BALANCED
SAMPLE_RATE_HZ: 5000
SAMPLE_PERIOD_US: 200
PROFILE_VSHCT_US: 150
PROFILE_AVG: 1
PROFILE_ADC_CONFIG: 0xAAA8
VSHCT_US: 150
AVG: 1
ADC_CONFIG: 0xAAA8
ADCRANGE: 0
ACQ_MODE: DMA
FW: 0.4.3
STATUS: OK
```

An unknown or extra-suffixed name returns `ERR: INVALID_PROFILE`. Switching during acquisition returns `ERR: BUSY_STREAMING`. During binary streaming it produces CSF4 ERROR code 1, including while queued samples are draining. Switching with a pending configuration restore or unfinished DMA returns `ERR: BUSY`. Invalid arguments never partially change a profile. Profile and POLL/DMA selections are RAM-only; after reset they return to FAST/DMA. They do not write Flash or alter saved calibration.

Selection changes the stopped TIM3 only. Its clock is checked for an exact 1 MHz prescaler; at the current 96 MHz timer clock PSC=95 and ARR=period_us-1. The update interrupt is disabled, PSC/ARR loaded by UG, CNT reset, and peripheral/NVIC pending update flags cleared before the next start. TIM2 is never reset or reconfigured by a profile switch. Active DMA and SPI BSY prevent selection.

At start, the controller saves the diagnostic ADC_CONFIG, writes the selected value once, reads it back and requires an exact match. Writing MODE restarts the converter and clears the previous CNVRF. Before TIM3 starts, foreground startup waits `2 * VSHCT_US + 100 us`, then reads DIAG_ALRT once, requires CNVRF=1, and discards this startup flag. Every recorded slot subsequently checks CNVRF again. This preparation uses interrupts normally, has a 10 ms guard if TIM2 fails to progress, and records no samples. It is not part of the IRQ/DMA measurement path. A nonzero preconfigured CONVDLY can cause `ADC_STARTUP_TIMEOUT`; firmware does not silently change CONFIG.

`STARTUP_US` measures preparation from the ADC_CONFIG write stage to timer arming. `TIME_US`, binary `ACQ_ELAPSED_US`, and text `SESSION_DURATION_US` measure the sampling interval after this preparation. `TOTAL_SESSION_US` adds startup and sampling time; binary `STREAM_ELAPSED_US` also includes USB drain time but excludes startup. All three durations are retained in metadata. Slot numbering and session counters restart for each successful start. Startup failures return `SPI_ERROR`, `ADC_CONFIG_READBACK`, `ADC_STARTUP_TIMEOUT` or `TIMER_ERROR` and attempt to restore the saved diagnostic ADC_CONFIG. If restoration fails, selection/start remain blocked until `acq stop` successfully retries it.

Stopping preserves the existing DMA stop/abort sequence and restores the exact saved diagnostic configuration (normally 0xFB68). `measure` then reads VSHUNT, VBUS and DIETEMP as before. `acq probe [delay_us]` still performs its separate single-shot test at 0x2A28.

`acq config` shows the actual diagnostic registers plus the selected profile in `PROFILE_*`, `SAMPLE_RATE_HZ` and `SAMPLE_PERIOD_US`; its historical `FAST_*` fields now describe the selected acquisition profile. `stream config` describes the next/active stream profile. `acq status` describes the last/current acquisition, and `stream status` retains the last stream profile even after a later selection or standalone test. `ADC_CONFIG_READBACK` in stream status is the verified acquisition setting, not a fresh SPI read after restore.

## Acquisition and counter semantics

TIM2 provides 1 us read-completion timestamps. TIM3 supplies the selected reading period. In DMA mode, TIM3 starts DIAG_ALRT, DMA completion checks CNVRF, and a ready flag starts VSHUNT DMA. In polling mode the foreground performs the same register sequence. CNVRF=0 consumes a slot without reusing old data. The ADC clock is not synchronized to TIM3; intentionally unread intermediate internal conversions have no STM32 slot and cannot be counted with the one-bit CNVRF flag. Identical numerical codes are valid distinct measurements.

The existing freshness guard is `VSHCT_US + 10 us` (60 us in FAST): if the previous VSHUNT receipt is too close to the next tick, the ready slot is skipped rather than risking reuse of an already-read result. Guard skips are visible. The 1024-entry acquisition ring (1023 usable) stores full raw values, timestamps, slot indices and command-byte diagnostics. Statistics consume and free entries; `acq test` drains remaining entries before reporting. Integer device statistics cover up to one million consumed samples; host statistics cover the full capture.

| Field | Meaning |
| --- | --- |
| TIMER_TICKS | Observed TIM3 callbacks |
| CNVRF_READS | DIAG_ALRT attempts in measurement slots; startup read is excluded |
| CNVRF_READY / CNVRF_NOT_READY | Successful slot DIAG reads split by CNVRF |
| VSHUNT_READS | Read attempts only after CNVRF=1 and the freshness guard |
| SAMPLES / ADC_SAMPLES | Successful fresh reads, including later buffer losses |
| MISSED_TIMER_SLOTS | Skipped foreground slots plus inferred coalesced timer events |
| MISSED_SAMPLE_SLOTS / LOST_SAMPLE_SLOTS | All lost slots: timer/foreground losses, CNVRF=0, freshness guard, SPI/DMA errors or busy slots |
| FRESHNESS_SKIPS | Ready slots rejected by the profile-dependent freshness guard |
| BUFFER_OVERFLOWS / ACQ_BUFFER_OVERFLOWS | Reads dropped because the acquisition ring was full; separate from slot misses |
| TIMER_PERIOD_VIOLATIONS | Callback interval outside selected period +/- max(5 us, period/20) |
| RATE_HZ | Fresh reads divided by actual TIM2 acquisition duration |
| MIN/MAX_SAMPLE_PERIOD_US | Intervals between actual MCU VSHUNT receipts, not internal ADC timestamps |
| MAX_SERVICE_US | Maximum slot-to-VSHUNT receipt duration |
| MAX_SPI_TRANSACTION_US | Maximum acquisition SPI transaction duration |
| MAX_READ_LATENCY_US | Tick-to-start delay; MAX_TRIGGER_LATENCY_US is its legacy alias |
| INA_INTERMEDIATE_CONVERSIONS | UNKNOWN: decimated internal ADC results are not measurable losses |

Timer loss estimates and slot numbering use the selected period. `acq test 1/5` retains its existing 1% count/miss tolerance for `STATUS: OK`, with zero SPI, overflow and timer-period errors; hardware acceptance below requires **zero** losses. Neither a host test nor that tolerance proves the requested sample rate on hardware.

## CSF4 v1 binary stream

INA229 and TIM3 use the selected profile. Acquisition uses DMA by default, with polling available for comparison. A separate 2048-slot transport ring (2047 usable, about 205 ms at 10 kHz) receives signed 20-bit VSHUNT codes, slot numbers, and TIM2 read timestamps from the acquisition ring. The USB sender packs at most 32 samples per frame. USB transfer, CRC calculation, and command parsing occur in the foreground after `Acq_Process()`. A pending frame buffer is never rewritten until the CDC transfer-complete callback. `USB_BUSY` leaves the frame pending and is retried at most once per millisecond; both rings have finite capacity. A disconnected USB port stops acquisition in the foreground and returns to text mode on reconnection. A stop with an indefinitely busy link times out after 500 ms and reports a transport error when possible.

The USB FS IRQ has priority 6 and TIM3 priority 5, so its short timestamp callback can preempt the USB IRQ. Neither packet construction nor USB transmission disables interrupts. The foreground acquisition SPI transactions can still be preempted by USB interrupts; `MAX_SERVICE_US` includes that elapsed time.

The ASCII command `stream start` is acknowledged by `STREAM: BINARY V1\r\n`. This line finishes before the first binary frame is sent. During binary mode, ASCII input accepts `stream stop`, `stream status`, `stream stats`, and `stream config`; responses are binary END, STATS, or CONFIG frames. `cal save` and `acq profile set ...` receive a binary ERROR frame with code 1 (`BUSY_STREAMING`); other commands receive ERROR code 2 (`BAD_COMMAND`), and an RX ring overflow produces code 3 (`RX_OVERFLOW`). No ASCII output is sent between data frames. After `stream stop`, queued samples are drained, an END frame is transmitted and completed, then `STREAM: TEXT\r\nSTATUS: OK\r\n` restores the ASCII response boundary. If `ADC_CONFIG` restore fails, the final line reports `ERR: ADC_CONFIG_RESTORE`. A failed or disconnected USB link may prevent END delivery; the host must treat a missing END as an incomplete capture. `acq test 5` and all old commands remain available in text mode.

All multi-byte values are little-endian. CRC32 is IEEE reflected (`zlib.crc32`), initialized to all ones, with final XOR all ones. It covers every byte before the four-byte CRC. The maximum data frame is 228 bytes. At 10,000 samples/s, full 32-sample frames require about 71,250 bytes/s over CDC and arrive about every 3.2 ms. Each frame has a 32-byte header:

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | 4 bytes | Magic ASCII `CSF4` |
| 4 | u8 | Protocol version = 1 |
| 5 | u8 | Type: 1 DATA, 2 STATS, 3 END, 4 CONFIG, 5 ERROR |
| 6 | u16 | Header length = 32 |
| 8 | u16 | Total frame length including CRC |
| 10 | u16 | Sample count (0 for control frames, 1–32 for DATA) |
| 12 | u32 | Frame sequence, starts at 0 and increments for every transmitted frame |
| 16 | u32 | First TIM3 slot index, zero-based |
| 20 | u32 | TIM2 timestamp in us of first VSHUNT read completion |
| 24 | u16 | Nominal slot period in us: 100, 200, 400 or 1000 for the selected profile |
| 26 | u16 | Flags = 0 |
| 28 | u32 | Number of slot indices between this and the previous transmitted DATA frame; for the first DATA frame, equals its first slot index |

DATA payload: `sample_count` signed int32 codes, followed by `sample_count` unsigned 16-bit timestamp offsets in us from the first read timestamp. The final 4 bytes contain CRC32. Thus every received sample has its own TIM2 read-completion timestamp. A frame contains consecutive slot indices only; any missing acquisition slot or dropped transport sample starts a new frame, and the header gap plus slot indices expose the discontinuity. Intermediate INA229 conversions intentionally not read by the selected STM32 schedule have no slot index and are not considered lost samples. Code-to-current scale for ADCRANGE=0 and the 5 mOhm shunt is `current_mA = code / 16`; the stream contains uncalibrated codes. Calibration parameters and Flash sector 7 are untouched.

STATS and END payloads are 19 consecutive u32 fields in this order: `ADC_SAMPLES`, `USB_SAMPLES_SENT`, `FRAMES_SENT`, `FRAMES_DROPPED`, `USB_BUSY_COUNT`, `TX_BUFFER_OVERFLOWS`, `ACQ_BUFFER_OVERFLOWS`, `LOST_SAMPLE_SLOTS`, `SPI_ERRORS`, `MAX_SERVICE_US`, `MIN_SAMPLE_PERIOD_US`, `MAX_SAMPLE_PERIOD_US`, `EFFECTIVE_STREAM_RATE_HZ`, `BUFFER_DEPTH`, `MAX_BUFFER_DEPTH`, `MAX_BUFFER_AGE_US`, `ACQ_ELAPSED_US`, `STREAM_ELAPSED_US`, `MAX_TRANSPORT_POLL_US`. `ADC_SAMPLES` counts fresh ADC reads; `USB_SAMPLES_SENT` counts DATA samples whose CDC transfer completed on the device, not samples written to the host file. `LOST_SAMPLE_SLOTS` is acquisition slot loss. `ACQ_BUFFER_OVERFLOWS` is loss before forwarding to transport; `TX_BUFFER_OVERFLOWS` is rejection by the transport ring. `FRAMES_DROPPED` counts assembled frames discarded on transport abort. `EFFECTIVE_STREAM_RATE_HZ` divides completed USB samples by elapsed stream time, including drain time after acquisition stops. `MAX_TRANSPORT_POLL_US` measures the longest foreground transport call including interrupts that preempted it; compare it with `MAX_SERVICE_US` and the sample period bounds. STATS is a live snapshot and END is the final snapshot after queued data frames; `FRAMES_SENT` counts DATA frames only. CONFIG payload contains u16 values for session ADC_CONFIG, session period in us, frame sample capacity (32), sample code size (4 bytes), transport ring capacity (2048), acquisition ring capacity (1024), then a zero u32. ERROR payload is one u32 code.

For a first host check, install [pySerial](https://pyserial.readthedocs.io/en/latest/pyserial_api.html) and run the CLI without PuTTY holding the COM port:

```sh
python -m pip install pyserial
python tools/stream_capture.py --port COM7 --seconds 5 --acq-mode poll --output poll_5s.csv
python tools/stream_capture.py --port COM7 --seconds 30 --acq-mode dma --output dma_30s.csv --status-interval 1
python tools/stream_capture.py --port COM7 --seconds 60 --acq-mode dma --output dma_60s.csv
```

The tool checks boundaries, length, version, CRC, frame sequence, signed 20-bit code range, slot gaps and TIM2 read timestamp intervals. It writes `slot,tim2_read_us,vshunt_code,sample_period_us` CSV (the original first three columns remain unchanged), an exact-code histogram (`vshunt_code,current_mA,count,fraction`), a separate out-of-range CSV, and a JSON metadata file containing experiment settings and final STM32 counters. Filenames are unique by default; an existing explicit path receives a numbered suffix. `--range-min-ma` and `--range-max-ma` default to -50 and 10000 mA, so small negative readings near zero remain in clean statistics. For a zero-current anomaly test, set both limits near zero, for example `--range-min-ma -50 --range-max-ma 50`. These thresholds flag suspicious measurements for separate statistics; they do not label the raw data corrupt. `--period-tolerance-us` defaults to 5 us for consecutive slots. The tool prints full and in-range-only statistics, missed-slot percentage, ten largest-magnitude codes, and separate transport, acquisition, period and suspicious-value statuses. Suspicious values alone do not fail the capture integrity check. It does not alter the raw CSV. CSF4 v1 carries the signed 20-bit code, not the low four bits of the physical 24-bit register; the on-device `stream anomaly` trace preserves the true 24-bit raw value. Run `acq test 5` before and after stream trials. The Python CLI can request binary status every second with `--status-interval 1`.

`--acq-mode poll` or `--acq-mode dma` sends the corresponding ASCII command before `stream start`, requires `STATUS: OK`, and checks `ACQ_MODE_SELECTED` in `acq config`. After END and the return to text mode, the CLI automatically reads `stream status`, `stream timing`, and `stream anomaly`. It prints the actual mode reported by `stream status`, saves all three reports and the requested, pre-stream and actual modes in metadata JSON, and checks that final counters match END. A requested mode that differs from the reported mode fails mode verification. In FW 0.4.3 the CLI defaults to DMA; `--acq-mode poll` explicitly selects polling. `--profile` is optional and leaves the selected profile unchanged when omitted. The owner now reports the FW 0.4.2 DMA result as hardware-confirmed; every new capture independently records and verifies its actual mode.

## Python profile control and distribution analysis

The CLI synchronizes with `ping`, selects and verifies POLL/DMA, optionally sends `acq profile set <name>`, and always queries `acq profile show` before starting. A rejected or mismatched selection aborts before `stream start`. After the binary acknowledgement it requests a CONFIG frame. It compares the period in every CSF4 v1 header, CONFIG parameters, the selected profile, and the final stream snapshot. A mismatch sets `profile_verification_error`; CRC damage, transport loss, acquisition loss, timing violations and suspicious values remain separate statuses. The decoder continues to accept the original 100 us CSF4 v1 frames. The capture workflow requires FW 0.4.3 profile commands; there is no silent fallback to assumed FAST parameters on an older firmware.

The existing v1 header already carries a u16 period at offset 24, so no wire-format or protocol-version change is needed. CONFIG carries the session period and ADC_CONFIG. DATA still carries each original TIM2 timestamp as the first timestamp plus actual offsets. Time is never reconstructed as `slot * 100`. A period change inside a session is a format error. Slot gaps and sequence checks are independent of nominal frequency.

For future GUI reuse, `get_available_profiles(port)`, `get_active_profile(port)`, `set_profile(port, name)`, `start_stream(port)`, `stop_stream(port)` and `get_stream_status(port)` share the text/binary helpers in `tools/stream_capture.py`. These functions take an already-open serial object; the caller owns its lifetime. `capture()` opens/closes the COM port with a context manager. Profile names are CLI choices, but their timing and ADC parameters are obtained from firmware rather than copied into Python.

With FW 0.4.4-debug, `stream_capture.py` uses the post-capture text `ACQ_ELAPSED_US64` and `STREAM_ELAPSED_US64` for durations and acquisition rate, after checking their low 32 bits against CSF4 END. Metadata records each duration's source. Older firmware without these fields retains the 32-bit END duration, which wraps after about 71.6 minutes.

JSON includes requested/actual profile, firmware/protocol version, ADC_CONFIG, ADCRANGE, VSHCT, averaging count, nominal rate/period, requested/actual POLL/DMA mode, actual sample count, acquisition duration and measured acquisition rate. Full END counters and the automatic post-capture `stream status`, `stream timing`, `stream anomaly` reports are retained, including startup/total duration and SPI/DMA/transport losses. All three diagnostic reports must refer to the same stream session.

Statistics use the full raw data: MIN/MAX/MEAN/population STDDEV, distinct-code count, the ten most frequent codes and their fractions, exact-code histogram, a frequency table of differences between sorted distinct codes, and a distribution of signed code changes between adjacent **contiguous slots**. Repeated identical adjacent codes are counted as valid samples. Pairs separated by missing slots are excluded from adjacent-change statistics and counted separately. Drift is reported as raw mean current in 1-second windows of elapsed TIM2 time, unwrapped across the 32-bit timer rollover, plus the last-minus-first window mean. The histogram CSV now uses exact codes rather than 1 mA bins. No samples are filtered or replaced in raw CSV; `clean` is only a separate summary within the user-selected current range. Small negative currents around zero are valid. Out-of-range counts refer to the explicit range, not proven data corruption.

Observed code spacing, repeat rate and standard deviation are descriptive statistics. They are not a measured ENOB specification, and lower standard deviation alone does not establish better absolute accuracy. Distinct-code counts also depend on sample count and observation duration.

## FW 0.4.3 board verification

Program `build/CurrentSensor_FW.elf` through STM32CubeProgrammer with sector 7 preserved as described below. These are planned tests, not completed FW 0.4.3 hardware validation.

1. In PuTTY run `info`, `cal show`, `measure`, `regs`, `acq profile list`, `acq profile show`. Expect FW 0.4.3, FAST/DMA after reset, the same saved calibration, and diagnostic ADC_CONFIG=0xFB68.
2. For each profile, run `acq profile set <name>`, `acq config`, `acq test 1`, `acq test 5`. Check the table's PSC/ARR, `PROFILE_ADC_CONFIG`, actual `FAST_ADC_CONFIG` in the test result, rate, startup duration, CNVRF readiness, zero SPI/DMA errors and all loss/overflow counters. After each test run `regs` and `measure`; ADC_CONFIG must be restored to 0xFB68.
3. Run `acq start`, then try `acq profile set balanced` and `cal save`; both must be rejected while active. Stop with `acq stop`, then select `acq profile set PrEcIsIoN` and confirm case-insensitive selection. Select an invalid name and verify the previous profile remains selected. Reset and confirm FAST/DMA with calibration retained.
4. With zero current, set `stream anomaly set 50` in PuTTY, close PuTTY, then run all four 60-second captures below. Do not keep another application attached to the COM port.

```powershell
python tools/stream_capture.py --port COM18 --acq-mode dma --profile fast --seconds 60 --range-min-ma -50 --range-max-ma 50 --output zero_fast.csv
python tools/stream_capture.py --port COM18 --acq-mode dma --profile balanced --seconds 60 --range-min-ma -50 --range-max-ma 50 --output zero_balanced.csv
python tools/stream_capture.py --port COM18 --acq-mode dma --profile precision --seconds 60 --range-min-ma -50 --range-max-ma 50 --output zero_precision.csv
python tools/stream_capture.py --port COM18 --acq-mode dma --profile low_noise --seconds 60 --range-min-ma -50 --range-max-ma 50 --output zero_low_noise.csv
```

5. Apply the stable approximately 117 mA load. In PuTTY set `stream anomaly set 200`, then close it. Repeat the four commands with `--range-min-ma 50 --range-max-ma 200` and filenames prefixed `load117_`. These limits select an anomaly observation range; they do not change coefficients or ADC behavior.
6. For every session require matching requested/actual profile, `actual_acq_mode: DMA`, `profile_confirmed: true`, zero CRC/format/frame gaps, zero acquisition/USB buffer losses, zero SPI/DMA errors/timeouts, and no unexplained slot/timestamp gaps. Compare nominal rates 10000/5000/2500/1000 Hz and periods 100/200/400/1000 us with actual TIM2 statistics. Nominal counts over exactly 60 acquisition seconds are 600000/300000/150000/60000; host stop latency means the observed duration and count can be slightly longer. Use `ADC_SAMPLES / ACQ_ELAPSED_US` for the actual rate.
7. Inspect MAX_SERVICE_US, MAX_DMA_TRANSACTION_US, MAX_TIM3_LATENCY_US, period bounds, all three automatic post-capture reports, raw traces, noise, observed spacing, repeated-code fraction and drift. Compare each profile at the same load and capture duration. Reopen PuTTY for `measure`, `regs`, `cal show` and verify restore/calibration again. An additional `--status-interval 1` run checks control traffic under streaming. `--acq-mode poll` remains available for a direct comparison.

Only these board results can establish whether the new profiles achieve their rates without losses or improve effective resolution. The default remains FAST until those measurements justify a different choice.

## FW 0.4.1 streaming diagnosis (history)

The owner measured FW 0.4.0 on hardware: standalone `acq test 5` returned 49,990 samples, 9997 Hz, zero missed slots and 35 us maximum service time. With USB streaming, 5/30/60-second runs returned 44,180/272,129/546,923 ADC samples and 5,747/27,848/53,005 lost slots. Maximum service time rose to 123/130/130 us and maximum transport poll time to 115/121/115 us. Frame CRC, frame gaps, SPI errors and both buffer overflow counters stayed at zero. These results locate the loss before USB delivery, but the old `MAX_TRANSPORT_POLL_US` includes USB interrupt preemption and cannot identify the sole blocking operation.

FW 0.4.1 builds each DATA frame in short foreground stages: at most four codes/timestamps or 32 CRC bytes per `StreamTransport_Poll()` call. Acquisition is serviced before each stage. A frame is held unchanged from `CDC_Transmit_FS` acceptance until its transfer-complete callback. `USB_BUSY` leaves the frame pending and retries later; no USB send or frame construction runs in TIM3 IRQ. The RX parser processes at most 32 bytes per foreground call. The only global interrupt mask remains the short RX overflow index reset.

After a streaming run, `stream timing` reports maximum and average elapsed microseconds, call counts, and counts over 25 us for frame construction, USB submit, USB IRQ, RX processing and acquisition-to-transport forwarding. `MAX_TRANSPORT_POLL_US` is also shown. These elapsed timings include preemption and have 1 us TIM2 resolution. A large `MAX_USB_IRQ_US` or `MAX_USB_SUBMIT_US` together with slot loss would point toward USB scheduling; a large `MAX_BUILD_US` would point toward frame work. Compare with `MAX_SERVICE_US` and sample periods. These diagnostics are retained in FW 0.4.3; their new-profile results still require board measurements.

At stream start the bounded anomaly trace clears and watches raw current magnitude against its configured threshold. The first crossing stores up to four preceding samples, the crossing and four following samples, each with slot, TIM2 read timestamp, full 24-bit VSHUNT raw and decoded signed 20-bit code. `ANOMALY_EVENTS` counts all threshold crossings, but only the first has context. The trace neither changes nor discards data. Read it with `stream anomaly` after `stream stop`; set the threshold beforehand with `stream anomaly limit_ma 10000`. The observed FW 0.4.0 outliers at zero current (about -18.96 A, -27.15 A and +0.52 A) remain unexplained. Compare the raw trace with neighboring samples and host CSV before attributing them to SPI, USB, INA229 or the analog circuit.

FW 0.4.1 did not configure DMA streams; USB OTG FS used interrupt-driven transfers. Per [RM0383 DMA2 mapping](https://www.st.com/resource/en/reference_manual/dm00119316-stm32f411xce-advanced-armbased-32bit-mcus-stmicroelectronics.pdf), SPI1 RX can use DMA2 Stream0 or Stream2, Channel3, and SPI1 TX can use Stream3 or Stream5, Channel3. FW 0.4.2 uses Stream0 and Stream3; see the following section for the implementation and the owner-reported hardware history.

Board comparison: flash FW 0.4.1 while preserving Flash sector 7, confirm `info`, `measure`, `cal show`, then run `acq test 5`. With no other terminal holding the COM port, run the Python CLI for 5, 30 and 60 seconds (use `--range-min-ma -20` for a zero-current noise test). After each run reconnect with PuTTY and issue `stream timing`, `stream anomaly`, `stream status`, `measure`; repeat `acq test 5` after the last run. Record `MAX_BUILD_US`, `MAX_USB_IRQ_US`, `MAX_USB_SUBMIT_US`, `MAX_TRANSPORT_POLL_US`, `MAX_SERVICE_US`, `LOST_SAMPLE_SLOTS`, both buffer overflows, CRC errors, slot gaps, and period violations. Only board data can establish whether 10 kHz with zero acquisition and transport loss has been reached or whether SPI DMA is warranted.

## FW 0.4.2 DMA acquisition experiment

The owner reported that FW 0.4.1 still lost 80,418 of roughly 600,000 reading slots during a 60-second USB stream: 519,600 ADC samples, 8659 Hz effective stream rate, zero SPI errors, zero CRC errors, and zero dropped frames. Measured average/max foreground build time was 7/39 us (310,576 calls); USB submit 15/22 us (44,199 calls); USB IRQ 8/17 us (88,732 calls); forwarding 3/13 us (519,600 calls). Maximum acquisition service time was 74 us and transport poll 52 us. This supports moving the SPI start out of the foreground loop, but it does not prove DMA will reach 10 kHz.

Select `acq mode dma` or `acq mode poll` only while stopped. Both use the selected continuous-shunt profile (FAST was fixed at `ADC_CONFIG=0xAA28` in FW 0.4.2) and the same CSF4 v1 frame. DMA mode uses DMA2 Stream0/Channel3 for SPI1 RX and Stream3/Channel3 for SPI1 TX, as mapped by [RM0383](https://www.st.com/resource/en/reference_manual/dm00119316-stm32f411xce-advanced-armbased-32bit-mcus-stmicroelectronics.pdf). No other configured peripheral uses these streams. The DMA setup lives in the separate acquisition module and IRQ functions in CubeMX USER CODE blocks; reserve these streams if editing the `.ioc` later. TIM3 priority 5 launches a 3-byte DIAG_ALRT full-duplex transfer. DMA RX priority 4 handles completion; a ready CNVRF launches a 4-byte VSHUNT transfer. The received command byte is ignored for register decoding but retained in anomaly traces. CS remains low for each entire transfer; if SPI is still busy when RX DMA finishes, TIM2 compare channel 1 checks again after 2 us before raising CS. Neither DMA completion nor TIM3 performs USB transmission or text formatting. DMA timeouts are detected by TIM3 or the TIM2 settle check; stop disables DMA requests and waits for the peripheral to finish before restoring ADC_CONFIG. The earlier polling path remains selectable for a board comparison.

`acq status`, `acq test 1/5`, and text `stream status` report `DMA_STARTS` and `DMA_COMPLETIONS` per SPI transaction, `DMA_BUSY` for a slot or attempted launch while the previous transfer is active, `DMA_ERRORS` for transfer/start errors, `DMA_TIMEOUTS`, `MAX_DMA_TRANSACTION_US`, `MAX_TIM3_LATENCY_US` from the estimated TIM3 update (`TIM2 time - TIM3 counter`) to DMA start, and `MAX_ACQUISITION_CYCLE_US` from that tick to VSHUNT receipt. The TIM3 counter wraps every selected sample period; a delay longer than one full period makes this latency estimate ambiguous, so check `MISSED_TIMER_SLOTS` and `TIMER_PERIOD_VIOLATIONS` too. `MISSED_SAMPLE_SLOTS` counts unavailable CNVRF, busy slots, SPI failures and timer losses; `MISSED_TIMER_SLOTS` isolates timer/foreground schedule losses in polling mode plus hardware timer loss. TIM2 timestamps describe MCU receipt, not INA229 internal conversion time. The CSF4 v1 END frame retains its 19-field layout; obtain DMA counters from text `stream status` after stop. Python reads that report, checks its key counters against the END frame, and saves both in the JSON metadata.

`stream status`, `stream stats`, `stream timing`, and `stream anomaly` refer to the last completed stream after stop, even if a later `acq test` runs. The text reports include a monotonic RAM session ID and session type; the stored stream snapshot is replaced at the next stream start. `stream anomaly set 50` sets a 50 mA magnitude threshold for the next stream only while stopped, preserving the prior trace and its threshold until that stream begins. The trace prints all four received VSHUNT SPI bytes in DMA mode (the first command-phase byte is `??` in polling mode), the full 24-bit data word, signed 20-bit code, slot, TIM2 timestamp, and whether code bit `0x2000` is set. Bit `0x2000` in the code corresponds to raw bit `0x20000` after the four-bit shift. That bit is also naturally set in many negative two's-complement codes, so compare the entire raw word and neighbors. The trace never filters the stream.

For a board comparison, preserve Flash sector 7 when programming, verify `info`, `measure`, and `cal show`, then run `stream anomaly set 50`. Run `acq mode poll`, `acq test 5`, and Python captures lasting 5, 30 and 60 seconds. Save the JSON and CSV outputs and inspect `stream status`, `stream timing`, `stream anomaly` after each run. Repeat the same sequence with `acq mode dma`. Use identical load and host settings, including `--status-interval`, for both modes. The CLI now sends a newline and confirms `ping` before `stream start`, allowing PuTTY-to-Python switching without MCU reset; confirm this on the board. Compare actual ADC and USB sample counts, `LOST_SAMPLE_SLOTS`, `MISSED_TIMER_SLOTS`, DMA counts, `MAX_ACQUISITION_CYCLE_US`, timestamp intervals, CRC errors, frame gaps and anomalous raw bytes. Acceptance requires about 10,000 distinct ready conversions per second with zero measured slot, SPI/DMA, frame and transport losses under normal PC operation. The owner subsequently reported a successful FW 0.4.2 DMA run; the FW 0.4.3 profiles need their own tests.

## Flash storage and updates

The STM32F411RET6 has 512 KiB of Flash. The linker limits firmware to `0x08000000-0x0805FFFF` and reserves sector 7, `0x08060000-0x0807FFFF` (128 KiB), for calibration. The vector table and current firmware image are in the first sectors. Storage is enabled only when the device reports 512 KiB and the linker symbols match sector 7. See the [STM32F411 reference manual](https://www.st.com/resource/en/reference_manual/rm0383-stm32f411xce-advanced-armbased-32bit-mcus-stmicroelectronics.pdf).

`cal save` is the only firmware command that writes Flash. It appends a 36-byte record containing magic, format version, length, sequence, offset, gain, enabled, IEEE CRC32, and a final commit word. Loading scans all slots and selects the newest complete valid record. An interrupted save or corrupt CRC leaves the previous valid record usable; if none is valid, factory values are loaded. `cal load` and startup only read Flash. If an occupied slot has an unknown header, `cal save` returns `ERR: STORAGE_UNAVAILABLE` instead of writing alongside unrecognized data. There is no automatic sector erase: after 3640 record slots are used, `cal save` returns `ERR: STORAGE_FULL`, preserving the last valid record. Reclaiming the sector then requires a deliberate sector-7 erase, which deletes saved calibration.

For STM32CubeProgrammer DFU updates, program the `.elf` with **Full chip erase disabled** and leave **Skip flash memory erase before programming disabled**. Do not run `Erase all sectors` or select sector 7 for manual erase. Check the programmer log for the erased sector numbers and verify that sector 7 is absent. A full-chip erase removes saved calibration. Firmware updates performed with sector erase should leave sector 7 intact; verify this on the board with `cal show` after an update. The [STM32CubeProgrammer manual](https://www.st.com/resource/en/user_manual/dm00403500.pdf) describes these options.

Hardware testing through FW 0.4.2 DMA was reported by the owner. FW 0.4.3 has only build and host-test validation. Calibration sector 7 and the CSF4 v1 wire layout remain unchanged.

## FW 0.4.5-debug: USB CDC lifecycle diagnosis

`info` now reports `FW: 0.4.5-debug`, a compile-time `FW_BUILD`, and `USB_DIAG_REV: 1`. `make` recompiles `app.c` for each firmware build so the timestamp in `FW_BUILD` identifies the produced image. Check these three fields on the **board** before comparing results. A successful local build alone does not update the MCU. The ELF and BIN are `build/CurrentSensor_FW.elf` and `build/CurrentSensor_FW.bin`.

The code review found a recoverability defect: after `stream stop`, a CDC IN transfer can remain pending when the host closes COM before consuming the final text. `App_Process()` then waits for `tx_ready` and does not parse a new text command. FW 0.4.5-debug records CDC DTR falling in the USB callback and, in the foreground, stops any active acquisition and aborts the stale CDC IN transfer for that closed serial session. A normal END is counted only after its real CDC TX completion. CDC deinitialization no longer calls the TX-completion callback. Repeated STOP in one stream session is processed only once. This is a code-level failure path; its role in the reported COM18 incident still requires board verification.

After STOP and before disconnect, run `usb diag` in text mode. It reports cumulative `CDC_RX_CALLBACKS`, `CDC_RX_REARM`, `CDC_RX_REARM_ERRORS`, `STOP_RECEIVED`, `STOP_PROCESSED`, `END_QUEUED`, `END_TX_COMPLETED`, `USB_CONTROL_REQUESTS`, `USB_TX_BUSY`, `USB_IRQ_COUNT`, `USB_RESET_COUNT`, `SPI_OWNER_CONFLICTS`, `DMA_ABORT_ERRORS`, `CDC_TX_ABORTS`, and `CDC_TX_ABORT_ERRORS`. `STOP_RECEIVED` counts accepted ASCII STOP lines, including repeats; `STOP_PROCESSED` counts the first stop action per stream session. `END_QUEUED` means an END frame was built; `END_TX_COMPLETED` requires CDC completion. `DMA_ABORT_ERRORS` counts a DMA stop that did not become idle within its bounded wait. The command also shows current USB/CDC TX, RX-ring, acquisition, stream and DTR state. All counters persist in RAM until MCU reset.

`USB_TRACE_<index>: <TIM2_us>,<event>` lists the most recent 16 lifecycle events in RAM. Event IDs: 1 RX callback, 2 RX rearm error, 3 STOP parsed, 4 STOP processed, 5 END built, 6 END TX completed, 7 TX busy (logged at counts 1, 2, 4, ...), 8 USB reset, 9 CDC deinit, 10 DMA stop error, 11 DTR falling, 12 CDC IN aborted, 13 CDC IN abort error, 14 stream start. The timestamp is a wrapping TIM2 microsecond counter and may be zero during early USB startup. No trace data is written to Flash and no formatting occurs in USB/DMA IRQs. If COM becomes inaccessible, RAM trace can be inspected through a debugger before MCU reset.

For built-in STM32 DFU through STM32CubeProgrammer CLI, enter System Memory DFU mode, close programs using COM, and run from the repository root (adjust the installed CLI path if needed):

```powershell
& 'C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe' -c port=usb1 -w 'build\CurrentSensor_FW.elf' -v
```

The command uses the ELF load addresses and normal sector erase; it does not request `-e all` or `--skipErase`. The linked image ends below `0x08060000`; sector 7 (`0x08060000` to `0x0807FFFF`) remains reserved for calibration. Check the programmer log to ensure sector 7 was not erased, then boot the application and confirm `cal show` retains the FLASH parameters. Do not use full-chip erase.

Board regression: (1) connect and disconnect COM without measuring, then reconnect; (2) select FAST/DMA and run START, STOP, DISCONNECT, RECONNECT; repeat ten times; (3) run `stream_capture.py` for 1, 5, and 60 seconds and check END, `ping`, `info`, `usb diag`, and `stream status` after each; (4) repeat STOP during active DMA and while USB TX is busy, then test unplug during streaming. Compare `CDC_RX_CALLBACKS`/`STOP_RECEIVED` to distinguish USB OUT failure from command-parser delay, and `END_QUEUED`/`END_TX_COMPLETED` to distinguish queued END from delivered END. If the failure persists, preserve the GUI lifecycle log and RAM trace and compare against a separately archived FW 0.4.3 image. The repository has no committed FW 0.4.3 snapshot, so an exact source diff is unavailable here. These board tests have not been run for FW 0.4.5-debug.

## FW 0.4.5-debug.1: 64-bit CLI formatting and END-to-text diagnosis

The owner confirmed FW_BUILD `usbcdc-Nov 13 2026-16:22:41` on the board: a BALANCED DMA stream produced 5050 RAW samples, valid DATA/END frames and no CRC or SPI errors. The fixed END-to-text boundary and `ping` worked. `acq test 1` then showed `ACQ_ELAPSED_US64: lu` followed by shifted counters, while `stream status` never completed. The firmware links `newlib-nano` (`-specs=nano.specs`); this observed build does not correctly handle the `%llu`/`%lld` formats used in those reports. Because `stream status` places two 64-bit formats before `%s`, misreading the variadic arguments can corrupt the following string pointer and stop the foreground CLI. The later `stream timing` timeout and `stream anomaly` write timeout are consistent downstream symptoms. This causal explanation still needs confirmation with the new board image.

All firmware `%llu`/`%lld` conversions have been replaced with `Format64_Unsigned`/`Format64_Signed`, which produce decimal ASCII including full uint64 range and INT64_MIN without narrowing. `acq status/test`, `stream status/stats`, and `spi repeat show` retain their field names and meanings. Host tests cover 0, 1, 1000014, 2^32-1, 2^32, 5402197401, UINT64_MAX, INT64_MIN, and adjacent `TIMER_TICKS`, `SAMPLES`, `RATE_HZ`, `STATUS` fields.

USB diagnostic revision 2 preserves all revision-1 counters and adds `TEXT_RESPONSES_QUEUED`, `TEXT_RESPONSES_DONE`, `TEXT_TX_SUBMIT_ATTEMPTS`, `TEXT_TX_BUSY`, `TEXT_TX_MAX_WAIT_US`, `TEXT_FORMAT_ERRORS`, `TEXT_LAST_LENGTH`, and `TEXT_MAX_LENGTH`. The first count is a formatted or fixed response waiting for CDC; the second increments only in the actual CDC completion callback. The RAM trace adds event 15 TEXT mode entered, 16 text transfer completed, and 17 CLI line accepted. CLI formatting and logging stay outside IRQs. The text response uses one static 2048-byte buffer until CDC completion; a conservative source-level bound for `stream status`, including its profile prefix and firmware version, is under 2 KiB with all string values at most 20 bytes and decimal values at most 10 digits. The USB device stack divides a long transfer into full-speed packets and clears `TxState` only after the whole transfer, including any required zero-length packet. A failed format produces `ERR: INTERNAL` instead of a truncated report.

After CSF4 END, firmware queues exactly `STREAM: TEXT\r\nSTATUS: OK\r\n` on a successful stop. The capture CLI now stores the bytes it actually receives after END as `post_end_text_hex` and the first `ping` reply as `first_cli_reply_hex` in metadata JSON, and prints both in its console report. Expected hex values are `53545245414d3a20544558540d0a5354415455533a204f4b0d0a` and `706f6e670d0a`. The exact bytes on the board must be checked in a new capture.

Program the ELF with the DFU command above, keeping sector 7. Confirm `info` reports `FW: 0.4.5-debug.1`, a new `FW_BUILD`, and `USB_DIAG_REV: 2`; also confirm `cal show`. In PuTTY text mode run `info`, `acq profile set fast`, `acq test 1`, `acq status`, `usb diag`, `ping`; check that `ACQ_ELAPSED_US64` and all following counters remain plausible. Then run BALANCED DMA 1 s and 5 s, FAST DMA 1 s, and reconnect COM after each. Confirm END, complete `stream status`, `stream timing`, `stream anomaly`, no write timeout, and `TEXT_RESPONSES_DONE` increasing. Repeat GUI START/STOP/Disconnect/Reconnect. The owner subsequently confirmed USB CDC recovery in BALANCED/DMA 5 s and 60 s captures on FW 0.4.5-debug.1; the stream-duration mismatch is addressed in the next section.

## FW 0.4.5-debug.2: frozen END stream duration

The owner confirmed that FW 0.4.5-debug.1 restores the USB CDC lifecycle: BALANCED/DMA 5 s and 60 s captures received END and all three text diagnostics, with no CRC, frame, SPI/DMA, or USB losses, and COM reconnect worked. The capture tool correctly rejected a duration mismatch:

| Capture | CSF4 END `STREAM_ELAPSED_US` | Text `STREAM_ELAPSED_US64` | Text minus END |
| --- | ---: | ---: | ---: |
| BALANCED/DMA 5 s, session 10 | 5017895 | 5018562 | +667 us |
| BALANCED/DMA 60 s, session 11 | 60029604 | 60030062 | +458 us |

In both metadata files, END and text agree on `ADC_SAMPLES`, `USB_SAMPLES_SENT`, and the 64-bit acquisition duration; the text diagnostic reports identify the same session. The mismatch is specific to stream duration. `App_Process()` previously recalculated `stream_elapsed_us64` on every transport poll. CSF4 END serialized the low 32 bits when the END frame was **built**; the later poll acknowledging CDC completion updated the 64-bit application variable again. After END completion the value stopped changing, but it described a later instant than the binary END. The differences above are that interval, not TIM2 rollover errors.

The transport now stores the full 64-bit elapsed value at the same moment it builds END and retains it through USB busy retries and TX completion. On successful completion the application publishes that saved value for `stream status`. A missing END snapshot reports `ERR: END_SNAPSHOT`; the Python modulo-2^32 consistency check remains unchanged. A host regression test covers a duration of 5402197401 us, advances time while END is in flight, confirms that the transmitted END and saved 64-bit value stay aligned, and verifies that a new session clears the snapshot. The existing TIM2 read retries if the rollover ISR changes the high word during the read and accounts for a pending update flag; host tests cover those races. No ISR writes the END snapshot.

For the two archived captures, the expected **post-fix** text values for the same END values are exactly 5017895 and 60029604 us; these are predictions from the corrected code, not new board measurements. Confirm on FW 0.4.5-debug.2 by flashing the ELF without erasing sector 7, checking `info`/`FW_BUILD`, then running BALANCED/DMA 5 s and 60 s captures. In each new metadata JSON compare `device_counters.STREAM_ELAPSED_US` to `post_stream_status.STREAM_ELAPSED_US64 & 0xffffffff` and require equality with no `stream_duration` diagnostic error. Repeat with a session longer than 4294.967296 s to exercise TIM2 rollover; the full text duration must exceed 2^32 us while its low 32 bits exactly equal END. CSF4 v1, acquisition, calibration, and USB CDC lifecycle are unchanged.
# Проверка FW 0.5.0 около 100 мА

Операторский HIL-сценарий, результаты четырёх аппаратных записей и ограничения
оценки ±10 мА описаны в [ACCURACY_100MA.md](ACCURACY_100MA.md).
Инструмент: `python -m tools.hil.accuracy_suite`; каждый этап требует
подтверждения оператора. Исходные RAW сохраняются без исправления.

Побитовый разбор редких RAW-выбросов, результаты повторной нулевой записи
и план физической проверки SPI: [RAW_SPIKE_FORENSICS.md](RAW_SPIKE_FORENSICS.md).
Offline-воспроизведение: `python -m tools.hil.raw_forensics --include-comparison`.

# CurrentSensor GUI 0.6.0

Интерфейс измерения и анализа на существующей FW 0.5.0: RAW/Marked/Display Clean,
курсоры по реальным отсчётам, точная фоновая статистика, архивы и экспорт RAW24.
[Описание, проверки, аппаратный smoke-test и скриншоты](GUI_0.6.0.md).

```powershell
.\.venv\Scripts\python.exe -m current_sensor --port COM18
```

Подключение не запускает реальные измерения автоматически. Для проверенного
режима выбрать «Native DMA · 84 us · FW 0.5» и нажать «Измерять».

# CurrentSensor GUI 0.6.1

Компактные панели, регулируемый QSplitter, явные LIVE FOLLOW/MANUAL,
отдельные состояния измерения/записи и средний RAW за 100 мс без исключения SUSPECT.
[Скриншоты до/после, регрессия и аппаратная проверка](GUI_0.6.1.md).
FW остаётся 0.5.0; команда запуска: `python -m current_sensor --port COM18`.

# CurrentSensor GUI 0.6.2

Настройки без внутренней прокрутки: адаптивная панель или отдельное окно.
Overview Robust Y / Full RAW, переход к исходному RAW по SUSPECT-маркеру,
сравнение полного RAW и диагностической статистики без SUSPECT.
[Скриншоты всех разделов, проверки и ограничения](GUI_0.6.2.md).
FW остаётся 0.5.0. Запуск: `python -m current_sensor --port COM18`.

# CurrentSensor GUI 0.6.2a

Исправлено переключение из сохранённого Native после переподключения:
continuous явно выбирается до DMA/POLL. Контролируемые ERR конфигурации/START
сохраняют COM; журнал разделяет host-close, отсутствие USB и отсутствие ответа.
Выбор ComboBox не отправляет команды. Native сохраняет режим 84 мкс и около
11912 Гц. Для FW 0.5.0 POLL/FAST streaming блокируется после аппаратного отказа
END; остальные POLL-профили остаются диагностическими (BALANCED имеет потери).
[Матрица режимов, HIL, ограничения, логи и скриншоты](GUI_0.6.2a.md).
FW, CSF4 и Flash-калибровка не изменены. Запуск: `python -m current_sensor --port COM18`.
