#include "app.h"

#include <stdio.h>
#include <string.h>

#include "calibration.h"
#include "format64.h"
#include "acquisition.h"
#include "acquisition_core.h"
#include "acquisition_dma.h"
#include "stream_transport.h"
#include "ina229.h"
#include "main.h"
#include "spi.h"
#include "spi_control.h"
#include "tim.h"
#include "timebase.h"
#include "spi_freeze.h"
#include "pair_diagnostic.h"
#include "usb_device.h"
#include "usbd_cdc_if.h"

#define APP_RX_RING_SIZE 256U
#define APP_LINE_SIZE     96U
#define APP_TX_SIZE       8192U
#define APP_USB_TRACE_SIZE 16U

extern USBD_HandleTypeDef hUsbDeviceFS;

static INA229_HandleTypeDef ina;
static uint8_t rx_ring[APP_RX_RING_SIZE];
static volatile uint16_t rx_head;
static volatile uint16_t rx_tail;
static volatile uint8_t rx_overflow;
static char line[APP_LINE_SIZE];
static uint8_t line_length;
static uint8_t line_discard;
static uint8_t last_was_cr;
static uint8_t tx_buffer[APP_TX_SIZE];
static uint16_t tx_length;
static uint8_t tx_pending;
static volatile uint8_t tx_ready;
static volatile uint8_t text_tx_inflight;
static uint32_t text_tx_started_us;
static uint32_t text_responses_queued;
static volatile uint32_t text_responses_done;
static uint32_t text_tx_submit_attempts;
static uint32_t text_tx_busy;
static volatile uint32_t text_tx_max_wait_us;
static uint32_t text_format_errors;
static uint16_t text_max_length;
static volatile bool streaming_active;
static volatile bool stream_timing_active;
static volatile uint32_t cdc_rx_packets;
static volatile uint32_t cdc_rx_bytes;
static volatile uint32_t cdc_out_rearm_errors;
static volatile uint32_t cdc_control_line_events;
static volatile uint16_t cdc_last_control_line;
static volatile uint32_t cdc_tx_completions;
static uint32_t stream_stop_commands;
static uint32_t stream_end_completed;
static volatile uint32_t cdc_rx_rearms;
static volatile uint32_t cdc_control_requests;
static volatile uint32_t usb_tx_busy;
static volatile uint32_t usb_irq_count;
static volatile uint32_t usb_reset_count;
static volatile uint32_t dma_abort_errors;
static volatile uint32_t cdc_tx_aborts;
static volatile uint32_t cdc_tx_abort_errors;
static volatile uint8_t cdc_dtr_drop_pending;
static uint32_t cdc_abort_last_attempt_ms;
static uint32_t stop_received;
static uint32_t stop_processed;
static uint32_t end_queued;
static bool stream_stop_seen;
typedef struct { uint32_t us; uint8_t event; } App_UsbTraceEntry;
static App_UsbTraceEntry usb_trace[APP_USB_TRACE_SIZE];
static volatile uint32_t usb_trace_count;
static bool stream_stop_error;
static uint32_t stream_text_last_attempt_ms = UINT32_MAX;
static Acq_Core stream_snapshot;
static AcqDma_Core stream_dma_snapshot;
static bool have_stream_snapshot;
static uint32_t stream_session_id;
static uint32_t last_stream_session_id;
static Acq_Mode last_stream_mode;
static uint64_t stream_elapsed_us64;
static uint32_t last_stream_spi_hz;

typedef struct
{
    volatile uint32_t usb_irq_max_us;
    volatile uint32_t usb_irq_sum_us;
    volatile uint32_t usb_irq_calls;
    volatile uint32_t usb_irq_over_25us;
    uint32_t rx_max_us;
    uint32_t rx_sum_us;
    uint32_t rx_calls;
    uint32_t rx_over_25us;
    uint32_t forward_max_us;
    uint32_t forward_sum_us;
    uint32_t forward_calls;
    uint32_t forward_over_25us;
} App_StreamTiming;

static App_StreamTiming stream_timing;

static uint32_t App_NowUs(void) { return __HAL_TIM_GET_COUNTER(&htim2); }

static void App_UsbTrace(App_UsbEvent event)
{
    /* Only two word stores and one counter update with IRQs masked. */
    uint32_t mask = __get_PRIMASK();
    __disable_irq();
    usb_trace[usb_trace_count % APP_USB_TRACE_SIZE].us = App_NowUs();
    usb_trace[usb_trace_count % APP_USB_TRACE_SIZE].event = (uint8_t)event;
    ++usb_trace_count;
    __set_PRIMASK(mask);
}

static int App_FormatUsbDiag(char *out, size_t size)
{
    const USBD_CDC_HandleTypeDef *cdc =
        (const USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
    uint32_t count = usb_trace_count;
    uint32_t first = count > APP_USB_TRACE_SIZE ? count - APP_USB_TRACE_SIZE : 0U;
    size_t used = 0U;
    uint32_t i;
    int n = snprintf(out, size,
        "USB_DIAG_REV: %u\r\nCDC_RX_CALLBACKS: %lu\r\nCDC_RX_REARM: %lu\r\n"
        "CDC_RX_REARM_ERRORS: %lu\r\nSTOP_RECEIVED: %lu\r\nSTOP_PROCESSED: %lu\r\n"
        "END_QUEUED: %lu\r\nEND_TX_COMPLETED: %lu\r\n"
        "USB_CONTROL_REQUESTS: %lu\r\nUSB_TX_BUSY: %lu\r\n"
        "USB_IRQ_COUNT: %lu\r\nUSB_RESET_COUNT: %lu\r\n"
        "SPI_OWNER_CONFLICTS: %lu\r\nDMA_ABORT_ERRORS: %lu\r\n"
        "CDC_TX_ABORTS: %lu\r\nCDC_TX_ABORT_ERRORS: %lu\r\n"
        "TEXT_RESPONSES_QUEUED: %lu\r\nTEXT_RESPONSES_DONE: %lu\r\n"
        "TEXT_TX_SUBMIT_ATTEMPTS: %lu\r\nTEXT_TX_BUSY: %lu\r\n"
        "TEXT_TX_MAX_WAIT_US: %lu\r\nTEXT_FORMAT_ERRORS: %lu\r\n"
        "TEXT_LAST_LENGTH: %u\r\nTEXT_MAX_LENGTH: %u\r\n"
        "USB_DEVICE_STATE: %u\r\nCDC_TX_STATE: %lu\r\n"
        "TX_READY: %u\r\nTX_PENDING: %u\r\n"
        "RX_RING_DEPTH: %u\r\nACQ_RUNNING: %u\r\nSTREAM_ACTIVE: %u\r\n"
        "CDC_DTR: %u\r\nUSB_TRACE_COUNT: %lu\r\n",
        APP_USB_DIAG_REV, (unsigned long)cdc_rx_packets,
        (unsigned long)cdc_rx_rearms, (unsigned long)cdc_out_rearm_errors,
        (unsigned long)stop_received, (unsigned long)stop_processed,
        (unsigned long)end_queued, (unsigned long)stream_end_completed,
        (unsigned long)cdc_control_requests, (unsigned long)usb_tx_busy,
        (unsigned long)usb_irq_count, (unsigned long)usb_reset_count,
        (unsigned long)INA229_ContentionCount(), (unsigned long)dma_abort_errors,
        (unsigned long)cdc_tx_aborts, (unsigned long)cdc_tx_abort_errors,
        (unsigned long)text_responses_queued,
        (unsigned long)text_responses_done,
        (unsigned long)text_tx_submit_attempts,
        (unsigned long)text_tx_busy,
        (unsigned long)text_tx_max_wait_us,
        (unsigned long)text_format_errors,
        (unsigned int)tx_length, (unsigned int)text_max_length,
        (unsigned int)hUsbDeviceFS.dev_state,
        cdc != NULL ? (unsigned long)cdc->TxState : 0UL,
        tx_ready, tx_pending,
        (unsigned int)((rx_head + APP_RX_RING_SIZE - rx_tail) % APP_RX_RING_SIZE),
        Acq_IsRunning() ? 1U : 0U, StreamTransport_IsActive() ? 1U : 0U,
        cdc_last_control_line & 1U, (unsigned long)count);
    if (n < 0 || (size_t)n >= size) return n;
    used = (size_t)n;
    for (i = first; i < count; ++i)
    {
        App_UsbTraceEntry entry = usb_trace[i % APP_USB_TRACE_SIZE];
        n = snprintf(out + used, size - used, "USB_TRACE_%lu: %lu,%u\r\n",
            (unsigned long)i, (unsigned long)entry.us, entry.event);
        if (n < 0 || (size_t)n >= size - used) return (int)(used + (size_t)n);
        used += (size_t)n;
    }
    n = snprintf(out + used, size - used, "STATUS: OK\r\n");
    return n < 0 ? n : (int)(used + (size_t)n);
}

static void App_ResetStreamTiming(void)
{
    stream_timing.usb_irq_max_us = 0U;
    stream_timing.usb_irq_sum_us = 0U;
    stream_timing.usb_irq_calls = 0U;
    stream_timing.usb_irq_over_25us = 0U;
    stream_timing.rx_max_us = 0U;
    stream_timing.rx_sum_us = 0U;
    stream_timing.rx_calls = 0U;
    stream_timing.rx_over_25us = 0U;
    stream_timing.forward_max_us = 0U;
    stream_timing.forward_sum_us = 0U;
    stream_timing.forward_calls = 0U;
    stream_timing.forward_over_25us = 0U;
}

static void App_QueueText(const char *message)
{
    size_t length = strlen(message);
    if (length >= sizeof(tx_buffer))
    {
        message = "ERR: INTERNAL\r\n";
        length = strlen(message);
    }
    memcpy(tx_buffer, message, length);
    tx_length = (uint16_t)length;
    if (tx_length > text_max_length) text_max_length = tx_length;
    tx_pending = 1U;
    ++text_responses_queued;
}

static void App_QueueFormatted(int length)
{
    if (length < 0 || (size_t)length >= sizeof(tx_buffer))
    {
        ++text_format_errors;
        App_QueueText("ERR: INTERNAL\r\n");
        return;
    }
    tx_length = (uint16_t)length;
    if (tx_length > text_max_length) text_max_length = tx_length;
    tx_pending = 1U;
    ++text_responses_queued;
}

static void App_CalibrationCommand(void)
{
    Calibration_Command command;
    Calibration_Status status = Calibration_ParseCommand(line, &command);
    const Calibration_State *cal;

    if (status == CAL_OK)
    {
        switch (command.type)
        {
        case CAL_CMD_SHOW:
            cal = Calibration_GetState();
            App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "CAL:\r\nOFFSET_UA: %ld\r\nGAIN_PPM: %lu\r\nENABLED: %u\r\n"
                "SOURCE: %s\r\nDIRTY: %u\r\nSTATUS: OK\r\n",
                (long)cal->parameters.offset_ua,
                (unsigned long)cal->parameters.gain_ppm,
                cal->parameters.enabled ? 1U : 0U,
                Calibration_SourceText(cal->source), cal->dirty ? 1U : 0U));
            return;
        case CAL_CMD_SET_OFFSET:
            status = Calibration_SetOffset(command.offset_ua);
            break;
        case CAL_CMD_SET_GAIN:
            status = Calibration_SetGain(command.gain_ppm);
            break;
        case CAL_CMD_ENABLE:
            Calibration_SetEnabled(command.enabled);
            break;
        case CAL_CMD_SAVE:
            status = Calibration_Save(streaming_active);
            break;
        case CAL_CMD_LOAD:
            status = Calibration_Load();
            break;
        case CAL_CMD_RESET:
            Calibration_Reset();
            break;
        }
    }
    if (status == CAL_OK)
    {
        App_QueueText("STATUS: OK\r\n");
    }
    else
    {
        App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
            "ERR: %s\r\n", Calibration_StatusText(status)));
    }
}

static void App_Measure(void)
{
    uint16_t config;
    uint32_t vshunt_raw;
    uint32_t vbus_raw;
    uint16_t dietemp_raw;
    uint32_t shunt_bits;
    int32_t shunt_code;
    int32_t temp_code;
    int64_t shunt_mv_scaled;
    int64_t current_ma_scaled;
    int64_t current_cal_na;
    int64_t temp_c_scaled;
    uint64_t shunt_magnitude;
    uint64_t current_magnitude;
    uint64_t current_cal_magnitude;
    uint64_t temp_magnitude;
    uint64_t bus_v_scaled;
    uint64_t bus_fraction;
    INA229_Status status;

    status = INA229_ReadReg16(&ina, INA229_REG_CONFIG, &config);
    if (status == INA229_OK)
    {
        status = INA229_ReadVShunt(&ina, &vshunt_raw);
    }
    if (status == INA229_OK)
    {
        status = INA229_ReadVBus(&ina, &vbus_raw);
    }
    if (status == INA229_OK)
    {
        status = INA229_ReadDieTemp(&ina, &dietemp_raw);
    }
    if (status != INA229_OK)
    {
        App_QueueText((status == INA229_TIMEOUT) ? "STATUS: SPI_TIMEOUT\r\n" : "STATUS: SPI_ERROR\r\n");
        return;
    }

    /* Bits 23:4 are a signed 20-bit two's-complement number. */
    shunt_bits = (vshunt_raw >> 4) & 0xFFFFFU;
    shunt_code = (shunt_bits & 0x80000U) != 0U ?
        (int32_t)shunt_bits - 0x100000L : (int32_t)shunt_bits;
    temp_code = (dietemp_raw & 0x8000U) != 0U ?
        (int32_t)dietemp_raw - 0x10000L : (int32_t)dietemp_raw;

    /* Fixed-point scales: 10^9 per mV, 10^6 per mA,
       10^10 per V, and 10^7 per degree C. */
    shunt_mv_scaled = (int64_t)shunt_code *
        (((config & INA229_CONFIG_ADCRANGE) != 0U) ? 78125LL : 312500LL);
    current_ma_scaled = (int64_t)shunt_code *
        (((config & INA229_CONFIG_ADCRANGE) != 0U) ? 15625LL : 62500LL);
    if (Calibration_ApplyRawCurrent(current_ma_scaled, &current_cal_na) != CAL_OK)
    {
        App_QueueText("STATUS: CAL_OVERFLOW\r\n");
        return;
    }
    bus_v_scaled = (uint64_t)(vbus_raw >> 4) * 1953125ULL;
    temp_c_scaled = (int64_t)temp_code * 78125LL;

    shunt_magnitude = (uint64_t)(shunt_mv_scaled < 0 ? -shunt_mv_scaled : shunt_mv_scaled);
    current_magnitude = (uint64_t)(current_ma_scaled < 0 ? -current_ma_scaled : current_ma_scaled);
    current_cal_magnitude = (uint64_t)(current_cal_na < 0 ? -current_cal_na : current_cal_na);
    temp_magnitude = (uint64_t)(temp_c_scaled < 0 ? -temp_c_scaled : temp_c_scaled);
    bus_fraction = bus_v_scaled % 10000000000ULL;

    App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
        "VSHUNT_RAW: 0x%06lX\r\n"
        "VSHUNT_CODE: %ld\r\n"
        "VSHUNT_mV: %s%lu.%09lu\r\n"
        "CURRENT_mA: %s%lu.%06lu\r\n"
        "CURRENT_CAL_mA: %s%lu.%06lu\r\n"
        "CAL_ENABLED: %u\r\n"
        "VBUS_RAW: 0x%06lX\r\n"
        "VBUS_V: %lu.%05lu%05lu\r\n"
        "DIETEMP_RAW: 0x%04X\r\n"
        "DIETEMP_C: %s%lu.%07lu\r\n"
        "STATUS: OK\r\n",
        (unsigned long)vshunt_raw,
        (long)shunt_code,
        (shunt_mv_scaled < 0) ? "-" : "",
        (unsigned long)(shunt_magnitude / 1000000000ULL),
        (unsigned long)(shunt_magnitude % 1000000000ULL),
        (current_ma_scaled < 0) ? "-" : "",
        (unsigned long)(current_magnitude / 1000000ULL),
        (unsigned long)(current_magnitude % 1000000ULL),
        (current_cal_na < 0) ? "-" : "",
        (unsigned long)(current_cal_magnitude / 1000000ULL),
        (unsigned long)(current_cal_magnitude % 1000000ULL),
        Calibration_GetState()->parameters.enabled ? 1U : 0U,
        (unsigned long)vbus_raw,
        (unsigned long)(bus_v_scaled / 10000000000ULL),
        (unsigned long)(bus_fraction / 100000ULL),
        (unsigned long)(bus_fraction % 100000ULL),
        (unsigned int)dietemp_raw,
        (temp_c_scaled < 0) ? "-" : "",
        (unsigned long)(temp_magnitude / 10000000ULL),
        (unsigned long)(temp_magnitude % 10000000ULL)));
}

static int App_FormatStreamTiming(char *out, size_t size)
{
    const Stream_Timing *transport = StreamTransport_GetTiming();
    uint32_t over_budget = transport->build_over_25us +
        transport->submit_over_25us + stream_timing.rx_over_25us +
        stream_timing.forward_over_25us + stream_timing.usb_irq_over_25us;
    return snprintf(out, size,
        "SESSION_TYPE: STREAM\r\nSESSION_ID: %lu\r\n"
        "MAX_BUILD_US: %lu\r\nAVG_BUILD_US: %lu\r\nBUILD_CALLS: %lu\r\n"
        "MAX_USB_SUBMIT_US: %lu\r\nAVG_USB_SUBMIT_US: %lu\r\nSUBMIT_CALLS: %lu\r\n"
        "MAX_USB_IRQ_US: %lu\r\nAVG_USB_IRQ_US: %lu\r\nUSB_IRQ_CALLS: %lu\r\n"
        "MAX_RX_PROCESS_US: %lu\r\nAVG_RX_PROCESS_US: %lu\r\nRX_CALLS: %lu\r\n"
        "MAX_FORWARD_US: %lu\r\nAVG_FORWARD_US: %lu\r\nFORWARD_CALLS: %lu\r\n"
        "BUILD_OVER_25US: %lu\r\nSUBMIT_OVER_25US: %lu\r\n"
        "USB_IRQ_OVER_25US: %lu\r\nRX_OVER_25US: %lu\r\n"
        "FORWARD_OVER_25US: %lu\r\nTIMING_BUDGET_VIOLATIONS: %lu\r\n"
        "MAX_TRANSPORT_POLL_US: %lu\r\nSTATUS: OK\r\n",
        (unsigned long)last_stream_session_id,
        (unsigned long)transport->build_max_us,
        (unsigned long)(transport->build_calls ?
            transport->build_sum_us / transport->build_calls : 0U),
        (unsigned long)transport->build_calls,
        (unsigned long)transport->submit_max_us,
        (unsigned long)(transport->submit_calls ?
            transport->submit_sum_us / transport->submit_calls : 0U),
        (unsigned long)transport->submit_calls,
        (unsigned long)stream_timing.usb_irq_max_us,
        (unsigned long)(stream_timing.usb_irq_calls ?
            stream_timing.usb_irq_sum_us / stream_timing.usb_irq_calls : 0U),
        (unsigned long)stream_timing.usb_irq_calls,
        (unsigned long)stream_timing.rx_max_us,
        (unsigned long)(stream_timing.rx_calls ?
            stream_timing.rx_sum_us / stream_timing.rx_calls : 0U),
        (unsigned long)stream_timing.rx_calls,
        (unsigned long)stream_timing.forward_max_us,
        (unsigned long)(stream_timing.forward_calls ?
            stream_timing.forward_sum_us / stream_timing.forward_calls : 0U),
        (unsigned long)stream_timing.forward_calls,
        (unsigned long)transport->build_over_25us,
        (unsigned long)transport->submit_over_25us,
        (unsigned long)stream_timing.usb_irq_over_25us,
        (unsigned long)stream_timing.rx_over_25us,
        (unsigned long)stream_timing.forward_over_25us,
        (unsigned long)over_budget,
        (unsigned long)StreamTransport_GetCore()->max_poll_us);
}

static int App_FormatProfile(char *out, size_t size)
{
    const MeasurementProfile *profile = MeasurementProfile_Selected();
    int prefix = MeasurementProfile_Format(profile, out, size);
    if (prefix < 0 || (size_t)prefix >= size) return prefix;
    return prefix + snprintf(out + prefix, size - (size_t)prefix,
        "VSHCT_US: %u\r\nAVG: %u\r\nADC_CONFIG: 0x%04X\r\n"
        "ADCRANGE: 0\r\nACQ_MODE: %s\r\nFW: " APP_FW_VERSION "\r\nSTATUS: OK\r\n",
        profile->vshct_us, profile->averages, profile->adc_config,
        Acq_GetMode() == ACQ_MODE_DMA ? "DMA" : "POLL");
}

static void App_ProfileCommand(void)
{
    if (strcmp(line, "acq profile list") == 0)
    {
        size_t used = 0U;
        unsigned int i;
        for (i = 0U; i < PROFILE_COUNT; ++i)
        {
            const MeasurementProfile *p = MeasurementProfile_Get((MeasurementProfile_Id)i);
            int n = snprintf((char *)tx_buffer + used, sizeof(tx_buffer) - used,
                "PROFILE_%u: %s,%lu,%u,%u,%u,0x%04X\r\n",
                i, p->name, (unsigned long)p->rate_hz, p->period_us,
                p->vshct_us, p->averages, p->adc_config);
            if (n < 0 || (size_t)n >= sizeof(tx_buffer) - used)
            {
                App_QueueText("ERR: INTERNAL\r\n");
                return;
            }
            used += (size_t)n;
        }
        App_QueueFormatted((int)used + snprintf((char *)tx_buffer + used,
            sizeof(tx_buffer) - used, "PROFILE_COUNT: %u\r\nSTATUS: OK\r\n", PROFILE_COUNT));
    }
    else if (strcmp(line, "acq profile show") == 0)
        App_QueueFormatted(App_FormatProfile((char *)tx_buffer, sizeof(tx_buffer)));
    else if (strncmp(line, "acq profile set ", 16U) == 0)
    {
        const MeasurementProfile *profile = MeasurementProfile_Find(line + 16U);
        Acq_Status status;
        if (profile == NULL) App_QueueText("ERR: INVALID_PROFILE\r\n");
        else if (StreamTransport_IsActive() || Acq_IsRunning())
            App_QueueText("ERR: BUSY_STREAMING\r\n");
        else
        {
            status = Acq_SelectProfile(profile->id);
            if (status == ACQ_OK)
                App_QueueFormatted(App_FormatProfile((char *)tx_buffer, sizeof(tx_buffer)));
            else App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "ERR: %s\r\n", Acq_StatusName(status)));
        }
    }
    else App_QueueText("ERR: BAD_COMMAND\r\n");
}

static void App_ExecuteCommand(void)
{
    uint16_t manufacturer;
    uint16_t device;
    uint16_t config;
    uint16_t adc_config;
    uint16_t diag_alrt;
    INA229_Status status;

    line[line_length] = '\0';
    if (memchr(line, '\0', line_length) != NULL)
    {
        App_QueueText("ERR: BAD_COMMAND\r\n");
        return;
    }
    if (PairDiag_IsBusy() && strncmp(line, "spi pair ", 9U) != 0 &&
        strcmp(line, "ping") != 0 && strcmp(line, "info") != 0 && strcmp(line, "help") != 0) {
        App_QueueText("ERR: PAIR_ACTIVE\r\n"); return;
    }
    if (SpiFreeze_IsBusy() && strncmp(line, "spi freeze ", 11U) != 0 &&
        strcmp(line, "ping") != 0 && strcmp(line, "info") != 0 && strcmp(line, "help") != 0) {
        App_QueueText("ERR: FREEZE_ACTIVE\r\n");
        return;
    }
    if (strncmp(line, "spi pair ", 9U) == 0) {
        App_QueueFormatted(PairDiag_Command(line + 9U, (char *)tx_buffer, sizeof(tx_buffer)));
        return;
    }
    if (strncmp(line, "spi freeze ", 11U) == 0) {
        if (Acq_IsRunning() || StreamTransport_IsActive()) {
            App_QueueText("ERR: ACQ_OR_STREAM_ACTIVE\r\n"); return;
        }
        if (strcmp(line, "spi freeze show") == 0)
            App_QueueFormatted(SpiFreeze_FormatShow((char *)tx_buffer, sizeof(tx_buffer)));
        else if (strcmp(line, "spi freeze report") == 0)
            App_QueueFormatted(SpiFreeze_FormatReport((char *)tx_buffer, sizeof(tx_buffer)));
        else if (strcmp(line, "spi freeze stop") == 0) {
            SpiFreeze_Stop(false);
            App_QueueText(SpiFreeze_IsBusy() ? "ERR: FREEZE_RESTORE\r\n" : "STATUS: OK\r\n");
        }
        else if (strcmp(line, "spi freeze events") == 0 ||
                 (strncmp(line, "spi freeze events ", 18U) == 0 &&
                  line_length == 19U && line[18] >= '0' && line[18] <= '3')) {
            uint32_t page = line_length == 19U ? (uint32_t)(line[18] - '0') : 0U;
            App_QueueFormatted(SpiFreeze_FormatEvents((char *)tx_buffer, sizeof(tx_buffer), page));
        }
        else if (strcmp(line, "spi freeze vshct 150") == 0 ||
                 strcmp(line, "spi freeze vshct 84") == 0 || strcmp(line, "spi freeze vshct 50") == 0) {
            uint32_t us = strcmp(line, "spi freeze vshct 150") == 0 ? 150U :
                          strcmp(line, "spi freeze vshct 84") == 0 ? 84U : 50U;
            App_QueueText(SpiFreeze_SetVshct(us) ? "STATUS: OK\r\n" : "ERR: FREEZE_BUSY\r\n");
        }
        else if (strcmp(line, "spi freeze test 10000") == 0 ||
                 strcmp(line, "spi freeze test 100000") == 0 ||
                 strcmp(line, "spi freeze ids 10000") == 0 || strcmp(line, "spi freeze ids 100000") == 0) {
            bool ids = strncmp(line, "spi freeze ids ", 15U) == 0;
            uint32_t pairs = strcmp(line, "spi freeze test 100000") == 0 ||
                             strcmp(line, "spi freeze ids 100000") == 0 ? 100000U : 10000U;
            const char *result = SpiFreeze_Start(pairs, ids);
            if (strcmp(result, "OK") == 0) App_QueueText("FREEZE: STARTED\r\nSTATUS: OK\r\n");
            else App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer), "ERR: %s\r\n", result));
        }
        else App_QueueText("ERR: BAD_COMMAND\r\n");
        return;
    }
    if (strncmp(line, "acq ", 4U) == 0)
    {
        Acq_Status acq_status;
        if (strncmp(line, "acq profile ", 12U) == 0)
        {
            App_ProfileCommand();
        }
        else if (strcmp(line, "acq start") == 0 || strcmp(line, "acq test 1") == 0 ||
            strcmp(line, "acq test 5") == 0)
        {
            uint32_t test_seconds = strcmp(line, "acq test 1") == 0 ? 1U :
                                    (strcmp(line, "acq test 5") == 0 ? 5U : 0U);
            acq_status = Acq_Start(test_seconds);
            if (acq_status == ACQ_OK)
            {
                App_SetStreamingActive(true);
                /* A test sends its sole report after stopping, avoiding USB work in the run. */
                if (test_seconds == 0U) App_QueueText("STATUS: OK\r\n");
            }
            else App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "ERR: %s\r\n", Acq_StatusName(acq_status)));
        }
        else if (strcmp(line, "acq stop") == 0)
        {
            acq_status = Acq_Stop();
            App_SetStreamingActive(false);
            if (acq_status == ACQ_OK) App_QueueText("STATUS: OK\r\n");
            else App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "ERR: %s\r\n", Acq_StatusName(acq_status)));
        }
        else if (strcmp(line, "acq status") == 0)
            App_QueueFormatted(Acq_FormatStatus((char *)tx_buffer, sizeof(tx_buffer)));
        else if (strcmp(line, "acq config") == 0)
        {
            if (Acq_IsRunning()) App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueFormatted(Acq_FormatConfig((char *)tx_buffer, sizeof(tx_buffer)));
        }
        else if (strcmp(line, "acq mode dma") == 0 ||
                 strcmp(line, "acq mode poll") == 0)
        {
            if (!Acq_SetMode(strcmp(line, "acq mode dma") == 0 ?
                             ACQ_MODE_DMA : ACQ_MODE_POLL))
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "acq capture continuous") == 0 ||
                 strcmp(line, "acq capture alert") == 0 ||
                 strcmp(line, "acq capture single") == 0 ||
                 strcmp(line, "acq capture alert_driven") == 0 ||
                 strcmp(line, "acq capture native") == 0)
        {
            Acq_CaptureMode mode = strcmp(line, "acq capture continuous") == 0 ?
                ACQ_CAPTURE_CONTINUOUS :
                (strcmp(line, "acq capture alert") == 0 ?
                    ACQ_CAPTURE_ALERT_MONITOR :
                    (strcmp(line, "acq capture single") == 0 ?
                     ACQ_CAPTURE_SINGLE_SHOT : ACQ_CAPTURE_ALERT_DRIVEN));
            if (strcmp(line, "acq capture native") == 0) mode = ACQ_CAPTURE_NATIVE;
            if (StreamTransport_IsActive() || !Acq_SetCaptureMode(mode))
                App_QueueText("ERR: ACQ_ACTIVE_OR_MODE\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "acq alert report") == 0)
        {
            if (Acq_IsRunning()) App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueFormatted(Acq_FormatAlertReport((char *)tx_buffer,
                                                           sizeof(tx_buffer)));
        }
        else if (strcmp(line, "acq dma report") == 0)
        {
            if (Acq_IsRunning() || StreamTransport_IsActive()) App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueFormatted(AcqDma_FormatTimeouts((char *)tx_buffer, sizeof(tx_buffer)));
        }
        else if (strcmp(line, "acq event report") == 0)
        {
            if (Acq_IsRunning() || StreamTransport_IsActive()) App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueFormatted(Acq_FormatEventReport((char *)tx_buffer, sizeof(tx_buffer)));
        }
        else if (strcmp(line, "acq native report") == 0 || strcmp(line, "acq native trace") == 0)
        {
            if (Acq_IsRunning() || StreamTransport_IsActive()) App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else if (!Acq_IsNativeSession()) App_QueueText("ERR: NO_NATIVE_SESSION\r\n");
            else if (strcmp(line, "acq native trace") == 0)
                App_QueueFormatted(Acq_FormatNativeTrace((char *)tx_buffer, sizeof(tx_buffer), false));
            else App_QueueFormatted(Acq_FormatNativeReport((char *)tx_buffer, sizeof(tx_buffer)));
        }
        else if (strcmp(line, "acq native latch 0") == 0 || strcmp(line, "acq native latch 1") == 0)
        {
            if (StreamTransport_IsActive() || !Acq_SetNativeLatch(line[17] == '1'))
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "acq alert adc profile") == 0 ||
                 strcmp(line, "acq alert adc 50") == 0 ||
                 strcmp(line, "acq alert adc 84") == 0)
        {
            uint32_t us = strcmp(line, "acq alert adc 50") == 0 ? 50U :
                (strcmp(line, "acq alert adc 84") == 0 ? 84U : 0U);
            if (StreamTransport_IsActive() || !Acq_SetAlertConversion(us))
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "acq probe") == 0)
            App_QueueFormatted(Acq_FormatProbe((char *)tx_buffer, sizeof(tx_buffer), 200U));
        else if (strncmp(line, "acq probe ", 10U) == 0)
        {
            uint32_t delay;
            if (!AcqCore_ParseProbeDelay(line + 10U, &delay))
                App_QueueText("ERR: BAD_DELAY_US\r\n");
            else App_QueueFormatted(Acq_FormatProbe((char *)tx_buffer, sizeof(tx_buffer), delay));
        }
        else App_QueueText("ERR: BAD_COMMAND\r\n");
        return;
    }
    if (strncmp(line, "spi ", 4U) == 0)
    {
        if (strcmp(line, "spi verify show") == 0 ||
            strcmp(line, "spi verify report") == 0)
        {
            if (Acq_IsRunning()) App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueFormatted(Acq_FormatVerifyReport((char *)tx_buffer,
                                                           sizeof(tx_buffer)));
        }
        else if (strcmp(line, "spi verify off") == 0 ||
                 strcmp(line, "spi verify diagnostic") == 0 ||
                 strcmp(line, "spi verify reject") == 0)
        {
            Acq_VerifyPolicy policy = strcmp(line, "spi verify off") == 0 ?
                ACQ_VERIFY_OFF :
                (strcmp(line, "spi verify diagnostic") == 0 ?
                    ACQ_VERIFY_DIAGNOSTIC : ACQ_VERIFY_REJECT);
            if (StreamTransport_IsActive() || !Acq_SetVerifyPolicy(policy))
                App_QueueText("ERR: ACQ_ACTIVE_OR_MODE\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "spi repeat show") == 0)
        {
            if (Acq_IsRunning() || StreamTransport_IsActive())
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else App_QueueFormatted(AcqDma_FormatRepeat((char *)tx_buffer,
                                                        sizeof(tx_buffer)));
        }
        else if (strcmp(line, "spi repeat enable 0") == 0 ||
                 strcmp(line, "spi repeat enable 1") == 0)
        {
            bool enabled = line[18] == '1';
            if (Acq_IsRunning() || StreamTransport_IsActive())
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else if (enabled && (Acq_GetMode() != ACQ_MODE_DMA ||
                    MeasurementProfile_Selected()->id == PROFILE_FAST))
                App_QueueText("ERR: REPEAT_REQUIRES_DMA_NON_FAST\r\n");
            else if (!AcqDma_RepeatConfigure(enabled, AcqDma_RepeatThresholdMa()))
                App_QueueText("ERR: SPI_BUSY\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strncmp(line, "spi repeat threshold_ma ", 24U) == 0)
        {
            uint32_t threshold;
            if (!AnomalyTrace_ParseLimitMa(line + 24U, &threshold) || threshold == 0U)
                App_QueueText("ERR: BAD_THRESHOLD_MA\r\n");
            else if (Acq_IsRunning() || StreamTransport_IsActive())
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
            else if (!AcqDma_RepeatConfigure(AcqDma_RepeatEnabled(), threshold))
                App_QueueText("ERR: SPI_BUSY\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "spi config") == 0)
            App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "SPI_PCLK2_HZ: %lu\r\nSPI_DIVIDER: %u\r\nSPI_HZ: %lu\r\n"
                "SPI_MODE: 1\r\nSPI_OWNER_CONFLICTS: %lu\r\nSTATUS: OK\r\n",
                (unsigned long)HAL_RCC_GetPCLK2Freq(),
                (unsigned int)SpiControl_GetDivider(),
                (unsigned long)SpiControl_GetHz(),
                (unsigned long)INA229_ContentionCount()));
        else if (strncmp(line, "spi speed ", 10U) == 0)
        {
            uint32_t requested;
            SpiControl_Status change;
            if (strcmp(line + 10U, "6000000") == 0) requested = 6000000U;
            else if (strcmp(line + 10U, "3000000") == 0) requested = 3000000U;
            else if (strcmp(line + 10U, "1500000") == 0) requested = 1500000U;
            else { App_QueueText("ERR: BAD_SPEED_HZ\r\n"); return; }
            if (StreamTransport_IsActive() || Acq_IsRunning())
                { App_QueueText("ERR: ACQ_ACTIVE\r\n"); return; }
            change = SpiControl_SetHz(requested);
            if (change == SPI_CONTROL_BUSY) App_QueueText("ERR: SPI_BUSY\r\n");
            else if (change == SPI_CONTROL_UNSUPPORTED_CLOCK)
                App_QueueText("ERR: UNSUPPORTED_CLOCK\r\n");
            else if (change != SPI_CONTROL_OK) App_QueueText("ERR: SPI_INIT\r\n");
            else App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "SPI_HZ: %lu\r\nSTATUS: OK\r\n", (unsigned long)SpiControl_GetHz()));
        }
        else App_QueueText("ERR: BAD_COMMAND\r\n");
        return;
    }
    if (strncmp(line, "stream ", 7U) == 0)
    {
        const Stream_Core *transport = StreamTransport_GetCore();
        const Acq_Core *samples = StreamTransport_IsActive() ? Acq_GetCore() :
            &stream_snapshot;
        const AcqDma_Core *dma_stats = StreamTransport_IsActive() ?
            Acq_GetDmaCore() : &stream_dma_snapshot;
        AcqDma_VerifySummary verify_stats = AcqDma_GetVerifySummary();
        if (strcmp(line, "stream start") == 0)
        {
            Acq_Status start;
            if (Acq_IsRunning() || StreamTransport_IsActive())
            {
                App_QueueText("ERR: ACQ_ACTIVE\r\n");
                return;
            }
            Acq_SetStreamMode(true);
            start = Acq_Start(0U);
            if (start != ACQ_OK)
            {
                Acq_SetStreamMode(false);
                App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                    "ERR: %s\r\n", Acq_StatusName(start)));
                return;
            }
            StreamTransport_Start();
            if (Acq_IsNativeSession()) StreamTransport_SetNativeV2();
            App_UsbTrace(APP_USB_EVENT_STREAM_START);
            stream_elapsed_us64 = 0U;
            last_stream_spi_hz = SpiControl_GetHz();
            App_ResetStreamTiming();
            stream_timing_active = true;
            have_stream_snapshot = false;
            ++stream_session_id;
            last_stream_mode = Acq_GetMode();
            stream_stop_error = false;
            stream_stop_seen = false;
            stream_text_last_attempt_ms = UINT32_MAX;
            App_SetStreamingActive(true);
            App_QueueText(Acq_IsNativeSession() ? "STREAM: BINARY V2\r\n" : "STREAM: BINARY V1\r\n");
        }
        else if (Acq_IsRunning())
            App_QueueText("ERR: ACQ_ACTIVE\r\n");
        else if (strcmp(line, "stream status") == 0)
        {
            char acq_elapsed_text[FORMAT64_UDEC_SIZE];
            char stream_elapsed_text[FORMAT64_UDEC_SIZE];
            if (!StreamTransport_IsActive() && !have_stream_snapshot)
            {
                App_QueueText("ERR: NO_STREAM_SESSION\r\n");
                return;
            }
            uint64_t acq_elapsed64 = (samples->running ? Timebase_NowUs64() :
                samples->stopped_us64) - samples->started_us64;
            uint64_t elapsed64 = stream_elapsed_us64;
            if (!Format64_Unsigned(acq_elapsed_text, sizeof(acq_elapsed_text),
                                   acq_elapsed64) ||
                !Format64_Unsigned(stream_elapsed_text, sizeof(stream_elapsed_text),
                                   elapsed64))
            {
                App_QueueText("ERR: INTERNAL\r\n");
                return;
            }
            uint32_t effective_rate = elapsed64 ?
                (uint32_t)(((uint64_t)transport->usb_samples_sent * 1000000ULL) /
                           elapsed64) : 0U;
            int prefix = MeasurementProfile_Format(
                MeasurementProfile_Get(samples->profile_id), (char *)tx_buffer, sizeof(tx_buffer));
            if (prefix < 0 || (size_t)prefix >= sizeof(tx_buffer))
            {
                App_QueueText("ERR: INTERNAL\r\n");
                return;
            }
            App_QueueFormatted(prefix + snprintf((char *)tx_buffer + prefix, sizeof(tx_buffer) - (size_t)prefix,
                "STREAM: %s\r\nSESSION_TYPE: STREAM\r\nSESSION_ID: %lu\r\n"
                "SESSION_DURATION_US: %lu\r\nACQ_ELAPSED_US64: %s\r\n"
                "STREAM_ELAPSED_US64: %s\r\nACQ_MODE: %s\r\n"
                "ACQ_CAPTURE_MODE: %s\r\n"
                "SPI_HZ: %lu\r\nSPI_OWNER_CONFLICTS: %lu\r\n"
                "CDC_RX_PACKETS: %lu\r\nCDC_RX_BYTES: %lu\r\n"
                "CDC_OUT_REARM_ERRORS: %lu\r\nCDC_CONTROL_LINE_EVENTS: %lu\r\n"
                "CDC_LAST_CONTROL_LINE: 0x%04X\r\nCDC_TX_COMPLETIONS: %lu\r\n"
                "STREAM_STOP_COMMANDS: %lu\r\nSTREAM_END_COMPLETED: %lu\r\n"
                "STARTUP_US: %lu\r\nTOTAL_SESSION_US: %lu\r\nFW: " APP_FW_VERSION "\r\n"
                "ADC_CONFIG_READBACK: 0x%04X\r\n"
                "ADC_SAMPLES: %lu\r\nUSB_SAMPLES_SENT: %lu\r\n"
                "FRAMES_SENT: %lu\r\nFRAMES_DROPPED: %lu\r\nUSB_BUSY_COUNT: %lu\r\n"
                "TX_BUFFER_OVERFLOWS: %lu\r\nACQ_BUFFER_OVERFLOWS: %lu\r\n"
                "LOST_SAMPLE_SLOTS: %lu\r\nSPI_ERRORS: %lu\r\n"
                "TIMER_TICKS: %lu\r\nMISSED_TIMER_SLOTS: %lu\r\n"
                "CNVRF_READS: %lu\r\nCNVRF_READY: %lu\r\nCNVRF_NOT_READY: %lu\r\n"
                "VSHUNT_READS: %lu\r\nFRESHNESS_SKIPS: %lu\r\nTIMER_PERIOD_VIOLATIONS: %lu\r\n"
                "MAX_SERVICE_US: %lu\r\nMIN_SAMPLE_PERIOD_US: %lu\r\n"
                "MAX_SAMPLE_PERIOD_US: %lu\r\nBUFFER_DEPTH: %lu\r\n"
                "MAX_BUFFER_DEPTH: %lu\r\nMAX_BUFFER_AGE_US: %lu\r\n"
                "MAX_TRANSPORT_POLL_US: %lu\r\n"
                "DMA_STARTS: %lu\r\nDMA_COMPLETIONS: %lu\r\nDMA_BUSY: %lu\r\n"
                "DMA_ERRORS: %lu\r\nDMA_TIMEOUTS: %lu\r\n"
                "DMA_BUFFER_REUSE_ATTEMPTS: %lu\r\n"
                "DMA_CALLBACK_ORDER_ERRORS: %lu\r\nCS_STATE_ERRORS: %lu\r\n"
                "MAX_DMA_TRANSACTION_US: %lu\r\nMAX_TIM3_LATENCY_US: %lu\r\n"
                "MAX_ACQUISITION_CYCLE_US: %lu\r\n"
                "VERIFY_TOTAL: %lu\r\nVERIFY_MATCH: %lu\r\n"
                "VERIFY_MISMATCH: %lu\r\nVERIFY_RAW_MISMATCH: %lu\r\n"
                "VERIFY_REJECTED: %lu\r\nVERIFY_SECOND_READ_ERRORS: %lu\r\n"
                "VERIFY_MAX_CYCLE_US: %lu\r\n"
                "VERIFY_UNCERTAIN_CONVERSION: %lu\r\n"
                "EFFECTIVE_STREAM_RATE_HZ: %lu\r\nSTATUS: OK\r\n",
                StreamTransport_IsActive() ? "BINARY" : "TEXT",
                (unsigned long)(StreamTransport_IsActive() ? stream_session_id :
                                last_stream_session_id),
                (unsigned long)(uint32_t)acq_elapsed64,
                acq_elapsed_text, stream_elapsed_text,
                last_stream_mode == ACQ_MODE_DMA ? "DMA" : "POLL",
                Acq_GetSessionCaptureName(),
                (unsigned long)last_stream_spi_hz,
                (unsigned long)INA229_ContentionCount(),
                (unsigned long)cdc_rx_packets, (unsigned long)cdc_rx_bytes,
                (unsigned long)cdc_out_rearm_errors,
                (unsigned long)cdc_control_line_events,
                cdc_last_control_line,
                (unsigned long)cdc_tx_completions,
                (unsigned long)stream_stop_commands,
                (unsigned long)stream_end_completed,
                (unsigned long)samples->startup_us,
                (unsigned long)(samples->startup_us + samples->stopped_us - samples->started_us),
                samples->last_adc_config,
                (unsigned long)samples->samples,
                (unsigned long)transport->usb_samples_sent,
                (unsigned long)transport->frames_sent,
                (unsigned long)transport->frames_dropped,
                (unsigned long)transport->usb_busy_count,
                (unsigned long)transport->tx_buffer_overflows,
                (unsigned long)samples->overflows,
                (unsigned long)(samples->missed + samples->timer_lost),
                (unsigned long)samples->spi_errors,
                (unsigned long)samples->ticks,
                (unsigned long)(samples->missed_timer_slots + samples->timer_lost),
                (unsigned long)samples->cnvrf_reads,
                (unsigned long)samples->cnvrf_ready,
                (unsigned long)samples->cnvrf_not_ready,
                (unsigned long)samples->vshunt_reads,
                (unsigned long)samples->freshness_skips,
                (unsigned long)samples->period_violations,
                (unsigned long)samples->max_service_us,
                (unsigned long)samples->min_sample_period_us,
                (unsigned long)samples->max_sample_period_us,
                (unsigned long)StreamCore_Depth(transport),
                (unsigned long)transport->max_depth,
                (unsigned long)transport->max_age_us,
                (unsigned long)transport->max_poll_us,
                (unsigned long)dma_stats->starts,
                (unsigned long)dma_stats->completions,
                (unsigned long)dma_stats->busy,
                (unsigned long)dma_stats->errors,
                (unsigned long)dma_stats->timeouts,
                (unsigned long)dma_stats->buffer_reuse_attempts,
                (unsigned long)dma_stats->callback_order_errors,
                (unsigned long)dma_stats->cs_state_errors,
                (unsigned long)dma_stats->max_transaction_us,
                (unsigned long)dma_stats->max_latency_us,
                (unsigned long)dma_stats->max_cycle_us,
                (unsigned long)verify_stats.total,
                (unsigned long)verify_stats.match,
                (unsigned long)verify_stats.mismatch,
                (unsigned long)verify_stats.raw_mismatch,
                (unsigned long)verify_stats.rejected,
                (unsigned long)verify_stats.second_errors,
                (unsigned long)verify_stats.max_cycle_us,
                (unsigned long)verify_stats.uncertain,
                (unsigned long)effective_rate));
        }
        else if (strcmp(line, "stream config") == 0)
        {
            const MeasurementProfile *profile = MeasurementProfile_Selected();
            int prefix = snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "PROTOCOL: CSF4 V1\r\nFRAME_SAMPLES: 32\r\n"
                "FRAME_MAX_BYTES: 228\r\nPERIOD_US: %u\r\nFAST_ADC_CONFIG: 0x%04X\r\n",
                profile->period_us, profile->adc_config);
            if (prefix < 0 || (size_t)prefix >= sizeof(tx_buffer))
                App_QueueText("ERR: INTERNAL\r\n");
            else App_QueueFormatted(prefix + App_FormatProfile((char *)tx_buffer + prefix,
                sizeof(tx_buffer) - (size_t)prefix));
        }
        else if (strcmp(line, "stream stats") == 0)
        {
            char mean_text[FORMAT64_SDEC_SIZE];
            char std_text[FORMAT64_UDEC_SIZE];
            if (!StreamTransport_IsActive() && !have_stream_snapshot)
            {
                App_QueueText("ERR: NO_STREAM_SESSION\r\n");
                return;
            }
            int64_t mean_na = samples->stats_count ?
                samples->sum_code * 62500LL / samples->stats_count : 0;
            uint64_t std_na = AcqCore_StdCodeMilli(samples) * 62500ULL / 1000ULL;
            if (!Format64_Signed(mean_text, sizeof(mean_text), mean_na) ||
                !Format64_Unsigned(std_text, sizeof(std_text), std_na))
            {
                App_QueueText("ERR: INTERNAL\r\n");
                return;
            }
            App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
                "SESSION_TYPE: STREAM\r\nSESSION_ID: %lu\r\n"
                "STATS_SAMPLES: %lu\r\nMIN_CODE: %ld\r\nMAX_CODE: %ld\r\n"
                "MEAN_NA: %s\r\nSTDDEV_NA: %s\r\n"
                "OUTLIER_COUNTS: HOST_TOOL\r\nHISTOGRAM: HOST_TOOL\r\nSTATUS: OK\r\n",
                (unsigned long)(StreamTransport_IsActive() ? stream_session_id :
                                last_stream_session_id),
                (unsigned long)samples->stats_count, (long)samples->min_code,
                (long)samples->max_code, mean_text, std_text));
        }
        else if (strcmp(line, "stream timing") == 0)
        {
            if (!have_stream_snapshot) App_QueueText("ERR: NO_STREAM_SESSION\r\n");
            else App_QueueFormatted(App_FormatStreamTiming((char *)tx_buffer,
                                                          sizeof(tx_buffer)));
        }
        else if (strcmp(line, "stream anomaly") == 0)
        {
            if (!have_stream_snapshot) App_QueueText("ERR: NO_STREAM_SESSION\r\n");
            else
            {
                int prefix = snprintf((char *)tx_buffer, sizeof(tx_buffer),
                    "SESSION_TYPE: STREAM\r\nSESSION_ID: %lu\r\n",
                    (unsigned long)last_stream_session_id);
                if (prefix < 0 || (size_t)prefix >= sizeof(tx_buffer))
                    App_QueueText("ERR: INTERNAL\r\n");
                else App_QueueFormatted(prefix + Acq_FormatAnomalyTrace(
                    (char *)tx_buffer + prefix, sizeof(tx_buffer) - (size_t)prefix));
            }
        }
        else if (strncmp(line, "stream anomaly set ", 19U) == 0)
        {
            uint32_t limit_ma;
            if (!AnomalyTrace_ParseLimitMa(line + 19U, &limit_ma) ||
                !Acq_SetAnomalyLimitMa(limit_ma))
                App_QueueText("ERR: BAD_LIMIT_MA\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strncmp(line, "stream anomaly limit_ma ",
                         sizeof("stream anomaly limit_ma ") - 1U) == 0)
        {
            uint32_t limit_ma;
            if (!AnomalyTrace_ParseLimitMa(
                    line + sizeof("stream anomaly limit_ma ") - 1U, &limit_ma) ||
                !Acq_SetAnomalyLimitMa(limit_ma))
                App_QueueText("ERR: BAD_LIMIT_MA\r\n");
            else App_QueueText("STATUS: OK\r\n");
        }
        else if (strcmp(line, "stream stop") == 0)
            App_QueueText("ERR: NOT_RUNNING\r\n");
        else App_QueueText("ERR: BAD_COMMAND\r\n");
        return;
    }
    if (Acq_IsRunning() && strcmp(line, "ping") != 0 && strcmp(line, "info") != 0 &&
        strcmp(line, "help") != 0 && strcmp(line, "cal show") != 0 &&
        strcmp(line, "cal save") != 0)
    {
        App_QueueText("ERR: ACQ_ACTIVE\r\n");
        return;
    }
    if (strcmp(line, "help") == 0)
    {
        App_QueueText("Commands: help, ping, info, ina, regs, measure\r\n"
                      "spi config|speed 6000000|3000000|1500000\r\n"
                      "spi repeat show|enable 0/1|threshold_ma N (DMA, non-FAST)\r\n"
                      "spi verify off|diagnostic|reject|show|report\r\n"
                      "spi freeze show|test 10000/100000|ids 10000/100000|report|events [0..3]|stop\r\n"
                      "spi freeze vshct 150|84|50 (RAM)\r\n"
                      "spi pair show|test 10000/100000|report|events [0..15]|failures [0..3]|stop\r\n"
                      "spi pair wait (saved WAIT_TIMEOUT context and rearm count)\r\n"
                      "spi pair vshct 84/150|latch 0/1|delay <first_us> <gap_us> (0..500)|bins 0..3|marker on/off\r\n"
                      "cal show|set offset_ua N|set gain_ppm N|enable 0/1|save|load|reset\r\n"
                      "acq start|stop|status|test 1|test 5|config|probe [delay_us]|mode dma|poll\r\n"
                      "acq capture continuous|alert|single|alert_driven|native; acq alert report\r\n"
                      "acq alert adc profile|50|84 (ALERT_DRIVEN/NATIVE only)\r\n"
                      "acq dma report (after STOP)\r\n"
                      "acq event report (after STOP)\r\n"
                      "acq native report|trace|latch 0/1 (RAM, native only)\r\n"
                      "acq profile list|show|set fast|balanced|precision|low_noise (names case-insensitive)\r\n"
                      "stream start|stop|status|config|stats|timing|anomaly|anomaly set N\r\n"
                      "usb diag\r\n");
    }
    else if (strcmp(line, "ping") == 0)
    {
        App_QueueText("pong\r\n");
    }
    else if (strcmp(line, "info") == 0)
    {
        App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
            "CurrentSensor\r\nFW: " APP_FW_VERSION "\r\nMCU: STM32F411RET6\r\n"
            "FW_BUILD: " APP_FW_BUILD "\r\nUSB_DIAG_REV: %u\r\n"
            "USB: CDC\r\nSPI: %lu Hz\r\n",
            APP_USB_DIAG_REV, (unsigned long)SpiControl_GetHz()));
    }
    else if (strcmp(line, "usb diag") == 0)
    {
        App_QueueFormatted(App_FormatUsbDiag((char *)tx_buffer, sizeof(tx_buffer)));
    }
    else if (strcmp(line, "ina") == 0)
    {
        status = INA229_ReadManufacturerID(&ina, &manufacturer);
        if (status == INA229_OK)
        {
            status = INA229_ReadDeviceID(&ina, &device);
        }
        if (status != INA229_OK)
        {
            App_QueueText((status == INA229_TIMEOUT) ? "STATUS: SPI_TIMEOUT\r\n" : "STATUS: SPI_ERROR\r\n");
            return;
        }
        App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
            "INA229:\r\nMANUFACTURER_ID: 0x%04X %s\r\n"
            "DEVICE_ID:       0x%04X\r\nDIE_ID:          0x%03X %s\r\n"
            "REV_ID:          0x%X\r\nSTATUS:          %s\r\n",
            manufacturer, (manufacturer == 0x5449U) ? "OK" : "BAD",
            device, device >> 4, ((device >> 4) == 0x229U) ? "OK" : "BAD",
            device & 0x0FU,
            (manufacturer != 0x5449U) ? "BAD_MANUFACTURER_ID" :
            (((device >> 4) != 0x229U) ? "BAD_DEVICE_ID" : "OK")));
    }
    else if (strcmp(line, "regs") == 0)
    {
        status = INA229_ReadReg16(&ina, INA229_REG_CONFIG, &config);
        if (status == INA229_OK)
        {
            status = INA229_ReadReg16(&ina, INA229_REG_ADC_CONFIG, &adc_config);
        }
        if (status == INA229_OK)
        {
            status = INA229_ReadReg16(&ina, INA229_REG_DIAG_ALRT, &diag_alrt);
        }
        if (status != INA229_OK)
        {
            App_QueueText((status == INA229_TIMEOUT) ? "STATUS: SPI_TIMEOUT\r\n" : "STATUS: SPI_ERROR\r\n");
            return;
        }
        App_QueueFormatted(snprintf((char *)tx_buffer, sizeof(tx_buffer),
            "CONFIG:     0x%04X\r\nADC_CONFIG: 0x%04X\r\nDIAG_ALRT:  0x%04X\r\n",
            config, adc_config, diag_alrt));
    }
    else if (strcmp(line, "measure") == 0)
    {
        App_Measure();
    }
    else if (strcmp(line, "cal") == 0 || strncmp(line, "cal ", 4U) == 0)
    {
        App_CalibrationCommand();
    }
    else
    {
        App_QueueText("ERR: UNKNOWN_COMMAND\r\n");
    }
}

void App_Init(void)
{
    ina.hspi = &hspi1;
    ina.cs_port = INA_CS_GPIO_Port;
    ina.cs_pin = INA_CS_Pin;
    Calibration_Init();
    Acq_Init(&ina);
    SpiFreeze_Init(&ina);
    PairDiag_Init(&ina);
    tx_ready = 1U;
}

void App_CdcRx(const uint8_t *data, uint32_t length)
{
    uint32_t i;
    uint16_t next;

    if (data == NULL)
    {
        return;
    }
    ++cdc_rx_packets;
    cdc_rx_bytes += length;
    App_UsbTrace(APP_USB_EVENT_RX);
    for (i = 0U; i < length; ++i)
    {
        next = (uint16_t)((rx_head + 1U) % APP_RX_RING_SIZE);
        if (next == rx_tail)
        {
            rx_overflow = 1U;
            break;
        }
        rx_ring[rx_head] = data[i];
        rx_head = next;
    }
}

void App_CdcRxRearmResult(bool success)
{
    ++cdc_rx_rearms;
    if (!success) ++cdc_out_rearm_errors;
    if (!success) App_UsbTrace(APP_USB_EVENT_REARM_ERROR);
}

void App_UsbControlRequest(void) { ++cdc_control_requests; }
void App_UsbIrq(void) { ++usb_irq_count; }
void App_UsbReset(void)
{
    ++usb_reset_count;
    App_UsbTrace(APP_USB_EVENT_RESET);
}
void App_UsbTxBusy(void)
{
    ++usb_tx_busy;
    if ((usb_tx_busy & (usb_tx_busy - 1U)) == 0U)
        App_UsbTrace(APP_USB_EVENT_TX_BUSY);
}
void App_UsbEndQueued(void)
{
    ++end_queued;
    App_UsbTrace(APP_USB_EVENT_END_QUEUED);
}
void App_UsbDmaAbortError(void)
{
    ++dma_abort_errors;
    App_UsbTrace(APP_USB_EVENT_DMA_ABORT_ERROR);
}

void App_CdcDeInit(void)
{
    /* Deinit aborts an in-flight IN transfer; it is not a TX completion. */
    text_tx_inflight = 0U;
    App_UsbTrace(APP_USB_EVENT_DEINIT);
}

void App_CdcLineState(uint16_t value)
{
    ++cdc_control_line_events;
    if ((cdc_last_control_line & 1U) != 0U && (value & 1U) == 0U)
    {
        cdc_dtr_drop_pending = 1U;
        App_UsbTrace(APP_USB_EVENT_DTR_DROP);
    }
    cdc_last_control_line = value;
}

void App_CdcTxComplete(void)
{
    ++cdc_tx_completions;
    if (text_tx_inflight != 0U)
    {
        uint32_t elapsed = App_NowUs() - text_tx_started_us;
        if (elapsed > text_tx_max_wait_us) text_tx_max_wait_us = elapsed;
        text_tx_inflight = 0U;
        ++text_responses_done;
        App_UsbTrace(APP_USB_EVENT_TEXT_DONE);
    }
    tx_ready = 1U;
    StreamTransport_TxCompleteISR();
}

void App_SetStreamingActive(bool active)
{
    streaming_active = active;
}

void App_RecordUsbIrqDuration(uint32_t duration_us)
{
    if (!stream_timing_active) return;
    if (duration_us > stream_timing.usb_irq_max_us)
        stream_timing.usb_irq_max_us = duration_us;
    stream_timing.usb_irq_sum_us += duration_us;
    ++stream_timing.usb_irq_calls;
    if (duration_us > 25U) ++stream_timing.usb_irq_over_25us;
}

static void App_ProcessStreamRx(void)
{
    uint32_t processed = 0U;
    while (rx_tail != rx_head && processed < 32U)
    {
        uint8_t character = rx_ring[rx_tail];
        rx_tail = (uint16_t)((rx_tail + 1U) % APP_RX_RING_SIZE);
        ++processed;
        if (character == '\r' || character == '\n')
        {
            if (character == '\n' && last_was_cr != 0U)
            {
                last_was_cr = 0U;
                continue;
            }
            last_was_cr = character == '\r' ? 1U : 0U;
            if (line_discard != 0U) StreamTransport_RequestError(2U);
            if (line_discard == 0U && line_length != 0U)
            {
                line[line_length] = '\0';
                if (memchr(line, '\0', line_length) != NULL)
                    StreamTransport_RequestError(2U);
                else if (strcmp(line, "stream stop") == 0)
                {
                    ++stream_stop_commands;
                    ++stop_received;
                    App_UsbTrace(APP_USB_EVENT_STOP_RX);
                    if (!stream_stop_seen)
                    {
                        stream_stop_seen = true;
                        if (Acq_IsRunning() && Acq_Stop() != ACQ_OK)
                            stream_stop_error = true;
                        StreamTransport_RequestStop();
                        ++stop_processed;
                        App_UsbTrace(APP_USB_EVENT_STOP_DONE);
                    }
                }
                else if (strcmp(line, "stream status") == 0 ||
                         strcmp(line, "stream stats") == 0)
                    StreamTransport_RequestStats();
                else if (strcmp(line, "stream config") == 0)
                    StreamTransport_RequestConfig();
                else if (strcmp(line, "cal save") == 0 ||
                         strncmp(line, "acq profile set ", 16U) == 0)
                    StreamTransport_RequestError(1U); /* BUSY_STREAMING */
                else StreamTransport_RequestError(2U); /* BAD_COMMAND */
            }
            line_length = 0U;
            line_discard = 0U;
        }
        else
        {
            last_was_cr = 0U;
            if (line_discard != 0U) continue;
            if (line_length >= APP_LINE_SIZE - 1U) line_discard = 1U;
            else line[line_length++] = (char)character;
        }
    }
    if (rx_overflow != 0U)
    {
        uint32_t interrupt_state = __get_PRIMASK();
        __disable_irq();
        rx_tail = rx_head;
        rx_overflow = 0U;
        __set_PRIMASK(interrupt_state);
        line_length = 0U;
        line_discard = 0U;
        StreamTransport_RequestError(3U); /* RX_OVERFLOW */
    }
}

void App_Process(void)
{
    uint8_t character;
    uint32_t interrupt_state;

    if (SpiFreeze_IsBusy() && (cdc_dtr_drop_pending != 0U ||
        hUsbDeviceFS.dev_state != USBD_STATE_CONFIGURED)) SpiFreeze_Stop(true);
    if (PairDiag_IsBusy() && (cdc_dtr_drop_pending != 0U ||
        hUsbDeviceFS.dev_state != USBD_STATE_CONFIGURED)) PairDiag_Stop(true);
    PairDiag_Process();
    SpiFreeze_Process();
    Acq_Process();
    if (Acq_TestFinished()) App_SetStreamingActive(false);

    if (cdc_dtr_drop_pending != 0U)
    {
        if (cdc_tx_abort_errors != 0U &&
            (uint32_t)(HAL_GetTick() - cdc_abort_last_attempt_ms) < 10U)
            return;
        cdc_abort_last_attempt_ms = HAL_GetTick();
        /* A closed COM handle may leave the last CDC IN transfer pending.
         * Process this in the main loop, outside the USB control callback. */
        if (StreamTransport_IsActive())
        {
            if (Acq_IsRunning()) (void)Acq_Stop();
            StreamTransport_RequestStop();
            (void)StreamTransport_Poll(false, false, true,
                App_NowUs(), HAL_GetTick(), NULL, App_NowUs);
            stream_snapshot = *Acq_GetCore();
            stream_dma_snapshot = *Acq_GetDmaCore();
            have_stream_snapshot = true;
            last_stream_session_id = stream_session_id;
            Acq_SetStreamMode(false);
            App_SetStreamingActive(false);
            stream_timing_active = false;
        }
        if (CDC_AbortTx_FS() == USBD_OK)
        {
            ++cdc_tx_aborts;
            App_UsbTrace(APP_USB_EVENT_TX_ABORT);
            cdc_dtr_drop_pending = 0U;
            tx_pending = 0U;
            text_tx_inflight = 0U;
            tx_ready = 1U;
            line_length = 0U;
            line_discard = 0U;
            last_was_cr = 0U;
        }
        else
        {
            ++cdc_tx_abort_errors;
            App_UsbTrace(APP_USB_EVENT_TX_ABORT_ERROR);
            return;
        }
    }

    if (StreamTransport_IsActive())
    {
        Acq_Sample sample;
        uint32_t moved = 0U;
        uint32_t started;
        uint32_t elapsed;
        if (rx_tail != rx_head || rx_overflow != 0U)
        {
            started = App_NowUs();
            App_ProcessStreamRx();
            elapsed = App_NowUs() - started;
            if (elapsed > stream_timing.rx_max_us) stream_timing.rx_max_us = elapsed;
            stream_timing.rx_sum_us += elapsed;
            ++stream_timing.rx_calls;
            if (elapsed > 25U) ++stream_timing.rx_over_25us;
        }
        started = App_NowUs();
        while (moved < 16U && Acq_PopSample(&sample))
        {
            StreamTransport_Push(&sample);
            ++moved;
        }
        if (moved != 0U)
        {
            elapsed = App_NowUs() - started;
            if (elapsed > stream_timing.forward_max_us)
                stream_timing.forward_max_us = elapsed;
            stream_timing.forward_sum_us += elapsed;
            ++stream_timing.forward_calls;
            if (elapsed > 25U) ++stream_timing.forward_over_25us;
        }
    }

    if (hUsbDeviceFS.dev_state != USBD_STATE_CONFIGURED)
    {
        tx_ready = 1U;
        if (StreamTransport_IsActive())
        {
            if (Acq_IsRunning() && Acq_Stop() != ACQ_OK)
                stream_stop_error = true;
            StreamTransport_RequestStop();
            (void)StreamTransport_Poll(false, false, true,
                App_NowUs(), HAL_GetTick(), NULL, App_NowUs);
            Acq_SetStreamMode(false);
            App_SetStreamingActive(false);
            stream_timing_active = false;
            App_QueueText("STREAM: TEXT\r\nERR: USB_DISCONNECTED\r\n");
        }
    }

    if (tx_pending != 0U)
    {
        if (tx_ready != 0U && hUsbDeviceFS.dev_state == USBD_STATE_CONFIGURED)
        {
            uint32_t now_ms = HAL_GetTick();
            if (StreamTransport_IsActive() && stream_text_last_attempt_ms == now_ms)
                return;
            if (StreamTransport_IsActive()) stream_text_last_attempt_ms = now_ms;
            tx_ready = 0U;
            text_tx_inflight = 1U;
            text_tx_started_us = App_NowUs();
            ++text_tx_submit_attempts;
            if (CDC_Transmit_FS(tx_buffer, tx_length) == USBD_OK)
            {
                tx_pending = 0U;
            }
            else
            {
                text_tx_inflight = 0U;
                ++text_tx_busy;
                tx_ready = 1U;
                App_UsbTxBusy();
                if (StreamTransport_IsActive()) StreamTransport_RecordBusy();
            }
        }
        return;
    }
    if (StreamTransport_IsActive())
    {
        const Acq_Core *acq = Acq_GetCore();
        /* USB forwarding and RX may have queued a ready notification. Service
           it before the next bounded transport build/submit step. */
        if (Acq_IsNativeSession()) Acq_Process();
        Stream_AcqMetrics metrics;
        bool submitted;
        uint64_t now64 = Timebase_NowUs64();
        uint32_t now_us = (uint32_t)now64;
        metrics.adc_samples = acq->samples;
        metrics.lost_sample_slots = acq->missed + acq->timer_lost;
        metrics.acq_buffer_overflows = acq->overflows;
        metrics.spi_errors = acq->spi_errors;
        metrics.max_service_us = acq->max_service_us;
        metrics.min_sample_period_us = acq->min_sample_period_us;
        metrics.max_sample_period_us = acq->max_sample_period_us;
        metrics.elapsed_us = (uint32_t)((acq->running ? now64 : acq->stopped_us64) -
                                        acq->started_us64);
        stream_elapsed_us64 = now64 - acq->started_us64;
        metrics.stream_elapsed_us = (uint32_t)stream_elapsed_us64;
        metrics.stream_elapsed_us64 = stream_elapsed_us64;
        submitted = StreamTransport_Poll(tx_ready != 0U,
            hUsbDeviceFS.dev_state == USBD_STATE_CONFIGURED,
            acq->head == acq->tail, now_us, HAL_GetTick(), &metrics, App_NowUs);
        StreamTransport_RecordPollDuration(__HAL_TIM_GET_COUNTER(&htim2) - now_us);
        if (submitted) tx_ready = 0U;
        if (StreamTransport_IsFinished())
        {
            bool end_snapshot_ok = StreamTransport_Failed() ||
                StreamTransport_EndElapsedUs64(&stream_elapsed_us64);
            if (!StreamTransport_Failed())
            {
                ++stream_end_completed;
                App_UsbTrace(APP_USB_EVENT_END_DONE);
            }
            stream_timing_active = false;
            stream_snapshot = *Acq_GetCore();
            stream_dma_snapshot = *Acq_GetDmaCore();
            have_stream_snapshot = true;
            last_stream_session_id = stream_session_id;
            tx_ready = 1U;
            Acq_SetStreamMode(false);
            App_SetStreamingActive(false);
            App_QueueText(StreamTransport_Failed() ?
                "STREAM: TEXT\r\nERR: USB_TRANSPORT\r\n" :
                (!end_snapshot_ok ? "STREAM: TEXT\r\nERR: END_SNAPSHOT\r\n" :
                (stream_stop_error ? "STREAM: TEXT\r\nERR: ADC_CONFIG_RESTORE\r\n" :
                                     "STREAM: TEXT\r\nSTATUS: OK\r\n")));
            App_UsbTrace(APP_USB_EVENT_TEXT_MODE);
        }
        return;
    }
    if (tx_ready == 0U)
    {
        return;
    }

    if (Acq_TestFinished())
    {
        App_QueueFormatted(Acq_FormatTest((char *)tx_buffer, sizeof(tx_buffer)));
        Acq_ClearTestFinished();
        return;
    }

    if (rx_overflow != 0U)
    {
        interrupt_state = __get_PRIMASK();
        __disable_irq();
        rx_tail = rx_head;
        rx_overflow = 0U;
        __set_PRIMASK(interrupt_state);
        line_length = 0U;
        line_discard = 0U;
        last_was_cr = 0U;
        App_QueueText("ERR: RX_OVERFLOW\r\n");
        return;
    }

    while (rx_tail != rx_head)
    {
        character = rx_ring[rx_tail];
        rx_tail = (uint16_t)((rx_tail + 1U) % APP_RX_RING_SIZE);
        if (character == '\r' || character == '\n')
        {
            if (character == '\n' && last_was_cr != 0U)
            {
                last_was_cr = 0U;
                continue;
            }
            last_was_cr = (character == '\r') ? 1U : 0U;
            if (line_discard != 0U)
            {
                line_discard = 0U;
                line_length = 0U;
                App_QueueText("ERR: LINE_TOO_LONG\r\n");
                return;
            }
            if (line_length != 0U)
            {
                App_UsbTrace(APP_USB_EVENT_CLI_ACCEPTED);
                App_ExecuteCommand();
                line_length = 0U;
                return;
            }
            continue;
        }
        last_was_cr = 0U;
        if (line_discard != 0U)
        {
            continue;
        }
        if (line_length >= APP_LINE_SIZE - 1U)
        {
            line_discard = 1U;
            continue;
        }
        line[line_length++] = (char)character;
    }
}
