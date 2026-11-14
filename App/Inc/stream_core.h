#ifndef STREAM_CORE_H
#define STREAM_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "acquisition_core.h"

#define STREAM_RING_CAPACITY 2048U
#define STREAM_FRAME_SAMPLES 32U
#define STREAM_HEADER_SIZE 32U
#define STREAM_FRAME_MAX (STREAM_HEADER_SIZE + STREAM_FRAME_SAMPLES * 6U + 4U)
#define STREAM_STATS_SIZE (STREAM_HEADER_SIZE + 76U + 4U)
#define STREAM_TYPE_DATA 1U
#define STREAM_TYPE_STATS 2U
#define STREAM_TYPE_END 3U
#define STREAM_TYPE_CONFIG 4U
#define STREAM_TYPE_ERROR 5U

typedef struct
{
    uint32_t adc_samples;
    uint32_t lost_sample_slots;
    uint32_t acq_buffer_overflows;
    uint32_t spi_errors;
    uint32_t max_service_us;
    uint32_t min_sample_period_us;
    uint32_t max_sample_period_us;
    uint32_t elapsed_us;
    uint32_t stream_elapsed_us;
    uint64_t stream_elapsed_us64;
} Stream_AcqMetrics;

typedef struct
{
    Acq_Sample ring[STREAM_RING_CAPACITY];
    const MeasurementProfile *profile;
    bool native_v2;
    uint16_t head;
    uint16_t tail;
    uint16_t pending_count;
    uint32_t pending_last_slot;
    uint32_t next_sequence;
    uint32_t last_sent_slot;
    bool have_last_sent_slot;
    uint32_t usb_samples_sent;
    uint32_t frames_sent;
    uint32_t frames_dropped;
    uint32_t usb_busy_count;
    uint32_t tx_buffer_overflows;
    uint32_t max_depth;
    uint32_t max_age_us;
    uint32_t stream_elapsed_us;
    uint32_t max_poll_us;
    uint16_t build_index;
    uint16_t build_crc_offset;
    uint16_t build_length;
    uint32_t build_crc;
} Stream_Core;

void StreamCore_Init(Stream_Core *core);
bool StreamCore_Push(Stream_Core *core, const Acq_Sample *sample);
uint32_t StreamCore_Depth(const Stream_Core *core);
void StreamCore_ObserveAge(Stream_Core *core, uint32_t now_us);
uint32_t StreamCore_Crc32(const uint8_t *data, size_t length);
uint16_t StreamCore_BuildData(Stream_Core *core, uint8_t *out, size_t capacity,
                              uint32_t now_us, bool flush);
uint16_t StreamCore_BeginData(Stream_Core *core, uint8_t *out, size_t capacity,
                              uint32_t now_us, bool flush);
bool StreamCore_BuildStep(Stream_Core *core, uint8_t *out, uint16_t max_samples,
                          uint16_t max_crc_bytes);
uint16_t StreamCore_BuildStats(Stream_Core *core, uint8_t *out, size_t capacity,
                               uint8_t type, const Stream_AcqMetrics *acq);
uint16_t StreamCore_BuildConfig(Stream_Core *core, uint8_t *out, size_t capacity);
uint16_t StreamCore_BuildError(Stream_Core *core, uint8_t *out, size_t capacity,
                               uint32_t error_code);
void StreamCore_CommitData(Stream_Core *core);
void StreamCore_CommitControl(Stream_Core *core);
void StreamCore_DropPending(Stream_Core *core);
void StreamCore_RecordBusy(Stream_Core *core);

#endif
