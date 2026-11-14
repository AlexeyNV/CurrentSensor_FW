#ifndef SPI_CONTROL_H
#define SPI_CONTROL_H

#include <stdint.h>

typedef enum
{
    SPI_CONTROL_OK,
    SPI_CONTROL_BUSY,
    SPI_CONTROL_UNSUPPORTED_CLOCK,
    SPI_CONTROL_HAL_ERROR
} SpiControl_Status;

SpiControl_Status SpiControl_SetHz(uint32_t requested_hz);
uint32_t SpiControl_GetHz(void);
uint16_t SpiControl_GetDivider(void);

#endif
