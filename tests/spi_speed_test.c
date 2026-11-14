#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "spi_speed_core.h"

int main(void)
{
    uint16_t divider = 0U;
    assert(SpiSpeedCore_Select(96000000U, 6000000U, &divider) && divider == 16U);
    assert(SpiSpeedCore_Select(96000000U, 3000000U, &divider) && divider == 32U);
    assert(SpiSpeedCore_Select(96000000U, 1500000U, &divider) && divider == 64U);
    assert(SpiSpeedCore_Select(48000000U, 6000000U, &divider) && divider == 8U);
    assert(!SpiSpeedCore_Select(95000000U, 6000000U, &divider));
    assert(!SpiSpeedCore_Select(96000000U, 5000000U, &divider));
    assert(!SpiSpeedCore_Select(96000000U, 6000000U, 0));
    puts("SPI clock divider tests passed");
    return 0;
}
