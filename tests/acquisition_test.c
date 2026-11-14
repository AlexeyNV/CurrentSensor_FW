#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "acquisition_core.h"

static Acq_Core core;

typedef struct
{
    uint16_t written;
    uint32_t calls;
    bool succeed;
} MockConfig;

static bool mock_write_config(void *context, uint16_t value)
{
    MockConfig *mock = (MockConfig *)context;
    ++mock->calls;
    mock->written = value;
    return mock->succeed;
}

static void test_continuous_fresh_slots(void)
{
    Acq_Sample sample;
    AcqCore_Start(&core, 0U);
    AcqCore_Tick(&core, 100U);
    assert(AcqCore_Schedule(&core, core.ticks, 100U, 105U, 100U));
    AcqCore_RecordReadLatency(&core, 5U, 480U);
    AcqCore_DiagAttempt(&core);
    AcqCore_DiagResult(&core, 0x0003U);
    assert(AcqCore_FreshnessAllowsRead(&core, 100U));
    AcqCore_VShuntAttempt(&core);
    AcqCore_Sample(&core, 0x001000U, 120U, 20U, 0U);
    assert(AcqCore_Pop(&core, &sample));
    assert(sample.raw == 0x001000U && sample.time_us == 120U && sample.slot == 0U);

    AcqCore_Tick(&core, 200U);
    assert(AcqCore_Schedule(&core, core.ticks, 200U, 205U, 100U));
    AcqCore_DiagAttempt(&core);
    AcqCore_DiagResult(&core, 0x0003U);
    assert(AcqCore_FreshnessAllowsRead(&core, 200U));
    AcqCore_VShuntAttempt(&core);
    AcqCore_Sample(&core, 0x001000U, 220U, 20U, 1U);
    assert(core.samples == 2U && core.cnvrf_ready == 2U);
    assert(core.min_sample_period_us == 100U && core.max_sample_period_us == 100U);
    assert(core.missed == 0U && core.missed_timer_slots == 0U);
    assert(core.max_read_latency_us == 5U && core.max_read_latency_cycles == 480U);
    assert(AcqCore_Rate(&core, 200U) == 10000U);
}

static void test_not_ready_and_spi_errors(void)
{
    AcqCore_Start(&core, 0U);
    AcqCore_Tick(&core, 100U);
    assert(AcqCore_Schedule(&core, core.ticks, 100U, 105U, 100U));
    AcqCore_DiagAttempt(&core);
    AcqCore_DiagResult(&core, 0x0001U);
    AcqCore_Missed(&core);
    assert(core.samples == 0U && core.cnvrf_not_ready == 1U);
    assert(core.missed == 1U && core.missed_timer_slots == 0U);

    AcqCore_Tick(&core, 200U);
    assert(AcqCore_Schedule(&core, core.ticks, 200U, 205U, 100U));
    AcqCore_DiagAttempt(&core);
    AcqCore_SpiError(&core);
    AcqCore_Missed(&core);
    assert(core.spi_errors == 1U && core.missed == 2U);

    AcqCore_Tick(&core, 300U);
    assert(AcqCore_Schedule(&core, core.ticks, 300U, 305U, 100U));
    AcqCore_DiagAttempt(&core);
    AcqCore_DiagResult(&core, 0x0003U);
    AcqCore_VShuntAttempt(&core);
    AcqCore_SpiError(&core);
    AcqCore_Missed(&core);
    assert(core.vshunt_reads == 1U && core.samples == 0U);
    assert(core.spi_errors == 2U && core.missed == 3U);
}

static void test_backlog_and_freshness_guard(void)
{
    AcqCore_Start(&core, 0U);
    AcqCore_Tick(&core, 100U);
    AcqCore_Tick(&core, 200U);
    AcqCore_Tick(&core, 300U);
    assert(AcqCore_Schedule(&core, core.ticks, 300U, 305U, 100U));
    assert(core.missed == 2U && core.missed_timer_slots == 2U);
    assert(!AcqCore_Schedule(&core, core.ticks, 300U, 306U, 100U));

    AcqCore_Sample(&core, 0x100U, 390U, 90U, 2U);
    AcqCore_Tick(&core, 400U);
    assert(AcqCore_Schedule(&core, core.ticks, 400U, 405U, 100U));
    AcqCore_DiagAttempt(&core);
    AcqCore_DiagResult(&core, 0x0003U);
    assert(!AcqCore_FreshnessAllowsRead(&core, 400U));
    assert(core.freshness_skips == 1U && core.missed_timer_slots == 2U);
    assert(core.cnvrf_reads == 1U && core.cnvrf_ready == 1U);
    AcqCore_Tick(&core, 500U);
    assert(AcqCore_Schedule(&core, core.ticks, 500U, 505U, 100U));
    assert(AcqCore_FreshnessAllowsRead(&core, 500U));

    AcqCore_Start(&core, 0U);
    AcqCore_Tick(&core, 100U);
    assert(!AcqCore_Schedule(&core, core.ticks, 100U, 205U, 100U));
    assert(core.missed_timer_slots == 1U && core.missed == 1U);
}

static void test_ring_overflow_and_statistics(void)
{
    Acq_Sample sample;
    uint32_t i;
    AcqCore_Start(&core, 0U);
    for (i = 0U; i < ACQ_RING_CAPACITY + 5U; ++i)
        AcqCore_Sample(&core, i << 4, i * 100U, 70U, i);
    assert(core.samples == ACQ_RING_CAPACITY + 5U);
    assert(core.overflows == 6U);
    for (i = 0U; i < ACQ_RING_CAPACITY - 1U; ++i)
    {
        assert(AcqCore_Pop(&core, &sample));
        assert(sample.raw == i << 4);
    }
    assert(!AcqCore_Pop(&core, &sample));
    assert(core.stats_count == ACQ_RING_CAPACITY - 1U);
    assert(core.max_service_us == 70U);
    assert(AcqCore_Decode(0xFFFF00U) == -16);
    assert(AcqCore_Decode(0x7FFFF0U) == 524287);
    assert(AcqCore_Decode(0x800000U) == -524288);
    assert(AcqCore_Decode(0x85F330U) == -499917);

    AcqCore_Start(&core, 0U);
    AcqCore_SampleSpi(&core, 0x020000U, 100U, 25U, 7U, 0xA5U);
    assert(AcqCore_Pop(&core, &sample));
    assert(sample.raw == 0x020000U && sample.slot == 7U);
    assert(sample.spi_command_rx_valid && sample.spi_command_rx == 0xA5U);
}

static void test_worst_case_statistics(void)
{
    Acq_Sample sample;
    uint32_t i;
    AcqCore_Start(&core, 0U);
    for (i = 0U; i < 50000U; ++i)
    {
        AcqCore_Sample(&core, (i & 1U) ? 0x800000U : 0x7FFFF0U, i * 100U, 70U, i);
        assert(AcqCore_Pop(&core, &sample));
    }
    assert(core.stats_count == 50000U);
    assert(AcqCore_StdCodeMilli(&core) >= 524287000U);
    assert(AcqCore_StdCodeMilli(&core) <= 524288000U);
    assert(core.overflows == 0U);

    AcqCore_Start(&core, 0U);
    AcqCore_Sample(&core, 0x000000U, 100U, 20U, 0U);
    assert(AcqCore_Pop(&core, &sample));
    AcqCore_Sample(&core, 0x000010U, 200U, 20U, 1U);
    assert(AcqCore_Pop(&core, &sample));
    assert(AcqCore_StdCodeMilli(&core) == 500U);

    AcqCore_Start(&core, 0U);
    AcqCore_Sample(&core, 0xFFFFF0U, 100U, 20U, 0U);
    assert(AcqCore_Pop(&core, &sample));
    AcqCore_Sample(&core, 0x000000U, 200U, 20U, 1U);
    assert(AcqCore_Pop(&core, &sample));
    assert(AcqCore_StdCodeMilli(&core) == 500U);
}

static void test_stop_and_config_restore(void)
{
    MockConfig mock = {0U, 0U, true};
    AcqCore_Start(&core, 0U);
    AcqCore_SaveConfig(&core, 0xFB68U);
    AcqCore_Tick(&core, 100U);
    AcqCore_Stop(&core, 150U);
    assert(!core.running);
    assert(core.config_saved);
    assert(AcqCore_RestoreConfig(&core, mock_write_config, &mock));
    assert(mock.calls == 1U && mock.written == 0xFB68U);
    assert(!core.config_saved);
    assert(!AcqCore_RestoreConfig(&core, mock_write_config, &mock));

    AcqCore_Start(&core, 0U);
    AcqCore_SaveConfig(&core, 0xFB68U);
    mock.succeed = false;
    assert(!AcqCore_RestoreConfig(&core, mock_write_config, &mock));
    assert(core.config_saved && core.spi_errors == 1U);
    mock.succeed = true;
    assert(AcqCore_RestoreConfig(&core, mock_write_config, &mock));
    assert(mock.written == 0xFB68U && !core.config_saved);
}

static void test_probe_delay_arguments(void)
{
    static const char *valid[] = {"50", "75", "100", "150", "200", "1000"};
    static const uint32_t expected[] = {50U, 75U, 100U, 150U, 200U, 1000U};
    static const char *invalid[] = {"", "0", "1001", "-50", "+50", "50x",
                                     " 50", "50 ", "4294967295"};
    uint32_t delay;
    unsigned int i;
    for (i = 0U; i < sizeof(valid) / sizeof(valid[0]); ++i)
    {
        delay = 0U;
        assert(AcqCore_ParseProbeDelay(valid[i], &delay));
        assert(delay == expected[i]);
    }
    for (i = 0U; i < sizeof(invalid) / sizeof(invalid[0]); ++i)
    {
        delay = 123U;
        assert(!AcqCore_ParseProbeDelay(invalid[i], &delay));
        assert(delay == 123U);
    }
}

int main(void)
{
    uint64_t long_us = 5400000000ULL;
    AcqCore_Start(&core, 0U);
    core.samples = 54000000U;
    AcqCore_Stop(&core, long_us);
    assert(core.stopped_us == (uint32_t)(long_us & UINT32_MAX));
    assert(AcqCore_Rate(&core, 0U) == 10000U);
    AcqCore_Start(&core, 0U);
    core.samples = 100000000U;
    AcqCore_Stop(&core, 10000000000ULL);
    assert(AcqCore_Rate(&core, 0U) == 10000U);
    test_continuous_fresh_slots();
    test_not_ready_and_spi_errors();
    test_backlog_and_freshness_guard();
    test_ring_overflow_and_statistics();
    test_worst_case_statistics();
    test_stop_and_config_restore();
    test_probe_delay_arguments();
    puts("acquisition tests passed");
    return 0;
}
