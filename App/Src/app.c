#include "app.h"

#include <stdio.h>
#include <string.h>

#include "calibration.h"
#include "ina229.h"
#include "main.h"
#include "spi.h"
#include "usb_device.h"
#include "usbd_cdc_if.h"

#define APP_RX_RING_SIZE 256U
#define APP_LINE_SIZE     96U
#define APP_TX_SIZE       256U

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
static volatile bool streaming_active;

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
    tx_pending = 1U;
}

static void App_QueueFormatted(int length)
{
    if (length < 0 || (size_t)length >= sizeof(tx_buffer))
    {
        App_QueueText("ERR: INTERNAL\r\n");
        return;
    }
    tx_length = (uint16_t)length;
    tx_pending = 1U;
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
    if (strcmp(line, "help") == 0)
    {
        App_QueueText("Commands: help, ping, info, ina, regs, measure\r\n"
                      "cal show|set offset_ua N|set gain_ppm N|enable 0/1|save|load|reset\r\n");
    }
    else if (strcmp(line, "ping") == 0)
    {
        App_QueueText("pong\r\n");
    }
    else if (strcmp(line, "info") == 0)
    {
        App_QueueText("CurrentSensor\r\nFW: 0.2.0\r\nMCU: STM32F411RET6\r\nUSB: CDC\r\nSPI: 6000000 Hz\r\n");
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

void App_CdcTxComplete(void)
{
    tx_ready = 1U;
}

void App_SetStreamingActive(bool active)
{
    streaming_active = active;
}

void App_Process(void)
{
    uint8_t character;
    uint32_t interrupt_state;

    if (hUsbDeviceFS.dev_state != USBD_STATE_CONFIGURED)
    {
        tx_ready = 1U;
    }

    if (tx_pending != 0U)
    {
        if (tx_ready != 0U && hUsbDeviceFS.dev_state == USBD_STATE_CONFIGURED)
        {
            tx_ready = 0U;
            if (CDC_Transmit_FS(tx_buffer, tx_length) == USBD_OK)
            {
                tx_pending = 0U;
            }
            else
            {
                tx_ready = 1U;
            }
        }
        return;
    }
    if (tx_ready == 0U)
    {
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
