#include "ina229.h"

#define INA229_SPI_TIMEOUT_MS 10U

static volatile bool dma_owned;
static volatile uint32_t contention_count;
static volatile uint32_t adc_write_attempts;
static volatile uint32_t last_hal_status;

void INA229_SetDmaOwner(bool owned) { dma_owned = owned; }
bool INA229_DmaOwned(void) { return dma_owned; }
uint32_t INA229_ContentionCount(void) { return contention_count; }
uint32_t INA229_AdcWriteAttempts(void) { return adc_write_attempts; }
uint32_t INA229_LastHalStatus(void) { return last_hal_status; }

static INA229_Status INA229_Transfer(INA229_HandleTypeDef *handle,
                                      uint8_t *tx, uint8_t *rx, uint16_t length)
{
    HAL_StatusTypeDef result;

    if (handle == NULL || handle->hspi == NULL || handle->cs_port == NULL ||
        handle->cs_pin == 0U || tx == NULL || rx == NULL)
    {
        return INA229_INVALID_ARG;
    }

    if (dma_owned)
    {
        ++contention_count;
        return INA229_BUSY;
    }

    HAL_GPIO_WritePin(handle->cs_port, handle->cs_pin, GPIO_PIN_RESET);
    result = HAL_SPI_TransmitReceive(handle->hspi, tx, rx, length, INA229_SPI_TIMEOUT_MS);
    last_hal_status = (uint32_t)result;
    HAL_GPIO_WritePin(handle->cs_port, handle->cs_pin, GPIO_PIN_SET);

    if (result == HAL_OK)
    {
        return INA229_OK;
    }
    return (result == HAL_TIMEOUT) ? INA229_TIMEOUT : INA229_ERROR;
}

INA229_Status INA229_ReadReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t *value)
{
    uint8_t tx[3] = {0};
    uint8_t rx[3] = {0};
    INA229_Status status;

    if (reg > 0x3FU || value == NULL)
    {
        return INA229_INVALID_ARG;
    }
    tx[0] = (uint8_t)((reg << 2) | 0x01U);
    status = INA229_Transfer(handle, tx, rx, sizeof(tx));
    if (status == INA229_OK)
    {
        *value = (uint16_t)(((uint16_t)rx[1] << 8) | rx[2]);
    }
    return status;
}

INA229_Status INA229_WriteReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t value)
{
    uint8_t tx[3] = {(uint8_t)(reg << 2), (uint8_t)(value >> 8), (uint8_t)value};
    uint8_t rx[3] = {0};

    if (reg > 0x3FU)
    {
        return INA229_INVALID_ARG;
    }
    if (reg == INA229_REG_ADC_CONFIG) ++adc_write_attempts;
    return INA229_Transfer(handle, tx, rx, sizeof(tx));
}

INA229_Status INA229_ReadReg24(INA229_HandleTypeDef *handle, uint8_t reg, uint32_t *value)
{
    uint8_t tx[4] = {0};
    uint8_t rx[4] = {0};
    INA229_Status status;

    if (reg > 0x3FU || value == NULL)
    {
        return INA229_INVALID_ARG;
    }
    tx[0] = (uint8_t)((reg << 2) | 0x01U);
    status = INA229_Transfer(handle, tx, rx, sizeof(tx));
    if (status == INA229_OK)
    {
        *value = ((uint32_t)rx[1] << 16) | ((uint32_t)rx[2] << 8) | rx[3];
    }
    return status;
}

INA229_Status INA229_ReadManufacturerID(INA229_HandleTypeDef *handle, uint16_t *value)
{
    return INA229_ReadReg16(handle, INA229_REG_MANUFACTURER_ID, value);
}

INA229_Status INA229_ReadDeviceID(INA229_HandleTypeDef *handle, uint16_t *value)
{
    return INA229_ReadReg16(handle, INA229_REG_DEVICE_ID, value);
}

INA229_Status INA229_ReadVShunt(INA229_HandleTypeDef *handle, uint32_t *value)
{
    return INA229_ReadReg24(handle, INA229_REG_VSHUNT, value);
}

INA229_Status INA229_ReadVBus(INA229_HandleTypeDef *handle, uint32_t *value)
{
    return INA229_ReadReg24(handle, INA229_REG_VBUS, value);
}

INA229_Status INA229_ReadDieTemp(INA229_HandleTypeDef *handle, uint16_t *value)
{
    return INA229_ReadReg16(handle, INA229_REG_DIETEMP, value);
}
