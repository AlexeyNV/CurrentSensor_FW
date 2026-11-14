#ifndef APP_H
#define APP_H

#include <stdint.h>
#include <stdbool.h>

#define APP_FW_VERSION "1.0.0"
#define APP_FW_BUILD "release-1.0.0-" __DATE__ "-" __TIME__
#define APP_USB_DIAG_REV 2U

typedef enum {
    APP_USB_EVENT_RX = 1,
    APP_USB_EVENT_REARM_ERROR,
    APP_USB_EVENT_STOP_RX,
    APP_USB_EVENT_STOP_DONE,
    APP_USB_EVENT_END_QUEUED,
    APP_USB_EVENT_END_DONE,
    APP_USB_EVENT_TX_BUSY,
    APP_USB_EVENT_RESET,
    APP_USB_EVENT_DEINIT,
    APP_USB_EVENT_DMA_ABORT_ERROR,
    APP_USB_EVENT_DTR_DROP,
    APP_USB_EVENT_TX_ABORT,
    APP_USB_EVENT_TX_ABORT_ERROR,
    APP_USB_EVENT_STREAM_START,
    APP_USB_EVENT_TEXT_MODE,
    APP_USB_EVENT_TEXT_DONE,
    APP_USB_EVENT_CLI_ACCEPTED
} App_UsbEvent;

void App_Init(void);
void App_Process(void);
void App_CdcRx(const uint8_t *data, uint32_t length);
void App_CdcRxRearmResult(bool success);
void App_CdcLineState(uint16_t value);
void App_CdcTxComplete(void);
void App_CdcDeInit(void);
void App_UsbControlRequest(void);
void App_UsbIrq(void);
void App_UsbReset(void);
void App_UsbTxBusy(void);
void App_UsbEndQueued(void);
void App_UsbDmaAbortError(void);
void App_SetStreamingActive(bool active);
void App_RecordUsbIrqDuration(uint32_t duration_us);

#endif
