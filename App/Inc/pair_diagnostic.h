#ifndef PAIR_DIAGNOSTIC_H
#define PAIR_DIAGNOSTIC_H
#include <stddef.h>
#include "ina229.h"
#include "spi_dma_diagnostic.h"
void PairDiag_Init(INA229_HandleTypeDef *sensor);
void PairDiag_NativeStart(bool enabled, uint32_t ct, uint16_t adc, uint16_t diag);
void PairDiag_NativeStop(void);
void PairDiag_Begin(uint32_t seq, uint32_t irq, uint32_t alert);
void PairDiag_NativeResult(uint32_t phase, const SpiDiag_Result *r, uint32_t raw1, uint32_t raw2);
void PairDiag_NativeFailure(uint32_t phase, const SpiDiag_Result *r);
void PairDiag_Irq(uint32_t us, bool low);
bool PairDiag_Tracing(void);
uint32_t PairDiag_EdgeCount(void);
void PairDiag_SnapshotDuration(uint32_t us);
bool PairDiag_IsBusy(void);
void PairDiag_Process(void);
void PairDiag_Stop(bool error);
void PairDiag_MarkerIRQ(void);
int PairDiag_Command(const char *args, char *out, size_t size);
#endif
