#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "alert_driven_core.h"

static uint32_t feed(AlertDriven_Core *c, uint64_t event, bool ready, bool fail)
{
    uint32_t slot;
    AlertDriven_Irq(c, (uint32_t)event);
    assert(AlertDriven_Take(c, event + 2U));
    if (!AlertDriven_Ready(c, ready, &slot)) return 0U;
    if (fail) AlertDriven_Failed(c, true);
    else AlertDriven_Accept(c, (uint32_t)(event + 20U));
    return !fail;
}
static void five_seconds(uint64_t start, uint32_t phase, bool jitter, bool missing)
{
    AlertDriven_Core c;
    uint32_t samples = 0U;
    AlertDriven_Start(&c, start, 100U);
    for (uint32_t i = 0U; i < 100000U; ++i) {
        int64_t t = (int64_t)i * 50 + phase;
        if (jitter) t += (int32_t)(i % 11U) - 5;
        if (t < 0 || t >= 5000000) continue;
        if (missing && t / 100 == 1234) continue;
        samples += feed(&c, start + (uint64_t)t, true, false);
    }
    AlertDriven_Finish(&c, start + 5000000ULL);
    assert(c.accepted == samples);
    assert(samples + c.missed_slots == 50000U);
    if (!jitter && phase < 50U) assert(samples == (missing ? 49999U : 50000U));
    else assert(samples >= (missing ? 49998U : 49999U));
    assert(c.next_slot <= 50000U);
    assert(c.trace_count <= 32U);
    printf("100000 ALERT events phase=%u jitter=%u missing=%u: samples=%u lost=%u\n",
           phase, (unsigned)jitter, (unsigned)missing, samples, c.missed_slots);
}
static void future_clock_race(void)
{
    AlertDriven_Core c;
    uint32_t slot;
    uint64_t old_now = 1000U;
    uint32_t future_event = 1001U;
    uint64_t broken = old_now - (uint32_t)((uint32_t)old_now - future_event);
    assert(broken > (1ULL << 63)); /* reproduce the former unsigned underflow */
    {
        uint64_t n = 4542000U;
        uint32_t stamp = 4542001U;
        uint64_t legacy = n - (uint32_t)((uint32_t)n - stamp);
        assert((uint32_t)(legacy / 100U) == 4080264351U);
        printf("legacy hardware-counter reproduction: %u\n", (uint32_t)(legacy / 100U));
    }
    printf("legacy race: projected=%llu lost32=%u\n",
        (unsigned long long)broken, (uint32_t)(broken / 100U));
    AlertDriven_Start(&c, 900U, 100U);
    AlertDriven_Irq(&c, future_event);
    assert(!AlertDriven_Take(&c, old_now));
    assert(c.handled == 0U && c.next_slot == 0U && c.future_snapshots == 1U);
    assert(AlertDriven_Take(&c, 1002U));
    assert(AlertDriven_Ready(&c, true, &slot) && slot == 1U);
    AlertDriven_Accept(&c, 1020U);
    assert(c.missed_slots == 1U && c.next_slot == 2U);
}
static void retry_false_ready_busy_stop(void)
{
    AlertDriven_Core c;
    uint32_t slot;
    AlertDriven_Start(&c, 1000U, 100U);
    assert(!feed(&c, 1000U, false, false));
    assert(c.rearm_failures == 0U && c.missed_slots == 0U);
    assert(!feed(&c, 1020U, true, true));
    assert(c.next_slot == 0U && c.start_failed == 1U);
    assert(feed(&c, 1050U, true, false)); /* same slot may retry after DMA failure */
    assert(!feed(&c, 1050U, true, false)); /* duplicate event */
    AlertDriven_Irq(&c, 1100U);
    AlertDriven_Irq(&c, 1150U); /* mailbox overwrite during active SPI */
    assert(AlertDriven_Take(&c, 1152U) && c.overruns == 1U);
    assert(AlertDriven_Ready(&c, true, &slot) && slot == 1U);
    AlertDriven_Expired(&c); /* diagnostic must not discard RAW or reserve forever */
    AlertDriven_Accept(&c, 1190U);
    assert(c.accepted == 2U && c.expired == 1U && c.missed_slots == 0U);
    AlertDriven_Irq(&c, 1200U);
    assert(AlertDriven_Take(&c, 1202U));
    assert(AlertDriven_Ready(&c, true, &slot));
    AlertDriven_Finish(&c, 1250U); /* STOP during read; no fabricated sample */
    assert(c.accepted == 2U && c.missed_slots == 1U && !c.reserved);
    AlertDriven_Start(&c, 2000U, 100U);
    assert(!AlertDriven_Take(&c, 2001U));
}
int main(void)
{
    for (uint32_t delay = 5U; delay <= 40U; delay += 5U) {
        AlertDriven_Core c;
        uint32_t slot;
        AlertDriven_Start(&c, 0xFFFFFF00ULL, 100U);
        for (uint32_t i = 0U; i < 100000U; ++i) {
            uint64_t t = 0xFFFFFF00ULL + i * 50ULL;
            AlertDriven_Irq(&c, (uint32_t)t);
            assert(AlertDriven_Take(&c, t + 1U));
            if (AlertDriven_ReadyAt(&c, true, (uint32_t)t + 4U, &slot)) {
                /* Completion after 5..40 us; the optional second read does
                   not move the primary timestamp or cause a second commit. */
                AlertDriven_Accept(&c, (uint32_t)t + delay);
                AlertDriven_Accept(&c, (uint32_t)t + delay + 5U);
            }
        }
        AlertDriven_Finish(&c, 0xFFFFFF00ULL + 5000000ULL);
        assert(c.accepted == 50000U && c.missed_slots == 0U);
    }
    /* BALANCED: separate ideal ADC events from an aggregate-compatible
       three-ready/one-zero confirmation waveform. No fabricated backfill. */
    for (uint32_t zeros = 0U; zeros <= 1U; ++zeros) {
      for (uint32_t phase = 45U; phase <= 55U; phase += 5U) {
        for (uint32_t delay = 5U; delay <= 40U; delay += 5U) {
            AlertDriven_Core c;
            uint32_t slot, n = 0U;
            AlertDriven_Start(&c, 1000U, 200U);
            for (uint32_t i = 0U; (uint64_t)i * 150U + phase < 5000000ULL; ++i) {
                uint64_t t = 1000ULL + i * 150ULL + phase;
                AlertDriven_Irq(&c, (uint32_t)t);
                assert(AlertDriven_Take(&c, t + 2U));
                if (AlertDriven_ReadyAt(&c, !zeros || i % 4U != 0U,
                                        (uint32_t)t + delay, &slot)) {
                    if (t + delay + 6U <= 5001000ULL) {
                        AlertDriven_Accept(&c, (uint32_t)t + delay + 6U);
                        ++n;
                    }
                    else AlertDriven_Failed(&c, false); /* STOP before primary completion */
                }
            }
            AlertDriven_Finish(&c, 5001000ULL);
            assert(n + c.missed_slots == 25000U);
            if (!zeros) assert(n == 25000U && c.missed_slots == 0U);
            else if (phase >= 50U) {
                assert(n == 16666U && c.missed_slots == 8334U);
                assert(c.trace_count >= 5U);
                assert(c.trace[0].slot == 1U && c.trace[1].slot == 4U && c.trace[2].slot == 7U);
            }
            else assert(n == 24999U && c.missed_slots == 1U);
            if (delay == 5U && phase == 50U) printf("BALANCED zeros=%u: samples=%u empty=%u\n",
                                    zeros, n, c.missed_slots);
        }
      }
    }
    future_clock_race(); retry_false_ready_busy_stop();
    five_seconds(1000U, 0U, false, false);
    five_seconds(1000U, 25U, false, false);
    five_seconds(1000U, 49U, true, false);
    five_seconds(1000U, 5U, true, false);
    five_seconds(1000U, 45U, true, false);
    five_seconds(1000U, 0U, false, true);
    five_seconds(0xFFFFFF00ULL, 0U, false, false);
    five_seconds(0x200000010ULL, 25U, true, true);
    puts("alert driven tests: OK");
    return 0;
}
