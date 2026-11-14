#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>

#include "ina229.h"

static SPI_HandleTypeDef spi;
static GPIO_TypeDef gpio;
static uint32_t transfers;
static uint32_t cs_changes;
static HAL_StatusTypeDef next_result = HAL_OK;

void HAL_GPIO_WritePin(GPIO_TypeDef *port, uint16_t pin, uint32_t value)
{
    assert(port == &gpio && pin == 1U);
    port->high = value;
    ++cs_changes;
}

HAL_StatusTypeDef HAL_SPI_TransmitReceive(SPI_HandleTypeDef *handle,
    uint8_t *tx, uint8_t *rx, uint16_t length, uint32_t timeout)
{
    assert(handle == &spi && timeout == 10U);
    assert((length == 4U && tx[0] == ((INA229_REG_VSHUNT << 2) | 1U)) ||
           (length == 3U && tx[0] == (INA229_REG_ADC_CONFIG << 2)));
    assert(gpio.high == GPIO_PIN_RESET);
    ++transfers;
    rx[0] = 0U; rx[1] = 0x85U; rx[2] = 0xF3U;
    if (length == 4U) rx[3] = 0x30U;
    return next_result;
}

int main(void)
{
    INA229_HandleTypeDef sensor = {&spi, &gpio, 1U};
    uint32_t raw = 0xDEADBEEFU;
    gpio.high = GPIO_PIN_SET;
    INA229_SetDmaOwner(true);
    assert(INA229_DmaOwned());
    assert(INA229_ReadVShunt(&sensor, &raw) == INA229_BUSY);
    assert(raw == 0xDEADBEEFU && transfers == 0U && cs_changes == 0U);
    assert(INA229_ContentionCount() == 1U);
    INA229_SetDmaOwner(false);
    assert(!INA229_DmaOwned());
    assert(INA229_ReadVShunt(&sensor, &raw) == INA229_OK);
    assert(raw == 0x85F330U && gpio.high == GPIO_PIN_SET);
    assert(transfers == 1U && cs_changes == 2U);
    next_result = HAL_TIMEOUT;
    raw = 0xDEADBEEFU;
    assert(INA229_ReadVShunt(&sensor, &raw) == INA229_TIMEOUT);
    assert(raw == 0xDEADBEEFU && gpio.high == GPIO_PIN_SET);
    assert(INA229_LastHalStatus() == HAL_TIMEOUT);
    /* The freeze audit counts every ADC write attempt, even an attempt blocked
       by DMA ownership. Reads and invalid register addresses must not count. */
    assert(INA229_AdcWriteAttempts() == 0U);
    INA229_SetDmaOwner(true);
    assert(INA229_WriteReg16(&sensor, INA229_REG_ADC_CONFIG, 0x2AA8U) == INA229_BUSY);
    assert(INA229_AdcWriteAttempts() == 1U && transfers == 2U);
    INA229_SetDmaOwner(false);
    assert(INA229_WriteReg16(&sensor, 64U, 0U) == INA229_INVALID_ARG);
    assert(INA229_AdcWriteAttempts() == 1U);
    next_result = HAL_OK;
    assert(INA229_WriteReg16(&sensor, INA229_REG_ADC_CONFIG, 0x2AA8U) == INA229_OK);
    assert(INA229_AdcWriteAttempts() == 2U && INA229_LastHalStatus() == HAL_OK);
    next_result = HAL_ERROR;
    assert(INA229_WriteReg16(&sensor, INA229_REG_ADC_CONFIG, 0xFB68U) == INA229_ERROR);
    assert(INA229_AdcWriteAttempts() == 3U && INA229_LastHalStatus() == HAL_ERROR);
    assert(gpio.high == GPIO_PIN_SET);
    puts("INA229 SPI ownership and CS tests passed");
    return 0;
}
