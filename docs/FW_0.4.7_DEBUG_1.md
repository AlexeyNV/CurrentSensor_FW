# FW 0.4.7-debug.1: ALERT-driven slot repair

This supersedes the slot accounting and age-discard rules in the 0.4.7 guide.
The firmware is built for standalone board tests; 10 kHz is not hardware confirmed.
Continuous TIM3/DMA, profiles, SPI 6 MHz, calibration/sector 7, CSF4, GUI archive
and USB lifecycle are preserved. ALERT-driven streaming is explicitly blocked
in this build until the standalone hardware tests succeed. No reject policy is
enabled. FW_BUILD starts with `alert-slot-` and contains compilation date/time.

## Confirmed code defects

The old call `AlertDriven_Take(c, Timebase_NowUs64())` samples now64 before the
function snapshots the EXTI mailbox. An intervening IRQ can publish a timestamp
one microsecond newer than now. Subtracting their unsigned 32-bit difference
projects that event one TIM2 wrap into the past; at short uptime the subsequent
64-bit subtraction underflows. The resulting enormous output-slot candidate
advances next_slot, suppressing all subsequent normal candidates.

The regression reproduces the reported loss counter exactly with synthetic
values: start=0, now=4,542,000 us, IRQ stamp=4,542,001 us yields a legacy
32-bit slot/loss value of **4,080,264,351**. This demonstrates the defect; it does
not reconstruct the actual board's interrupt timing from the aggregate report.
The supplied board values READY=52,317 and SKIPPED=47,745 differ by exactly the
4,572 selected samples, consistent with selection suppression. New code defers
a future snapshot WITHOUT consuming the mailbox, then retries with a newer
clock. Both 32-bit age and 64-bit subtraction are validated before projection.

A second defect consumed the slot in Ready before VSHUNT succeeded. DMA failure
or the former 40-us age rejection could then block a valid second event from
the same output interval. Selection now reserves a slot; only actual sample
receipt commits it. Failures release the reservation and allow a retry. The age
threshold is now observation-only (EVENT_EXPIRED): it does not discard RAW.
An event at/before a previous accepted first-read completion is rejected before
starting another read, to avoid treating an already-read conversion as new.

## State transitions and counters

| Condition | Action | Accounting |
| --- | --- | --- |
| EXTI edge, any GPIO level | Store timestamp and generation | IRQ count; GPIO-high diagnostic |
| Edge during DMA | Leave latest event pending | EVENT_WHILE_DMA_ACTIVE; mailbox overwrites separate |
| No new generation | Wait | No slot loss |
| IRQ timestamp newer than now snapshot | Retry next foreground pass | EVENT_CLOCK_RETRIES; mailbox retained |
| Stale pre-session timestamp | Consume notification, no SPI | EVENT_WRONG_SLOT |
| Idle DMA + pending event | Start DIAG DMA | CNVRF_READS |
| DIAG CNVRF=0 | Finish rearm; await next event | CNVRF_NOT_READY / EVENT_ALREADY_USED; no lost slot |
| DIAG CNVRF=1, candidate already committed | Finish rearm, decimate | EVENT_WRONG_SLOT / CONVERSION_EVENTS_SKIPPED |
| Ready event at/before previous first read | Finish rearm, do not reread old result | EVENT_ALREADY_USED |
| Ready + new slot | Reserve and launch first VSHUNT DMA | EVENT_SELECTED (attempts, not samples) |
| DMA start/transfer error | Release reservation | SPI/DMA counters; SPI_DMA_START_FAILED only on failed launch |
| First read older than age budget | Record latency context, retain RAW | EVENT_EXPIRED; no automatic rejection |
| Successful primary / completed diagnostic pair | Store first RAW and its actual completion time | SAMPLES; commit slot |
| Secondary read fails in diagnostic | Keep valid primary | SAMPLES and VERIFY_SECOND_READ_ERRORS |
| STOP/disconnect | Abort DMA, restore registers, finalize empty slots | TARGET_SLOT_EMPTY |

The first 32 confirmed events with a rejection/failure reason are retained
in RAM and printed only by `acq alert report` after Stop. Trace states: 1 ready
without reservation, 2 reserved/read phase. Reasons: 1 previously used event,
2 committed/old slot, 3 reservation busy, 5 transfer failure. Expired latency is
counted separately because it does not reject RAW. Normal decimation can fill
the trace before later failures.

## CNVRF audit

[TI INA229 Rev. A](https://www.ti.com/lit/ds/symlink/ina229.pdf) states that reading
DIAG_ALRT clears CNVRF and a new completed conversion sets it. The ADC continues
independently of SPI. CNVR is enabled; ALATCH=0 and APOL=0 are preserved.
An IRQ captured during a preceding DIAG transaction can remain pending even
though that transaction already acknowledged its flag. Reading again can return
CNVRF=0. This is a plausible source of the nearly half-zero results, not proof
that half the conversions were lost. GPIO-high at ISR service is likewise not
proof of electrical noise. `CNVRF_ZERO_IRQ_BEFORE_PREV_REARM` counts zero-flag
notifications whose timestamp precedes the previous DIAG completion; compare it
against CNVRF_NOT_READY on the board to test this explanation. Other zero cases
remain unexplained until timing is captured externally.

Rearm failures now count SPI/DMA acknowledgement failures, not CNVRF=0 or normal
decimation. Queued notifications are still acknowledged: dropping them merely
because they overlap DIAG could leave ALERT asserted and stop further edges.
Internal completed-conversion totals remain UNKNOWN; neither IRQ nor TIM3 counts
replace them. Double-read diagnostic behavior is unchanged; continuous pairs
remain VERIFY_UNCERTAIN_CONVERSION and mismatches do not cause rejection.

## Output slots and bounded loss arithmetic

Event slots use elapsed IRQ time divided by the selected 100/200-us period. At
Stop, the accounting boundary includes a partial final slot:
`N = ceil(acquisition_elapsed_us / period_us)`. Thus for normal-size sessions:
`SAMPLES + LOST_TARGET_SLOTS = TARGET_SLOTS_ACCOUNTED = N`.
An exact five-second run has 50,000 FAST slots; 5,000,001 us has 50,001 slots.
Empty bins are counted once, independently of ADC notifications and failed retry
attempts. Lost TIM3 IRQ periods are diagnostics only in ALERT-driven mode.
Loss values saturate at UINT32_MAX for extremely long sessions, rather than wrap.
Timestamps in CSF4 still reflect first-read completion, never a fabricated grid.

The synthetic scheduler regression feeds 100,000 50-us events into five seconds:
50,000 accepted / 0 lost; phase 25 us and jitter ±5 us also give 50,000 / 0.
Removing both events from one bin gives 49,999 / 1. The same tests cross TIM2
rollover and run with a 64-bit start beyond two wraps. The original underflow,
duplicate/zero notifications, overwritten pending events, DMA retry in the same
slot, expired latency without discard, STOP/restart and slot boundaries are tested.
These are host/controller tests, not evidence of actual ADC/IRQ throughput.

## Board procedure and budget

1. Program `build/CurrentSensor_FW.elf` using the usual CubeProgrammer selective
   erase procedure. Keep full-chip erase disabled and preserve sector 7.
2. Check `info` for 0.4.7-debug.1 and new FW_BUILD; check `spi config` for 6 MHz.
3. Run:

```text
acq mode dma
acq profile set fast
acq capture alert_driven
acq alert adc profile
spi verify off
acq test 5
acq alert report
spi verify diagnostic
acq test 5
acq alert report
spi verify report
```

Expect about 50,000 samples and 10 kHz; compare lost slots against the documented
boundary count, not IRQ count. Require zero SPI/DMA errors, explain all long gaps,
and save the full reports including rejection trace and zero/rearm correlation.
Run repeated START/STOP, `measure`, `regs`, `cal show`, and reconnect checks.
Do not run ALERT-driven streaming yet: the firmware returns
DIAGNOSTIC_MODE_UNSUPPORTED. The established TIM3-driven streaming remains usable.

At 6 MHz, DIAG costs 4 us of clocks and each VSHUNT transaction 5.33 us. At
20 kHz notification rate / 10 kHz output, wire occupancy is about 133 ms/s
single or 187 ms/s double. Per-selected-event wire time is 9.33/14.67 us, leaving
foreground, DMA/IRQ and CS overhead to measure against each 100-us slot. There is
no host-derived worst-case service guarantee. Hardware must verify both throughput
and the absence of unexplained millisecond gaps before streaming is enabled.
