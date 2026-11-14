# FW 0.5.0: measured HIL results

Date: 2026-11-14. Board STM32F411RET6 + INA229, CDC serial 3282346D3032, discovered
as COM18 by VID/PID and confirmed INFO. Measurement configuration: DMA Native,
ADC_CONFIG=0xAA68, 84 us, AVG field=0, ALATCH1, verify off, SPI 6 MHz, 5 mOhm.

**Transport/recording works; overall streaming verdict is WARNING.** Service
cycles beyond the nominal period and suspicious measured values remain visible.
No absolute accuracy or every-internal-conversion guarantee is established.

## Actual runs

| Test | Samples | Observed rate Hz | Runtime losses / queue overflow | Result |
| --- | ---: | ---: | --- | --- |
| Baseline 0.4.8-debug.2a smoke 1/2/3 | 59556 / 59556 / 59557 | 11911 each | 0 | PASS |
| Final FW 0.5.0 smoke 1/2/3 | 59557 / 59557 / 59557 | 11911 each | 0 | PASS |
| Final 5 s capture | 59987 | 11912.34 | 0 | WARNING |
| 60 s capture | 715154 | 11912.33 | 0 | WARNING |
| 300 s capture | 3574434 | 11912.40 | 0 | WARNING |
| GUI record/stop/reconnect | 61249 | 11912.34 | 0 | WARNING |

For all listed streaming runs: END received, ADC/USB/CSV counts agree, no CRC,
frame/sequence errors, no SPI/DMA errors or timeouts, no acquisition-ring or USB
buffer overflows. Separate offline replay of full CDC logs matched every
sequence/TIM2/RAW tuple against CSV via rolling SHA256 (5/60/300 seconds).
Native known runtime-event losses and queue drops were zero; intentional Stop
tail=1 remains in END/loss diagnostics. No samples were synthesized or filtered.

Complete-service cycles >=84 us were 45 / 41 / 676 for the listed 5 / 60 / 300 s
runs and 5 in the final GUI run. These are not physical DMA errors or a proven
number of overwritten ADC results. Smoke retains a strict zero-overrun rule;
streaming's named integrity criteria treat these as explicit performance warnings.
This distinction was introduced after the first streaming harness classified
the diagnostic counter as a fatal DMA error; original FAIL logs are retained.

The first FW 0.5.0 streaming attempt also exposed a startup delay: first ACK
began 2482 us after IRQ; a large stream-ring memset happened after Native start.
Resetting only metadata removed that delay. Native gained a service point before
transport building, and v2 build steps became smaller. Physical SPI, watchdog
and the Native/ALERT acquisition algorithm were not changed. Later streaming
still exhibits service-cycle warnings; no deterministic timing claim is made.

## RAW reliability is separate

The 60 s recording contains 2 values outside the control range -50..10000 mA;
the 300 s recording contains 12. The final GUI run contains RAW 0x66C950 at
accepted sequence 4381, CODE=421013, corresponding to +26313.3125 mA. This word
was independently found in the recorded valid v2 frame. The GUI detector reports
two anomaly transitions around that sample. RAW bytes, CSV and numeric history
were preserved. This is evidence of a suspicious measured value, not a CRC or
Python reconstruction failure. It does not establish whether the source is INA,
electrical SPI or another active-mode effect. No phase/double-read investigation
was restarted and no corrective filter/reject was introduced.

Therefore this is a functioning streaming/GUI prototype, **not a metrologically
validated current meter**. Next measurement step: known zero input, stable known
load and external ammeter comparison; assess bias/noise/gain/temperature and
rare RAW outliers separately. Load switching requires a separately verified
safe bench; no automatic load or calibration action was performed.

## Evidence directories (local, ignored by Git)

- [Baseline smoke](../results/hil/20261114T003816Z_dd5169a9/report.md)
- [Final flash + three smoke tests](../results/hil/20261114T005550Z_f346296a/summary.json)
- [5 s raw verification](../results/hil/20261114T024824Z_4bd096c2/verification.json)
- [60 s raw verification](../results/hil/20261114T005949Z_fd5d5600/verification.json)
- [300 s raw verification](../results/hil/20261114T023833Z_b1f5863a/verification.json)
- [GUI summary](../results/hil/gui_20261114T025052Z_c431413c/summary.json)
- [GUI live plot](../results/hil/gui_20261114T025052Z_c431413c/gui_live.png)
- [GUI suspect RAW](../results/hil/gui_20261114T025052Z_c431413c/raw_suspects.json)

The final GUI recording declares protocol_version=2, is completed/end-verified,
and reopened successfully with all 61249 samples. The earlier GUI run exposed a
hardcoded protocol_version=1 in recorder setup; this was corrected and retested.
Headless Qt exercised the existing live window, recorder, Stop, Disconnect and
Reconnect on the physical board. Long Native GUI archive endurance is untested.

## Flash/calibration and image identity

Operator explicitly approved programming and twice entered DFU. Only application
sectors 0,1,2,3,4 were erased; --skipErase download+verify succeeded. Sector 7 was
read before/after each successful flash, SHA256 identical:

`6d25dad15300736cfa485ab994c4cd3b83ec7e7e5d91d5beeb64c1602350e28a`

Calibration after testing: source FLASH, offset_ua=0, gain_ppm=1000000,
enabled=0, dirty=0. Final idle CONFIG=0, ADC_CONFIG=0xFB68, DIAG=0x0001, SPI6MHz;
capture selected continuous for compatibility. No calibration save was issued.

The physically tested ELF SHA256 is:
`5634401c2210461c60243ab6ac947e72cdc3e07416e53066823938ccccbf22e6`.
It is preserved as [build/CurrentSensor_FW_validated.elf](../build/CurrentSensor_FW_validated.elf)
and in the flash result directory. A later make recompiled app.c and changed
three FW_BUILD timestamp characters; comparing complete binary load images
found exactly those three ASCII differences and equal image lengths. The normal
fresh-build ELF is distinct; its metadata must not be called the identical
hardware-tested image. Use the explicitly validated artifact for exact replay.

## Software checks and scope

make -j4 succeeded without compiler warnings; all 16 C test executables, 35
CLI/HIL Python tests and 96 GUI tests passed. Coverage includes v1 regressions,
v2 RAW reserved bits/actual offsets/rollover, Native stream guards, metadata-only
ring reset, device ambiguity, missing checks, ELF bounds, explicit erase and
skipErase+verify, wrong target/no erase, failed sector hash/no GO, GUI Native
configuration, event-time processing, Stop-tail semantics and recording metadata.

Added tools/hil package: run.py, flash_safe.py, native_stream.py, gui_smoke.py,
verify_capture.py; tools/test_hil.py and tests_gui/test_native.py. Firmware edits
are limited to Native streaming gate/loss metrics, explicit v2 serialization,
transport setup/budget and version. Existing GUI received a Native option,
v2 negotiation, actual-time semantics and metadata/health integration. Tests,
README and .gitignore were updated. No phase diagnostic expansion, GUI rewrite,
calibration/Flash-layout change, ADC/SPI/watchdog change, commit or push occurred.
