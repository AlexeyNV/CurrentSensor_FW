#ifndef STREAM_TRANSPORT_H
#define STREAM_TRANSPORT_H

#include <stdbool.h>
#include <stdint.h>

#include "stream_core.h"

typedef uint32_t (*StreamClockFn)(void);

typedef struct
{
    uint32_t build_max_us;
    uint64_t build_sum_us;
    uint32_t build_calls;
    uint32_t build_over_25us;
    uint32_t submit_max_us;
    uint64_t submit_sum_us;
    uint32_t submit_calls;
    uint32_t submit_over_25us;
} Stream_Timing;

void StreamTransport_Start(void);
void StreamTransport_SetNativeV2(void);
void StreamTransport_Push(const Acq_Sample *sample);
void StreamTransport_RequestStop(void);
void StreamTransport_RequestStats(void);
void StreamTransport_RequestConfig(void);
void StreamTransport_RequestError(uint32_t code);
void StreamTransport_RecordBusy(void);
void StreamTransport_RecordPollDuration(uint32_t duration_us);
void StreamTransport_TxCompleteISR(void);
bool StreamTransport_Poll(bool usb_idle, bool configured, bool source_empty,
                          uint32_t now_us, uint32_t now_ms,
                          const Stream_AcqMetrics *acq, StreamClockFn clock_us);
bool StreamTransport_IsActive(void);
bool StreamTransport_IsFinished(void);
bool StreamTransport_Failed(void);
/* Snapshot used to serialize CSF4 END, available until the next Start. */
bool StreamTransport_EndElapsedUs64(uint64_t *elapsed_us64);
const Stream_Core *StreamTransport_GetCore(void);
const Stream_Timing *StreamTransport_GetTiming(void);

#endif
