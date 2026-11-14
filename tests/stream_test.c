#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stream_core.h"

static Stream_Core stream;
static uint8_t frame[STREAM_FRAME_MAX];

static uint16_t read16(const uint8_t *p)
{
    return (uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8);
}

static uint32_t read32(const uint8_t *p)
{
    return (uint32_t)read16(p) | ((uint32_t)read16(p + 2) << 16);
}

static void push(uint32_t slot, uint32_t time_us, uint32_t raw)
{
    Acq_Sample sample = {raw, time_us, slot};
    assert(StreamCore_Push(&stream, &sample));
}

static void test_crc_and_packet(void)
{
    uint16_t size;
    uint8_t copy[STREAM_FRAME_MAX];
    assert(StreamCore_Crc32((const uint8_t *)"123456789", 9U) == 0xCBF43926U);
    StreamCore_Init(&stream);
    push(0U, 100U, 0xFFFFF0U);
    push(1U, 201U, 0x000010U);
    assert(StreamCore_BuildData(&stream, frame, sizeof(frame), 500U, false) == 0U);
    size = StreamCore_BuildData(&stream, frame, sizeof(frame), 500U, true);
    assert(size == 48U);
    assert(memcmp(frame, "CSF4", 4U) == 0);
    assert(frame[4] == 1U && frame[5] == STREAM_TYPE_DATA);
    assert(read16(frame + 8) == size && read16(frame + 10) == 2U);
    assert(read32(frame + 12) == 0U && read32(frame + 16) == 0U);
    assert((int32_t)read32(frame + 32) == -1);
    assert((int32_t)read32(frame + 36) == 1);
    assert(read16(frame + 40) == 0U && read16(frame + 42) == 101U);
    assert(read32(frame + size - 4U) == StreamCore_Crc32(frame, size - 4U));
    memcpy(copy, frame, size);
    StreamCore_RecordBusy(&stream);
    assert(stream.usb_busy_count == 1U && stream.pending_count == 2U);
    assert(memcmp(copy, frame, size) == 0); /* USB_BUSY must not rewrite USB-owned bytes. */
    StreamCore_CommitData(&stream);
    assert(stream.usb_samples_sent == 2U && stream.frames_sent == 1U);
    assert(StreamCore_Depth(&stream) == 0U && stream.next_sequence == 1U);
}

static void test_gaps_and_frame_sequence(void)
{
    uint16_t size;
    StreamCore_Init(&stream);
    push(0U, 100U, 0x100U);
    push(1U, 200U, 0x100U);
    push(4U, 500U, 0x100U);
    size = StreamCore_BuildData(&stream, frame, sizeof(frame), 600U, false);
    assert(size == 48U && read16(frame + 10) == 2U);
    StreamCore_CommitData(&stream);
    size = StreamCore_BuildData(&stream, frame, sizeof(frame), 4000U, true);
    assert(size == 42U && read32(frame + 12) == 1U);
    assert(read32(frame + 16) == 4U && read32(frame + 28) == 2U);
    StreamCore_CommitData(&stream);
    assert(stream.usb_samples_sent == 3U);
}

static void test_incremental_build_with_new_sample(void)
{
    uint32_t i;
    uint16_t size;
    StreamCore_Init(&stream);
    for (i = 0U; i < 32U; ++i) push(i, i * 100U, i << 4);
    size = StreamCore_BeginData(&stream, frame, sizeof(frame), 4000U, false);
    assert(size == STREAM_FRAME_MAX);
    assert(!StreamCore_BuildStep(&stream, frame, 4U, 32U));
    push(32U, 3200U, 0x12340U); /* Acquisition continues during packet assembly. */
    for (i = 0U; i < 20U && !StreamCore_BuildStep(&stream, frame, 4U, 32U); ++i) { }
    assert(i < 20U);
    assert(read32(frame + size - 4U) == StreamCore_Crc32(frame, size - 4U));
    StreamCore_CommitData(&stream);
    assert(StreamCore_Depth(&stream) == 1U);
    size = StreamCore_BuildData(&stream, frame, sizeof(frame), 5000U, true);
    assert(size == 42U && read32(frame + 16) == 32U);
}

static void test_overflow_drop_and_stats(void)
{
    uint32_t i;
    uint16_t size;
    Stream_AcqMetrics acq = {100U, 1U, 2U, 0U, 34U, 98U, 102U, 10000U, 11000U};
    StreamCore_Init(&stream);
    for (i = 0U; i < STREAM_RING_CAPACITY - 1U; ++i)
        push(i, i * 100U, i << 4);
    {
        Acq_Sample sample = {0U, 0U, 9999U};
        assert(!StreamCore_Push(&stream, &sample));
    }
    assert(stream.tx_buffer_overflows == 1U);
    assert(stream.max_depth == STREAM_RING_CAPACITY - 1U);
    StreamCore_ObserveAge(&stream, 250000U);
    assert(stream.max_age_us == 250000U);
    size = StreamCore_BuildData(&stream, frame, sizeof(frame), 250000U, false);
    assert(size == STREAM_FRAME_MAX);
    StreamCore_DropPending(&stream);
    assert(stream.frames_dropped == 1U && stream.next_sequence == 1U);
    size = StreamCore_BuildStats(&stream, frame, sizeof(frame), STREAM_TYPE_END, &acq);
    assert(size == STREAM_STATS_SIZE && frame[5] == STREAM_TYPE_END);
    assert(read32(frame + 32) == 100U);
    assert(read32(frame + 32 + 4U * 5U) == 1U);
    assert(read32(frame + 32 + 4U * 6U) == 2U);
    assert(read32(frame + 32 + 4U * 12U) == 0U);
    assert(read32(frame + size - 4U) == StreamCore_Crc32(frame, size - 4U));
    stream.usb_samples_sent = 54000000U;
    acq.stream_elapsed_us64 = 5400000000ULL;
    acq.stream_elapsed_us = (uint32_t)(acq.stream_elapsed_us64 & UINT32_MAX);
    size = StreamCore_BuildStats(&stream, frame, sizeof(frame), STREAM_TYPE_END, &acq);
    assert(read32(frame + 32 + 4U * 12U) == 10000U);
    assert(read32(frame + 32 + 4U * 17U) ==
           (uint32_t)(acq.stream_elapsed_us64 & UINT32_MAX));
    StreamCore_CommitControl(&stream);
    assert(stream.next_sequence == 2U);
    size = StreamCore_BuildConfig(&stream, frame, sizeof(frame));
    assert(size == 52U && frame[5] == STREAM_TYPE_CONFIG);
    assert(read16(frame + 32) == 0xAA28U);
    assert(read32(frame + size - 4U) == StreamCore_Crc32(frame, size - 4U));
    size = StreamCore_BuildError(&stream, frame, sizeof(frame), 1U);
    assert(size == 40U && frame[5] == STREAM_TYPE_ERROR);
    assert(read32(frame + 32) == 1U);
}

int main(void)
{
    memset(&stream, 0xA5, sizeof(stream));
    StreamCore_Init(&stream); stream.native_v2 = true;
    assert(StreamCore_Depth(&stream) == 0U && stream.pending_count == 0U);
    push(0U, 0xFFFFFFF0U, 0xABCDEFU); push(1U, 73U, 0x00001FU);
    assert(StreamCore_BuildData(&stream, frame, sizeof(frame), 1000U, true) == 48U);
    assert(frame[4] == 2U && read16(frame+24) == 0U && read16(frame+26) == 1U);
    assert(read32(frame+32) == 0xABCDEFU && read32(frame+36) == 0x1FU);
    assert(read16(frame+42) == 89U); /* real rollover-safe offset, not 84 */
    StreamCore_CommitData(&stream);
    assert(StreamCore_BuildConfig(&stream, frame, sizeof(frame)) == 52U);
    assert(read16(frame+32) == 0xAA68U && read16(frame+34) == 0U);
    test_crc_and_packet();
    test_gaps_and_frame_sequence();
    test_incremental_build_with_new_sample();
    test_overflow_drop_and_stats();
    puts("stream tests passed");
    return 0;
}
