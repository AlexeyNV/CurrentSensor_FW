#ifndef APP_H
#define APP_H

#include <stdint.h>
#include <stdbool.h>

void App_Init(void);
void App_Process(void);
void App_CdcRx(const uint8_t *data, uint32_t length);
void App_CdcTxComplete(void);
void App_SetStreamingActive(bool active);

#endif
