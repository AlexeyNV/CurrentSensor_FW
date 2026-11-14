#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "stream_transport.h"

static uint32_t calls;
static uint8_t *owned_buffer;
static uint16_t owned_length;
static uint8_t owned_copy[STREAM_FRAME_MAX];
static uint32_t fake_clock;
static uint32_t end_queued;
static uint32_t tx_busy;

void App_UsbEndQueued(void) { ++end_queued; }
void App_UsbTxBusy(void) { ++tx_busy; }

static uint32_t clock_us(void) { return ++fake_clock; }

static uint32_t read32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

uint8_t CDC_Transmit_FS(uint8_t *buffer, uint16_t length)
{
    ++calls;
    if (calls == 1U) return 1U; /* USB_BUSY */
    owned_buffer = buffer;
    owned_length = length;
    memcpy(owned_copy, buffer, length);
    return 0U; /* USBD_OK */
}

static void test_busy_retry_and_stop(void)
{
    Stream_AcqMetrics acq = {33U, 0U, 0U, 0U, 34U, 98U, 102U, 3300U, 3400U};
    const Stream_Core *core;
    uint32_t i;
    uint32_t now_ms = 10U;
    calls = 0U;
    end_queued = 0U;
    tx_busy = 0U;
    fake_clock = 0U;
    StreamTransport_Start();
    for (i = 0U; i < 32U; ++i)
    {
        Acq_Sample sample = {i << 4, 100U * i, i};
        StreamTransport_Push(&sample);
    }
    assert(!StreamTransport_Poll(true, true, false, 4000U, now_ms++, &acq,
                                 clock_us));
    core = StreamTransport_GetCore();
    assert(core->pending_count == 32U && calls == 0U);
    for (i = 0U; i < 30U && calls < 1U; ++i)
        (void)StreamTransport_Poll(true, true, false, 4001U + i, now_ms++,
                                   &acq, clock_us);
    assert(core->usb_busy_count == 1U);
    assert(tx_busy == 1U);
    assert(calls == 1U);
    assert(!StreamTransport_Poll(true, true, false, 5000U, now_ms - 1U,
                                 &acq, clock_us));
    assert(calls == 1U); /* One retry per millisecond. */
    assert(StreamTransport_Poll(true, true, false, 5001U, now_ms++, &acq,
                                clock_us));
    assert(owned_length == STREAM_FRAME_MAX);
    {
        Acq_Sample sample = {0x200U, 3200U, 32U};
        StreamTransport_Push(&sample);
    }
    assert(memcmp(owned_buffer, owned_copy, owned_length) == 0);
    StreamTransport_RequestStop();
    StreamTransport_TxCompleteISR();
    for (i = 0U; i < 30U && calls < 3U; ++i)
        (void)StreamTransport_Poll(false, true, true, 5002U + i, now_ms++,
                                   &acq, clock_us);
    assert(calls == 3U && owned_buffer[5] == STREAM_TYPE_DATA);
    assert(core->usb_samples_sent == 32U && core->frames_sent == 1U);
    StreamTransport_TxCompleteISR();
    for (i = 0U; i < 30U && calls < 4U; ++i)
        (void)StreamTransport_Poll(false, true, true, 5100U + i, now_ms++,
                                   &acq, clock_us);
    assert(owned_buffer[5] == STREAM_TYPE_END);
    assert(end_queued == 1U);
    assert(!StreamTransport_IsFinished()); /* queued is not delivered */
    assert(core->usb_samples_sent == 33U && core->frames_sent == 2U);
    StreamTransport_TxCompleteISR();
    assert(!StreamTransport_Poll(false, true, true, 5200U, now_ms++, &acq,
                                 clock_us));
    assert(StreamTransport_IsFinished() && !StreamTransport_IsActive());
    assert(StreamTransport_GetTiming()->build_calls > 2U);
    assert(StreamTransport_GetTiming()->submit_calls == 4U);
}

static void test_disconnect_does_not_claim_delivery(void)
{
    Stream_AcqMetrics acq = {32U, 0U, 0U, 0U, 34U, 98U, 102U, 3200U, 3300U};
    uint32_t i;
    uint32_t now_ms = 10U;
    calls = 0U;
    StreamTransport_Start();
    for (i = 0U; i < 32U; ++i)
    {
        Acq_Sample sample = {0U, i * 100U, i};
        StreamTransport_Push(&sample);
    }
    for (i = 0U; i < 30U && calls < 2U; ++i)
        (void)StreamTransport_Poll(true, true, false, 4000U + i, now_ms++,
                                   &acq, clock_us);
    assert(calls == 2U);
    /* USB deinit must not synthesize a completed TX callback. */
    assert(!StreamTransport_Poll(false, false, false, 5000U, now_ms++, &acq,
                                 clock_us));
    assert(StreamTransport_Failed());
    assert(StreamTransport_GetCore()->usb_samples_sent == 0U);
    assert(StreamTransport_GetCore()->frames_dropped == 1U);
}

static void test_end_without_tx_completion_is_incomplete(void)
{
    Stream_AcqMetrics acq = {0};
    uint32_t ms = 20U;
    calls = 0U;
    StreamTransport_Start();
    StreamTransport_RequestStop();
    assert(!StreamTransport_Poll(true, true, true, 1000U, ms++, &acq,
                                 clock_us)); /* first attempt reports USB_BUSY */
    assert(StreamTransport_Poll(true, true, true, 1001U, ms++, &acq,
                                clock_us));
    assert(owned_buffer[5] == STREAM_TYPE_END);
    assert(!StreamTransport_IsFinished());
    assert(!StreamTransport_Poll(false, true, true, 1002U, ms + 501U,
                                 &acq, clock_us));
    assert(StreamTransport_IsFinished() && StreamTransport_Failed());
}

static void test_end_duration_is_frozen_through_tx_and_rollover(void)
{
    Stream_AcqMetrics acq = {0};
    uint64_t frozen = 0U;
    const uint64_t end_us64 = 5402197401ULL;
    calls = 0U;
    acq.stream_elapsed_us64 = end_us64;
    acq.stream_elapsed_us = (uint32_t)end_us64;
    StreamTransport_Start();
    assert(!StreamTransport_EndElapsedUs64(&frozen));
    StreamTransport_RequestStop();
    assert(!StreamTransport_Poll(true, true, true, 1000U, 20U, &acq,
                                 clock_us)); /* END built; first submit busy */
    assert(StreamTransport_EndElapsedUs64(&frozen) && frozen == end_us64);
    acq.stream_elapsed_us64 += 667U; /* USB completion happens later. */
    acq.stream_elapsed_us = (uint32_t)acq.stream_elapsed_us64;
    assert(StreamTransport_Poll(true, true, true, 1667U, 21U, &acq,
                                clock_us));
    assert(owned_buffer[5] == STREAM_TYPE_END);
    assert(read32(owned_buffer + 32U + 17U * 4U) == (uint32_t)end_us64);
    StreamTransport_TxCompleteISR();
    assert(!StreamTransport_Poll(false, true, true, 1800U, 22U, &acq,
                                 clock_us));
    assert(StreamTransport_IsFinished() && !StreamTransport_Failed());
    assert(StreamTransport_EndElapsedUs64(&frozen) && frozen == end_us64);
    acq.stream_elapsed_us64 += 1000000U;
    acq.stream_elapsed_us = (uint32_t)acq.stream_elapsed_us64;
    assert(!StreamTransport_Poll(true, true, true, 2000000U, 1022U,
                                 &acq, clock_us));
    assert(StreamTransport_EndElapsedUs64(&frozen) && frozen == end_us64);
    StreamTransport_Start();
    assert(!StreamTransport_EndElapsedUs64(&frozen));
}

static void test_slow_usb_does_not_drop_acquisition_samples(void)
{
    Stream_AcqMetrics acq = {0U, 0U, 0U, 0U, 35U, 98U, 103U, 0U, 0U};
    uint32_t ms;
    uint32_t slot = 0U;
    uint32_t launched_ms = 0U;
    bool pending = false;
    calls = 0U;
    StreamTransport_Start();
    for (ms = 1U; ms < 1000U; ++ms)
    {
        uint32_t substep;
        for (substep = 0U; substep < 10U; ++substep)
        {
            Acq_Sample sample = {slot << 4, slot * 100U, slot};
            StreamTransport_Push(&sample);
            ++slot;
        }
        acq.adc_samples = slot;
        if (pending && ms - launched_ms >= 2U)
        {
            StreamTransport_TxCompleteISR();
            pending = false;
        }
        for (substep = 0U; substep < 50U; ++substep)
        {
            if (StreamTransport_Poll(!pending, true, false, ms * 1000U,
                                     ms, &acq, clock_us))
            {
                pending = true;
                launched_ms = ms;
            }
        }
    }
    StreamTransport_RequestStop();
    for (; ms < 1200U && !StreamTransport_IsFinished(); ++ms)
    {
        uint32_t substep;
        if (pending && ms - launched_ms >= 2U)
        {
            StreamTransport_TxCompleteISR();
            pending = false;
        }
        for (substep = 0U; substep < 50U && !StreamTransport_IsFinished(); ++substep)
        {
            if (StreamTransport_Poll(!pending, true, true, ms * 1000U,
                                     ms, &acq, clock_us))
            {
                pending = true;
                launched_ms = ms;
            }
        }
    }
    assert(StreamTransport_IsFinished() && !StreamTransport_Failed());
    assert(StreamTransport_GetCore()->usb_samples_sent == slot);
    assert(StreamTransport_GetCore()->tx_buffer_overflows == 0U);
    assert(StreamTransport_GetCore()->frames_dropped == 0U);
}

int main(void)
{
    test_busy_retry_and_stop();
    test_disconnect_does_not_claim_delivery();
    test_end_without_tx_completion_is_incomplete();
    test_end_duration_is_frozen_through_tx_and_rollover();
    test_slow_usb_does_not_drop_acquisition_samples();
    puts("stream transport tests passed");
    return 0;
}
