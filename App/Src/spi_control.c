#include "spi_control.h"

#include "acquisition.h"
#include "acquisition_dma.h"
#include "spi.h"
#include "spi_speed_core.h"

static uint16_t DividerFromPrescaler(uint32_t value)
{
    switch (value)
    {
    case SPI_BAUDRATEPRESCALER_2: return 2U;
    case SPI_BAUDRATEPRESCALER_4: return 4U;
    case SPI_BAUDRATEPRESCALER_8: return 8U;
    case SPI_BAUDRATEPRESCALER_16: return 16U;
    case SPI_BAUDRATEPRESCALER_32: return 32U;
    case SPI_BAUDRATEPRESCALER_64: return 64U;
    case SPI_BAUDRATEPRESCALER_128: return 128U;
    case SPI_BAUDRATEPRESCALER_256: return 256U;
    default: return 0U;
    }
}

static uint32_t PrescalerFromDivider(uint16_t divider)
{
    switch (divider)
    {
    case 2U: return SPI_BAUDRATEPRESCALER_2;
    case 4U: return SPI_BAUDRATEPRESCALER_4;
    case 8U: return SPI_BAUDRATEPRESCALER_8;
    case 16U: return SPI_BAUDRATEPRESCALER_16;
    case 32U: return SPI_BAUDRATEPRESCALER_32;
    case 64U: return SPI_BAUDRATEPRESCALER_64;
    case 128U: return SPI_BAUDRATEPRESCALER_128;
    case 256U: return SPI_BAUDRATEPRESCALER_256;
    default: return 0U;
    }
}

uint16_t SpiControl_GetDivider(void)
{
    return DividerFromPrescaler(hspi1.Init.BaudRatePrescaler);
}

uint32_t SpiControl_GetHz(void)
{
    uint16_t divider = SpiControl_GetDivider();
    return divider ? HAL_RCC_GetPCLK2Freq() / divider : 0U;
}

SpiControl_Status SpiControl_SetHz(uint32_t requested_hz)
{
    uint16_t divider;
    uint32_t old_prescaler;
    if (Acq_IsRunning() || !AcqDma_IsStopped() ||
        hspi1.State != HAL_SPI_STATE_READY || (SPI1->SR & SPI_SR_BSY) != 0U)
        return SPI_CONTROL_BUSY;
    if (!SpiSpeedCore_Select(HAL_RCC_GetPCLK2Freq(), requested_hz, &divider))
        return SPI_CONTROL_UNSUPPORTED_CLOCK;
    old_prescaler = hspi1.Init.BaudRatePrescaler;
    __HAL_SPI_DISABLE(&hspi1);
    hspi1.Init.BaudRatePrescaler = PrescalerFromDivider(divider);
    if (HAL_SPI_Init(&hspi1) != HAL_OK)
    {
        hspi1.Init.BaudRatePrescaler = old_prescaler;
        (void)HAL_SPI_Init(&hspi1);
        __HAL_SPI_ENABLE(&hspi1);
        return SPI_CONTROL_HAL_ERROR;
    }
    __HAL_SPI_ENABLE(&hspi1);
    return SPI_CONTROL_OK;
}
