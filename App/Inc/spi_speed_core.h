#ifndef SPI_SPEED_CORE_H
#define SPI_SPEED_CORE_H

#include <stdbool.h>
#include <stdint.h>

bool SpiSpeedCore_Select(uint32_t pclk_hz, uint32_t requested_hz,
                         uint16_t *divider);

#endif
