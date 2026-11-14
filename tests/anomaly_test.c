#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "anomaly_trace.h"

static Anomaly_Trace trace;

static void record(uint32_t slot, int32_t code, uint8_t low_nibble)
{
    Acq_Sample sample = {
        (((uint32_t)code & 0xFFFFFU) << 4) | low_nibble,
        slot * 100U + 23U,
        slot
    };
    AnomalyTrace_Record(&trace, &sample);
}

static void test_context_and_raw_bits(void)
{
    uint32_t i;
    AnomalyTrace_Init(&trace);
    assert(trace.limit_ma == 10000U);
    assert(AnomalyTrace_SetLimitMa(&trace, 100U));
    AnomalyTrace_Reset(&trace);
    for (i = 0U; i < 6U; ++i) record(i, (int32_t)i, 0U);
    record(6U, -2000, 0x0BU); /* -125 mA, trigger. */
    for (i = 7U; i < 11U; ++i) record(i, 0, 0U);
    assert(trace.events == 1U && trace.capture_count == 9U);
    assert(trace.capture[0].slot == 2U);
    assert(trace.capture[4].slot == 6U);
    assert((trace.capture[4].raw & 0xFU) == 0x0BU);
    assert(AcqCore_Decode(trace.capture[4].raw) == -2000);
    assert(trace.capture[8].slot == 10U && trace.post_remaining == 0U);
    record(11U, 3000, 0U);
    assert(trace.events == 2U && trace.capture_count == 9U);
}

static void test_limits_and_partial_capture(void)
{
    uint32_t limit = 0U;
    AnomalyTrace_Init(&trace);
    assert(AnomalyTrace_ParseLimitMa("0", &limit) && limit == 0U);
    assert(AnomalyTrace_ParseLimitMa("32767", &limit) && limit == 32767U);
    assert(!AnomalyTrace_ParseLimitMa("32768", &limit));
    assert(!AnomalyTrace_ParseLimitMa("4294967295", &limit));
    assert(!AnomalyTrace_ParseLimitMa("-1", &limit));
    assert(!AnomalyTrace_ParseLimitMa("2x", &limit));
    assert(!AnomalyTrace_ParseLimitMa("", &limit));
    assert(!AnomalyTrace_SetLimitMa(&trace, 32768U));
    assert(AnomalyTrace_SetLimitMa(&trace, 0U));
    record(0U, 1, 0U);
    assert(trace.capture_count == 1U && trace.post_remaining == 4U);
    assert(trace.capture[0].slot == 0U);
}

int main(void)
{
    test_context_and_raw_bits();
    test_limits_and_partial_capture();
    puts("anomaly tests passed");
    return 0;
}
