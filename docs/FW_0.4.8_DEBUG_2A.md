# FW 0.4.8-debug.2a: phase experiment rearm

The confirmed software defect is VSHUNT2 -> WAIT_ALERT discarding all observed
busy notifications without acknowledging readiness again. CNVRF can become set
during the two reads, so the line may already be active when the FSM begins
waiting for another falling edge. Native instead services queued notifications
through DIAG. The phase path had neither that service nor an explicit rearm.
This explains a possible dead wait and reproduces in a held-line host model.
The exact physical PB1/pending state in the owner's failing run was not captured;
hardware confirmation is still needed. This change does not establish the cause
of the RAW disagreement.

The [TI INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf), sections
7.3.4 and DIAG_ALRT definitions, relates CNVR output assertion to conversion
readiness and describes DIAG read/flag clearing. ALATCH=0 does not authorize
software to assume an unacknowledged CNVR condition will produce fresh falling
edges for every conversion. Both latch settings are tested conservatively.

## Corrected sequence

BOOT ACK -> WAIT_ALERT -> selected-edge ACK with CNVRF check -> first-phase
wait -> VSHUNT1 -> gap wait -> VSHUNT2 -> REARM ACK -> WAIT_ALERT.

After the last requested pair the original stop/restore path runs directly.
REARM uses the same DMA DIAG transaction as BOOT, with the existing watchdog.
It clears readiness from deliberately unserviced conversions without accepting
their old VSHUNT words. Its response is not a sample or mismatch pair. Its reads
are counted by PAIR_REARM_READS (including BOOT), and update last-DIAG/CNVRF.
All DMA starts/completions include these extra transactions. No ADC retrigger or
fake IRQ is generated. A selected edge still requires its own CNVRF=1 ACK before
any VSHUNT pair is accepted. An edge overlapping ACK can be consumed by that
ACK; a subsequent CNVRF=0 is a visible NOT_READY, never a fabricated sample.

The busy-edge baseline is now sampled **before issuing** BOOT/REARM, not after
processing its result in foreground. IRQs published after that baseline remain
available, including an edge after physical ACK but before foreground handles
the completion mailbox. Older busy notifications are counted and deliberately
discarded. The existing EXTI handler already skips the second HAL pending check
during the phase test; its flag handling, IRQ enablement and priorities were not
changed. Pending EXTI is not manually cleared during rearm. A truly missing
edge still reaches the existing bounded deadline.

## Timeout evidence

Run `spi pair wait` after completion/error (also available during RESTORE_FAILED).
PAIR_WAIT_CONTEXT_VALID=0 means no timeout was recorded. On timeout, before
DMA shutdown or register restoration, the saved fields are:

- WAIT_STATE, WAIT_PREVIOUS_STATE, WAIT_TIM2_US;
- WAIT_LAST_IRQ_US, WAIT_LAST_DIAG_US, WAIT_CNVRF;
- WAIT_PB1_LOW, WAIT_EXTI_PENDING;
- WAIT_IRQ_COUNT, WAIT_BUSY_EDGES, WAIT_SKIPPED_EDGES, WAIT_PAIRS;
- WAIT_SPI_SR, WAIT_DMA_STATE, WAIT_DMA_FLAGS.

State numbers: BOOT=1, WAIT_ALERT=2, selected DIAG=3, first delay=4, first read=5,
gap delay=6, second read=7, REARM=8. Timer fields are actual 32-bit TIM2 stamps;
the original report's elapsed time remains 64-bit. Zero last-IRQ means none has
been observed. PAIR_REARM_READS is also shown by this command, even without a
timeout. Context survives cleanup and remains until the next phase test. WAIT
errors remain separate from SPI/DMA errors and the 80 us transaction watchdog.

## Supplied timing analysis

| Interval | Supplied observation | Interpretation |
| --- | --- | --- |
| ALERT -> DIAG start | 39 us | Dispatch/selection/start delay; not an intentional FIRST delay |
| DIAG start -> end | 10 us | Observed transaction including DMA/IRQ/CS service, not just SPI clocks |
| DIAG end -> first start | 41 us | Foreground mailbox processing/state dispatch/start; desired age 40 us was already past when DIAG ended at age 49 us |
| First start -> end | 11 us | Observed first transaction/service interval |
| First end -> second start | 39 us | Foreground processing and next dispatch/start, despite zero requested gap |
| Second start -> end | 10 us | Observed second transaction/service interval |

The old FSM returned after consuming each response and advanced delayed-read
states on a later Process call. SPI DMA completion itself does not launch the
next phase transaction; the main loop does. Thus FIRST=40/GAP=0 are minimum
eligibility delays, not deadlines or guaranteed phase offsets. The supplied
timestamps locate the extra time in foreground dispatch but cannot partition
it exactly among USB/application/event work without additional profiling. The
new rearm fixes notification lifecycle, not that dispatch accuracy. No artificial
timestamp adjustment or unsupported claim of precise phase control is made.
Use actual event timings and histogram bins; do not infer phase from requested
delay alone. Normal Native's IRQ chain is unchanged.

## Repeat on the board

Load the ELF with selective erase, preserving sector 7; full-chip erase stays
disabled. Verify info=0.4.8-debug.2a, SPI=6 MHz, then in PuTTY:

```text
acq stop
spi pair vshct 84
spi pair latch 0
spi pair delay 40 0
spi pair test 10000
spi pair show
```

Wait for DONE, then save:

```text
spi pair report
spi pair wait
spi pair events 0
spi pair bins 0
spi pair bins 1
spi pair bins 2
spi pair bins 3
regs
measure
```

Expect 10000 completed pairs, zero WAIT/DMA/SPI/restore errors and restored
configuration. Mismatches are permitted diagnostic findings, not an abort.
Repeat latch 1, delay 0/0 and 40/100 (multiple busy conversions), then VSHCT150.
Check explicit stop, USB close/reconnect and Native controls:

```text
acq mode dma
acq capture native
acq profile set fast
acq alert adc 84
acq native latch 0
spi verify off
acq test 5
acq native report
```

Repeat Native150 using BALANCED and `acq alert adc profile`, then diagnostic
verification as in the previous guide. Compare rate/runtime loss, preserving
the supplied 59556-sample Native run as prior owner evidence, not a new result.

## Checks and scope

The host model now supports held ALERT until DIAG acknowledgement, rather than
only generating unconditional periodic edges. It completes 10000 pairs at 84 us
for both ALATCH settings with busy conversions, mismatches and no wait timeout;
it covers an IRQ retained across ACK completion/foreground dispatch. Existing
tests cover IRQs during reads/gaps, delayed dispatch, no-ready/missing-edge
timeout, DMA errors, cleanup retry and STOP (now including REARM). The production
CLI timeout report is checked with injected GPIO/pending state. Physical EXTI,
line release, DMA timing and Native performance still require board tests.

Build: make -j4; CTest: all 16 executables pass. CLI/GUI regression results are
recorded in the completion report. No Native/ALERT core, acquisition DMA backend,
watchdog, freeze logic, USB, CSF4, GUI, calibration or Flash layout changes.
Changed: pair_experiment_core.h/.c, pair_diagnostic.c, app.h/app.c (version/help),
tests/pair_diagnostic_test.c, tests/CMakeLists.txt, README and this guide.
No commit/push or board programming was performed.
