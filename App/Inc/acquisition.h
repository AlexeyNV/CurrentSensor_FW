#ifndef ACQUISITION_H
#define ACQUISITION_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ina229.h"
#include "acquisition_core.h"
#include "anomaly_trace.h"
#include "acquisition_dma_core.h"
#include "measurement_profile.h"

typedef enum { ACQ_MODE_DMA, ACQ_MODE_POLL } Acq_Mode;
typedef enum { ACQ_CAPTURE_CONTINUOUS, ACQ_CAPTURE_ALERT_MONITOR,
               ACQ_CAPTURE_SINGLE_SHOT, ACQ_CAPTURE_ALERT_DRIVEN,
               ACQ_CAPTURE_NATIVE } Acq_CaptureMode;
typedef enum { ACQ_VERIFY_OFF, ACQ_VERIFY_DIAGNOSTIC,
               ACQ_VERIFY_REJECT } Acq_VerifyPolicy;

typedef enum
{
    ACQ_OK = 0,
    ACQ_BUSY,
    ACQ_NOT_RUNNING,
    ACQ_RANGE,
    ACQ_SPI_ERROR,
    ACQ_TIMER_ERROR,
    ACQ_CONFIG_ERROR,
    ACQ_STARTUP_TIMEOUT,
    ACQ_DIAGNOSTIC_MODE_UNSUPPORTED
} Acq_Status;

void Acq_Init(INA229_HandleTypeDef *handle);
Acq_Status Acq_Start(uint32_t test_seconds); /* 0 = continuous, otherwise 1 or 5 */
Acq_Status Acq_Stop(void);
void Acq_Process(void);
bool Acq_IsRunning(void);
bool Acq_TestFinished(void);
void Acq_ClearTestFinished(void);
void Acq_SetStreamMode(bool enabled);
bool Acq_SetMode(Acq_Mode mode);
Acq_Mode Acq_GetMode(void);
bool Acq_SetCaptureMode(Acq_CaptureMode mode);
Acq_CaptureMode Acq_GetCaptureMode(void);
bool Acq_SetAlertConversion(uint32_t us);
bool Acq_SetNativeLatch(bool enabled);
bool Acq_IsNativeSession(void);
void Acq_NativePrimary(uint32_t start, uint32_t end, uint32_t events_end);
void Acq_NativeAckStart(uint32_t start);
int Acq_FormatNativeReport(char *out, size_t size);
int Acq_FormatNativeTrace(char *out, size_t size, bool service);
bool Acq_AlertDrivenReady(uint16_t diag, uint32_t *slot);
void Acq_AlertDrivenComplete(uint32_t first_us, uint32_t finish_us);
uint32_t Acq_AlertEventCount(void);
void Acq_AlertDrivenStale(void);
void Acq_AlertDrivenRearmFailure(void);
void Acq_AlertDrivenFailed(bool start_failed);
void Acq_AlertDrivenSpiStart(uint32_t now);
const char *Acq_GetSessionCaptureName(void);
bool Acq_SetVerifyPolicy(Acq_VerifyPolicy policy);
Acq_VerifyPolicy Acq_GetVerifyPolicy(void);
int Acq_FormatAlertReport(char *out, size_t size);
int Acq_FormatEventReport(char *out, size_t size);
int Acq_FormatVerifyReport(char *out, size_t size);
void Acq_AlertIRQ(uint32_t timestamp_us, bool pin_low);
void Acq_AlertReadStarted(uint32_t timestamp_us);
void Acq_AlertSampleSaved(void);
void Acq_AlertPairComplete(uint32_t timestamp_us);
Acq_Status Acq_SelectProfile(MeasurementProfile_Id id);
const AcqDma_Core *Acq_GetDmaCore(void);
bool Acq_PopSample(Acq_Sample *sample);
const Acq_Core *Acq_GetCore(void);
const Anomaly_Trace *Acq_GetAnomalyTrace(void);
bool Acq_SetAnomalyLimitMa(uint32_t limit_ma);
int Acq_FormatAnomalyTrace(char *out, size_t size);
int Acq_FormatStatus(char *out, size_t size);
int Acq_FormatTest(char *out, size_t size);
int Acq_FormatConfig(char *out, size_t size);
int Acq_FormatProbe(char *out, size_t size, uint32_t wait_us);
const char *Acq_StatusName(Acq_Status status);

#endif
