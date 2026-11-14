#ifndef TEST_SPI_HAL_H
#define TEST_SPI_HAL_H

#include <stdint.h>

typedef struct { uint32_t unused; } SPI_HandleTypeDef;
typedef struct { uint32_t high; } GPIO_TypeDef;
typedef enum { HAL_OK = 0, HAL_ERROR = 1, HAL_TIMEOUT = 3 } HAL_StatusTypeDef;
#define GPIO_PIN_RESET 0U
#define GPIO_PIN_SET 1U

void HAL_GPIO_WritePin(GPIO_TypeDef *port, uint16_t pin, uint32_t value);
HAL_StatusTypeDef HAL_SPI_TransmitReceive(SPI_HandleTypeDef *spi,
    uint8_t *tx, uint8_t *rx, uint16_t length, uint32_t timeout);

#endif
