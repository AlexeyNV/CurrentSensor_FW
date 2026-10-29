#ifndef INA229_H
#define INA229_H

#include "stm32f4xx_hal.h"

#define INA229_REG_CONFIG           0x00U
#define INA229_REG_ADC_CONFIG       0x01U
#define INA229_REG_VSHUNT           0x04U
#define INA229_REG_VBUS             0x05U
#define INA229_REG_DIAG_ALRT        0x0BU
#define INA229_REG_MANUFACTURER_ID  0x3EU
#define INA229_REG_DEVICE_ID        0x3FU

typedef struct
{
    SPI_HandleTypeDef *hspi;
    GPIO_TypeDef *cs_port;
    uint16_t cs_pin;
} INA229_HandleTypeDef;

typedef enum
{
    INA229_OK = 0,
    INA229_ERROR,
    INA229_TIMEOUT,
    INA229_INVALID_ARG
} INA229_Status;

INA229_Status INA229_ReadReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t *value);
INA229_Status INA229_WriteReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t value);
INA229_Status INA229_ReadReg24(INA229_HandleTypeDef *handle, uint8_t reg, uint32_t *value);
INA229_Status INA229_ReadManufacturerID(INA229_HandleTypeDef *handle, uint16_t *value);
INA229_Status INA229_ReadDeviceID(INA229_HandleTypeDef *handle, uint16_t *value);

#endif
