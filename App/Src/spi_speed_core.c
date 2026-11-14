#include "spi_speed_core.h"

bool SpiSpeedCore_Select(uint32_t pclk_hz, uint32_t requested_hz,
                         uint16_t *divider)
{
    static const uint16_t choices[] = {2U, 4U, 8U, 16U, 32U, 64U, 128U, 256U};
    unsigned int i;
    if (divider == 0 || pclk_hz == 0U ||
        (requested_hz != 6000000U && requested_hz != 3000000U &&
         requested_hz != 1500000U)) return false;
    for (i = 0U; i < sizeof(choices) / sizeof(choices[0]); ++i)
    {
        if (pclk_hz / choices[i] == requested_hz &&
            pclk_hz % choices[i] == 0U)
        {
            *divider = choices[i];
            return true;
        }
    }
    return false;
}
