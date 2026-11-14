#include "acquisition_core.h"

#include <string.h>

#if defined(__GNUC__)
#define ACQ_RING_BARRIER() __asm__ volatile("" ::: "memory")
#elif defined(_MSC_VER)
#include <intrin.h>
#define ACQ_RING_BARRIER() _ReadWriteBarrier()
#else
#define ACQ_RING_BARRIER() ((void)0)
#endif

static uint64_t AcqCore_Sqrt(uint64_t x)
{
    uint64_t result = 0U;
    uint64_t bit = 1ULL << 62;
    while (bit > x) bit >>= 2;
    while (bit != 0U)
    {
        if (x >= result + bit)
        {
            x -= result + bit;
            result = (result >> 1) + bit;
        }
        else result >>= 1;
        bit >>= 2;
    }
    return result;
}

int32_t AcqCore_Decode(uint32_t raw)
{
    uint32_t code = (raw >> 4) & 0xFFFFFU;
    return (code & 0x80000U) ? (int32_t)code - 0x100000L : (int32_t)code;
}

void AcqCore_Start(Acq_Core *core, uint64_t now_us)
{
    const MeasurementProfile *profile = MeasurementProfile_Selected();
    memset(core, 0, sizeof(*core));
    core->running = true;
    core->started_us = (uint32_t)now_us;
    core->started_us64 = now_us;
    core->profile_id = profile->id;
    core->period_us = profile->period_us;
    core->freshness_guard_us = profile->vshct_us + 10U;
}

void AcqCore_Stop(Acq_Core *core, uint64_t now_us)
{
    core->running = false;
    core->stopped_us = (uint32_t)now_us;
    core->stopped_us64 = now_us;
}

void AcqCore_SaveConfig(Acq_Core *core, uint16_t value)
{
    core->saved_adc_config = value;
    core->config_saved = true;
}

bool AcqCore_RestoreConfig(Acq_Core *core, AcqCore_WriteConfigFn write_config,
                           void *context)
{
    if (!core->config_saved || write_config == NULL) return false;
    if (!write_config(context, core->saved_adc_config))
    {
        ++core->spi_errors;
        return false;
    }
    core->config_saved = false;
    return true;
}

bool AcqCore_Schedule(Acq_Core *core, uint32_t ticks, uint32_t tick_us,
                      uint32_t now_us, uint32_t period_us)
{
    uint32_t gap = ticks - core->handled_ticks;
    if (!core->running || gap == 0U) return false;
    core->handled_ticks = ticks;
    core->missed += gap - 1U;
    core->missed_timer_slots += gap - 1U;
    if ((uint32_t)(now_us - tick_us) >= period_us)
    {
        ++core->missed;
        ++core->missed_timer_slots;
        return false;
    }
    return true;
}

bool AcqCore_FreshnessAllowsRead(Acq_Core *core, uint32_t tick_us)
{
    if (core->samples != 0U)
    {
        uint32_t age = tick_us - core->previous_sample_us;
        if (age >= 0x80000000U || age < core->freshness_guard_us)
        {
            /* A flag left by the preceding read could describe that same result. */
            ++core->missed;
            ++core->freshness_skips;
            return false;
        }
    }
    return true;
}

void AcqCore_Tick(Acq_Core *core, uint32_t now_us)
{
    uint32_t delta;
    if (!core->running) return;
    if (core->ticks != 0U)
    {
        delta = now_us - core->previous_tick_us;
        uint32_t tolerance = core->period_us / 20U;
        if (tolerance < 5U) tolerance = 5U;
        if (delta < core->period_us - tolerance ||
            delta > core->period_us + tolerance) ++core->period_violations;
        if (delta >= core->period_us + core->period_us / 2U)
            core->timer_lost += (delta + core->period_us / 2U) /
                                core->period_us - 1U;
    }
    core->previous_tick_us = now_us;
    ++core->ticks;
}

void AcqCore_DiagAttempt(Acq_Core *core) { ++core->cnvrf_reads; }

void AcqCore_DiagResult(Acq_Core *core, uint16_t diag_alrt)
{
    core->last_diag_alrt = diag_alrt;
    if ((diag_alrt & 0x0002U) != 0U) ++core->cnvrf_ready;
    else ++core->cnvrf_not_ready;
}

void AcqCore_RecordSpi(Acq_Core *core, uint32_t duration_us)
{
    if (duration_us > core->max_spi_transaction_us)
        core->max_spi_transaction_us = duration_us;
}

void AcqCore_RecordReadLatency(Acq_Core *core, uint32_t latency_us,
                               uint32_t latency_cycles)
{
    if (latency_us > core->max_read_latency_us)
        core->max_read_latency_us = latency_us;
    if (latency_cycles > core->max_read_latency_cycles)
        core->max_read_latency_cycles = latency_cycles;
}

void AcqCore_VShuntAttempt(Acq_Core *core) { ++core->vshunt_reads; }

void AcqCore_Missed(Acq_Core *core) { ++core->missed; }
void AcqCore_MissedTimerSlot(Acq_Core *core)
{
    ++core->missed;
    ++core->missed_timer_slots;
}
void AcqCore_SpiError(Acq_Core *core)
{
    ++core->spi_errors;
}

static void AcqCore_StoreSample(Acq_Core *core, uint32_t raw, uint32_t now_us,
                                uint32_t service_us, uint32_t slot,
                                uint8_t command_rx, bool command_valid)
{
    uint16_t next = (uint16_t)((core->head + 1U) % ACQ_RING_CAPACITY);
    uint32_t period;
    if (core->samples != 0U)
    {
        period = now_us - core->previous_sample_us;
        if (core->samples == 1U || period < core->min_sample_period_us)
            core->min_sample_period_us = period;
        if (period > core->max_sample_period_us) core->max_sample_period_us = period;
    }
    core->previous_sample_us = now_us;
    ++core->samples;
    if (service_us > core->max_service_us) core->max_service_us = service_us;
    if (next == core->tail) { ++core->overflows; return; }
    core->ring[core->head].raw = raw & 0xFFFFFFU;
    core->ring[core->head].time_us = now_us;
    core->ring[core->head].slot = slot;
    core->ring[core->head].spi_command_rx = command_rx;
    core->ring[core->head].spi_command_rx_valid = command_valid;
    ACQ_RING_BARRIER();
    core->head = next;
}

void AcqCore_Sample(Acq_Core *core, uint32_t raw, uint32_t now_us,
                    uint32_t service_us, uint32_t slot)
{
    AcqCore_StoreSample(core, raw, now_us, service_us, slot, 0U, false);
}

void AcqCore_SampleSpi(Acq_Core *core, uint32_t raw, uint32_t now_us,
                       uint32_t service_us, uint32_t slot,
                       uint8_t command_rx)
{
    AcqCore_StoreSample(core, raw, now_us, service_us, slot, command_rx, true);
}

bool AcqCore_Pop(Acq_Core *core, Acq_Sample *sample)
{
    int32_t code;
    if (core->tail == core->head || sample == NULL) return false;
    *sample = core->ring[core->tail];
    ACQ_RING_BARRIER();
    core->tail = (uint16_t)((core->tail + 1U) % ACQ_RING_CAPACITY);
    /* At 1 million full-scale codes, sum of squares stays below 2^58. */
    if (core->stats_count < 1000000U)
    {
        code = AcqCore_Decode(sample->raw);
        if (core->stats_count == 0U || code < core->min_code) core->min_code = code;
        if (core->stats_count == 0U || code > core->max_code) core->max_code = code;
        core->sum_code += code;
        core->sum_square += (uint64_t)((int64_t)code * code);
        ++core->stats_count;
    }
    return true;
}

uint32_t AcqCore_Rate(const Acq_Core *core, uint64_t now_us)
{
    uint64_t elapsed = (core->running ? now_us : core->stopped_us64) - core->started_us64;
    if (elapsed == 0U) return 0U;
    return (uint32_t)(((uint64_t)core->samples * 1000000ULL) / elapsed);
}

uint64_t AcqCore_StdCodeMilli(const Acq_Core *core)
{
    uint64_t n = core->stats_count;
    uint64_t variance_scaled;
    uint64_t remainder_magnitude;
    int64_t centered;
    int64_t mean;
    int64_t remainder;
    if (n == 0U) return 0U;
    mean = core->sum_code / (int64_t)n;
    remainder = core->sum_code % (int64_t)n;
    centered = (int64_t)core->sum_square - 2LL * mean * core->sum_code + (int64_t)n * mean * mean;
    if (centered < 0) return 0U;
    /* Divide before multiplying to keep the intermediate below UINT64_MAX. */
    variance_scaled = ((uint64_t)centered / n) * 1000000ULL +
        (((uint64_t)centered % n) * 1000000ULL) / n;
    /* Remove the bias from truncating the mean to an integer code. */
    remainder_magnitude = (uint64_t)(remainder < 0 ? -remainder : remainder);
    variance_scaled -= (remainder_magnitude * remainder_magnitude * 1000000ULL) /
        (n * n);
    return AcqCore_Sqrt(variance_scaled);
}

bool AcqCore_ParseProbeDelay(const char *text, uint32_t *delay_us)
{
    uint32_t value = 0U;
    if (text == NULL || delay_us == NULL || *text == '\0') return false;
    while (*text != '\0')
    {
        if (*text < '0' || *text > '9') return false;
        value = value * 10U + (uint32_t)(*text - '0');
        /* Reject early; the intermediate cannot overflow uint32_t. */
        if (value > 1000U) return false;
        ++text;
    }
    if (value == 0U) return false;
    *delay_us = value;
    return true;
}
