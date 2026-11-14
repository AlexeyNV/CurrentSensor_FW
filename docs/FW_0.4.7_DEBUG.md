# FW 0.4.7-debug: continuous ALERT-driven experiment

This firmware is prepared for board testing, not hardware validated. The power-on
path remains continuous TIM3/DMA, FAST, SPI 6 MHz. USB CDC lifecycle, CSF4 v1,
calibration, sector 7 and GUI archive format are unchanged.

## Audit and reason for the observed ALERT rate

Reference: [TI INA229 Rev. A](https://www.ti.com/lit/ds/symlink/ina229.pdf),
7.3.4, 7.3.7, 7.4 and tables 7-6/7-16. MODE=A continuously updates VSHUNT;
CNVR=bit14 routes CNVRF=bit1 to ALERT. APOL=0 is active-low open drain;
ALATCH=0 is transparent. Reading DIAG_ALRT clears CNVRF. Writing ADC_CONFIG
restarts conversion; no repeated ADC_CONFIG writes occur in ALERT_DRIVEN.
The datasheet says output registers are replaced at completed conversions and
reading does not stop conversion. It does not supply a guarantee that two SPI
transactions in continuous mode access the same conversion. There is no ADC
external synchronization input on this device.

FW 0.4.6 read DIAG_ALRT on TIM3 every 100 us. This limits flag/line rearming and
can merge several 50 us conversions into one observed assertion. Consequently
50,002 ALERT IRQs in 5 s do not establish that the ADC converted at 10 kHz.
The former SPURIOUS counter included GPIO already high when the ISR sampled it;
that is not evidence of electrical noise. Oscilloscope measurements of ALERT,
CS and SCLK are required to distinguish pulse width, coalescence and IRQ latency.
PB1 remains EXTI1 falling-edge, external pull-up required. The EXTI pending bit
is now cleared before publishing the event, avoiding a late clear of a new edge.

## Sequence

```text
INA continuous conversion completes -> ALERT falling
  -> EXTI: TIM2 stamp, GPIO sample, latest-event mailbox, IRQ counter
  -> main loop: DMA DIAG_ALRT (confirm CNVRF and rearm ALERT)
  -> slot selection from the actual event time
       skipped event: finish after DIAG; wait for next ALERT
       selected event: DMA VSHUNT_1 -> optional DMA VSHUNT_2 -> acquisition ring
TIM3: period/tick diagnostics only; never initiates a read in ALERT_DRIVEN
```

START performs one bootstrap DMA DIAG read to release a possibly asserted line;
that startup discard is not an acquired conversion. An event arriving during DMA
remains pending. The mailbox retains the latest event, counts overwritten events,
and never launches overlapping transfers. Every observed event is acknowledged
through DIAG, including events intentionally omitted from output. SPI BSY/TXE
completion and separate CS transactions use the existing DMA completion path.
STOP/disconnect abort DMA through the existing bounded stop path and restore
ADC_CONFIG and DIAG_ALRT control bits. A DMA transaction times out at 80 us.

The main loop remains responsible for dispatch. Thus USB/foreground latency can
delay DMA start; this experiment does not guarantee an upper bound before board
measurements. No SPI, USB, or formatting is performed by EXTI.

## ADC experiments and output selection

Only explicit RAM commands change experimental settings:

```text
acq capture alert_driven
acq alert adc profile
acq alert adc 50
acq alert adc 84
spi verify off
spi verify diagnostic
```

`profile` is the default and retains profile VSHCT. Override 50/84 changes only
VSHCT in ALERT_DRIVEN; it does not mutate the profile table. FAST configurations
are AA28 (50 us), AA68 (84 us); BALANCED with `profile` is AAA8 (150 us).
DIAG control is CNVR=1, ALATCH=0, APOL=0; status bits are dynamic. CONFIG and
hardware calibration are not written. `acq alert report` shows session ADC config.
Overrides differing from the selected profile (including FAST 84 us) are currently
standalone `acq test` experiments: binary streaming rejects them explicitly to
avoid labeling a changed ADC configuration with unchanged CSF4 profile metadata.
Use `acq alert adc profile` for the ten-minute CSF4 test.

Output selects at most one event per nominal output interval:
`slot = floor((event_time64 - session_start64) / profile_period_us)`.
The first confirmed event in each new slot is selected; later events in the same
slot are decimated. Empty slots remain gaps. RAW is never interpolated. TIM2
rollover is projected onto the existing 64-bit session clock. CSF4 stores the
actual **first read completion** timestamp, not the nominal slot time.

At 50 us conversion this tends to select every second notification for FAST.
At 84 us it produces mostly 84 us and 168 us intervals; at 150 us with 200 us
output slots it produces 150 us and 300 us intervals. ADC/MCU clock drift and
dispatch latency change the pattern. Strict uniform 100 us timing and guaranteed
ADC phase alignment are not simultaneously supplied by this hardware interface.
Observed internal conversions can coalesce before DIAG clears the flag; the
firmware reports their total as UNKNOWN rather than deriving it from TIM3 ticks.

At 6 MHz the wire budget is 4 us for DIAG plus 5.33 us per VSHUNT read:
9.33 us single / 14.67 us double per selected event, plus DMA, IRQ, CS and main
loop overhead. Skipped notifications still cost 4 us. These are wire times,
not measured service bounds. At 20 kHz notifications and 10 kHz output the wire
occupancy is approximately 133 ms/s single or 187 ms/s double. 84 us reduces
notification/rearm load but changes bandwidth/noise and output jitter. Noise and
anomaly-rate comparisons require the same real input and capture duration.

The first read must complete before `VSHCT - 10 us` from the IRQ timestamp, and
its event must follow the previous accepted read. Late results are omitted with
an overrun and slot-loss counter. This is a conservative software age check,
not proof of ADC phase: EXTI service itself can be delayed, and the datasheet
does not promise an atomic multi-transaction snapshot. Validate latency using
ALERT/CS/SCLK before accepting this experiment for production.

## Diagnostics and verification

After Stop, query `acq alert report` and `spi verify report`.
CONVERSION_READY_EVENTS counts CNVRF-positive DMA acknowledgements, not all
internal conversions. ALERT_IRQ_COUNT includes captured edges even when GPIO is
already high; ALERT_GPIO_HIGH_AT_ISR identifies that observation. REARM_COUNT
counts completed event DIAG transactions; REARM_FAILURES includes CNVRF=0 or
failed acknowledgements. CONVERSION_EVENTS_SKIPPED is intentional slot decimation.
CONVERSION_EVENT_OVERRUNS counts mailbox overwrites and rejected late reads.
ALERT_TO_SPI_START_US is the maximum edge-to-first-VSHUNT start latency;
ALERT_TO_READ_COMPLETE_US and MAX_ALERT_READ_CYCLE_US cover primary and full cycle.

VERIFY_TOTAL/MATCH/MISMATCH describe completed pairs. Every continuous pair,
including a matching pair, increments VERIFY_UNCERTAIN_CONVERSION: flags and edge
counts cannot prove that both reads access one internal conversion. REJECT is
disabled in ALERT_DRIVEN. DIAGNOSTIC always retains the first valid RAW. The first
16 mismatches/large codes (>50 mA absolute, diagnostic threshold only) retain both
24-bit RAWs, signed codes, RX bytes, read timestamps/delta, ALERT stamp, slot,
CNVRF readback, and IRQ counts at each read. IRQ counts can themselves coalesce;
zero intervening edges is not proof of no ADC update. Status 8 marks an anomaly
context; status 7 in the last-error context marks a late primary read. Equality
does not certify the analog conversion. SPI/DMA errors remain separate counters.

## Board sequence

1. Check 6 MHz with `spi config`; acquire the previous TIM3/DMA baseline first.
2. `acq mode dma`, `acq profile set fast`, `acq capture alert_driven`,
   `acq alert adc profile`, `spi verify off`, `acq test 5`.
3. Query both reports. Repeat with `spi verify diagnostic`.
4. Repeat FAST explicitly with `acq alert adc 84`; compare throughput, intervals,
   latencies, overruns and noise against 50 us. Record actual ADC_CONFIG.
5. Set `acq profile set balanced`, `acq alert adc profile`; repeat without and
   with double reads. Target is 5 kHz average, not a synthesized uniform grid.
6. Exercise START/STOP, disconnect/reconnect, `ping`, `measure`, `regs`, `cal show`.
7. Close PuTTY after selecting the experiment; run
   `python tools/stream_capture.py --port COM18 --profile fast --acq-mode dma --seconds 600 --output recordings/alert_fast_600s.csv`.
   Confirm END, actual ALERT_DRIVEN mode in stream status, CRC/frame integrity,
   slot gaps and real timestamp intervals. Query ALERT/VERIFY reports afterward.
8. Compare anomaly counts per sample and raw contexts with the TIM3 baseline.
   Restore `spi verify off`, `acq capture continuous` after the experiment.

Host tests cover scheduler rates at 50/84/150 us, mailbox overwrites, false ready,
64-bit rollover, startup stale events, and restart; existing controller, DMA,
transport and calibration tests remain required. Electrical timing and the
ten-minute archive test are pending hardware work.
