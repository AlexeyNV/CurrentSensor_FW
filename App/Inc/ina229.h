#ifndef INA229_H
#define INA229_H

#include <stdbool.h>
#include "stm32f4xx_hal.h"

#define INA229_REG_CONFIG           0x00U
#define INA229_REG_ADC_CONFIG       0x01U
#define INA229_REG_VSHUNT           0x04U
#define INA229_REG_VBUS             0x05U
#define INA229_REG_DIETEMP          0x06U
#define INA229_REG_DIAG_ALRT        0x0BU
#define INA229_REG_MANUFACTURER_ID  0x3EU
#define INA229_REG_DEVICE_ID        0x3FU
#define INA229_CONFIG_ADCRANGE      (1U << 4)
#define INA229_DIAG_ALATCH          (1U << 15)
#define INA229_DIAG_CNVR            (1U << 14)
#define INA229_DIAG_SLOWALERT       (1U << 13)
#define INA229_DIAG_APOL            (1U << 12)
#define INA229_DIAG_CNVRF           (1U << 1)

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
    INA229_INVALID_ARG,
    INA229_BUSY
} INA229_Status;

void INA229_SetDmaOwner(bool owned);
bool INA229_DmaOwned(void);
uint32_t INA229_ContentionCount(void);
uint32_t INA229_AdcWriteAttempts(void);
uint32_t INA229_LastHalStatus(void);

INA229_Status INA229_ReadReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t *value);
INA229_Status INA229_WriteReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t value);
INA229_Status INA229_ReadReg24(INA229_HandleTypeDef *handle, uint8_t reg, uint32_t *value);
INA229_Status INA229_ReadManufacturerID(INA229_HandleTypeDef *handle, uint16_t *value);
INA229_Status INA229_ReadDeviceID(INA229_HandleTypeDef *handle, uint16_t *value);
INA229_Status INA229_ReadVShunt(INA229_HandleTypeDef *handle, uint32_t *value);
INA229_Status INA229_ReadVBus(INA229_HandleTypeDef *handle, uint32_t *value);
INA229_Status INA229_ReadDieTemp(INA229_HandleTypeDef *handle, uint16_t *value);

#endif
