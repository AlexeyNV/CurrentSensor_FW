#include "stream_transport.h"

#include <string.h>

#include "usbd_cdc_if.h"
#include "app.h"

static Stream_Core stream;
void StreamTransport_SetNativeV2(void) { stream.native_v2 = true; }
static uint8_t frame[STREAM_FRAME_MAX];
static uint16_t frame_length;
static uint8_t frame_type;
static volatile uint8_t tx_complete;
static bool active;
static bool stopping;
static bool finished;
static bool failed;
static bool inflight;
static bool link_idle;
static bool stats_requested;
static bool config_requested;
static uint32_t error_requested;
static uint32_t last_attempt_ms;
static bool attempted_once;
static uint32_t stop_started_ms;
static bool stop_clock_started;
static bool building;
static bool end_snapshot_valid;
static uint64_t end_elapsed_us64;
static Stream_Timing timing;

static void RecordBuild(uint32_t elapsed)
{
    if (elapsed > timing.build_max_us) timing.build_max_us = elapsed;
    timing.build_sum_us += elapsed;
    ++timing.build_calls;
    if (elapsed > 25U) ++timing.build_over_25us;
}

static void RecordSubmit(uint32_t elapsed)
{
    if (elapsed > timing.submit_max_us) timing.submit_max_us = elapsed;
    timing.submit_sum_us += elapsed;
    ++timing.submit_calls;
    if (elapsed > 25U) ++timing.submit_over_25us;
}

void StreamTransport_Start(void)
{
    StreamCore_Init(&stream);
    frame_length = 0U;
    frame_type = 0U;
    tx_complete = 0U;
    active = true;
    stopping = false;
    finished = false;
    failed = false;
    inflight = false;
    link_idle = false;
    stats_requested = false;
    config_requested = false;
    error_requested = 0U;
    last_attempt_ms = 0U;
    attempted_once = false;
    stop_started_ms = 0U;
    stop_clock_started = false;
    building = false;
    end_snapshot_valid = false;
    end_elapsed_us64 = 0U;
    memset(&timing, 0, sizeof(timing));
}

void StreamTransport_Push(const Acq_Sample *sample)
{
    if (active && sample != NULL) (void)StreamCore_Push(&stream, sample);
}

void StreamTransport_RequestStop(void)
{
    if (active) stopping = true;
}

void StreamTransport_RequestStats(void) { if (active) stats_requested = true; }
void StreamTransport_RequestConfig(void) { if (active) config_requested = true; }
void StreamTransport_RequestError(uint32_t code)
{
    if (active && code != 0U) error_requested = code;
}
void StreamTransport_RecordBusy(void) { StreamCore_RecordBusy(&stream); }
void StreamTransport_RecordPollDuration(uint32_t duration_us)
{
    if (duration_us > stream.max_poll_us) stream.max_poll_us = duration_us;
}
void StreamTransport_TxCompleteISR(void) { tx_complete = 1U; }
bool StreamTransport_IsActive(void) { return active; }
bool StreamTransport_IsFinished(void) { return finished; }
bool StreamTransport_Failed(void) { return failed; }
bool StreamTransport_EndElapsedUs64(uint64_t *elapsed_us64)
{
    if (!end_snapshot_valid || elapsed_us64 == NULL) return false;
    *elapsed_us64 = end_elapsed_us64;
    return true;
}
const Stream_Core *StreamTransport_GetCore(void) { return &stream; }
const Stream_Timing *StreamTransport_GetTiming(void) { return &timing; }

bool StreamTransport_Poll(bool usb_idle, bool configured, bool source_empty,
                          uint32_t now_us, uint32_t now_ms,
                          const Stream_AcqMetrics *acq, StreamClockFn clock_us)
{
    uint8_t result;
    uint32_t started;
    if (!active) return false;
    if (acq != NULL && !end_snapshot_valid)
        stream.stream_elapsed_us = acq->stream_elapsed_us;
    StreamCore_ObserveAge(&stream, now_us);
    if (usb_idle && !inflight) link_idle = true;
    if (stopping && !stop_clock_started)
    {
        stop_started_ms = now_ms;
        stop_clock_started = true;
    }
    if (!configured)
    {
        StreamCore_DropPending(&stream);
        active = false;
        finished = true;
        failed = true;
        return false;
    }
    if (inflight && tx_complete != 0U)
    {
        tx_complete = 0U;
        inflight = false;
        link_idle = true;
        if (frame_type == STREAM_TYPE_DATA) StreamCore_CommitData(&stream);
        else StreamCore_CommitControl(&stream);
        frame_length = 0U;
        if (frame_type == STREAM_TYPE_END)
        {
            active = false;
            finished = true;
            return false;
        }
    }
    if (stopping && (uint32_t)(now_ms - stop_started_ms) > 500U)
    {
        /* A disconnected or indefinitely busy link cannot block acquisition or stop. */
        StreamCore_DropPending(&stream);
        active = false;
        finished = true;
        failed = true;
        return false;
    }
    if (inflight || !link_idle) return false;
    if (building)
    {
        started = clock_us != NULL ? clock_us() : 0U;
        if (StreamCore_BuildStep(&stream, frame, stream.native_v2 ? 2U : 4U,
                                 stream.native_v2 ? 16U : 32U)) building = false;
        if (clock_us != NULL) RecordBuild(clock_us() - started);
        return false;
    }
    if (frame_length == 0U)
    {
        started = clock_us != NULL ? clock_us() : 0U;
        if (error_requested != 0U)
        {
            frame_type = STREAM_TYPE_ERROR;
            frame_length = StreamCore_BuildError(&stream, frame, sizeof(frame),
                                                 error_requested);
            error_requested = 0U;
        }
        else if (stats_requested)
        {
            frame_type = STREAM_TYPE_STATS;
            frame_length = StreamCore_BuildStats(&stream, frame, sizeof(frame),
                                                 frame_type, acq);
            stats_requested = false;
        }
        else if (config_requested)
        {
            frame_type = STREAM_TYPE_CONFIG;
            frame_length = StreamCore_BuildConfig(&stream, frame, sizeof(frame));
            config_requested = false;
        }
        else
        {
            frame_type = STREAM_TYPE_DATA;
            frame_length = StreamCore_BeginData(&stream, frame, sizeof(frame),
                                                now_us, stopping);
            if (frame_length != 0U) building = true;
            if (frame_length == 0U && stopping && source_empty &&
                StreamCore_Depth(&stream) == 0U)
            {
                frame_type = STREAM_TYPE_END;
                frame_length = StreamCore_BuildStats(&stream, frame, sizeof(frame),
                                                     frame_type, acq);
                if (frame_length != 0U)
                {
                    end_elapsed_us64 = acq->stream_elapsed_us64 != 0U ?
                        acq->stream_elapsed_us64 : acq->stream_elapsed_us;
                    end_snapshot_valid = true;
                    App_UsbEndQueued();
                }
            }
        }
        if (clock_us != NULL && frame_length != 0U)
            RecordBuild(clock_us() - started);
    }
    if (frame_length == 0U) return false;
    if (building) return false;
    if (attempted_once && last_attempt_ms == now_ms) return false;
    last_attempt_ms = now_ms;
    attempted_once = true;
    tx_complete = 0U;
    started = clock_us != NULL ? clock_us() : 0U;
    result = CDC_Transmit_FS(frame, frame_length);
    if (clock_us != NULL) RecordSubmit(clock_us() - started);
    if (result != USBD_OK)
    {
        StreamCore_RecordBusy(&stream);
        App_UsbTxBusy();
        return false;
    }
    inflight = true;
    link_idle = false;
    return true;
}
