# Native acquisition feasibility (analysis only)

Native would emit one sample per independently confirmed new INA229 result,
without selecting nominal 100/200/400/1000-us output bins. It is not implemented
in FW 0.4.7-debug.2, and no GUI or wire-format change is made here.

| Profile | ADC nominal period | Native nominal rate | DATA bandwidth, 32 samples/frame |
| --- | ---: | ---: | ---: |
| FAST | 50 us | 20,000 Hz | 142,500 B/s |
| BALANCED | 150 us | 6,666.7 Hz | 47,500 B/s |
| PRECISION | 280 us | 3,571.4 Hz | 25,446 B/s |
| LOW_NOISE | 540 us | 1,851.9 Hz | 13,194 B/s |

CSF4 DATA frames are 32-byte header + 6 bytes/sample + 4-byte CRC = 228 bytes
for 32 samples. Rates above exclude CONFIG/STATS/END and USB packet overhead.
USB FS CDC has plausible bus bandwidth, but available bandwidth does not prove
that the current IRQ, transport scheduling and host writer sustain those rates.
FAST needs about 625 DATA frames/s, each spanning four 64-byte USB packets. The
2048-entry transport ring is only about 102 ms of Native FAST data, with the
1023-usable-entry acquisition ring adding about 51 ms. A stalled PC still needs
explicit backpressure/loss policy; an in-flight TX buffer must remain immutable.

At SPI 6 MHz, DIAG plus primary read costs 9.33 us on the wire; adding verification
costs 14.67 us. At Native FAST that is about 187/293 ms of SPI clocks per second,
plus 40,000/60,000 transaction completions, 20,000 EXTI events, dispatch and USB.
The reported maximum full verification cycle of 75 us exceeds the 50-us Native
period. Thus lossless Native FAST with double reading is NOT established. All
profiles require measured completion distributions under real USB traffic,
including worst observed gaps and bounded recovery; slower profiles have more
budget but are not automatically proven safe.

An EXTI edge alone cannot define a new Native sample. The BALANCED hardware
report has ~6,667 IRQ/s but only ~5,000 CNVRF-positive reads/s. Reusing IRQ count
as sample count would hide precisely that uncertainty. Native requires a verified
flag/ALERT acknowledgement sequence, detection of mailbox overwrite/coalescence,
and assurance that the primary result has not already been consumed. Equality of
two continuous reads cannot certify either distinct conversions or ADC accuracy.
Both reads may straddle an update, even when no second edge was serviced.

Use actual first-read completion time for the existing receipt timestamp, retain
ALERT and service latencies separately, and keep the 64-bit session extension of
TIM2. Exact internal ADC conversion time cannot be inferred from an ISR stamp.
A Native index would enumerate accepted confirmed results; counts of missing
internal conversions remain unknown where flags coalesce. The current target-slot
loss meaning must therefore be explicitly distinguished from Native sequence loss.

CSF4 v1 structurally carries all proposed integer periods (50,150,280,540 us),
32-bit slot indices, real timestamp deltas and RAW codes. Its existing profile
identity and PC validation, however, expect the present output periods. Reusing
those identities while silently changing rate would mislabel records. A future
Native feature needs explicit mode/configuration negotiation, documented index
and loss semantics, verified host parser/metadata support, and then GUI work.
No such compatibility change is silently made in this debug firmware.

Proceed first with stable single-read rearm/confirmation evidence; then benchmark
Native without USB, then bounded USB captures and disconnect/overflow recovery.
Treat double reading as optional diagnostic load until its timing is measured.
