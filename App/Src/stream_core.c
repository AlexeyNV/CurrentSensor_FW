#include "stream_core.h"

#include <string.h>

static void Put16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
}

static void Put32(uint8_t *p, uint32_t value)
{
    Put16(p, (uint16_t)value);
    Put16(p + 2, (uint16_t)(value >> 16));
}

static uint32_t CrcUpdate(uint32_t crc, const uint8_t *data, size_t length)
{
    static const uint32_t table[16] = {
        0x00000000U, 0x1DB71064U, 0x3B6E20C8U, 0x26D930ACU,
        0x76DC4190U, 0x6B6B51F4U, 0x4DB26158U, 0x5005713CU,
        0xEDB88320U, 0xF00F9344U, 0xD6D6A3E8U, 0xCB61B38CU,
        0x9B64C2B0U, 0x86D3D2D4U, 0xA00AE278U, 0xBDBDF21CU
    };
    size_t i;
    for (i = 0U; i < length; ++i)
    {
        crc ^= data[i];
        crc = (crc >> 4) ^ table[crc & 0xFU];
        crc = (crc >> 4) ^ table[crc & 0xFU];
    }
    return crc;
}

uint32_t StreamCore_Crc32(const uint8_t *data, size_t length)
{
    return CrcUpdate(0xFFFFFFFFU, data, length) ^ 0xFFFFFFFFU;
}

void StreamCore_Init(Stream_Core *core)
{
    /* Only published slots are readable. Reset metadata, not the large payload
       ring, while Native may already be receiving its first ready event. */
    memset((uint8_t *)core + offsetof(Stream_Core, profile), 0,
           sizeof(*core) - offsetof(Stream_Core, profile));
    core->profile = MeasurementProfile_Selected();
}

uint32_t StreamCore_Depth(const Stream_Core *core)
{
    return (uint16_t)(core->head - core->tail) & (STREAM_RING_CAPACITY - 1U);
}

bool StreamCore_Push(Stream_Core *core, const Acq_Sample *sample)
{
    uint16_t next = (uint16_t)((core->head + 1U) & (STREAM_RING_CAPACITY - 1U));
    uint32_t depth;
    if (next == core->tail)
    {
        ++core->tx_buffer_overflows;
        return false;
    }
    core->ring[core->head] = *sample;
    core->head = next;
    depth = StreamCore_Depth(core);
    if (depth > core->max_depth) core->max_depth = depth;
    return true;
}

void StreamCore_ObserveAge(Stream_Core *core, uint32_t now_us)
{
    if (core->head != core->tail)
    {
        uint32_t age = now_us - core->ring[core->tail].time_us;
        if (age > core->max_age_us) core->max_age_us = age;
    }
}

static void Header(const Stream_Core *core, uint8_t *out, uint8_t type, uint16_t length,
                   uint16_t count, uint32_t sequence, uint32_t first_slot,
                   uint32_t first_time_us, uint32_t lost)
{
    out[0] = 'C'; out[1] = 'S'; out[2] = 'F'; out[3] = '4';
    out[4] = core->native_v2 ? 2U : 1U;
    out[5] = type;
    Put16(out + 6, STREAM_HEADER_SIZE);
    Put16(out + 8, length);
    Put16(out + 10, count);
    Put32(out + 12, sequence);
    Put32(out + 16, first_slot);
    Put32(out + 20, first_time_us);
    Put16(out + 24, core->native_v2 ? 0U : core->profile->period_us);
    Put16(out + 26, core->native_v2 ? 1U : 0U);
    Put32(out + 28, lost);
}

uint16_t StreamCore_BeginData(Stream_Core *core, uint8_t *out, size_t capacity,
                              uint32_t now_us, bool flush)
{
    uint32_t depth = StreamCore_Depth(core);
    const Acq_Sample *first;
    uint16_t count = 0U;
    uint16_t length;
    uint32_t lost;
    if (out == NULL || capacity < STREAM_FRAME_MAX || core->pending_count != 0U ||
        depth == 0U) return 0U;
    first = &core->ring[core->tail];
    while (count < depth && count < STREAM_FRAME_SAMPLES)
    {
        const Acq_Sample *sample = &core->ring[(core->tail + count) &
                                                 (STREAM_RING_CAPACITY - 1U)];
        uint32_t delta = sample->time_us - first->time_us;
        if (sample->slot != first->slot + count || delta > 65535U) break;
        ++count;
    }
    if (count == 0U) return 0U;
    /* A gap cannot be filled later. Otherwise wait for a full 32-sample frame. */
    if (!flush && count == depth && count < STREAM_FRAME_SAMPLES &&
        (uint32_t)(now_us - first->time_us) < 4000U) return 0U;
    length = (uint16_t)(STREAM_HEADER_SIZE + 6U * count + 4U);
    lost = core->have_last_sent_slot ?
        first->slot - core->last_sent_slot - 1U : first->slot;
    Header(core, out, STREAM_TYPE_DATA, length, count, core->next_sequence,
           first->slot, first->time_us, lost);
    core->pending_count = count;
    core->pending_last_slot = first->slot + count - 1U;
    core->build_index = 0U;
    core->build_crc_offset = 0U;
    core->build_length = length;
    core->build_crc = 0xFFFFFFFFU;
    return length;
}

bool StreamCore_BuildStep(Stream_Core *core, uint8_t *out, uint16_t max_samples,
                          uint16_t max_crc_bytes)
{
    uint16_t processed = 0U;
    uint16_t remaining;
    uint16_t chunk;
    const Acq_Sample *first;
    if (core->pending_count == 0U || core->build_length == 0U || out == NULL)
        return false;
    first = &core->ring[core->tail];
    while (core->build_index < core->pending_count && processed < max_samples)
    {
        uint16_t index = core->build_index;
        const Acq_Sample *sample = &core->ring[(core->tail + index) &
                                                (STREAM_RING_CAPACITY - 1U)];
        Put32(out + STREAM_HEADER_SIZE + 4U * index,
              core->native_v2 ? (sample->raw & 0xFFFFFFU) : (uint32_t)AcqCore_Decode(sample->raw));
        Put16(out + STREAM_HEADER_SIZE + 4U * core->pending_count + 2U * index,
              (uint16_t)(sample->time_us - first->time_us));
        ++core->build_index;
        ++processed;
    }
    if (core->build_index != core->pending_count) return false;
    remaining = (uint16_t)(core->build_length - 4U - core->build_crc_offset);
    chunk = remaining < max_crc_bytes ? remaining : max_crc_bytes;
    if (chunk != 0U)
    {
        core->build_crc = CrcUpdate(core->build_crc,
                                    out + core->build_crc_offset, chunk);
        core->build_crc_offset = (uint16_t)(core->build_crc_offset + chunk);
    }
    if (core->build_crc_offset != core->build_length - 4U) return false;
    Put32(out + core->build_length - 4U, core->build_crc ^ 0xFFFFFFFFU);
    return true;
}

uint16_t StreamCore_BuildData(Stream_Core *core, uint8_t *out, size_t capacity,
                              uint32_t now_us, bool flush)
{
    uint16_t length = StreamCore_BeginData(core, out, capacity, now_us, flush);
    if (length == 0U) return 0U;
    while (!StreamCore_BuildStep(core, out, STREAM_FRAME_SAMPLES,
                                 STREAM_FRAME_MAX)) { }
    return length;
}

uint16_t StreamCore_BuildStats(Stream_Core *core, uint8_t *out, size_t capacity,
                               uint8_t type, const Stream_AcqMetrics *acq)
{
    uint32_t values[19];
    uint16_t i;
    if (out == NULL || acq == NULL || capacity < STREAM_STATS_SIZE ||
        (type != STREAM_TYPE_STATS && type != STREAM_TYPE_END)) return 0U;
    Header(core, out, type, STREAM_STATS_SIZE, 0U, core->next_sequence,
           0U, 0U, 0U);
    values[0] = acq->adc_samples;
    values[1] = core->usb_samples_sent;
    values[2] = core->frames_sent;
    values[3] = core->frames_dropped;
    values[4] = core->usb_busy_count;
    values[5] = core->tx_buffer_overflows;
    values[6] = acq->acq_buffer_overflows;
    values[7] = acq->lost_sample_slots;
    values[8] = acq->spi_errors;
    values[9] = acq->max_service_us;
    values[10] = acq->min_sample_period_us;
    values[11] = acq->max_sample_period_us;
    values[12] = (acq->stream_elapsed_us64 || acq->stream_elapsed_us) ?
        (uint32_t)(((uint64_t)core->usb_samples_sent * 1000000ULL) /
                   (acq->stream_elapsed_us64 ? acq->stream_elapsed_us64 :
                    acq->stream_elapsed_us)) : 0U;
    values[13] = StreamCore_Depth(core);
    values[14] = core->max_depth;
    values[15] = core->max_age_us;
    values[16] = acq->elapsed_us;
    values[17] = acq->stream_elapsed_us;
    values[18] = core->max_poll_us;
    for (i = 0U; i < 19U; ++i) Put32(out + STREAM_HEADER_SIZE + 4U * i, values[i]);
    Put32(out + STREAM_STATS_SIZE - 4U,
          StreamCore_Crc32(out, STREAM_STATS_SIZE - 4U));
    return STREAM_STATS_SIZE;
}

uint16_t StreamCore_BuildConfig(Stream_Core *core, uint8_t *out, size_t capacity)
{
    const uint16_t length = STREAM_HEADER_SIZE + 16U + 4U;
    if (out == NULL || capacity < length) return 0U;
    Header(core, out, STREAM_TYPE_CONFIG, length, 0U, core->next_sequence,
           0U, 0U, 0U);
    Put16(out + 32, core->native_v2 ? 0xAA68U : core->profile->adc_config);
    Put16(out + 34, core->native_v2 ? 0U : core->profile->period_us);
    Put16(out + 36, STREAM_FRAME_SAMPLES);
    Put16(out + 38, 4U);
    Put16(out + 40, STREAM_RING_CAPACITY);
    Put16(out + 42, ACQ_RING_CAPACITY);
    Put32(out + 44, 0U);
    Put32(out + length - 4U, StreamCore_Crc32(out, length - 4U));
    return length;
}

uint16_t StreamCore_BuildError(Stream_Core *core, uint8_t *out, size_t capacity,
                               uint32_t error_code)
{
    const uint16_t length = STREAM_HEADER_SIZE + 4U + 4U;
    if (out == NULL || capacity < length) return 0U;
    Header(core, out, STREAM_TYPE_ERROR, length, 0U, core->next_sequence,
           0U, 0U, 0U);
    Put32(out + STREAM_HEADER_SIZE, error_code);
    Put32(out + length - 4U, StreamCore_Crc32(out, length - 4U));
    return length;
}

void StreamCore_CommitData(Stream_Core *core)
{
    if (core->pending_count == 0U) return;
    core->tail = (uint16_t)((core->tail + core->pending_count) &
                             (STREAM_RING_CAPACITY - 1U));
    core->usb_samples_sent += core->pending_count;
    core->last_sent_slot = core->pending_last_slot;
    core->have_last_sent_slot = true;
    core->pending_count = 0U;
    core->build_length = 0U;
    ++core->frames_sent;
    ++core->next_sequence;
}

void StreamCore_CommitControl(Stream_Core *core) { ++core->next_sequence; }

void StreamCore_DropPending(Stream_Core *core)
{
    if (core->pending_count != 0U)
    {
        core->tail = (uint16_t)((core->tail + core->pending_count) &
                                 (STREAM_RING_CAPACITY - 1U));
        core->pending_count = 0U;
        core->build_length = 0U;
        ++core->frames_dropped;
        ++core->next_sequence;
    }
}

void StreamCore_RecordBusy(Stream_Core *core) { ++core->usb_busy_count; }
