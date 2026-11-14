#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "native_core.h"
static void read_one(Native_Core *c, uint32_t event, bool ready, uint32_t delay)
{
    uint32_t seq;
    NativeCore_Irq(c, event, true);
    assert(NativeCore_Take(c));
    c->ack_start = event + 1U;
    if (NativeCore_ReadyAt(c, ready, event + 5U, &seq)) {
        assert(seq == c->samples);
        NativeCore_ReadStart(c, event + 6U);
        NativeCore_Primary(c, event + 6U, event + delay, c->irq_count);
        NativeCore_Accept(c);
        NativeCore_Complete(c, event + delay + 2U);
    }
}
static void uniform(uint32_t period)
{
    Native_Core c;
    uint32_t n = 0U;
    NativeCore_Start(&c, period);
    for (uint64_t t = 0U; t < 5000000ULL; t += period) {
        read_one(&c, (uint32_t)(0xFFFFFF00ULL + t), true, 20U);
        ++n;
    }
    NativeCore_Stop(&c);
    assert(c.samples == n && c.confirmed == n && c.primary_reads == n);
    assert(c.uncertain == n && c.lost == 0U && c.queue_drops == 0U);
    assert(c.min_age == 20U && c.max_age == 20U && NativeCore_P99(&c) == 20U);
    assert(c.overbudget_cycles == 0U);
    printf("native %u us: %u samples, 0 observed event losses\n", period, n);
}
static void zeros_duplicates_and_missing(void)
{
    Native_Core c;
    NativeCore_Start(&c, 150U);
    read_one(&c, 1000U, false, 20U);
    assert(c.samples == 0U && c.not_ready == 1U && c.lost == 0U);
    assert(!NativeCore_Take(&c)); /* no event, no invented sample */
    read_one(&c, 1150U, true, 20U);
    read_one(&c, 1150U, true, 20U); /* duplicated edge is not a new result */
    assert(c.samples == 1U && c.confirmed == 2U && c.duplicate_events == 1U && c.lost == 1U);
    read_one(&c, 1450U, true, 20U); /* a missing edge does not invent an ADC count */
    assert(c.samples == 2U && c.uncertain == 2U);
    NativeCore_Stop(&c);
}
static void callback_delay_queue_and_updates(void)
{
    Native_Core c;
    uint32_t seq;
    NativeCore_Start(&c, 50U);
    NativeCore_Irq(&c, 1000U, true);
    assert(NativeCore_Take(&c));
    assert(NativeCore_Ready(&c, true, &seq) && seq == 0U);
    NativeCore_ReadStart(&c, 1020U);
    NativeCore_Irq(&c, 1050U, true);
    NativeCore_Irq(&c, 1100U, false);
    assert(c.gpio_high == 1U);
    assert(!NativeCore_Take(&c)); /* no overlap with active SPI */
    NativeCore_Primary(&c, 1020U, 1110U, c.irq_count);
    NativeCore_Accept(&c);
    assert(c.trace[0].flags & 2U); /* result could update before read */
    assert(c.trace[0].flags & 4U); /* events occurred during read */
    NativeCore_Complete(&c, 1125U); /* optional second read/delayed callback */
    assert(c.overbudget_cycles == 1U && c.while_busy == 2U && c.samples == 1U);
    assert(NativeCore_Take(&c));
    assert(!NativeCore_ReadyAt(&c, false, 1130U, &seq));
    assert(NativeCore_Take(&c));
    assert(NativeCore_ReadyAt(&c, true, 1140U, &seq) && seq == 1U);
    NativeCore_Failed(&c, 1150U);
    assert(c.samples == 1U && c.lost == 1U && c.dma_failures == 1U);
    NativeCore_Stop(&c);
}
static void stop_and_overflow(void)
{
    Native_Core c;
    uint32_t seq;
    for (uint32_t phase = 0U; phase < 4U; ++phase) {
        NativeCore_Start(&c, 84U);
        NativeCore_Irq(&c, 1000U, true);
        if (phase >= 1U) assert(NativeCore_Take(&c));
        if (phase >= 2U) assert(NativeCore_Ready(&c, true, &seq));
        if (phase >= 3U) {
            NativeCore_ReadStart(&c, 1005U);
            NativeCore_Primary(&c, 1005U, 1020U, c.irq_count);
            NativeCore_Accept(&c); /* STOP between primary and secondary completion */
        }
        NativeCore_StopAt(&c, 1030U);
        assert(c.samples == (phase == 3U ? 1U : 0U));
        assert(c.lost == (phase == 3U ? 0U : 1U));
        assert(c.stop_discarded == c.lost);
        assert(c.max_cycle == (phase == 0U ? 0U : 30U));
        NativeCore_Irq(&c, 1100U, true);
        assert(!NativeCore_Take(&c) && c.irq_count == 1U);
        NativeCore_ReadStart(&c, 1110U);
        NativeCore_Primary(&c, 1110U, 1130U, 2U);
        NativeCore_Accept(&c);
        NativeCore_Complete(&c, 1140U);
        NativeCore_Failed(&c, 1150U);
        assert(!NativeCore_ReadyAt(&c, true, 1160U, &seq));
        assert(c.invalid_callbacks == 0U && c.dma_failures == 0U);
    }
    NativeCore_Start(&c, 50U);
    c.head = c.tail = 0xFFFFFFF8U; /* ring index rollover */
    for (uint32_t i = 0U; i < 32U; ++i) NativeCore_Irq(&c, i * 50U, true);
    assert(c.queue_drops == 16U);
    NativeCore_Stop(&c);
    assert(c.lost == 16U && c.samples == 0U);
}
static void percentile(void)
{
    Native_Core c;
    NativeCore_Start(&c, 540U);
    for (uint32_t i = 0U; i < 100U; ++i) read_one(&c, i * 1000U, true, i);
    assert(NativeCore_P99(&c) == 98U);
    NativeCore_Start(&c, 50U);
    read_one(&c, 0xFFFFFFF0U, true, 300U);
    assert(c.max_age == 300U && NativeCore_P99(&c) == 300U);
}
int main(void)
{
    uniform(50U); uniform(84U); uniform(150U);
    zeros_duplicates_and_missing(); callback_delay_queue_and_updates();
    stop_and_overflow(); percentile();
    puts("native tests: OK"); return 0;
}
