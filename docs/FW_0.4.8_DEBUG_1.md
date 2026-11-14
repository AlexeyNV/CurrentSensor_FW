# FW 0.4.8-debug.1: frozen-register SPI diagnostic

This build adds a RAM-only diagnostic. No board result is claimed. Earlier Native
84 us pair mismatches, including pairs without another observed ALERT, do not
establish their physical cause. This experiment removes continuous ADC updates
from the repeated-read window before comparing the digital words.

## Datasheet basis

The [official TI INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf),
sections 7.3.4 and 7.4.1 and ADC_CONFIG/DIAG_ALRT register definitions, specifies:

- MODE=2 performs a triggered shunt-only conversion and returns internally to
  shutdown when finished. The programmed MODE bits remain the selected mode;
  readback of MODE=2 is not an independent measurement of the ADC power state.
- Output registers retain the completed result until a subsequent conversion
  finishes. Registers remain accessible in shutdown. Reading VSHUNT does not
  start a conversion.
- Reading DIAG_ALRT clears CNVRF. An ADC_CONFIG write that starts conversion also
  clears it. Ready must therefore be consumed once and remembered in software.

The diagnostic saves ADC_CONFIG and DIAG_ALRT control bits, checks ADCRANGE=0,
temporarily disables CNVR/ALATCH/APOL while preserving SLOWALERT, and writes
ADC_CONFIG once. Default VSHCT=150 us gives `0x2AA8`; optional 84 us gives `0x2A68`,
and 50 us gives `0x2A28`. These all select MODE=2, AVG=0 (one conversion, no
averaging). Unused VBUSCT/VTCT fields retain the existing fast-shot encoding.
CONFIG, SHUNT_CAL and all hardware/software calibration coefficients are untouched.

Foreground DIAG_ALRT checks, spaced at least 50 us apart, wait for CNVRF. The
deadline includes the configured CONVDLY, VSHCT and a 2 ms margin. A missing-ready
timeout is distinct from a DMA transaction timeout. The datasheet's typical
shutdown wake-up time is 60 us; the margin is not a measured conversion bound.
There is **no additional shutdown write** after ready. No other conversion is
started until restoration, after the repeated-read window has ended.

Before the reference read and after the last pair, DMA reads verify ADC_CONFIG
still has the programmed single-shot value and CNVRF has not reappeared since
the initial ready read. A global counter audits *attempts* to write ADC_CONFIG,
including attempts blocked by SPI ownership. A clean VSHUNT run has exactly one
trigger write and zero ADC writes while frozen, excluding the subsequent restore.
This is a software/configuration audit, not a direct observation of the internal
ADC. If the audit fails, an apparent RAW change cannot establish an SPI fault.

## Commands and execution

All commands use the existing ASCII CLI and Enter. Counts must be exactly 10000
or 100000; VSHCT accepts exactly 50, 84 or 150. Other arguments are rejected.

```text
spi freeze show
spi freeze vshct 150
spi freeze test 10000
spi freeze test 100000
spi freeze report
spi freeze events
spi freeze events 1
spi freeze events 2
spi freeze events 3
spi freeze stop
spi freeze ids 10000
spi freeze ids 100000
```

`test` starts asynchronously and acknowledges STARTED. Use `show` to wait for
DONE, ERROR or STOPPED; no automatic USB completion report is emitted. `report`
and `events` are available after completion, or in RESTORE_FAILED to inspect a
failed cleanup. `show`, `stop`, `ping`, `info` and `help` remain available during
the test; competing acquisition, SPI, calibration and streaming commands are
blocked. A second test or parameter change while busy is rejected without
clearing the current run. Readout is paginated four error events at a time to
fit the existing bounded CLI TX buffer.

Start requires inactive acquisition/streaming, disabled TIM3, a stopped DMA
backend and the existing actual SPI rate of 6 MHz. The diagnostic never changes
SPI speed. For VSHUNT it also requires the saved ADC mode to be continuous and
ADCRANGE=0; it refuses an initial triggered/shutdown mode rather than restore a
state that could leave the device shut down. Selected acquisition/verify policies
are not changed. Test parameters, results and trace survive only in RAM until a
new test or reset. Save previous Native/verify diagnostic reports before this
test: starting the shared DMA backend resets its per-run diagnostics.

VSHUNT sequence: confirm ready, DMA-audit ADC/DIAG, DMA-read the reference, then
perform two separate VSHUNT DMA transactions per pair, followed by final audits.
All three RAW comparisons are exact: RAW1 versus RAW2, RAW1 versus reference,
RAW2 versus reference. Signed 20-bit codes are decoded without signed right
shift. Low four reserved bits are checked separately. No tolerance, correction,
filter, replacement or silent rejection is applied. Mismatches continue the
series; transfer/audit failures stop it and restore configuration.

`ids` is an independent control experiment. Each pair reads MANUFACTURER_ID and
DEVICE_ID through the same DMA backend and checks their actual received values
against `0x5449` and `0x2291` for this board. It does not trigger or stop the ADC,
write ADC_CONFIG or change DIAG_ALRT controls. The two different expected ID
values are not compared for equality with one another. Reference/code fields
are N/A in this mode; `FREEZE_LAST_RAW1/2` contain actual received IDs.

## Existing transport and recovery

Preparation and restoration use INA229_ReadReg16/WriteReg16 with the existing
HAL timeout and CS handling. The measurement series uses diagnostic read requests
added to `acquisition_dma.c`, sharing the existing StartTransfer, DMA2 Stream0 RX
and Stream3 TX, command encoding, static TX/RX buffers, IRQ handlers, TXE/BSY
settling and generation-aware watchdog. There is no second SPI implementation.
At 6 MHz, ideal wire time is 5.33 us for a 24-bit register and 4 us for a 16-bit
register. Actual measured transaction time includes software/IRQ/settling costs.

The existing watchdog remains **80 us**. Diagnostic requests arm its normal
pair-active gate. Completion publishes an immutable result mailbox before the
foreground engine can request another transfer. IRQs copy a small result and
update counters; they do not format text, call USB or commit acquisition samples.
Normal Native/ALERT state machines are not entered by diagnostic completion or
error paths. TIM3 remains stopped throughout the experiment.

Normal completion, explicit stop, transfer failure and USB deconfiguration/DTR
disconnect abort/finish DMA through the existing stop path before restoring
DIAG_ALRT control bits and the saved continuous ADC_CONFIG. Both restores are
read back. Read-only/sticky DIAG status bits cannot be restored as historical
events; only the original writable control bits are restored.
If stopping DMA or restoring registers fails, RESTORE_FAILED retains the busy
guard, and `spi freeze stop` retries cleanup. It does not pretend the device has
returned to normal. Late IRQs cannot commit a diagnostic sample: the shared stop
path disables ownership callbacks, marks DMA inactive, and the next start clears
pending flags before enabling IRQs. The frozen write audit and test elapsed time
are latched before restoration, so cleanup retries do not alter those values.

## Report semantics

`FREEZE_STATUS` is lifecycle: IDLE, RUNNING, DONE, STOPPED, ERROR or RESTORE_FAILED.
DONE means the requested reads completed; check the final STATUS as well.
Any mismatch, reserved-bit error, audit, transfer or restoration error makes the
report `STATUS: ERR_FREEZE_DIAGNOSTIC`. `show` reports command success, not a test
verdict. STOPPED is intentionally incomplete even if its report has STATUS OK.

| Field | Meaning |
| --- | --- |
| PAIRS_REQUESTED / COMPLETED | Requested pairs / pairs with both successful read responses. Pair numbering starts at 1. |
| READS_TOTAL | Pair-read responses consumed, including a failed response; excludes reference and audit reads. A clean run is exactly 2 x COMPLETED. |
| ALL_DMA_READS | All diagnostic responses, including reference/audits and failures. Clean VSHUNT: 2N+5; clean IDs: 2N+1. |
| PAIR_MATCH / PAIR_MISMATCH | VSHUNT: full RAW equality/inequality within a pair. IDs: both expected IDs received / either ID wrong. Their sum equals COMPLETED. |
| REFERENCE_MISMATCH | Number of complete pairs with either value differing from its baseline. |
| REFERENCE_MISMATCH_1 / _2 | Pairs whose first / second value differs from its respective baseline. |
| EQUAL_BUT_NOT_REFERENCE | VSHUNT pairs where RAW1=RAW2 but both differ from the reference. |
| ONLY_ONE_BAD / BOTH_BAD | Exactly one / both members differ from their baselines. |
| CODE_MISMATCH | VSHUNT pairs whose signed 20-bit codes differ, independently of reserved-bit-only differences. |
| RESERVED_BITS_ERRORS | Number of pair words with any low four bits set (0..2N). Bad reference RFU also produces an audit error. |
| FIRST_CHANGED_PAIR | First complete pair that differs from baseline; zero if none. |
| REFERENCE_RECOVERIES | Number of later complete pairs where both members again match baseline; counts pairs, not transitions. |
| CHANGED_SUFFIX_PAIRS | Consecutive baseline-bad pairs at the end of the run. |
| LONGEST_NEW_VALUE_RUN | Longest contiguous VSHUNT run where both reads equal the same non-reference word. |
| NO_REFERENCE_RECOVERY_AFTER_FIRST_CHANGE | A change occurred and no subsequent pair fully matched reference. Interpret with suffix/run counts and events. |
| SPI_ERRORS | Failed register operations/diagnostic transfer requests or responses, including DMA failures. Not a count of RAW mismatches. |
| DMA_ERRORS / DMA_TIMEOUTS | Diagnostic non-timeout failed responses / 80 us watchdog failed responses. These overlap SPI_ERRORS. |
| HAL_ERRORS | Failed setup/ready/restore polling-driver operations. LAST_INA229_STATUS is the driver enum; LAST_HAL_ERROR_STATUS retains the last actual HAL status observed for such failure. |
| AUDIT_ERRORS | ADC/ready/control-path invariant failures. These do not automatically increment SPI_ERRORS. |
| RESTORE_ERRORS / RESTORE_PENDING | Failed stop/restore operations / resources or registers still requiring cleanup. |
| TRIGGER_WRITE_ATTEMPTS | ADC write attempts before cleanup. Clean VSHUNT=1, clean IDs=0. |
| ADC_WRITES_WHILE_FROZEN | ADC write attempts after initial CNVRF confirmation through final audit, excluding restore. Clean=0. |
| ADC_START / END; DIAG_START / END | Actual audit readbacks. Clean VSHUNT ADC equals selected single-shot value, CNVRF=0 in both DIAG audits. IDs only audits ADC at the end. |
| OWNER_ERRORS | Increase in driver contention count during the run. Clean=0. |
| DMA_STARTS / COMPLETIONS | Shared-backend transaction starts / successful physical completions, saved at cleanup. Clean counts equal ALL_DMA_READS. Aborts can make starts exceed completions. |
| DMA_STOP_CONFIRMED / DMA_RESULT_PENDING | Cleanup verified backend idle and ownership released / a diagnostic response was still pending if cleanup failed. |
| CALLBACK_ORDER_ERRORS / CS_ERRORS / BUFFER_REUSE_ATTEMPTS | Shared-backend safety counters captured at cleanup; expected zero. |
| MAX_TRANSACTION_US | Maximum CS-start-to-transaction-completion/error time (TIM2), not just SPI clocks. |
| MAX_PAIR_US | Maximum first-start to second-end interval among completed pairs. |
| MAX_GAP_US | Maximum previous-end to next-start interval among diagnostic transfers, including audits/reference. |
| TEST_ELAPSED_US64 | Existing rollover-safe TIM2 session clock: test preparation to final audit/stop/error, latched before cleanup. Does not advance after completion. |
| COUNTER_INVARIANT_ERRORS | Explicit bounds/equality check on the counters; expected zero. |

Names in the table have prefix `FREEZE_`. LAST_RAW1/2 are actual last complete
pair values. A failed/incomplete DMA word is retained only as diagnostic bytes,
not a valid reading or a completed pair.

The first 16 error contexts are retained without overwrite; EVENTS_NOT_STORED
counts additional contexts beyond capacity. The trace captures pair/phase, RAW,
codes, reference/expected baselines and XORs, command RX byte and payload bytes,
TIM2 start/end, length, generation, actual HAL state/error fields, SPI_SR, DMA
flags, CS state and owner/callback/CS counters. STATUS distinguishes watchdog
timeout (2), I/O failure (1) and success (0). STARTED and DATA_VALID explicitly
mark an unstarted/partial/failed transaction. Three-byte ID/audit transfers pad
the unclocked fourth byte with zero; LENGTH disambiguates it. HAL state can
remain READY during raw-register DMA; the DMA result/flags are authoritative.
Watchdog errors are DMA_TIMEOUTS plus the event's timeout status; the existing
`acq dma report` also retains its low-level timeout snapshots.

Event PHASE values: 2 ADC-start audit, 3 DIAG-start audit, 4 reference, 5 first
pair member, 6 second member, 7 ADC-end audit, 8 DIAG-end audit. PAIR=0 denotes
a failure outside a measurement pair. In IDS mode BASE1/2 and XOR_REF1/2 use
the two different expected ID words; CODE1/2 are N/A. Only completed, successful
read pairs contribute to match/mismatch statistics.

## Board procedure and interpretation

Close GUI/Python/PuTTY before programming. Use STM32CubeProgrammer's existing
system DFU loading method and `build/CurrentSensor_FW.elf`, with full-chip/mass
erase **disabled** and sector 7 excluded from any manual erase selection.
Program/verify only the ELF image's sectors. The linker still reserves
0x08060000..0x0807FFFF for calibration and asserts application data ends below it.
For the verified build, file-backed ELF data occupies 0x08000000..0x0801668B
(exclusive end 0x0801668C), well below the reserved sector. Confirm the actual
ELF load ranges again after rebuilding. Never erase sector 7 for this diagnostic.

Open PuTTY with session logging, verify calibration and SPI first:

```text
info
cal show
spi config
acq stop
regs
spi freeze show
spi freeze vshct 150
spi freeze test 10000
```

Wait for `spi freeze show` to show DONE, then save `report` and `events`. Check
PAIRS_COMPLETED=10000, READS_TOTAL=20000, ALL_DMA_READS=20005, all mismatch/error
counts zero, SINGLE_SHOT_CONFIRMED=1, TRIGGER_WRITE_ATTEMPTS=1,
ADC_WRITES_WHILE_FROZEN=0, DMA_STOP_CONFIRMED=1, RESTORE_PENDING=0, and STATUS OK.
These are expected acceptance values, **not measured board results**. Confirm
`regs` returned to the original ADC_CONFIG (normally 0xFB68) and `measure` works.

Then repeat `spi freeze test 100000`, wait for completion and save its report
and all available events pages. Repeat at `spi freeze vshct 84` and then 50,
always as independent single-shot runs. Run `spi freeze ids 100000` independently:
expect 200000 pair reads, 200001 total DMA responses, actual IDs 0x5449/0x2291,
zero trigger writes and zero ID errors. Report/events are replaced by each new
test, so save them before starting the next experiment.

Check `spi freeze stop` during a test, repeat after USB disconnect/reconnect,
then verify `ping`, `regs`, `measure`, `cal show` and the previous acquisition
mode. Do not execute `cal save` for this diagnostic. If RESTORE_FAILED is shown,
save the report and retry `spi freeze stop` before any ordinary SPI command.

Clean frozen reads with continuous mismatches support the register-update
hypothesis; they do not prove it. Frozen mismatches with clean ADC audit and
corrupt constant IDs support investigating the shared digital path. Clean IDs
do not exclude a VSHUNT-specific receive/length/timing problem. Equal changed
pair values, new stable runs or returning reference values must be inspected
with command audits and RX bytes before assigning a cause. The reference itself
can be an erroneous first read; this test cannot prove absolute analog accuracy.

Power loss/MCU reset during a test loses the RAM restoration state; ordinary
exit recovery cannot run in that case. Do not infer that reading the programmed
MODE bits is independent physical proof of shutdown. The test has no separate
ADC internal-state pin, logic-analyzer measurement or hardware fault injection.

## Host checks and affected files

`make -j4` builds ELF/HEX/BIN without warnings. CMake/CTest passes all 15 C test
executables; CLI unittest passes 29 tests; offscreen GUI unittest passes 94 tests.
The new pure-C engine test executes 100000 identical pairs (200000 pair reads,
one trigger, zero frozen writes), 100000 ID pairs and all three VSHCT settings.
It injects first/second RAW bit flips, equal-but-new words, low-bit differences,
stable changes after pair 10, ready timeout, HAL/DMA failures, first/second-read
stop, USB disconnect, late results, busy restart, trace overflow, unexpected ADC
writes and failed cleanup followed by retry. Time crosses TIM2 wrap. The existing
DMA core tests cover stale generations and callbacks around watchdog/stop;
host tests cannot validate physical DMA IRQ timing or electrical CS/SPI signals.

Commands used:

```powershell
make -j4
cmake -S tests -B build/host-tests
cmake --build build/host-tests --config Debug
ctest --test-dir build/host-tests -C Debug --output-on-failure
.\.venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests_gui -p 'test_*.py'
```

Added: `App/Inc/spi_dma_diagnostic.h`, `App/Inc/spi_freeze.h`,
`App/Inc/spi_freeze_core.h`, `App/Src/spi_freeze.c`, `App/Src/spi_freeze_core.c`,
`tests/spi_freeze_test.c`, this guide.
Updated: `App/Inc/acquisition_dma.h`, `App/Src/acquisition_dma.c`,
`App/Inc/ina229.h`, `App/Src/ina229.c`, `App/Inc/app.h`, `App/Src/app.c`,
`Makefile`, `tests/CMakeLists.txt`, `tests/ina229_owner_test.c`, `README.md`.
No commit/push or hardware programming/testing was performed for this change.
