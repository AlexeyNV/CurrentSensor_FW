#ifndef ACQUISITION_DMA_H
#define ACQUISITION_DMA_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "acquisition_core.h"
#include "acquisition_dma_core.h"
#include "anomaly_trace.h"
#include "ina229.h"
#include "spi_dma_diagnostic.h"

void AcqDma_Init(INA229_HandleTypeDef *sensor, Acq_Core *samples,
                 Anomaly_Trace *trace);
bool AcqDma_Start(bool trace_enabled);
bool AcqDma_Stop(void);
bool AcqDma_IsStopped(void);
void AcqDma_Tick(uint32_t tick_us);
void AcqDma_RxIRQ(void);
void AcqDma_TxIRQ(void);
void AcqDma_SettleIRQ(void);
const AcqDma_Core *AcqDma_GetCore(void);
bool AcqDma_RepeatConfigure(bool enabled, uint32_t threshold_ma);
bool AcqDma_RepeatEnabled(void);
uint32_t AcqDma_RepeatThresholdMa(void);
int AcqDma_FormatRepeat(char *out, size_t size);
bool AcqDma_StartPair(uint32_t slot, uint32_t tick_us, uint32_t alert_us,
                      bool second_read, bool reject);
bool AcqDma_PairActive(void);
bool AcqDma_StartAlert(uint32_t event_us, bool second_read, uint32_t guard_us);
bool AcqDma_ArmAlert(void);
void AcqDma_CheckPairTimeout(void);
int AcqDma_FormatVerify(char *out, size_t size);
void AcqDma_SetContinuousVerify(bool enabled);
typedef struct
{
    uint32_t total, match, mismatch, raw_mismatch, rejected;
    uint32_t second_errors, max_cycle_us, uncertain;
} AcqDma_VerifySummary;
AcqDma_VerifySummary AcqDma_GetVerifySummary(void);
int AcqDma_FormatTimeouts(char *out, size_t size);
bool AcqDma_DiagnosticBegin(void);
bool AcqDma_DiagnosticRead(uint8_t reg, uint8_t length);
bool AcqDma_DiagnosticPoll(SpiDiag_Result *result);

#endif
