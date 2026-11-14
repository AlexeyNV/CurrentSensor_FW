#ifndef SPI_FREEZE_H
#define SPI_FREEZE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ina229.h"
void SpiFreeze_Init(INA229_HandleTypeDef *sensor);
bool SpiFreeze_IsBusy(void);
const char *SpiFreeze_Start(uint32_t pairs, bool ids);
bool SpiFreeze_SetVshct(uint32_t us);
void SpiFreeze_Process(void);
void SpiFreeze_Stop(bool disconnected);
int SpiFreeze_FormatShow(char *out, size_t size);
int SpiFreeze_FormatReport(char *out, size_t size);
int SpiFreeze_FormatEvents(char *out, size_t size, uint32_t page);
#endif
