#include "ina229.h"

#define INA229_SPI_TIMEOUT_MS 10U

static INA229_Status INA229_Transfer(INA229_HandleTypeDef *handle,
                                      uint8_t *tx, uint8_t *rx, uint16_t length)
{
    HAL_StatusTypeDef result;

    if (handle == NULL || handle->hspi == NULL || handle->cs_port == NULL ||
        handle->cs_pin == 0U || tx == NULL || rx == NULL)
    {
        return INA229_INVALID_ARG;
    }

    HAL_GPIO_WritePin(handle->cs_port, handle->cs_pin, GPIO_PIN_RESET);
    result = HAL_SPI_TransmitReceive(handle->hspi, tx, rx, length, INA229_SPI_TIMEOUT_MS);
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
