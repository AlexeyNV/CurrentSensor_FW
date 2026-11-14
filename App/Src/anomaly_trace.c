#include "anomaly_trace.h"

#include <string.h>

void AnomalyTrace_Init(Anomaly_Trace *trace)
{
    memset(trace, 0, sizeof(*trace));
    trace->limit_ma = 10000U;
}

void AnomalyTrace_Reset(Anomaly_Trace *trace)
{
    uint32_t limit = trace->limit_ma;
    memset(trace, 0, sizeof(*trace));
    trace->limit_ma = limit;
}

bool AnomalyTrace_SetLimitMa(Anomaly_Trace *trace, uint32_t limit_ma)
{
    if (trace == NULL || limit_ma > ANOMALY_MAX_LIMIT_MA) return false;
    trace->limit_ma = limit_ma;
    return true;
}

bool AnomalyTrace_ParseLimitMa(const char *text, uint32_t *limit_ma)
{
    uint32_t value = 0U;
    if (text == NULL || limit_ma == NULL || *text == '\0') return false;
    while (*text != '\0')
    {
        if (*text < '0' || *text > '9') return false;
        value = value * 10U + (uint32_t)(*text - '0');
        if (value > ANOMALY_MAX_LIMIT_MA) return false;
        ++text;
    }
    *limit_ma = value;
    return true;
}

void AnomalyTrace_Record(Anomaly_Trace *trace, const Acq_Sample *sample)
{
    int32_t code;
    uint32_t magnitude;
    bool unusual;
    uint8_t i;
    if (trace == NULL || sample == NULL) return;
    code = AcqCore_Decode(sample->raw);
    magnitude = (uint32_t)(code < 0 ? -code : code);
    unusual = magnitude > trace->limit_ma * 16U;
    if (unusual) ++trace->events;
    if (trace->post_remaining != 0U)
    {
        trace->capture[trace->capture_count++] = *sample;
        --trace->post_remaining;
        return;
    }
    if (trace->capture_count == 0U && unusual)
    {
        for (i = 0U; i < trace->recent_count; ++i)
        {
            uint8_t index = (uint8_t)((trace->recent_head + ANOMALY_PRE_SAMPLES -
                                        trace->recent_count + i) % ANOMALY_PRE_SAMPLES);
            trace->capture[trace->capture_count++] = trace->recent[index];
        }
        trace->capture[trace->capture_count++] = *sample;
        trace->post_remaining = ANOMALY_POST_SAMPLES;
        return;
    }
    if (trace->capture_count != 0U) return; /* Keep the first bounded context. */
    trace->recent[trace->recent_head] = *sample;
    trace->recent_head = (uint8_t)((trace->recent_head + 1U) % ANOMALY_PRE_SAMPLES);
    if (trace->recent_count < ANOMALY_PRE_SAMPLES) ++trace->recent_count;
}
