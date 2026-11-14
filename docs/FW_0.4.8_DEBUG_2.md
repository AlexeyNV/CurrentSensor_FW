# FW 0.4.8-debug.2: continuous VSHUNT pair evidence

The cause is **not established**. This build adds evidence collection; it does
not reject mismatches, correct RAW, change SPI speed or replace Native acquisition.
The new firmware has host coverage, but has not been tested on the board.

Verified default build: text=101488, data=380, BSS=108536 bytes. The existing
heap/stack reservation fits 128 KiB SRAM. File-backed Flash load data ends below
0x08020000; the 0x08060000..0x0807FFFF calibration sector remains outside it.

## Inputs supplied by the owner

FW 0.4.8-debug.1 at SPI 6 MHz:

| Experiment | Supplied result |
| --- | --- |
| Frozen VSHUNT, aggregate | 620000 reads; no reference mismatches, SPI/DMA errors or ADC retriggers; restore successful |
| Constant IDs | 100000 pairs, 200000 ID reads; zero errors; DMA starts=completions=200001, including the final ADC audit |
| Native 150 us | 33351 pairs, no mismatches |
| Native 84 us, ALATCH=0 | 59557 pairs, 1439 mismatches (about 2.42%) |
| Native 84 us, ALATCH=1 | 59556 pairs, 1491 mismatches (about 2.50%) |

The supplied Native rate is about 11911 Hz with no reported runtime loss or DMA
error. Some mismatching pairs completed 35..60 us after the recorded ALERT,
earlier than a nominal 84 us next conversion. These are owner measurements from
the preceding firmware, not new measurements performed during this change.

## SPI DMA audit

Reviewed `StartTransfer`, `FinishTransaction`, RX/TX/settling IRQs, `StartAlert`,
`StartPair`, `StoreVerify`, stop and the generation-aware watchdog in
`App/Src/acquisition_dma.c`, together with Native selection/commit logic.

1. A register read uses one static four-byte TX/RX buffer, command `(reg<<2)|1`
   and dummy bytes. VSHUNT is four SPI bytes, DIAG_ALRT three. RX[0] is the
   command-phase byte and is excluded from RAW. RAW is big-endian RX[1..3].
2. DMA preflight requires idle streams, SPI not BSY/MODF, SPE enabled, HAL state
   READY and CS high. Requests are mutually exclusive under the acquisition
   owner. TX/RX memory and NDTR are set before CS low and DMA request enable.
3. RX completion checks RX/TX errors and TX remaining count, disables SPI DMA
   requests, then waits for TXE and BSY=0 through the existing TIM2 CC1 settling
   callback. Each read has its own CS low/high transaction.
4. The first RAW and its four RX bytes are copied to `pair_current` **before**
   `StartTransfer` clears/reuses RX for the second transaction. The second RAW
   and bytes are likewise copied before a later transfer can start. No path
   found in this audit starts the second read before those copies.
5. DIAG is consumed before the first VSHUNT read. The three payloads/timings are
   now independently copied into the pair-evidence record before subsequent
   buffer reuse. Actual RAW supplied by the acquisition path is independently
   compared with reconstruction from those saved RX bytes.
6. The original Native sample timestamp remains the existing completion-handler
   timestamp. New evidence START is sampled just before CS low, END just after
   CS high, and RX_IRQ on servicing RX IRQ. None is an electrical edge capture
   or the exact hardware TC/ADC-completion time. END includes settling; an IRQ
   can have been delayed after the physical transfer ended.
7. On success CS is raised **before** copying the expanded evidence, so copying
   does not extend CS low. SPI/DMA registers and RX are copied before overrun
   clear, state transition and next read. SPI DMA request bits are already off
   by this snapshot. DMA flags include the saved pre-clear RX IRQ flags.
8. Stop still disables the owning DMA callbacks before abort/cleanup. Error
   results are explicitly invalid; no failed second word replaces the primary.
   The existing policy may retain a valid primary on secondary failure; the
   pair failure is separate from complete-pair statistics.

No premature RX reuse or incorrect byte order was confirmed by source review.
That is not a physical proof: actual wire bits, DMA delivery and phase-dependent
behavior still require the analyzer comparison below. Frozen/ID success is
strong control evidence, but does not exclude every active-ADC/timing fault.
The generation-aware watchdog implementation and its **80 us** limit are
unchanged. No reject policy is enabled, and no new hardware calibration writes,
Flash writes, CSF4 changes, USB lifecycle changes or GUI/archive changes are made.

The [TI INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf), sections
7.3.4, 7.3.7, 7.5 and DIAG_ALRT, describes continuous result updates after completed
conversions, and DIAG reads clearing CNVRF. It does not establish the required
one-to-one association between a software-serviced IRQ and a particular saved
VSHUNT word. The diagnostic therefore treats conversion association/read-phase
atomicity as uncertain, including when no extra IRQ was observed.

In this project DMA/TIM2 IRQ priority is 4 and EXTI1 priority 5. EXTI cannot
preempt a running DMA callback. ALERT may already have changed while its EXTI
handler has not run; EXTI pending state and sampled GPIO help identify that
case. Multiple physical transitions can coalesce. A 35..60 us *software* age
does not independently prove that the physical next ADC update is impossible.

## Remaining hypotheses and distinguishing evidence

| Hypothesis | Evidence needed |
| --- | --- |
| Output updated between reads; software ALERT is delayed/coalesced or refers to another result | Correlate physical ALERT, DIAG ACK and both wire words over several periods; compare phase histograms and nominal boundaries. Matching MCU RX/RAW with two different MISO words localizes the change upstream of software reconstruction. |
| Active-conversion interface behavior, register latching/byte coherence near an update | Capture complete transactions around the boundary and compare 84/150 us and delayed phase runs. The experiment does not assume a datasheet guarantee of CS-latching that was not established. |
| Electrical SPI/MISO timing problem under active ADC/load conditions | Decode MISO at both MCU and INA ends if possible; inspect setup/hold/edges, command and CS gaps. Frozen success reduces but does not eliminate this possibility. |
| STM32 DMA-buffer/callback/byte-processing defect | A saved RAW inconsistent with saved RX proves a software invariant failure. A saved RX word inconsistent with the wire word points to reception/capture; inspect generation, flags, remaining counts and timestamps before calling it an INA result change. |

Do not infer the cause merely because a deliberate delay changes the mismatch
rate. Delays also change service/rearm timing and which conversions are read.

## Native diagnostic collection

With the existing `acq capture native` and `spi verify diagnostic`, every
completed pair contributes to match/mismatch timing statistics. The first **16
full 24-bit RAW disagreements**, including reserved-low-bit-only differences,
are retained, not the first sixteen ordinary pairs. Later disagreements increase
the not-stored count without overwriting earlier evidence. Transfer errors and
partial Stop contexts have an independent first-four failure trace; they cannot
consume the sixteen mismatch slots.

`spi verify report` also now selects Native mismatches. The expanded persistent
report is `spi pair report`, with one event per `spi pair events N` (N=0..15),
and `spi pair failures N` (N=0..3). `events`/`failures` without N request index 0;
NO_EVENT with count=0 is normal if none were captured. Query after Stop/test
completion. Data remains in RAM until the next Native diagnostic session or
phase test, or reset. Ordinary Native with `spi verify off`, a freeze test and
ordinary measurement do not clear this expanded report.

Each event contains SEQ (zero-based accepted-result selection index), selected
Native IRQ_ID, software ALERT timestamp, all three transactions' start/end/RX
IRQ times, full RX including command-phase byte, RAW1/2, signed codes, XOR,
generation, HAL state/error, SPI SR/CR1/CR2, DMA state/flags/CR/remaining counts,
CS before/after release, GPIO low at start/end and EXTI pending. ADC_CONFIG and
ALATCH are the session settings verified at startup; CNVRF/DIAG are the actual
acknowledgement read, not additional flag-clearing reads for instrumentation.
The observer has its own ALERT IDs/ring; NEW_ALERT IDs need not match Native's
selected IRQ_ID if observing began after an initial startup edge.

NEW_ALERTS_DURING_READS counts observed IRQ publications from first-start to
second-end. NEW_ALERTS_IN_GAP counts from first-end to second-start. Up to four
of those IRQ timestamps are retained per event using a 32-edge history. Missing
or overwritten/truncated entries are explicitly counted. No extra EXTI or DIAG
read is generated to obtain this evidence. GPIO samples, EXTI pending and IRQ
publications are observations, not a count of all internal ADC conversions.

`VALID_MASK` bits 0/1/2 identify valid DIAG/first/second responses; each transfer
also prints VALID/STARTED/STATUS/LENGTH. Failed or missing words/times in failure
contexts are diagnostic placeholders and must not be interpreted as measurements.
FAILED_PHASE is 1/2/3 for a failed DIAG/first/second read; zero in partial-Stop
contexts. HAL state can remain READY while the existing register-driven DMA
backend is operating; DMA flags/result are authoritative for that transaction.
Bootstrap failures have SEQ=4294967295, IRQ_ID=0 and ALERT_US=0: no result/event
was selected, so relative sample-age fields are not meaningful for that context.

Flags: 1 continuous-association uncertainty (always set), 2 an observed new IRQ,
4 a nominal conversion boundary falls within first-start..second-end, 8 missing
edge history, 16 RAW/RX reconstruction failure, 32 invalid timestamp ordering.
`UPDATE_POSSIBLE` remains 1 for continuous mode. `NOMINAL_BOUNDARY_BETWEEN_READS`
is a calculation from selected software ALERT plus multiples of configured
VSHCT, including boundaries within either SPI transaction. It is not physical
proof, and zero cannot exclude a real update.

PAIR_MATCH/MISMATCH compare **all 24 bits**. Older VERIFY_MATCH/MISMATCH compare
signed code agreement, with reserved-only differences in VERIFY_RAW_MISMATCH;
therefore do not compare these names as if their semantics were identical.
PAIR_TOTAL=PAIR_MATCH+PAIR_MISMATCH and mismatch=events+events_not_stored are
checked explicitly. RAW_RX_ERRORS is independent of genuine word disagreements.
CAPTURE_ERRORS detects incomplete/wrong-length successful capture sequences;
TIME_ORDER_ERRORS counts invalid transaction ordering. Both are software
diagnostic failures, distinct from SPI/DMA errors and ordinary RAW disagreements.
STATUS OK means the diagnostic observed no I/O/software invariant error; it does
not say that the words matched or that the cause is known. Always inspect counts.

PAIR_MAX_SNAPSHOT_US measures expanded RX/register-copy service time;
PAIR_MAX_NATIVE_OBSERVER_US measures Native pair analysis/event-copy/marker work.
These are separate maxima, not worst-case execution guarantees. Existing Native
service/rate/loss counters remain essential when comparing instrumentation
overhead. The diagnostic adds no delays to ordinary Native. `spi verify off`
does not enable expanded snapshots/bins/event copies. No text or USB calls run
in EXTI, DMA completion or marker callbacks.

## Separate continuous phase experiment

```text
spi pair show
spi pair vshct 84
spi pair latch 0
spi pair delay 0 0
spi pair test 10000
spi pair show
spi pair report
spi pair events 0
spi pair failures 0
spi pair bins 0
spi pair bins 1
spi pair bins 2
spi pair bins 3
spi pair stop
```

`test` supports exactly 10000/100000 requested complete pairs. VSHCT is exactly
84 or 150 us; first/gap delays are decimal integers 0..500 us. Parsing rejects
signs, overflow, missing arguments and trailing text without changing settings.
Defaults are 84 us, ALATCH=0 and delays 0/0. These settings are RAM-only and
independent of the ordinary Native profile/latch/verify settings.

The experiment requires acquisition and streaming stopped, TIM3 stopped and
SPI/DMA idle at **6 MHz**. It saves configuration, verifies ADCRANGE=0, enables
continuous shunt-only (84 us `0xAA68`, 150 us `0xAAA8`), one conversion per result
(AVG field=0), CNVR=1, APOL=0,
selected ALATCH, preserves SLOWALERT, and verifies readbacks. CONFIG is not
written. A bootstrap DMA DIAG clears old CNVRF; thereafter it waits for a new
observed edge, acknowledges DIAG via DMA, checks CNVRF, waits until the selected
edge age reaches FIRST delay, reads VSHUNT1, waits GAP after first-end, then reads
VSHUNT2. Each read uses the same DMA backend and 80 us transaction watchdog as
freeze/Native. The foreground FSM waits by checking TIM2 and returning; no IRQ
masking, spinning or active DMA transaction is used to implement the delays.

Actual first-start age can exceed the requested delay because DIAG/dispatch
already took time. Histogram/event timestamps describe actual timing. New edges
while busy are observed but not serviced as queued synthetic pairs; after a
pair the engine waits for a later edge. BUSY_EDGES counts observed notifications
discarded while bootstrapping/servicing/delaying; SKIPPED_EDGES counts older
notifications superseded by a newer edge at selection. These are intentional
experiment selection, not claims about normal Native losses or ADC conversions.
NOT_READY counts actual CNVRF=0 acknowledgements; they do not become pairs.

Missing edges have a bounded `8*VSHCT+2000 us` deadline; an overall bound based
on requested pairs/delays also prevents infinite operation if edges continue but
CNVRF never confirms. WAIT_TIMEOUTS is separate from DMA_TIMEOUTS. SPI_DMA_ERRORS
counts failed DMA responses including bootstrap; DMA_TIMEOUTS is its watchdog
subset. IO_ERRORS counts failed setup/restore HAL operations or read requests.
RESTORE_ERRORS remains visible even after a successful cleanup retry.

Stop, transfer error, completion and USB deconfiguration/DTR loss stop the
observer and shared DMA backend, restore saved ADC_CONFIG and DIAG control bits,
and verify readbacks. Initial shutdown/triggered modes are refused so the saved
mode is safe to restore to continuous operation. RESTORE_FAILED keeps the busy
guard; `spi pair stop` retries. Failed cleanup does not unlock competing commands.
Other SPI, acquisition, streaming and calibration commands are blocked while the
experiment owns resources. `ping`, `info`, `help` and lightweight `spi pair show`
remain available. Final reports are emitted only on request, after stop. ELAPSED
uses the existing rollover-safe 64-bit TIM2 clock and is latched before restore.
Native report elapsed is deliberately 0 here; use `acq native report` for its
existing session duration.

Bins 0..3 select respectively ALERT->first-start, ALERT->first-end,
first-end->second-start gap, ALERT->second-end. Each histogram row reports raw
match/mismatch counts in 10 us half-open ranges; the final row is >=310 us.
All successful pairs, not just saved mismatches, contribute. Bin widths and
nominal conversion times are not evidence of synchronization with the ADC.

## Board runs

Program the current ELF through the existing CubeProgrammer/system DFU method
with full-chip erase disabled and sector 7 excluded from erase. The existing
linker sector-7 reservation is unchanged. Check `info`, `spi config` (6 MHz),
`cal show`, `regs`, and use a known steady input. Save a PuTTY log per run.

First repeat ordinary Native 150 us without and with verification:

```text
acq stop
acq mode dma
acq capture native
acq profile set balanced
acq alert adc profile
acq native latch 0
spi verify off
acq test 5
acq native report
spi verify diagnostic
acq test 5
acq native report
spi verify report
spi pair report
spi pair events 0
spi pair bins 0
spi pair bins 1
spi pair bins 2
spi pair bins 3
```

Next set FAST plus `acq alert adc 84` and repeat diagnostic runs with
`acq native latch 0` then 1. Save available events 0..15 and failures 0..3 after
each test; do not start the next diagnostic before saving the previous data.
Compare native effective rate/runtime losses, DMA/SPI errors, snapshot/observer
maxima, pair RAW/RX consistency and physical timestamps. The expanded observer
may perturb service timing; do not assume the previous 11911 Hz is unchanged.

For independent phase testing, stop acquisition, leave marker off initially,
then set `spi pair vshct 84`, latch 0 and `spi pair delay 0 0`. Run 10000 pairs,
wait for DONE using `show`, save report/events/bins. Repeat with first/gap
combinations `20 0`, `40 0`, `60 0`, `80 0`, `0 10`, `0 30`, `60 30`. Repeat the
matrix at 150 us and then selected latch=1 cases. Deliberate phase crossings
are expected to permit different valid results; do not label them corruption.

Check `spi pair stop` during bootstrap, DIAG, either read and each delay;
test USB close/reconnect; verify `regs`, `measure`, `cal show`, `ping` afterward.
Re-run `spi freeze test 100000` and `spi freeze ids 100000` as controls. Do not
write calibration. Finally restore `spi verify off`, `acq alert adc profile`,
`acq capture continuous` and the desired ordinary profile.

## Logic analyzer and optional mismatch marker

| Analyzer input | Board signal |
| --- | --- |
| CH0 | INA229 ALERT / STM32 PB1 |
| CH1 | INA229 CS / STM32 PB0 |
| CH2 | SPI1 SCLK / PA5 |
| CH3 | SPI1 MOSI / PA7 |
| CH4 | SPI1 MISO / PA6 |
| CH5 optional | PA8 mismatch marker, only after board verification below |
| GND | Common board digital ground |

Use suitable 3.3 V digital inputs, short probes and a capture rate of at least
50 MS/s (prefer 100 MS/s for 6 MHz timing inspection). Configure SPI **mode 1**,
MSB first, active-low CS; command `0x2D` is DIAG_ALRT and `0x11` is VSHUNT. Expect
three bytes for DIAG and four for VSHUNT, with command-phase MISO ignored.
Measure physical CS/clock windows; a coarse digital trace cannot itself qualify
20 ns setup/hold margins, which may require a faster instrument/oscilloscope.

PA8 is absent from the project's assigned-pin list in CurrentSensor_FW.ioc and
is not initialized by the firmware; SPI/USB/SWD/ALERT/CS use other pins. **There
is no schematic/netlist proving PA8 is electrically free on this board.** The
standard ELF therefore does not configure PA8 and `spi pair marker on` returns
MARKER_UNAVAILABLE_OR_PIN_BUSY. The test works without a marker using continuous
capture and manual correlation of CS groups, words and relative timestamps.

Only after the operator verifies PA8 routing, header availability and absence
of another driver/load, build an explicitly confirmed variant in a separate
directory (preserving the normal build):

```powershell
make -j4 BUILD_DIR=build-marker 'C_DEFS=-DUSE_HAL_DRIVER -DSTM32F411xE -DPAIR_DIAG_PA8_CONFIRMED=1'
```

After installing that variant, `spi pair marker on` explicitly configures PA8.
Runtime guards refuse an already-used GPIO/AF or TIM2 CC2. A mismatch raises PA8
and schedules a nominal 5 us falling edge using unused TIM2 CC2; IRQ latency
may extend the pulse. No busy wait is used and TIM2 frequency/CC1 watchdog
settling are untouched. `spi pair marker off` drives low and releases the pin
to input. See [ST RM0383](https://www.st.com/resource/en/reference_manual/rm0383-stm32f411xce-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)
for TIM2 compare/CC2 interrupt operation. The marker-enabled code branch was
cross-compiled, but its pin/pulse timing has not been validated on hardware.

Set a rising-edge marker trigger with substantial **pre-trigger history** (at
least several hundred us; preferably 1 ms). The marker is emitted after mismatch
detection, later than the two reads. It marks every RAW disagreement, even after
the first sixteen event slots fill. Capture early triggers to correlate with
events 0..15; the report includes a lifetime marker pulse counter.

For each event, reconstruct each MISO payload as `(byte1<<16)|(byte2<<8)|byte3`
and signed code from bits 23:4. Compare both payloads to saved RX and RAW and
their XOR; distinguish the command byte from payload. Match CS group order,
DIAG value, word values and relative ALERT/CS timings rather than assuming
an analyzer clock has the same absolute origin as TIM2. Inspect physical ALERT
assert/release/next edge and any pending indication. If a physical update lies
within a read, do not assume multi-byte atomicity without further evidence.

| Observation | Supported conclusion / limit |
| --- | --- |
| RAW differs from reconstruction of saved RX | Software invariant failure; investigate capture/processing before blaming INA |
| Saved RX differs from cleanly decoded MCU-pin MISO | Reception/DMA/capture issue or analyzer decoding/probing issue; validate both instruments |
| Each RAW equals saved RX and physical MISO, but wire words differ | Difference already exists on the wire; software byte reconstruction did not create it. This alone does not prove ADC-update timing or exclude electrical faults |
| Word changes follow a physical completed-conversion/ready boundary consistently across phase runs | Supports register update/association explanation; document actual boundaries and repeat at 150 us |
| No software IRQ inside pair, but physical edge/pending or delayed callback is visible | IRQ count is an incomplete physical timeline; correct the interpretation, not the RAW |
| Frozen control remains clean but active-mode wire disagreements persist without a proven update boundary | Root cause remains open; preserve trace and seek device/interface clarification rather than apply speculative filtering |

## Host validation and changed files

`make -j4` builds the default marker-disabled ELF/HEX/BIN. The C suite has 16
passing executables, CLI tests 29, and offscreen GUI tests 94. The marker-enabled
source was separately compiled with `-Wall -Wextra -Werror`. Host results do
not measure CS electrical timing, DMA IRQ latency, marker width or Native rate.

The new suite tests matching pairs, 50 clean pairs followed by 25 mismatches
(events preserve exactly the first 16 mismatches), RAW/RX reconstruction,
immutability after source-buffer mutation, signed codes, observed IRQs during
first read/gap, actual simulated register updates, phase histograms and TIM2
wrap. It exercises the production phase controller/CLI with mocked peripherals:
10000 pairs, delay/argument validation, busy restart, no-ready/missing-edge
deadlines, delayed DMA completion, bootstrap/DIAG/first/second read errors,
Stop in every phase, late results, failed restore and retry. Existing controller
tests explicitly check Native verify-off does not enable the observer. Existing
generation/watchdog, Native/ALERT, freeze, calibration, USB transport and Python
regressions continue to pass. Firmware IRQ electrical behavior remains untested.

```powershell
make -j4
cmake -S tests -B build/host-tests
cmake --build build/host-tests --config Debug
ctest --test-dir build/host-tests -C Debug --output-on-failure --timeout 30
.\.venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests_gui -p 'test_*.py'
```

Added `pair_trace_core.h/.c`, `pair_experiment_core.h/.c`, `pair_diagnostic.h/.c`
under App/Inc and App/Src, `tests/pair_diagnostic_test.c` and this report.
Updated `acquisition_dma.c`, `acquisition.c`, `spi_dma_diagnostic.h`, `app.c/.h`,
`spi_freeze.c` (mutual-exclusion guard), Core/Src/stm32f4xx_it.c USER CODE,
Makefile, tests/CMakeLists.txt, tests/measurement_profile_test.c,
tests/support/stm32f4xx_hal.h and README.md. Native/ALERT core state machines,
linker layout/sector 7, calibration, USB modules, CSF4 and GUI files were not
changed for this task. No commit/push or hardware programming was performed.
