# FW 0.4.8-debug: experimental Native continuous acquisition

Native is a separate RAM-selected `acq capture native` mode. It is prepared for
standalone board tests, not validated at 20 kHz. The stable continuous TIM3/DMA
path remains the reset default. The repaired generation-aware watchdog and
commit-once DMA path are reused; their timeout/ownership logic is unchanged.
SPI remains 6 MHz. CSF4 v1, USB CDC lifecycle, GUI, archives and calibration/
sector 7 are not changed. Native streaming is rejected before acquisition starts.

## Audit: what ALERT and the output register can establish

[TI INA229 Rev. A](https://www.ti.com/lit/ds/symlink/ina229.pdf), sections 7.3.4,
7.3.7 and table 7-16: CNVR=1 routes conversion-ready onto ALERT. APOL=0 is
active-low open drain. ALATCH=0 is transparent; ALATCH=1 holds a latched alert
until DIAG_ALRT is read. The conversion narrative says reading DIAG_ALRT clears
CNVRF; the flag table explicitly describes clearing for ALATCH=1. Conversion
continues and replaces output registers independently of reading them. No
explicit guarantee of a conversion-frozen, atomic 24-bit SPI snapshot was found.

Thus an edge is a notification, not a FIFO element containing one unread RAW.
Several conversions may coalesce before acknowledgement. Conversely a pending
edge may already have been acknowledged by another DIAG transaction. A CNVRF=1
read establishes a positive acknowledgement, not proven identity between that
IRQ and the RAW subsequently returned. Matching double reads do not prove ADC
correctness or independence either. These limits are retained explicitly.

```text
ADC:       conversion A ends          conversion B ends          C ends
VSHUNT:    A ------------------------ B ------------------------ C
ALERT:     edge A       [line may stay asserted / events may coalesce]
EXTI:          service timestamp A -> enqueue IRQ id, TIM2, GPIO observation
main/DMA:              DIAG: CNVRF snapshot/clear -> VSHUNT1 -> [VSHUNT2]
                           ^ B can complete during DIAG or either read ^
```

IRQ service timestamps are software observations of edges, not hardware input
capture timestamps. The first SPI-start stamp is taken immediately before CS
assertion; completion is taken when the existing DMA/SPI-BSY completion path
receives the result. These software stamps have microsecond resolution and
small instrumentation/IRQ latency. The watchdog retains its original transaction
start reference and 80-us limit; new CS timing instrumentation does not alter it.

The prior quarter-zero CNVRF pattern is not declared fixed. Native removes slot
decimation, but it does not turn zero confirmations into samples. The first 32
acknowledgement contexts and the zero-before-previous-rearm counter remain
available to investigate whether notifications were already covered by a
previous read. ALATCH can now be explicitly compared in Native with
`acq native latch 0` / `acq native latch 1` (default 0, RAM only). Prior DIAG
control bits and ADC_CONFIG are restored on Stop/error through the existing path.

An additional software edge-loss path was found in the existing EXTI wrapper:
it cleared pending and published the event, then called HAL_GPIO_EXTI_IRQHandler,
which could clear a NEW pending edge through its empty weak callback. Native
returns after its own clear/publication; a new edge remains pending for the next
IRQ entry. Other acquisition modes retain their prior EXTI path. This removes a
specific double-clear race but is not proof of the physical quarter-zero cause.
NATIVE_GPIO_HIGH_AT_ISR and the GPIO level in the DIAG trace support correlation.

## State machine and data identity

1. EXTI publishes timestamp, IRQ id and observed GPIO level into a 16-entry SPSC
   queue. It performs no SPI, text formatting or USB operation. A full queue drops
   the newest notification and increments a separate counter.
2. When DMA is idle, the main loop consumes the oldest queued event and starts
   one DIAG_ALRT DMA transaction. A bootstrap DIAG read at START releases an
   already-asserted ALERT; it is excluded from Native confirmation counters.
3. CNVRF=0 ends that notification without a sample or an inferred ADC loss.
   CNVRF=1 permits the first VSHUNT DMA read. No 100/200-us target-slot test runs.
4. Successful primary data are retained, or an optional second independent
   VSHUNT DMA transaction completes first. The existing commit-once path inserts
   the primary RAW and its actual receipt timestamp into the acquisition ring.
5. The accepted-result sequence advances once per committed primary (starts at
   zero), independently of IRQ id and TIM3. Failed/unconfirmed notifications do
   not fabricate sequence entries. Their losses/uncertainties are separate.

Repeated or backwards IRQ timestamps are not accepted as distinct notification
identities, even if a delayed acknowledgement is positive. That conservative
notification check is counted; it is not a test for equality of RAW values.
It can omit a newer conversion covered by an old notification, so no claim is
made that all internal conversions are recovered. Genuine constant RAW values
from distinct notifications are allowed. DMA start failures release the event
instead of leaving the state machine waiting forever.

TIM3 only diagnoses elapsed periods. It never starts a Native read and contributes
no output-slot index or loss count. Internal ring timestamps remain first-read
receipt times: association with the exact ADC-ready event is unproved, so the
firmware does not relabel the IRQ stamp as the measurement time. Native result
indices and counters are 32-bit per session; TIM2 age differences wrap safely,
and session elapsed uses the existing 64-bit timebase.

## Commands and measurement records

```text
acq capture native
acq native latch 0|1
acq alert adc profile|50|84
acq native report
acq native trace
acq event report
spi verify report
acq dma report
```

ADC override applies only to the experimental ALERT-driven/Native paths. `profile`
retains VSHCT from the profile table: BALANCED=150 us, FAST=50 us. FAST override
84 uses ADC_CONFIG=AA68; FAST 50 uses AA28; BALANCED profile uses AAA8. MODE stays
continuous shunt-only, averaging 1, ADCRANGE=0. Actual session config, VSHCT and
ALATCH are printed. Native test/status reports do not advertise profile output
periods as actual Native sample periods. `spi verify reject` is prohibited.

`acq native trace` stores the first 32 accepted-result timing contexts: sequence,
IRQ id, ALERT stamp, primary CS-start/first-read completion, age and IRQ counts
at read start/end. FLAGS bit 0 means association/atomicity unproved (always set),
bit 1 means age >= conversion period, bit 2 means another serviced IRQ during
the read, bit 3 means GPIO was high at EXTI service, bit 4 means queued events.
No timing flag filters, replaces or interpolates a RAW value.

`spi verify report` retains the first 16 Native transaction contexts, including
matches: full RAW1/RAW2, signed codes, both four-byte RX buffers (command-byte RX
is diagnostic only), first/second SPI-start and completion times, interval, ALERT
stamp, CNVRF snapshot and serviced IRQ counts. FIRST_VALID/SECOND_VALID distinguish
absent or failed reads from legitimate zero RAW. UPDATE_POSSIBLE remains true.
With verify off there is no valid second read. Continuous mismatches remain
VERIFY_UNCERTAIN_CONVERSION and do not trigger rejection. A secondary read error
still retains the valid primary through the existing error recovery path.

## Counter semantics

| Field | Meaning |
| --- | --- |
| NATIVE_ALERT_EVENTS | Physical EXTI notifications captured during the armed session, including queue drops |
| NATIVE_CONFIRMED_READY / NATIVE_CNVRF_ZERO | Completed event acknowledgements with flag 1 / 0; bootstrap excluded |
| NATIVE_PRIMARY_READS | Successful first VSHUNT transactions; may include a primary pending verification at Stop |
| NATIVE_SAMPLES | Accepted primary results, equal to SAMPLES |
| NATIVE_RESULT_UNCERTAIN | Accepted results without proved conversion/IRQ association; conservatively all accepted results |
| NATIVE_EVENTS_LOST | Known observed notifications omitted by queue overflow, I/O failure, duplicate identity or Stop truncation |
| NATIVE_RUNTIME_EVENTS_LOST | Same, excluding intentional Stop tail truncation |
| NATIVE_STOP_DISCARDED | Unconfirmed/unfinished current notification and queued notifications at Stop |
| NATIVE_DMA_OVERRUNS | Completed/failed/aborted service cycles whose edge-to-finish age reaches a conversion period; not a fabricated count of overwritten ADC results |
| NATIVE_ALERT_WHILE_BUSY | Notifications arriving while ACK/read processing is active; queued events are not automatically lost |
| NATIVE_REARM_FAILURES | Actual failed acknowledgement transaction, excluding normal CNVRF=0 |

INTERNAL_CONVERSIONS_LOST is UNKNOWN: flags do not encode how many ADC updates
coalesced. EVENTS_LOST is a notification accounting measure, not a count of all
missing ADC conversions. Actual rate is accepted results / 64-bit elapsed time;
nominal ADC rate is printed separately. Status OK means the command observed no
known runtime I/O/queue/invariant error; NATIVE_TEST_RESULT is OBSERVATION_ONLY,
and OK does not certify nominal rate or ADC independence. Stop-tail discards
remain visible without being reclassified as runtime I/O failures.

MIN/MAX/P99 latency use first-read completion minus IRQ service stamp. P99 is
nearest rank from 1-us histogram bins 0..254; if its rank falls in the overflow
bin, the observed maximum is returned as an explicit conservative bound.
NATIVE_MAX_SERVICE_CYCLE_US includes the complete optional second read and aborted
in-flight service at Stop. Strict sample/statistics invariants are checked after
Stop, with the existing one-million-sample statistics cap and ring-overflow rule.
Live status is observational and is not an atomic snapshot of all counters.

## Critical budget and achievable rate

DIAG at 6 MHz needs 4 us of clocks; one VSHUNT read 5.33 us. The nominal single/
double chain is 9.33/14.67 us on the wire, before main-loop dispatch, DMA/IRQ, CS,
queue and instrumentation time. The critical bound is completing service and
rearming without coalescing later ADC updates; an 80-us transaction watchdog is
not a guarantee that a 50-us conversion stream can be consumed losslessly.

The supplied hardware maximum 82 us already exceeds FAST's 50-us period. An
ALERT-to-first-read delay around 57 us means the register may have been updated
before that first read. A 13..15-us read-pair interval can then straddle another
update. Native does not explain those mismatches away or certify each conversion.
With the earlier confirmation ratios, Native might still deliver only about
5,000 BALANCED confirmations/s despite ~6,667 IRQ/s. That deficiency must remain
visible until service timing is understood. 84 us and 150 us offer more budget;
none of these rates is claimed hardware confirmed by this implementation.

## Board procedure, no streaming

Program `build/CurrentSensor_FW.elf` using selective erase with sector 7 preserved
and full-chip erase disabled. Check info=0.4.8-debug / native-alert build and
`spi config`=6 MHz. Start with a known steady input:

```text
acq mode dma
acq capture native
acq profile set balanced
acq alert adc profile
acq native latch 0
spi verify off
acq test 5
acq native report
acq native trace
acq event report
acq dma report
```

Repeat BALANCED with `acq native latch 1` and compare CNVRF/IRQ/rearm traces; then
restore latch 0 for the rate comparison. Next select FAST, `acq alert adc 84`,
and repeat the same single-read test. Finally select `acq alert adc 50` and repeat.
Save all reports and compare effective/nominal rate, known losses, zero flags,
age histogram, overbudget cycles and uncertainty. Do not treat IRQ count as
sample count. After the single-read behavior is understood, repeat each setting
with `spi verify diagnostic` and query `spi verify report` for both RAW words.

Use a logic analyzer on ALERT, CS, SCLK and MISO. Decode command 0x2D for DIAG_ALRT
and 0x11 for VSHUNT. Trigger on an ALERT falling edge; measure assertion/release,
DIAG clock window, first/second VSHUNT windows and whether another edge appears
during them. Compare ALATCH=0/1 and correlate early contexts in the event/timing
reports. Such traces can validate the acknowledgement mechanism; absence of an
observed extra edge still does not prove absence of an internal ADC update.

Check repeated START/STOP, pending Stop, USB close/reconnect, then `measure`,
`regs`, `cal show`. Restore `spi verify off`, `acq alert adc profile`, and
`acq capture continuous` for the standard firmware behavior. `stream start` in
Native returns DIAGNOSTIC_MODE_UNSUPPORTED; GUI and archive work are deferred.

Host coverage includes ideal 50/84/150-us notifications, zero confirmations,
duplicate edges, missing events, updates/IRQs during reads, delayed callbacks,
queue overflow, sequence/ring/TIM2 wrap, all Stop phases, late callbacks after
Stop, histogram overflow, and the real controller's mode/streaming/latch/restore
guards. Ideal rates in those tests are state-machine results, not board results.

For five-second ideal event streams starting with an event at t=0, host results
are 100,000 accepted at 50 us, 59,524 at 84 us, and 33,334 at 150 us, with no
observed-event loss. All accepted results still carry the uncertainty flag.
The C suite has 14 passing executables; existing CLI and GUI suites have 29 and
94 passing tests respectively. None of these results validates physical Native
throughput or resolves the reported CNVRF=0 mechanism.
