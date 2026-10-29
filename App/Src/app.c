#include "app.h"

#include <stdio.h>
#include <string.h>

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

static void App_ExecuteCommand(void)
{
    uint16_t manufacturer;
    uint16_t device;
    uint16_t config;
    uint16_t adc_config;
    uint16_t diag_alrt;
    INA229_Status status;

    line[line_length] = '\0';
    if (strcmp(line, "help") == 0)
    {
        App_QueueText("Commands: help, ping, info, ina, regs\r\n");
    }
    else if (strcmp(line, "ping") == 0)
    {
        App_QueueText("pong\r\n");
    }
    else if (strcmp(line, "info") == 0)
    {
        App_QueueText("CurrentSensor\r\nFW: 0.1.0\r\nMCU: STM32F411RET6\r\nUSB: CDC\r\nSPI: 6000000 Hz\r\n");
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
