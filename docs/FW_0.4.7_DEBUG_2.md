# FW 0.4.7-debug.2: DMA watchdog ownership and BALANCED evidence

This is a board-test build. FAST timeout recovery is repaired in code; zero
hardware errors have not yet been demonstrated. The BALANCED one-third loss is
reproduced and explained below, but its physical CNVRF service sequence has NOT
been proved fixed. No RAW filter, reject, interpolation, shifted sample time,
USB lifecycle, CSF4, profile-table or calibration/sector-7 change is introduced.
ALERT-driven streaming remains blocked while standalone validation is pending.

## FAST: actual race and sample-count consequences

The old foreground watchdog evaluated pair_active/state, Now(), and transaction
start independently. RX completion could finish a transaction or start VSHUNT2
between those reads. A start timestamp newer than the sampled Now underflowed
the subtraction; alternatively a valid overdue decision became obsolete before
Fail(true). Fail inspected/retained the first sample BEFORE disabling owning
IRQs, so completion could concurrently retain that same primary and modify the
ring producer index. This can produce timeout counts even when every launched
transaction completed, extra SAMPLES, a repeated first-read timestamp, zero
MIN_SAMPLE_PERIOD, and fewer ring-consumed STATS_SAMPLES. These are code-level
reproduced races consistent with the supplied 51 timeouts and 7 extra samples;
the aggregate hardware report does not identify each of those 51 interleavings.

Every transaction now has a generation and its completed generation/time. The
watchdog samples state/generation/clock in a short interrupt-protected decision.
It ignores completed generations, stale generations, a future start timestamp,
idle state and a pending RX transfer-complete flag. A genuine timeout claims
the owning DMA RX/TX and TIM2-settle IRQs BEFORE interrupts are restored. Recovery
runs with USB/EXTI enabled; those handlers cannot mutate DMA ownership. It then
restores the owning IRQ enable states. The 80-us timeout is unchanged.
WATCHDOG_MAX_CRITICAL_US reports the decision's measured masking interval.

Primary sample insertion is idempotent per pair. Normal completion and the
diagnostic secondary-error path use the same commit-once operation. STOP disables
owning callbacks before examining the active pair. First RAW and first-read
timestamp are retained after a secondary read error; double-read comparison and
its continuous-conversion uncertainty classification are otherwise unchanged.

`acq dma report` retains the first 16 genuine timeout contexts: numeric transaction
type (1 DIAG, 4 VSHUNT1, 5 VSHUNT2; see enum for older modes), slot, event counter,
generation, start/timeout times, SPI SR, DMA CR/remaining bytes/flags, last callback
(1 RX, 2 TX error, 3 TIM2 settle), last completed generation/time. A timed-out
generation has no successful finish; LAST_FINISH describes the preceding
completion, and FINISHED_GEN disambiguates it. All output is after Stop.

`SUCCESSFUL_PRIMARY_VSHUNT` counts successful first-read transactions; a primary
pending verification at STOP can therefore exceed accepted output by one.
PRIMARY_DUPLICATE_ATTEMPTS and TIMESTAMP_ORDER_ERRORS must be zero. After a
standalone stopped run with no ring overflow and <=1 million samples, SAMPLES,
ACCEPTED_OUTPUT_SAMPLES and STATS_SAMPLES must agree. The ALERT report's
COUNTER_INVARIANT_ERRORS checks that condition and accepted <= selected attempts.
CONFIRMED_EVENTS_WITHOUT_OUTPUT is confirmed-ready minus accepted samples; it
includes intentional decimation and interrupted/failed selected events, not just
errors. Empty target slots retain the documented ceil(elapsed/period) boundary.

## BALANCED: distinguish selection from confirmation service

For ideal ready events every 150 us, the existing one-result-per-200-us-bin rule
produces 25,000 samples / 0 empty bins in five seconds. It does NOT intrinsically
limit this profile to 3.33 kHz. Host tests vary DMA delay from 5 to 40 us and
retain real read completion timestamps.

A counterexample reproduces the reported rejection sequence exactly. Let IRQs
occur at `50 + 150*i` us and every fourth notification (`i%4==0`) return CNVRF=0.
For one six-event segment:

| IRQ us | CNVRF | 200-us slot | Result |
| ---: | ---: | ---: | --- |
| 50 | 0 | 0 | no confirmed result |
| 200 | 1 | 1 | accept |
| 350 | 1 | 1 | normal same-slot decimation |
| 500 | 1 | 2 | accept |
| 650 | 0 | 3 | no confirmed result |
| 800 | 1 | 4 | accept |
| 950 | 1 | 4 | normal same-slot decimation |
| 1100 | 1 | 5 | accept |

This yields 16,666 accepted / 8,334 empty slots, and rejections 1,4,7,10,13.
It is an aggregate-compatible synthetic waveform, NOT a captured trace from
the board. Small phase differences, oscillator drift and startup boundaries
explain why its counts need not exactly equal 16,645 / 8,356. Both mechanisms
are required: a quarter of notifications lack CNVRF, then a third of the
remaining confirmations share already-filled output bins. At other phases the
same zero proportion need not cause the same output loss.

Moving the 350-us result into missing slot 0 would backfill a different time
interval; accepting both in slot 1 would violate the existing output-slot rule.
Neither is done. No arbitrary phase shift or quota counter masks this loss.
The physical cause of the periodic CNVRF=0 still needs timing evidence.

[TI INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf), section 7.3.4,
says DIAG_ALRT reads clear CNVRF while conversion continues. A queued edge can
already have been acknowledged, or a new conversion can occur during servicing.
CNVRF=0 is not automatically an SPI failure or a lost output slot. GPIO-high at
IRQ service is not proof of noise. Current control remains CNVR=1, ALATCH=0,
APOL=0. No new rearm polling loop is added without hardware evidence.

`acq event report` records the first 32 serviced notifications, including zeros:
IRQ timestamp, DIAG completion timestamp, IRQ-based output slot, hypothetical
DIAG-completion slot, CNVRF, selection, prior accepted first-read time.
CONFIRMATION_SLOT_DIFFERENCES measures when callback latency crosses a slot
boundary. Actual selection remains IRQ-based; neither alternative slot nor
timestamp is silently substituted. Save this report alongside the rejected-event
trace and capture ALERT/CS/SCLK to establish which waveform occurred physically.

## Regression and board checks

Tests exercise completed/changed transaction generations at the timeout boundary,
old clock snapshots, genuine timeout before callback, commit-once, stop and TIM2
wrap. They exercise FAST 100,000 events -> 50,000 outputs, first-read delays
5..40 us, diagnostic double commit attempts, phase/jitter, and BALANCED with
both ideal and periodic-zero confirmation streams. These are deterministic core
and controller tests; they are not substitutes for physical IRQ timing.

Program `build/CurrentSensor_FW.elf` with the usual selective erase. Keep full-chip
erase off and preserve sector 7. Confirm `info` shows 0.4.7-debug.2 and a new
`dma-generation-...` build. Start with:

```text
spi config
acq mode dma
acq profile set fast
acq capture alert_driven
acq alert adc profile
spi verify off
acq test 5
acq alert report
acq dma report
acq event report
spi verify diagnostic
acq test 5
acq alert report
acq dma report
acq event report
spi verify report
```

Repeat both policies after `acq profile set balanced`. Require zero unexplained
SPI/DMA errors, zero duplicate/order/invariant errors, and matching sample counts.
For BALANCED compare the recorded CNVRF sequence against the two host models;
do not claim recovery to 5 kHz if actual confirmations still leave empty bins.
Then verify Stop, reconnect, ping/measure/regs/cal show and return to the stable
`spi verify off`, `acq capture continuous`. No ALERT streaming test is authorized
by a successful host test alone.

Wire budget at 6 MHz remains 4 us DIAG + 5.33 us per VSHUNT, or 9.33/14.67 us
per selected event before software/IRQ/CS overhead. The supplied FAST maximum
full verification cycle is 75 us; this is not the duration of one SPI transaction
and does not justify increasing an 80-us per-transaction watchdog. New contexts
will distinguish actual stalled transactions from completion-order problems.

Native feasibility is analyzed separately in [NATIVE_FEASIBILITY.md](NATIVE_FEASIBILITY.md).
