#ifndef ANOMALY_TRACE_H
#define ANOMALY_TRACE_H

#include <stdbool.h>
#include <stdint.h>

#include "acquisition_core.h"

#define ANOMALY_PRE_SAMPLES 4U
#define ANOMALY_POST_SAMPLES 4U
#define ANOMALY_CAPTURE_SAMPLES (ANOMALY_PRE_SAMPLES + 1U + ANOMALY_POST_SAMPLES)
#define ANOMALY_MAX_LIMIT_MA 32767U

typedef struct
{
    Acq_Sample recent[ANOMALY_PRE_SAMPLES];
    Acq_Sample capture[ANOMALY_CAPTURE_SAMPLES];
    uint32_t limit_ma;
    uint32_t events;
    uint8_t recent_head;
    uint8_t recent_count;
    uint8_t capture_count;
    uint8_t post_remaining;
} Anomaly_Trace;

void AnomalyTrace_Init(Anomaly_Trace *trace);
void AnomalyTrace_Reset(Anomaly_Trace *trace);
bool AnomalyTrace_SetLimitMa(Anomaly_Trace *trace, uint32_t limit_ma);
bool AnomalyTrace_ParseLimitMa(const char *text, uint32_t *limit_ma);
void AnomalyTrace_Record(Anomaly_Trace *trace, const Acq_Sample *sample);

#endif
