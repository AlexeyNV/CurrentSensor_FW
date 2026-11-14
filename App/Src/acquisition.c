#include "acquisition.h"

#include <stdio.h>

#include "acquisition_core.h"
#include "acquisition_dma.h"
#include "acquisition_alert_core.h"
#include "alert_driven_core.h"
#include "native_core.h"
#include "pair_diagnostic.h"
#include "format64.h"
#include "tim.h"
#include "timebase.h"
#include "spi_control.h"

static INA229_HandleTypeDef *sensor;
static Acq_Core core;
static Anomaly_Trace anomaly_trace;
static uint32_t next_anomaly_limit_ma = 10000U;
static uint16_t original_adc;
static uint16_t original_diag;
static bool diag_saved;
static uint16_t fast_adc;
static uint8_t test_running;
static uint8_t test_finished;
static bool stream_mode;
static Acq_Mode acq_mode = ACQ_MODE_DMA;
static Acq_Mode session_mode = ACQ_MODE_DMA;
static Acq_CaptureMode capture_mode = ACQ_CAPTURE_CONTINUOUS;
static Acq_CaptureMode session_capture_mode = ACQ_CAPTURE_CONTINUOUS;
static Acq_VerifyPolicy verify_policy = ACQ_VERIFY_OFF;
static Acq_VerifyPolicy session_verify_policy = ACQ_VERIFY_OFF;
static AcqAlert_Core alert_core;
static AlertDriven_Core driven;
static Native_Core native_core;
static bool native_latch;
static bool session_native_latch;
static uint32_t session_conversion_us;
static uint32_t alert_conversion_us; /* 0: profile, 50/84: explicit experiment */
static volatile bool pair_started;
static uint32_t acq_session_id;
static bool session_was_stream;
static uint32_t test_duration_us;
static volatile uint32_t tick_seq;
static volatile uint32_t tick_cycles;

static uint32_t Acq_Now(void) { return __HAL_TIM_GET_COUNTER(&htim2); }

static INA229_Status Acq_TrackedWrite(uint8_t reg, uint16_t value)
{
    uint32_t started = Acq_Now();
    INA229_Status status = INA229_WriteReg16(sensor, reg, value);
    AcqCore_RecordSpi(&core, Acq_Now() - started);
    return status;
}

static bool Acq_RestoreWrite(void *context, uint16_t value)
{
    (void)context;
    return Acq_TrackedWrite(INA229_REG_ADC_CONFIG, value) == INA229_OK;
}

static bool Acq_RestoreConfigs(void)
{
    bool adc_ok = !core.config_saved ||
                  AcqCore_RestoreConfig(&core, Acq_RestoreWrite, NULL);
    bool diag_ok = true;
    if (diag_saved)
    {
        diag_ok = INA229_WriteReg16(sensor, INA229_REG_DIAG_ALRT,
            (uint16_t)(original_diag & (INA229_DIAG_ALATCH | INA229_DIAG_CNVR |
                                        INA229_DIAG_SLOWALERT | INA229_DIAG_APOL))) == INA229_OK;
        if (diag_ok) diag_saved = false;
    }
    return adc_ok && diag_ok;
}

static INA229_Status Acq_TrackedRead16(uint8_t reg, uint16_t *value)
{
    uint32_t started = Acq_Now();
    INA229_Status status = INA229_ReadReg16(sensor, reg, value);
    AcqCore_RecordSpi(&core, Acq_Now() - started);
    return status;
}

static INA229_Status Acq_TrackedVShunt(uint32_t *value)
{
    uint32_t started = Acq_Now();
    INA229_Status status = INA229_ReadVShunt(sensor, value);
    AcqCore_RecordSpi(&core, Acq_Now() - started);
    return status;
}

static void Acq_FailSample(void)
{
    AcqCore_SpiError(&core);
    AcqCore_Missed(&core);
}

void Acq_Init(INA229_HandleTypeDef *handle)
{
    sensor = handle;
    AcqAlertCore_Reset(&alert_core);
    AnomalyTrace_Init(&anomaly_trace);
    AcqDma_Init(handle, &core, &anomaly_trace);
    /* TIM3 timestamps must preempt USB FS (priority 6). The ISR stays short. */
    HAL_NVIC_SetPriority(TIM3_IRQn, 5U, 0U);
    Timebase_Init();
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CYCCNT = 0U;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

bool Acq_IsRunning(void) { return core.running; }
bool Acq_TestFinished(void) { return test_finished != 0U; }
void Acq_ClearTestFinished(void) { test_finished = 0U; }
void Acq_SetStreamMode(bool enabled) { stream_mode = enabled; }
bool Acq_SetMode(Acq_Mode mode)
{
    if (core.running || core.config_saved || diag_saved || !AcqDma_IsStopped() ||
        (mode != ACQ_MODE_DMA && mode != ACQ_MODE_POLL) ||
        (mode == ACQ_MODE_POLL &&
         (capture_mode == ACQ_CAPTURE_SINGLE_SHOT ||
          capture_mode == ACQ_CAPTURE_ALERT_DRIVEN || capture_mode == ACQ_CAPTURE_NATIVE ||
          verify_policy != ACQ_VERIFY_OFF)))
        return false;
    acq_mode = mode;
    return true;
}
Acq_Mode Acq_GetMode(void) { return acq_mode; }
bool Acq_SetCaptureMode(Acq_CaptureMode mode)
{
    if (core.running || core.config_saved || diag_saved || !AcqDma_IsStopped() ||
        mode > ACQ_CAPTURE_NATIVE ||
        (verify_policy == ACQ_VERIFY_REJECT && mode != ACQ_CAPTURE_SINGLE_SHOT))
        return false;
    if ((mode == ACQ_CAPTURE_SINGLE_SHOT || mode == ACQ_CAPTURE_ALERT_DRIVEN ||
         mode == ACQ_CAPTURE_NATIVE) &&
        acq_mode != ACQ_MODE_DMA) return false;
    capture_mode = mode;
    return true;
}
Acq_CaptureMode Acq_GetCaptureMode(void) { return capture_mode; }
bool Acq_SetAlertConversion(uint32_t us)
{
    if (core.running || core.config_saved || diag_saved ||
        (us != 0U && us != 50U && us != 84U)) return false;
    alert_conversion_us = us;
    return true;
}
bool Acq_SetNativeLatch(bool enabled)
{
    if (core.running || core.config_saved || diag_saved || !AcqDma_IsStopped()) return false;
    native_latch = enabled; return true;
}
bool Acq_IsNativeSession(void) { return session_capture_mode == ACQ_CAPTURE_NATIVE; }
void Acq_NativeAckStart(uint32_t start)
{ if (Acq_IsNativeSession()) native_core.ack_start = start; }
void Acq_NativePrimary(uint32_t start, uint32_t end, uint32_t events_end)
{
    if (Acq_IsNativeSession()) NativeCore_Primary(&native_core, start, end, events_end);
}
uint32_t Acq_AlertEventCount(void)
{ return Acq_IsNativeSession() ? native_core.irq_count : driven.irq_count; }
bool Acq_AlertDrivenReady(uint16_t diag, uint32_t *slot)
{
    if (Acq_IsNativeSession())
        return NativeCore_ReadyAt(&native_core, (diag & INA229_DIAG_CNVRF) != 0U,
                                  Acq_Now(), slot);
    bool selected = AlertDriven_ReadyAt(&driven, (diag & INA229_DIAG_CNVRF) != 0U,
                                        Acq_Now(), slot);
    return selected;
}
void Acq_AlertDrivenComplete(uint32_t first_us, uint32_t finish_us)
{
    if (Acq_IsNativeSession()) { NativeCore_Complete(&native_core, finish_us); return; }
    uint32_t elapsed = first_us - driven.event_time;
    if (elapsed > driven.max_complete_us) driven.max_complete_us = elapsed;
    elapsed = finish_us - driven.event_time;
    if (elapsed > driven.max_cycle_us) driven.max_cycle_us = elapsed;
}
void Acq_AlertDrivenStale(void)
{ if (!Acq_IsNativeSession()) AlertDriven_Expired(&driven); }
void Acq_AlertDrivenRearmFailure(void)
{ if (Acq_IsNativeSession()) ++native_core.rearm_failures; else ++driven.rearm_failures; }
void Acq_AlertDrivenFailed(bool start_failed)
{
    if (Acq_IsNativeSession()) {
        if (start_failed) ++native_core.start_failed;
        else NativeCore_Failed(&native_core, Acq_Now());
        return;
    }
    /* Fail() may still retain a valid first read after a secondary error. Let
       it commit that sample before releasing the reservation. */
    if (start_failed) ++driven.start_failed;
    else AlertDriven_Failed(&driven, false);
}
void Acq_AlertDrivenSpiStart(uint32_t now)
{
    if (Acq_IsNativeSession()) { NativeCore_ReadStart(&native_core, now); return; }
    uint32_t latency = now - driven.event_time;
    if (latency > driven.max_start_us) driven.max_start_us = latency;
}
bool Acq_SetVerifyPolicy(Acq_VerifyPolicy policy)
{
    if (core.running || core.config_saved || diag_saved || !AcqDma_IsStopped() ||
        policy > ACQ_VERIFY_REJECT ||
        (policy != ACQ_VERIFY_OFF && acq_mode != ACQ_MODE_DMA) ||
        (policy == ACQ_VERIFY_REJECT && capture_mode != ACQ_CAPTURE_SINGLE_SHOT))
        return false;
    verify_policy = policy;
    return true;
}
Acq_VerifyPolicy Acq_GetVerifyPolicy(void) { return verify_policy; }
void Acq_AlertIRQ(uint32_t timestamp_us, bool pin_low)
{
    PairDiag_Irq(timestamp_us, pin_low);
    if (core.running && Acq_IsNativeSession()) {
        NativeCore_Irq(&native_core, timestamp_us, pin_low);
        return;
    }
    if (core.running && session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN)
    {
        /* A captured falling edge remains an event even if GPIO has risen by
           ISR service. GPIO-high is diagnostic, not evidence of noise. */
        AcqAlertCore_Irq(&alert_core, timestamp_us, true, false);
        if (!pin_low) ++alert_core.spurious;
        AlertDriven_Irq(&driven, timestamp_us);
        if (AcqDma_GetCore()->state != ACQ_DMA_IDLE) ++driven.while_dma;
        return;
    }
    if (core.running && session_capture_mode != ACQ_CAPTURE_CONTINUOUS)
        AcqAlertCore_Irq(&alert_core, timestamp_us, pin_low,
                         session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT);
}
void Acq_AlertReadStarted(uint32_t timestamp_us)
{
    uint32_t alert_us;
    uint32_t elapsed;
    if (!core.running || session_capture_mode != ACQ_CAPTURE_ALERT_MONITOR ||
        alert_core.irq_count == 0U) return;
    alert_us = alert_core.last_irq_us;
    elapsed = timestamp_us - alert_us;
    if (elapsed <= core.period_us && elapsed > alert_core.max_alert_to_read_us)
        alert_core.max_alert_to_read_us = elapsed;
}
void Acq_AlertSampleSaved(void)
{
    if (core.running && Acq_IsNativeSession()) NativeCore_Accept(&native_core);
    if (core.running && session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN)
    {
        AlertDriven_Accept(&driven, core.previous_sample_us);
        core.missed = driven.missed_slots;
    }
    if (core.running && session_capture_mode != ACQ_CAPTURE_CONTINUOUS)
        AcqAlertCore_Sample(&alert_core);
}
void Acq_AlertPairComplete(uint32_t timestamp_us)
{
    if (core.running && session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT)
    {
        pair_started = false;
        AcqAlertCore_Finish(&alert_core, timestamp_us);
    }
}
static uint32_t Acq_TimerClock(void)
{
    RCC_ClkInitTypeDef clocks;
    uint32_t latency;
    uint32_t clock;
    HAL_RCC_GetClockConfig(&clocks, &latency);
    clock = HAL_RCC_GetPCLK1Freq();
    if (clocks.APB1CLKDivider != RCC_HCLK_DIV1) clock *= 2U;
    return clock;
}

Acq_Status Acq_SelectProfile(MeasurementProfile_Id id)
{
    const MeasurementProfile *profile = MeasurementProfile_Get(id);
    uint16_t psc, arr;
    if (profile == NULL) return ACQ_RANGE;
    if (core.running || core.config_saved || diag_saved ||
        (TIM3->CR1 & TIM_CR1_CEN) != 0U ||
        !AcqDma_IsStopped()) return ACQ_BUSY;
    if (!MeasurementProfile_TimerDividers(Acq_TimerClock(), profile->period_us,
                                          &psc, &arr)) return ACQ_TIMER_ERROR;
    HAL_NVIC_DisableIRQ(TIM3_IRQn);
    __HAL_TIM_DISABLE_IT(&htim3, TIM_IT_UPDATE);
    __HAL_TIM_SET_PRESCALER(&htim3, psc);
    __HAL_TIM_SET_AUTORELOAD(&htim3, arr);
    TIM3->EGR = TIM_EGR_UG;
    __HAL_TIM_SET_COUNTER(&htim3, 0U);
    __HAL_TIM_CLEAR_FLAG(&htim3, TIM_FLAG_UPDATE);
    HAL_NVIC_ClearPendingIRQ(TIM3_IRQn);
    HAL_NVIC_EnableIRQ(TIM3_IRQn);
    (void)MeasurementProfile_Select(id);
    return ACQ_OK;
}
const AcqDma_Core *Acq_GetDmaCore(void) { return AcqDma_GetCore(); }
bool Acq_PopSample(Acq_Sample *sample) { return AcqCore_Pop(&core, sample); }
const Acq_Core *Acq_GetCore(void) { return &core; }
const Anomaly_Trace *Acq_GetAnomalyTrace(void) { return &anomaly_trace; }
bool Acq_SetAnomalyLimitMa(uint32_t limit_ma)
{
    if (limit_ma > ANOMALY_MAX_LIMIT_MA || core.running) return false;
    next_anomaly_limit_ma = limit_ma;
    return true;
}

Acq_Status Acq_Start(uint32_t test_seconds)
{
    uint16_t config;
    uint16_t adc;
    uint16_t diag = 0U;
    uint16_t start_adc;
    uint32_t startup_begin;
    uint32_t wait_begin;
    uint32_t wait_ms;
    const MeasurementProfile *profile = MeasurementProfile_Selected();
    Acq_Status profile_status;
    if (core.running || core.config_saved || diag_saved) return ACQ_BUSY;
    if (test_seconds != 0U && test_seconds != 1U && test_seconds != 5U) return ACQ_RANGE;
    if (AcqDma_RepeatEnabled() &&
        (profile->id == PROFILE_FAST || acq_mode != ACQ_MODE_DMA))
        return ACQ_DIAGNOSTIC_MODE_UNSUPPORTED;
    if (AcqDma_RepeatEnabled() && verify_policy != ACQ_VERIFY_OFF)
        return ACQ_DIAGNOSTIC_MODE_UNSUPPORTED;
    if (capture_mode == ACQ_CAPTURE_SINGLE_SHOT &&
        (acq_mode != ACQ_MODE_DMA || AcqDma_RepeatEnabled()))
        return ACQ_DIAGNOSTIC_MODE_UNSUPPORTED;
    if ((capture_mode == ACQ_CAPTURE_ALERT_DRIVEN || capture_mode == ACQ_CAPTURE_NATIVE) &&
        (acq_mode != ACQ_MODE_DMA || AcqDma_RepeatEnabled()))
        return ACQ_DIAGNOSTIC_MODE_UNSUPPORTED;
    /* Keep transmitted profile metadata truthful for the unchanged CSF4 CONFIG. */
    if (capture_mode == ACQ_CAPTURE_ALERT_DRIVEN && stream_mode)
        return ACQ_DIAGNOSTIC_MODE_UNSUPPORTED;
    if (capture_mode == ACQ_CAPTURE_NATIVE && stream_mode &&
        (profile->id != PROFILE_FAST || alert_conversion_us != 84U || !native_latch ||
         verify_policy != ACQ_VERIFY_OFF)) return ACQ_DIAGNOSTIC_MODE_UNSUPPORTED;
    profile_status = Acq_SelectProfile(profile->id);
    if (profile_status != ACQ_OK) return profile_status;
    if (INA229_ReadReg16(sensor, INA229_REG_CONFIG, &config) != INA229_OK ||
        INA229_ReadReg16(sensor, INA229_REG_ADC_CONFIG, &adc) != INA229_OK) return ACQ_SPI_ERROR;
    if ((config & INA229_CONFIG_ADCRANGE) != 0U) return ACQ_RANGE;
    startup_begin = Acq_Now();
    original_adc = adc;
    fast_adc = capture_mode == ACQ_CAPTURE_SINGLE_SHOT ?
        (uint16_t)((profile->adc_config & 0x0FFFU) | 0x2000U) : profile->adc_config;
    if ((capture_mode == ACQ_CAPTURE_ALERT_DRIVEN || capture_mode == ACQ_CAPTURE_NATIVE) &&
        alert_conversion_us != 0U)
        fast_adc = (uint16_t)((fast_adc & 0xFE3FU) |
                              (alert_conversion_us == 84U ? 0x0040U : 0U));
    start_adc = capture_mode == ACQ_CAPTURE_SINGLE_SHOT ?
        (uint16_t)(fast_adc & 0x0FFFU) : fast_adc;
    AcqCore_SaveConfig(&core, original_adc);
    diag_saved = false;
    if (capture_mode != ACQ_CAPTURE_CONTINUOUS)
    {
        if (INA229_ReadReg16(sensor, INA229_REG_DIAG_ALRT, &original_diag) != INA229_OK)
        {
            core.config_saved = false;
            return ACQ_SPI_ERROR;
        }
        diag_saved = true;
        diag = (uint16_t)((original_diag & INA229_DIAG_SLOWALERT) |
                          INA229_DIAG_CNVR);
        if (capture_mode == ACQ_CAPTURE_NATIVE && native_latch) diag |= INA229_DIAG_ALATCH;
        if (INA229_WriteReg16(sensor, INA229_REG_DIAG_ALRT, diag) != INA229_OK)
        {
            (void)Acq_RestoreConfigs();
            return ACQ_SPI_ERROR;
        }
        if (INA229_ReadReg16(sensor, INA229_REG_DIAG_ALRT, &adc) != INA229_OK)
        {
            (void)Acq_RestoreConfigs();
            return ACQ_SPI_ERROR;
        }
        if ((adc & 0xF000U) != diag)
        {
            (void)Acq_RestoreConfigs();
            return ACQ_CONFIG_ERROR;
        }
    }
    /* One write starts continuous shunt conversion and clears the old CNVRF. */
    if (INA229_WriteReg16(sensor, INA229_REG_ADC_CONFIG, start_adc) != INA229_OK)
    {
        (void)Acq_RestoreConfigs();
        return ACQ_SPI_ERROR;
    }
    if (INA229_ReadReg16(sensor, INA229_REG_ADC_CONFIG, &adc) != INA229_OK)
    {
        (void)Acq_RestoreConfigs();
        return ACQ_SPI_ERROR;
    }
    if (adc != start_adc)
    {
        (void)Acq_RestoreConfigs();
        return ACQ_CONFIG_ERROR;
    }
    /* Foreground startup only. MODE write restarts conversion. Discard the
       startup ready flag before arming TIM3; every recorded slot checks CNVRF
       again. TIM2 remains free-running. Report this preparation separately. */
    if (capture_mode != ACQ_CAPTURE_SINGLE_SHOT)
    {
    wait_begin = Acq_Now();
    wait_ms = HAL_GetTick();
    while (Acq_Now() - wait_begin < 2U * profile->vshct_us + 100U)
    {
        if (HAL_GetTick() - wait_ms >= 10U)
        {
            (void)Acq_RestoreConfigs();
            return ACQ_STARTUP_TIMEOUT;
        }
    }
    if (INA229_ReadReg16(sensor, INA229_REG_DIAG_ALRT, &diag) != INA229_OK)
    {
        (void)Acq_RestoreConfigs();
        return ACQ_SPI_ERROR;
    }
    if ((diag & INA229_DIAG_CNVRF) == 0U)
    {
        (void)Acq_RestoreConfigs();
        return ACQ_STARTUP_TIMEOUT;
    }
    /* The verification read clears CNVRF. Allow one more complete conversion
       before arming TIM3, so the first counted slot does not inherit that
       cleared flag at an unfortunate ADC/timer phase. */
    wait_begin = Acq_Now();
    while (Acq_Now() - wait_begin < (uint32_t)profile->vshct_us + 20U)
    {
        if (HAL_GetTick() - wait_ms >= 10U)
        {
            (void)Acq_RestoreConfigs();
            return ACQ_STARTUP_TIMEOUT;
        }
    }
    }
    test_running = test_seconds != 0U ? 1U : 0U;
    test_duration_us = test_seconds * 1000000U;
    test_finished = 0U;
    if (stream_mode)
    {
        (void)AnomalyTrace_SetLimitMa(&anomaly_trace, next_anomaly_limit_ma);
        AnomalyTrace_Reset(&anomaly_trace);
    }
    __HAL_TIM_SET_COUNTER(&htim3, 0U);
    AcqCore_Start(&core, Timebase_NowUs64());
    AcqAlertCore_Reset(&alert_core);
    pair_started = false;
    core.startup_us = core.started_us - startup_begin;
    tick_seq = 0U;
    ++acq_session_id;
    session_was_stream = stream_mode;
    session_mode = acq_mode;
    session_capture_mode = capture_mode;
    session_native_latch = native_latch;
    session_verify_policy = verify_policy;
    AcqCore_SaveConfig(&core, original_adc);
    core.last_adc_config = start_adc;
    __HAL_TIM_CLEAR_FLAG(&htim3, TIM_FLAG_UPDATE);
    HAL_NVIC_ClearPendingIRQ(TIM3_IRQn);
    if (acq_mode == ACQ_MODE_DMA && !AcqDma_Start(stream_mode))
    {
        AcqCore_Stop(&core, Timebase_NowUs64());
        (void)Acq_RestoreConfigs();
        return ACQ_SPI_ERROR;
    }
    if (acq_mode == ACQ_MODE_DMA)
        AcqDma_SetContinuousVerify(verify_policy == ACQ_VERIFY_DIAGNOSTIC &&
                                   capture_mode != ACQ_CAPTURE_SINGLE_SHOT);
    core.started_us64 = Timebase_NowUs64();
    core.started_us = (uint32_t)core.started_us64;
    AlertDriven_Start(&driven, core.started_us64, core.period_us);
    session_conversion_us = (capture_mode == ACQ_CAPTURE_NATIVE && alert_conversion_us != 0U) ?
        alert_conversion_us : profile->vshct_us;
    NativeCore_Start(&native_core, session_conversion_us);
    PairDiag_NativeStart(capture_mode == ACQ_CAPTURE_NATIVE && verify_policy == ACQ_VERIFY_DIAGNOSTIC,
        session_conversion_us, start_adc, capture_mode == ACQ_CAPTURE_NATIVE ? diag : 0U);
    core.startup_us = core.started_us - startup_begin;
    if ((capture_mode == ACQ_CAPTURE_ALERT_DRIVEN || capture_mode == ACQ_CAPTURE_NATIVE) &&
        !AcqDma_ArmAlert())
    {
        (void)Acq_Stop();
        return ACQ_SPI_ERROR;
    }
    if (HAL_TIM_Base_Start_IT(&htim3) != HAL_OK)
    {
        if (acq_mode == ACQ_MODE_DMA) (void)AcqDma_Stop();
        PairDiag_NativeStop();
        AcqCore_Stop(&core, Timebase_NowUs64());
        return Acq_RestoreConfigs() ?
            ACQ_TIMER_ERROR : ACQ_SPI_ERROR;
    }
    return ACQ_OK;
}

Acq_Status Acq_Stop(void)
{
    bool dma_clean = true;
    if (!core.running && !core.config_saved && !diag_saved) return ACQ_NOT_RUNNING;
    if (!core.running && core.config_saved && session_mode == ACQ_MODE_DMA &&
        !AcqDma_IsStopped()) dma_clean = AcqDma_Stop();
    if (core.running)
    {
        (void)HAL_TIM_Base_Stop_IT(&htim3);
        AcqAlertCore_Stop(&alert_core);
        if (acq_mode == ACQ_MODE_DMA) dma_clean = AcqDma_Stop();
        PairDiag_NativeStop();
        AcqCore_Stop(&core, Timebase_NowUs64());
        test_running = 0U;
        if (Acq_IsNativeSession()) {
            NativeCore_StopAt(&native_core, core.stopped_us);
            core.missed = native_core.lost + native_core.queue_drops;
        }
        if (session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN)
        {
            AlertDriven_Finish(&driven, core.stopped_us64);
            core.missed = driven.missed_slots;
        }
        if (!stream_mode)
        {
            Acq_Sample sample;
            while (AcqCore_Pop(&core, &sample)) { }
        }
    }
    return Acq_RestoreConfigs() && dma_clean ? ACQ_OK : ACQ_SPI_ERROR;
}

void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim)
{
    if (htim->Instance == TIM3 && core.running)
    {
        uint32_t irq_us = Acq_Now();
        uint32_t tick_us = irq_us - __HAL_TIM_GET_COUNTER(&htim3);
        ++tick_seq;
        tick_cycles = DWT->CYCCNT;
        AcqCore_Tick(&core, irq_us);
        if (Acq_IsNativeSession()) {
            native_core.timer_lost += core.timer_lost;
            core.timer_lost = 0U;
        }
        if (session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN)
        {
            /* Event slots already account for empty output time bins. Keep
               lost TIM3 IRQ periods separate to avoid counting them twice. */
            driven.timer_lost += core.timer_lost;
            core.timer_lost = 0U;
        }
        if (session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT)
        {
            Alert_State previous = alert_core.state;
            AcqAlertCore_Tick(&alert_core,
                             core.ticks + core.timer_lost - 1U, tick_us);
            if (previous != ALERT_IDLE) AcqCore_Missed(&core);
        }
        else if (session_capture_mode != ACQ_CAPTURE_ALERT_DRIVEN && !Acq_IsNativeSession() &&
                 acq_mode == ACQ_MODE_DMA) AcqDma_Tick(tick_us);
        ++tick_seq;
    }
}

void Acq_Process(void)
{
    uint32_t ticks = 0U;
    uint32_t tick_us = 0U;
    uint32_t cycles = 0U;
    uint32_t lost_ticks = 0U;
    uint32_t seq;
    uint32_t now;
    uint16_t diag;
    uint32_t raw;
    bool slot_due;
    Acq_Sample discarded;
    if (!core.running) return;
    /* The foreground consumer keeps the raw-code ring available for the next sample. */
    if (!stream_mode) (void)AcqCore_Pop(&core, &discarded);
    now = Acq_Now();
    if (test_running != 0U && Timebase_NowUs64() - core.started_us64 >= test_duration_us)
    {
        (void)Acq_Stop();
        test_finished = 1U;
        return;
    }
    if (Acq_IsNativeSession()) {
        AcqDma_CheckPairTimeout();
        core.missed = native_core.lost + native_core.queue_drops;
        if (AcqDma_GetCore()->state == ACQ_DMA_IDLE && NativeCore_Take(&native_core)) {
            PairDiag_Begin(native_core.samples, native_core.current.id, native_core.current.us);
            native_core.ack_start = Acq_Now();
            if (!AcqDma_StartAlert(native_core.current.us,
                    verify_policy == ACQ_VERIFY_DIAGNOSTIC, session_conversion_us - 10U) &&
                native_core.state != NATIVE_IDLE) {
                ++native_core.start_failed;
                NativeCore_Failed(&native_core, Acq_Now());
            }
        }
        return;
    }
    if (session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN)
    {
        AcqDma_CheckPairTimeout();
        if (AcqDma_GetCore()->state == ACQ_DMA_IDLE &&
            AlertDriven_Take(&driven, Timebase_NowUs64()))
        {
            (void)AcqDma_StartAlert(driven.event_time,
                    verify_policy == ACQ_VERIFY_DIAGNOSTIC,
                    (alert_conversion_us != 0U ? alert_conversion_us :
                     MeasurementProfile_Selected()->vshct_us) - 10U);
        }
        return;
    }
    if (session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT)
    {
        if (alert_core.state == ALERT_PENDING)
        {
            uint16_t stale;
            INA229_SetDmaOwner(false);
            if (Acq_TrackedRead16(INA229_REG_DIAG_ALRT, &stale) != INA229_OK ||
                Acq_TrackedWrite(INA229_REG_ADC_CONFIG, fast_adc) != INA229_OK)
            {
                INA229_SetDmaOwner(true);
                AcqCore_SpiError(&core);
                AcqCore_Missed(&core);
                AcqAlertCore_Finish(&alert_core, Acq_Now());
                return;
            }
            INA229_SetDmaOwner(true);
            AcqAlertCore_Trigger(&alert_core, Acq_Now());
            core.last_adc_config = fast_adc;
        }
        if (AcqAlertCore_Timeout(&alert_core, Acq_Now(),
                                 MeasurementProfile_Selected()->vshct_us + 500U))
        {
            AcqCore_Missed(&core);
            return;
        }
        if (alert_core.state == ALERT_READING)
        {
            if (!pair_started)
            {
                INA229_SetDmaOwner(false);
                AcqCore_DiagAttempt(&core);
                if (Acq_TrackedRead16(INA229_REG_DIAG_ALRT, &diag) != INA229_OK)
                {
                    INA229_SetDmaOwner(true);
                    AcqCore_SpiError(&core);
                    AcqCore_Missed(&core);
                    AcqAlertCore_Finish(&alert_core, Acq_Now());
                    return;
                }
                INA229_SetDmaOwner(true);
                AcqCore_DiagResult(&core, diag);
                if ((diag & INA229_DIAG_CNVRF) == 0U)
                {
                    ++alert_core.spurious;
                    AcqCore_Missed(&core);
                    AcqAlertCore_Finish(&alert_core, Acq_Now());
                    return;
                }
                (void)AcqAlertCore_Read(&alert_core, Acq_Now());
                AcqCore_RecordReadLatency(&core,
                    Acq_Now() - alert_core.tick_us, 0U);
                pair_started = true;
                if (!AcqDma_StartPair(alert_core.slot, alert_core.tick_us,
                                      alert_core.alert_us,
                                      verify_policy != ACQ_VERIFY_OFF,
                                      verify_policy == ACQ_VERIFY_REJECT))
                {
                    pair_started = false;
                    if (alert_core.state == ALERT_READING)
                        AcqAlertCore_Finish(&alert_core, Acq_Now());
                    return;
                }
            }
            AcqDma_CheckPairTimeout();
            if (pair_started && !AcqDma_PairActive())
            {
                pair_started = false;
                AcqAlertCore_Finish(&alert_core, Acq_Now());
            }
        }
        return;
    }
    if (acq_mode == ACQ_MODE_DMA) return;
    /* The ISR updates the timestamp and count together; retry a torn snapshot. */
    do
    {
        seq = tick_seq;
        if ((seq & 1U) != 0U) continue;
        ticks = core.ticks;
        tick_us = core.previous_tick_us;
        lost_ticks = core.timer_lost;
        cycles = tick_cycles;
    } while (seq != tick_seq);
    now = Acq_Now();
    slot_due = AcqCore_Schedule(&core, ticks, tick_us, now,
                                core.period_us);
    if (!slot_due) return;
    now = Acq_Now();
    if ((uint32_t)(now - tick_us) >= core.period_us || core.ticks != ticks)
    {
        AcqCore_MissedTimerSlot(&core);
        return;
    }
    AcqCore_RecordReadLatency(&core, now - tick_us, DWT->CYCCNT - cycles);
    AcqCore_DiagAttempt(&core);
    if (Acq_TrackedRead16(INA229_REG_DIAG_ALRT, &diag) != INA229_OK)
    {
        Acq_FailSample();
        return;
    }
    AcqCore_DiagResult(&core, diag);
    if ((diag & INA229_DIAG_CNVRF) == 0U)
    {
        AcqCore_Missed(&core);
        return;
    }
    if (!AcqCore_FreshnessAllowsRead(&core, tick_us)) return;
    AcqCore_VShuntAttempt(&core);
    Acq_AlertReadStarted(Acq_Now());
    if (Acq_TrackedVShunt(&raw) != INA229_OK)
    {
        Acq_FailSample();
        return;
    }
    now = Acq_Now();
    AcqCore_Sample(&core, raw, now, now - tick_us, ticks + lost_ticks - 1U);
    Acq_AlertSampleSaved();
    if (stream_mode)
    {
        Acq_Sample sample = {raw, now, ticks + lost_ticks - 1U};
        AnomalyTrace_Record(&anomaly_trace, &sample);
    }
}

int Acq_FormatAnomalyTrace(char *out, size_t size)
{
    size_t used = 0U;
    uint8_t i;
    int written = snprintf(out, size,
        "ANOMALY_LIMIT_MA: %lu\r\nANOMALY_EVENTS: %lu\r\n"
        "CAPTURE_COUNT: %u\r\nPOST_PENDING: %u\r\n",
        (unsigned long)anomaly_trace.limit_ma,
        (unsigned long)anomaly_trace.events,
        (unsigned int)anomaly_trace.capture_count,
        (unsigned int)anomaly_trace.post_remaining);
    if (written < 0 || (size_t)written >= size) return written;
    used = (size_t)written;
    for (i = 0U; i < anomaly_trace.capture_count; ++i)
    {
        const Acq_Sample *sample = &anomaly_trace.capture[i];
        char first_rx[3] = "??";
        if (sample->spi_command_rx_valid)
            (void)snprintf(first_rx, sizeof(first_rx), "%02X",
                           (unsigned int)sample->spi_command_rx);
        written = snprintf(out + used, size - used,
            "ENTRY_%u: SLOT=%lu TIM2_US=%lu SPI_RX_BYTES=%s,%02lX,%02lX,%02lX "
            "VSHUNT_RAW=0x%06lX VSHUNT_CODE=%ld CODE_BIT_0x2000=%u\r\n",
            (unsigned int)i, (unsigned long)sample->slot,
            (unsigned long)sample->time_us,
            first_rx,
            (unsigned long)((sample->raw >> 16) & 0xFFU),
            (unsigned long)((sample->raw >> 8) & 0xFFU),
            (unsigned long)(sample->raw & 0xFFU),
            (unsigned long)sample->raw,
            (long)AcqCore_Decode(sample->raw),
            (unsigned int)(((uint32_t)AcqCore_Decode(sample->raw) & 0x2000U) != 0U));
        if (written < 0 || (size_t)written >= size - used) return (int)size;
        used += (size_t)written;
    }
    written = snprintf(out + used, size - used, "STATUS: OK\r\n");
    if (written < 0 || (size_t)written >= size - used) return (int)size;
    return (int)(used + (size_t)written);
}

static int64_t Acq_CurrentNa(int32_t code) { return (int64_t)code * 62500LL; }

static uint64_t Acq_Abs(int64_t value) { return (uint64_t)(value < 0 ? -value : value); }

static int Acq_PrintCurrent(char *out, size_t size, int64_t na)
{
    uint64_t magnitude = Acq_Abs(na);
    return snprintf(out, size, "%s%lu.%06lu", na < 0 ? "-" : "",
        (unsigned long)(magnitude / 1000000ULL),
        (unsigned long)(magnitude % 1000000ULL));
}

int Acq_FormatNativeReport(char *out, size_t size)
{
    const AcqDma_Core *d = AcqDma_GetCore();
    uint64_t elapsed = (core.running ? Timebase_NowUs64() : core.stopped_us64) - core.started_us64;
    uint32_t rate = elapsed != 0U ? (uint32_t)((uint64_t)native_core.samples * 1000000ULL / elapsed) : 0U;
    uint32_t lost = native_core.lost + native_core.queue_drops;
    uint32_t runtime_lost = lost - native_core.stop_discarded;
    uint32_t nominal_millihz = session_conversion_us != 0U ?
        1000000000U / session_conversion_us : 0U;
    char elapsed_text[FORMAT64_UDEC_SIZE];
    bool invariant = !core.running && (native_core.samples != core.samples ||
        native_core.samples > native_core.confirmed ||
        (core.overflows == 0U && core.samples <= 1000000U && core.stats_count != core.samples));
    if (!Format64_Unsigned(elapsed_text, sizeof(elapsed_text), elapsed)) return -1;
    return snprintf(out, size,
        "ACQ_CAPTURE_MODE: NATIVE\r\nACQ: %s\r\nACQ_MODE: DMA\r\n"
        "PROFILE: %s\r\nSESSION_ADC_CONFIG: 0x%04X\r\nNATIVE_CONVERSION_US: %lu\r\n"
        "NATIVE_ALATCH: %u\r\nACQ_ELAPSED_US64: %s\r\n"
        "OUTPUT_TIMING: EVENT_DRIVEN_NO_TARGET_SLOTS\r\n"
        "SAMPLES: %lu\r\nSTATS_SAMPLES: %lu\r\nRATE_HZ: %lu\r\n"
        "NATIVE_ALERT_EVENTS: %lu\r\nNATIVE_CONFIRMED_READY: %lu\r\nNATIVE_CNVRF_ZERO: %lu\r\n"
        "NATIVE_PRIMARY_READS: %lu\r\nVSHUNT_READ_ATTEMPTS: %lu\r\n"
        "CNVRF_READS_WITH_BOOTSTRAP: %lu\r\n"
        "NATIVE_SAMPLES: %lu\r\nNATIVE_RESULT_UNCERTAIN: %lu\r\n"
        "NATIVE_EVENTS_LOST: %lu\r\nNATIVE_QUEUE_DROPS: %lu\r\nNATIVE_DMA_OVERRUNS: %lu\r\n"
        "NATIVE_RUNTIME_EVENTS_LOST: %lu\r\nNATIVE_STOP_DISCARDED: %lu\r\n"
        "NATIVE_ALERT_WHILE_BUSY: %lu\r\nNATIVE_DUPLICATE_NOTIFICATIONS: %lu\r\n"
        "NATIVE_GPIO_HIGH_AT_ISR: %lu\r\n"
        "NATIVE_ALERT_TO_READ_MIN_US: %lu\r\nNATIVE_ALERT_TO_READ_MAX_US: %lu\r\n"
        "NATIVE_ALERT_TO_READ_P99_US: %lu\r\nP99_METHOD: 1US_BINS_OVERFLOW_RETURNS_MAX\r\n"
        "AGE_REFERENCE: FIRST_READ_COMPLETION_FROM_IRQ_SERVICE\r\n"
        "NATIVE_EFFECTIVE_RATE_HZ: %lu\r\nNATIVE_MAX_SERVICE_CYCLE_US: %lu\r\n"
        "NATIVE_NOMINAL_RATE_HZ: %lu.%03lu\r\n"
        "NATIVE_CNVRF_ZERO_BEFORE_PREV_REARM: %lu\r\nNATIVE_DMA_FAILURES: %lu\r\n"
        "NATIVE_REARM_FAILURES: %lu\r\nNATIVE_START_FAILED: %lu\r\n"
        "NATIVE_COUNTER_INVARIANT_ERRORS: %u\r\nNATIVE_INVALID_CALLBACKS: %lu\r\n"
        "INVARIANT_SCOPE: STOPPED_SESSION_ONLY\r\n"
        "INTERNAL_CONVERSIONS_LOST: UNKNOWN\r\n"
        "SPI_ERRORS: %lu\r\nDMA_ERRORS: %lu\r\nDMA_TIMEOUTS: %lu\r\nBUFFER_OVERFLOWS: %lu\r\n"
        "MIN_SAMPLE_PERIOD_US: %lu\r\nMAX_SAMPLE_PERIOD_US: %lu\r\n"
        "TIMER_TICKS: %lu\r\nTIM3_LOST_PERIODS: %lu\r\n"
        "NATIVE_TEST_RESULT: OBSERVATION_ONLY\r\nSTATUS: %s\r\n",
        core.running ? "RUNNING" : "STOPPED", MeasurementProfile_Get(core.profile_id)->name,
        core.last_adc_config, (unsigned long)session_conversion_us, (unsigned int)session_native_latch,
        elapsed_text, (unsigned long)core.samples, (unsigned long)core.stats_count, (unsigned long)rate,
        (unsigned long)native_core.irq_count, (unsigned long)native_core.confirmed,
        (unsigned long)native_core.not_ready, (unsigned long)native_core.primary_reads,
        (unsigned long)core.vshunt_reads, (unsigned long)core.cnvrf_reads,
        (unsigned long)native_core.samples,
        (unsigned long)native_core.uncertain, (unsigned long)lost, (unsigned long)native_core.queue_drops,
        (unsigned long)native_core.overbudget_cycles, (unsigned long)runtime_lost,
        (unsigned long)native_core.stop_discarded, (unsigned long)native_core.while_busy,
        (unsigned long)native_core.duplicate_events, (unsigned long)native_core.gpio_high,
        (unsigned long)native_core.min_age,
        (unsigned long)native_core.max_age, (unsigned long)NativeCore_P99(&native_core),
        (unsigned long)rate, (unsigned long)native_core.max_cycle,
        (unsigned long)(nominal_millihz / 1000U), (unsigned long)(nominal_millihz % 1000U),
        (unsigned long)native_core.zero_before_rearm, (unsigned long)native_core.dma_failures,
        (unsigned long)native_core.rearm_failures, (unsigned long)native_core.start_failed,
        (unsigned int)invariant, (unsigned long)native_core.invalid_callbacks,
        (unsigned long)core.spi_errors, (unsigned long)d->errors, (unsigned long)d->timeouts,
        (unsigned long)core.overflows, (unsigned long)core.min_sample_period_us,
        (unsigned long)core.max_sample_period_us, (unsigned long)core.ticks,
        (unsigned long)native_core.timer_lost,
        (runtime_lost || invariant || core.spi_errors || core.overflows || native_core.invalid_callbacks) ?
            "ERR_NATIVE_LOSS_OR_IO" : "OK");
}

int Acq_FormatNativeTrace(char *out, size_t size, bool service)
{
    size_t used = 0U;
    uint32_t i, count = service ? native_core.service_count : native_core.trace_count;
    int n = snprintf(out, size, "NATIVE_TRACE: %s\r\nCOUNT: %lu\r\n",
        service ? "DIAG_SERVICE" : "SAMPLE_TIMING", (unsigned long)count);
    if (n < 0 || (size_t)n >= size) return -1;
    used = (size_t)n;
    for (i = 0U; i < count; ++i) {
        if (service) n = snprintf(out + used, size - used,
            "ACK_%lu: IRQ_ID=%lu ALERT=%lu DIAG_START=%lu DIAG_END=%lu CNVRF=%lu PREV_END=%lu GPIO_LOW=%lu\r\n",
            (unsigned long)i, (unsigned long)native_core.service[i].id,
            (unsigned long)native_core.service[i].alert, (unsigned long)native_core.service[i].start,
            (unsigned long)native_core.service[i].end, (unsigned long)native_core.service[i].ready,
            (unsigned long)native_core.service[i].previous_end,
            (unsigned long)native_core.service[i].pin_low);
        else n = snprintf(out + used, size - used,
            "SAMPLE_%lu: SEQ=%lu IRQ_ID=%lu ALERT=%lu SPI_START=%lu SPI_END=%lu AGE=%lu "
            "EVENTS_START=%lu EVENTS_END=%lu FLAGS=0x%02lX\r\n",
            (unsigned long)i, (unsigned long)native_core.trace[i].sequence,
            (unsigned long)native_core.trace[i].event_id, (unsigned long)native_core.trace[i].alert,
            (unsigned long)native_core.trace[i].start, (unsigned long)native_core.trace[i].end,
            (unsigned long)native_core.trace[i].age, (unsigned long)native_core.trace[i].events_start,
            (unsigned long)native_core.trace[i].events_end, (unsigned long)native_core.trace[i].flags);
        if (n < 0 || (size_t)n >= size - used) return -1;
        used += (size_t)n;
    }
    n = snprintf(out + used, size - used, "STATUS: OK\r\n");
    if (n < 0 || (size_t)n >= size - used) return -1;
    return (int)(used + (size_t)n);
}

static const char *Acq_StateName(void)
{
    if (!core.running) return "STOPPED";
    if (session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT)
    {
        switch (alert_core.state)
        {
            case ALERT_PENDING: return "TRIGGER_PENDING";
            case ALERT_WAITING: return "WAIT_ALERT";
            case ALERT_READING: return "READ_VSHUNT";
            default: break;
        }
    }
    return "WAIT_TICK";
}

static const char *Acq_CaptureName(Acq_CaptureMode mode)
{
    switch (mode)
    {
        case ACQ_CAPTURE_ALERT_MONITOR: return "ALERT_MONITOR";
        case ACQ_CAPTURE_SINGLE_SHOT: return "SINGLE_SHOT";
        case ACQ_CAPTURE_ALERT_DRIVEN: return "ALERT_DRIVEN";
        case ACQ_CAPTURE_NATIVE: return "NATIVE";
        default: return "CONTINUOUS";
    }
}
const char *Acq_GetSessionCaptureName(void)
{
    return Acq_CaptureName(session_capture_mode);
}

static const char *Acq_VerifyName(Acq_VerifyPolicy policy)
{
    switch (policy)
    {
        case ACQ_VERIFY_DIAGNOSTIC: return "DIAGNOSTIC";
        case ACQ_VERIFY_REJECT: return "REJECT";
        default: return "OFF";
    }
}

int Acq_FormatAlertReport(char *out, size_t size)
{
    if (Acq_IsNativeSession()) return Acq_FormatNativeReport(out, size);
    size_t used;
    uint32_t first;
    uint32_t index;
    int written;
    uint64_t elapsed = (core.running ? Timebase_NowUs64() : core.stopped_us64) -
                       core.started_us64;
    uint32_t alert_rate = elapsed != 0U ?
        (uint32_t)(((uint64_t)alert_core.irq_count * 1000000ULL) / elapsed) : 0U;
    uint32_t timer_rate = elapsed != 0U ?
        (uint32_t)(((uint64_t)core.ticks * 1000000ULL) / elapsed) : 0U;
    written = snprintf(out, size,
        "ACQ_CAPTURE_MODE: %s\r\nALERT_IRQ_COUNT: %lu\r\n"
        "ALERT_RATE_HZ: %lu\r\nTIM3_RATE_HZ: %lu\r\n"
        "ALERT_MIN_PERIOD_US: %lu\r\nALERT_MAX_PERIOD_US: %lu\r\n"
        "ALERT_MISSED: %lu\r\nALERT_SPURIOUS: %lu\r\n"
        "ALERT_TO_READ_US: %lu\r\nTRIGGER_TO_ALERT_US: %lu\r\n"
        "MAX_TRIGGER_LATENCY_US: %lu\r\n"
        "TRIGGER_TIMEOUTS: %lu\r\nALERT_BUSY_SLOTS: %lu\r\n"
        "MAX_EXPERIMENT_CYCLE_US: %lu\r\n"
        "ALERT_EVENTS_PER_SAMPLE_LAST: %lu\r\n"
        "ALERT_EVENTS_PER_SAMPLE_MAX: %lu\r\n"
        "TIM3_TICKS: %lu\r\nSAMPLES: %lu\r\n",
        Acq_CaptureName(acq_session_id != 0U ? session_capture_mode : capture_mode),
        (unsigned long)alert_core.irq_count,
        (unsigned long)alert_rate, (unsigned long)timer_rate,
        (unsigned long)alert_core.min_period_us,
        (unsigned long)alert_core.max_period_us,
        (unsigned long)alert_core.timeouts,
        (unsigned long)alert_core.spurious,
        (unsigned long)alert_core.max_alert_to_read_us,
        (unsigned long)alert_core.max_trigger_to_alert_us,
        (unsigned long)alert_core.max_trigger_latency_us,
        (unsigned long)alert_core.timeouts,
        (unsigned long)alert_core.missed_slots,
        (unsigned long)alert_core.max_cycle_us,
        (unsigned long)alert_core.last_events_between_samples,
        (unsigned long)alert_core.max_events_between_samples,
        (unsigned long)core.ticks, (unsigned long)core.samples);
    if (written < 0 || (size_t)written >= size) return -1;
    used = (size_t)written;
    written = snprintf(out + used, size - used, "ALERT_LAST_8_US:");
    if (written < 0 || (size_t)written >= size - used) return -1;
    used += (size_t)written;
    first = alert_core.irq_count > 8U ? alert_core.irq_count - 8U : 0U;
    for (index = first; index < alert_core.irq_count; ++index)
    {
        written = snprintf(out + used, size - used, "%s%lu",
            index == first ? " " : ",",
            (unsigned long)alert_core.irq_times[index & 7U]);
        if (written < 0 || (size_t)written >= size - used) return -1;
        used += (size_t)written;
    }
    written = snprintf(out + used, size - used,
        "%s\r\nCONVERSION_READY_EVENTS: %lu\r\n"
        "INTERNAL_COMPLETED_CONVERSIONS: UNKNOWN\r\n"
        "ALERT_REARM_COUNT: %lu\r\nALERT_REARM_FAILURES: %lu\r\n"
        "ALERT_TO_SPI_START_US: %lu\r\nALERT_TO_READ_COMPLETE_US: %lu\r\n"
        "CONVERSION_EVENTS_SKIPPED: %lu\r\nCONVERSION_EVENT_OVERRUNS: %lu\r\n"
        "MAX_ALERT_READ_CYCLE_US: %lu\r\nOUTPUT_EVENTS_SELECTED: %lu\r\n"
        "ALERT_ADC_OVERRIDE_US: %lu\r\nSESSION_ADC_CONFIG: 0x%04X\r\n"
        "TIM3_LOST_PERIODS: %lu\r\n"
        "ALERT_GPIO_HIGH_AT_ISR: %lu\r\n"
        "EVENT_ALREADY_USED: %lu\r\nEVENT_WRONG_SLOT: %lu\r\n"
        "EVENT_WHILE_DMA_ACTIVE: %lu\r\nEVENT_EXPIRED: %lu\r\nEVENT_SELECTED: %lu\r\n"
        "TARGET_SLOT_EMPTY: %lu\r\nLOST_TARGET_SLOTS: %lu\r\n"
        "TARGET_SLOTS_ACCOUNTED: %lu\r\n"
        "SPI_DMA_START_FAILED: %lu\r\nEVENT_CLOCK_RETRIES: %lu\r\n"
        "CNVRF_ZERO_IRQ_BEFORE_PREV_REARM: %lu\r\nREJECT_TRACE_COUNT: %lu\r\n"
        "CONFIRMED_EVENTS_WITHOUT_OUTPUT: %lu\r\nACCEPTED_OUTPUT_SAMPLES: %lu\r\n"
        "COUNTER_INVARIANT_ERRORS: %u\r\n",
        alert_core.irq_count == 0U ? " NONE" : "",
        (unsigned long)driven.ready, (unsigned long)driven.rearmed,
        (unsigned long)driven.rearm_failures, (unsigned long)driven.max_start_us,
        (unsigned long)driven.max_complete_us, (unsigned long)driven.skipped,
        (unsigned long)driven.overruns, (unsigned long)driven.max_cycle_us,
        (unsigned long)driven.selected, (unsigned long)alert_conversion_us,
        core.last_adc_config,
        (unsigned long)driven.timer_lost,
        (unsigned long)(session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN ?
                         alert_core.spurious : 0U),
        (unsigned long)driven.already_used, (unsigned long)driven.wrong_slot,
        (unsigned long)driven.while_dma, (unsigned long)driven.expired,
        (unsigned long)driven.selected, (unsigned long)driven.missed_slots,
        (unsigned long)driven.missed_slots,
        (unsigned long)(driven.accepted + driven.missed_slots),
        (unsigned long)driven.start_failed,
        (unsigned long)driven.future_snapshots, (unsigned long)driven.zero_before_rearm,
        (unsigned long)driven.trace_count,
        (unsigned long)(driven.ready >= driven.accepted ? driven.ready - driven.accepted : 0U),
        (unsigned long)driven.accepted,
        (unsigned int)(session_capture_mode == ACQ_CAPTURE_ALERT_DRIVEN &&
                       (core.samples != driven.accepted || driven.accepted > driven.selected ||
                        (!core.running && !session_was_stream && core.overflows == 0U &&
                         core.samples <= 1000000U && core.stats_count != core.samples))));
    if (written < 0 || (size_t)written >= size - used) return -1;
    used += (size_t)written;
    for (index = 0U; index < driven.trace_count; ++index)
    {
        written = snprintf(out + used, size - used,
            "REJECT_%lu: TIM2_US=%lu SLOT=%lu STATE=%lu REASON=%lu\r\n",
            (unsigned long)index, (unsigned long)driven.trace[index].timestamp,
            (unsigned long)driven.trace[index].slot, (unsigned long)driven.trace[index].state,
            (unsigned long)driven.trace[index].reason);
        if (written < 0 || (size_t)written >= size - used) return -1;
        used += (size_t)written;
    }
    written = snprintf(out + used, size - used, "STATUS: OK\r\n");
    if (written < 0 || (size_t)written >= size - used) return -1;
    return (int)(used + (size_t)written);
}

int Acq_FormatEventReport(char *out, size_t size)
{
    if (Acq_IsNativeSession()) return Acq_FormatNativeTrace(out, size, true);
    int n = snprintf(out, size, "CONFIRMATION_SLOT_DIFFERENCES: %lu\r\nEVENT_SERVICE_COUNT: %lu\r\n",
        (unsigned long)driven.confirmation_slot_differences, (unsigned long)driven.service_count);
    size_t used;
    uint32_t i;
    if (n < 0 || (size_t)n >= size) return -1;
    used = (size_t)n;
    for (i = 0U; i < driven.service_count; ++i) {
        n = snprintf(out + used, size - used,
            "SERVICE_%lu: IRQ=%lu DIAG=%lu IRQ_SLOT=%lu CONFIRM_SLOT=%lu CNVRF=%lu SELECTED=%lu PRIOR_READ=%lu\r\n",
            (unsigned long)i, (unsigned long)driven.service[i].irq_us,
            (unsigned long)driven.service[i].diag_us, (unsigned long)driven.service[i].irq_slot,
            (unsigned long)driven.service[i].confirm_slot, (unsigned long)driven.service[i].ready,
            (unsigned long)driven.service[i].selected, (unsigned long)driven.service[i].prior_read);
        if (n < 0 || (size_t)n >= size - used) return -1;
        used += (size_t)n;
    }
    n = snprintf(out + used, size - used, "STATUS: OK\r\n");
    if (n < 0 || (size_t)n >= size - used) return -1;
    return (int)(used + (size_t)n);
}

int Acq_FormatVerifyReport(char *out, size_t size)
{
    int prefix = snprintf(out, size,
        "VERIFY_POLICY_SELECTED: %s\r\nVERIFY_POLICY_SESSION: %s\r\nVERIFY_SCOPE: %s\r\n",
        Acq_VerifyName(verify_policy),
        Acq_VerifyName(session_verify_policy),
        (acq_session_id != 0U ? session_capture_mode : capture_mode) ==
            ACQ_CAPTURE_SINGLE_SHOT ? "SAME_SINGLE_SHOT" :
        "CONTINUOUS_UNPROVEN");
    int result;
    if (prefix < 0 || (size_t)prefix >= size) return -1;
    result = AcqDma_FormatVerify(out + prefix, size - (size_t)prefix);
    if (result < 0 || (size_t)result >= size - (size_t)prefix) return -1;
    return prefix + result + snprintf(out + prefix + result,
        size - (size_t)(prefix + result), "STATUS: OK\r\n");
}

int Acq_FormatStatus(char *out, size_t size)
{
    if (Acq_IsNativeSession()) return Acq_FormatNativeReport(out, size);
    char elapsed_text[FORMAT64_UDEC_SIZE];
    static const AcqDma_Core zero_dma = {0};
    const AcqDma_Core *dma_stats = session_mode == ACQ_MODE_DMA ?
        AcqDma_GetCore() : &zero_dma;
    AcqDma_VerifySummary verify = session_mode == ACQ_MODE_DMA ?
        AcqDma_GetVerifySummary() : (AcqDma_VerifySummary){0};
    uint64_t now64 = Timebase_NowUs64();
    uint64_t elapsed64 = (core.running ? now64 : core.stopped_us64) - core.started_us64;
    if (!Format64_Unsigned(elapsed_text, sizeof(elapsed_text), elapsed64)) return -1;
    uint32_t elapsed = (uint32_t)elapsed64;
    uint32_t missed = core.missed + core.timer_lost;
    uint32_t missed_timer = core.missed_timer_slots + core.timer_lost;
    int prefix = MeasurementProfile_Format(MeasurementProfile_Get(core.profile_id), out, size);
    if (prefix < 0 || (size_t)prefix >= size) return prefix;
    return prefix + snprintf(out + prefix, size - (size_t)prefix,
        "ACQ: %s\r\nSESSION_TYPE: %s\r\nSESSION_ID: %lu\r\n"
        "STARTUP_US: %lu\r\nTOTAL_SESSION_US: %lu\r\n"
        "ACQ_MODE: %s\r\nTIME_US: %lu\r\nACQ_ELAPSED_US64: %s\r\nTIMER_TICKS: %lu\r\n"
        "SAMPLES: %lu\r\nRATE_HZ: %lu\r\n"
        "CNVRF_READS: %lu\r\nCNVRF_READY: %lu\r\nCNVRF_NOT_READY: %lu\r\n"
        "VSHUNT_READS: %lu\r\nSPI_ERRORS: %lu\r\n"
        "MISSED_SAMPLE_SLOTS: %lu\r\nMISSED_TIMER_SLOTS: %lu\r\nFRESHNESS_SKIPS: %lu\r\n"
        "INA_INTERMEDIATE_CONVERSIONS: UNKNOWN\r\nBUFFER_OVERFLOWS: %lu\r\n"
        "MIN_SAMPLE_PERIOD_US: %lu\r\nMAX_SAMPLE_PERIOD_US: %lu\r\n"
        "MAX_SPI_TRANSACTION_US: %lu\r\nMAX_READ_LATENCY_US: %lu\r\n"
        "MAX_TRIGGER_LATENCY_US: %lu\r\nMAX_READ_LATENCY_CYCLES: %lu\r\n"
        "MAX_SERVICE_US: %lu\r\nTIMER_PERIOD_VIOLATIONS: %lu\r\n"
        "LAST_DIAG_ALRT: 0x%04X\r\nFAST_ADC_CONFIG: 0x%04X\r\n"
        "ACQ_STATE: %s\r\nSTATS_SAMPLES: %lu\r\n"
        "DMA_STARTS: %lu\r\nDMA_COMPLETIONS: %lu\r\nDMA_BUSY: %lu\r\n"
        "DMA_ERRORS: %lu\r\nDMA_TIMEOUTS: %lu\r\n"
        "SPI_OWNER_CONFLICTS: %lu\r\nDMA_BUFFER_REUSE_ATTEMPTS: %lu\r\n"
        "DMA_CALLBACK_ORDER_ERRORS: %lu\r\nCS_STATE_ERRORS: %lu\r\n"
        "MAX_DMA_TRANSACTION_US: %lu\r\nMAX_TIM3_LATENCY_US: %lu\r\n"
        "MAX_ACQUISITION_CYCLE_US: %lu\r\n"
        "ACQ_CAPTURE_MODE: %s\r\nALERT_IRQ_COUNT: %lu\r\n"
        "ALERT_MIN_PERIOD_US: %lu\r\nALERT_MAX_PERIOD_US: %lu\r\n"
        "ALERT_MISSED: %lu\r\nALERT_SPURIOUS: %lu\r\n"
        "ALERT_TO_READ_US: %lu\r\nTRIGGER_TO_ALERT_US: %lu\r\n"
        "TRIGGER_TIMEOUTS: %lu\r\nALERT_BUSY_SLOTS: %lu\r\n"
        "VERIFY_TOTAL: %lu\r\nVERIFY_MATCH: %lu\r\nVERIFY_MISMATCH: %lu\r\n"
        "VERIFY_RAW_MISMATCH: %lu\r\nVERIFY_REJECTED: %lu\r\n"
        "VERIFY_SECOND_READ_ERRORS: %lu\r\nVERIFY_MAX_CYCLE_US: %lu\r\n"
        "VERIFY_UNCERTAIN_CONVERSION: %lu\r\n"
        "STATUS: OK\r\n",
        core.running ? "RUNNING" : "STOPPED",
        session_was_stream ? "STREAM" : "ACQ", (unsigned long)acq_session_id,
        (unsigned long)core.startup_us, (unsigned long)(core.startup_us + elapsed),
        session_mode == ACQ_MODE_DMA ? "DMA" : "POLL", (unsigned long)elapsed,
        elapsed_text,
        (unsigned long)core.ticks, (unsigned long)core.samples,
        (unsigned long)AcqCore_Rate(&core, now64),
        (unsigned long)core.cnvrf_reads, (unsigned long)core.cnvrf_ready,
        (unsigned long)core.cnvrf_not_ready, (unsigned long)core.vshunt_reads,
        (unsigned long)core.spi_errors, (unsigned long)missed,
        (unsigned long)missed_timer, (unsigned long)core.freshness_skips,
        (unsigned long)core.overflows,
        (unsigned long)core.min_sample_period_us,
        (unsigned long)core.max_sample_period_us,
        (unsigned long)core.max_spi_transaction_us,
        (unsigned long)core.max_read_latency_us,
        (unsigned long)(session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT ?
            alert_core.max_trigger_latency_us : core.max_read_latency_us),
        (unsigned long)core.max_read_latency_cycles,
        (unsigned long)core.max_service_us,
        (unsigned long)core.period_violations,
        (unsigned int)core.last_diag_alrt, (unsigned int)core.last_adc_config,
        Acq_StateName(), (unsigned long)core.stats_count,
        (unsigned long)dma_stats->starts, (unsigned long)dma_stats->completions,
        (unsigned long)dma_stats->busy, (unsigned long)dma_stats->errors,
        (unsigned long)dma_stats->timeouts,
        (unsigned long)INA229_ContentionCount(),
        (unsigned long)dma_stats->buffer_reuse_attempts,
        (unsigned long)dma_stats->callback_order_errors,
        (unsigned long)dma_stats->cs_state_errors,
        (unsigned long)dma_stats->max_transaction_us,
        (unsigned long)dma_stats->max_latency_us,
        (unsigned long)dma_stats->max_cycle_us,
        Acq_CaptureName(session_capture_mode),
        (unsigned long)alert_core.irq_count,
        (unsigned long)alert_core.min_period_us,
        (unsigned long)alert_core.max_period_us,
        (unsigned long)alert_core.timeouts,
        (unsigned long)alert_core.spurious,
        (unsigned long)alert_core.max_alert_to_read_us,
        (unsigned long)alert_core.max_trigger_to_alert_us,
        (unsigned long)alert_core.timeouts,
        (unsigned long)alert_core.missed_slots,
        (unsigned long)verify.total, (unsigned long)verify.match,
        (unsigned long)verify.mismatch, (unsigned long)verify.raw_mismatch,
        (unsigned long)verify.rejected, (unsigned long)verify.second_errors,
        (unsigned long)verify.max_cycle_us, (unsigned long)verify.uncertain);
}

int Acq_FormatTest(char *out, size_t size)
{
    if (Acq_IsNativeSession()) return Acq_FormatNativeReport(out, size);
    char elapsed_text[FORMAT64_UDEC_SIZE];
    static const AcqDma_Core zero_dma = {0};
    const AcqDma_Core *dma_stats = session_mode == ACQ_MODE_DMA ?
        AcqDma_GetCore() : &zero_dma;
    AcqDma_VerifySummary verify = session_mode == ACQ_MODE_DMA ?
        AcqDma_GetVerifySummary() : (AcqDma_VerifySummary){0};
    uint64_t elapsed64 = core.stopped_us64 - core.started_us64;
    if (!Format64_Unsigned(elapsed_text, sizeof(elapsed_text), elapsed64)) return -1;
    uint32_t elapsed = (uint32_t)elapsed64;
    uint32_t expected = test_duration_us / core.period_us;
    uint32_t tolerance = expected / 100U;
    uint32_t missed = core.missed + core.timer_lost;
    uint32_t missed_timer = core.missed_timer_slots + core.timer_lost;
    int64_t mean = core.stats_count ? (core.sum_code * 62500LL) / core.stats_count : 0;
    uint64_t std_na = (AcqCore_StdCodeMilli(&core) * 62500ULL) / 1000ULL;
    char min[32], max[32], avg[32], std[32];
    int prefix = MeasurementProfile_Format(MeasurementProfile_Get(core.profile_id), out, size);
    if (prefix < 0 || (size_t)prefix >= size) return prefix;
    Acq_PrintCurrent(min, sizeof(min), Acq_CurrentNa(core.min_code));
    Acq_PrintCurrent(max, sizeof(max), Acq_CurrentNa(core.max_code));
    Acq_PrintCurrent(avg, sizeof(avg), mean);
    Acq_PrintCurrent(std, sizeof(std), (int64_t)std_na);
    return prefix + snprintf(out + prefix, size - (size_t)prefix,
        "ACQ_TEST: %lu s\r\nSESSION_TYPE: ACQ\r\nSESSION_ID: %lu\r\n"
        "STARTUP_US: %lu\r\nTOTAL_SESSION_US: %lu\r\n"
        "ACQ_MODE: %s\r\nTIME_US: %lu\r\nACQ_ELAPSED_US64: %s\r\nTIMER_TICKS: %lu\r\n"
        "SAMPLES: %lu\r\nRATE_HZ: %lu\r\n"
        "MIN_mA: %s\r\nMAX_mA: %s\r\nMEAN_mA: %s\r\nSTDDEV_mA: %s\r\n"
        "CNVRF_READS: %lu\r\nCNVRF_READY: %lu\r\nCNVRF_NOT_READY: %lu\r\n"
        "VSHUNT_READS: %lu\r\nSPI_ERRORS: %lu\r\n"
        "MISSED_SAMPLE_SLOTS: %lu\r\nMISSED_TIMER_SLOTS: %lu\r\nFRESHNESS_SKIPS: %lu\r\n"
        "INA_INTERMEDIATE_CONVERSIONS: UNKNOWN\r\nBUFFER_OVERFLOWS: %lu\r\n"
        "MIN_SAMPLE_PERIOD_US: %lu\r\nMAX_SAMPLE_PERIOD_US: %lu\r\n"
        "MAX_SPI_TRANSACTION_US: %lu\r\nMAX_READ_LATENCY_US: %lu\r\n"
        "MAX_TRIGGER_LATENCY_US: %lu\r\nMAX_READ_LATENCY_CYCLES: %lu\r\n"
        "MAX_SERVICE_US: %lu\r\nTIMER_PERIOD_VIOLATIONS: %lu\r\n"
        "LAST_DIAG_ALRT: 0x%04X\r\nFAST_ADC_CONFIG: 0x%04X\r\n"
        "ACQ_STATE: %s\r\nSTATS_SAMPLES: %lu\r\n"
        "DMA_STARTS: %lu\r\nDMA_COMPLETIONS: %lu\r\nDMA_BUSY: %lu\r\n"
        "DMA_ERRORS: %lu\r\nDMA_TIMEOUTS: %lu\r\n"
        "SPI_OWNER_CONFLICTS: %lu\r\nDMA_BUFFER_REUSE_ATTEMPTS: %lu\r\n"
        "DMA_CALLBACK_ORDER_ERRORS: %lu\r\nCS_STATE_ERRORS: %lu\r\n"
        "MAX_DMA_TRANSACTION_US: %lu\r\nMAX_TIM3_LATENCY_US: %lu\r\n"
        "MAX_ACQUISITION_CYCLE_US: %lu\r\n"
        "ACQ_CAPTURE_MODE: %s\r\nALERT_IRQ_COUNT: %lu\r\n"
        "ALERT_MIN_PERIOD_US: %lu\r\nALERT_MAX_PERIOD_US: %lu\r\n"
        "ALERT_MISSED: %lu\r\nALERT_SPURIOUS: %lu\r\n"
        "ALERT_TO_READ_US: %lu\r\nTRIGGER_TO_ALERT_US: %lu\r\n"
        "TRIGGER_TIMEOUTS: %lu\r\nALERT_BUSY_SLOTS: %lu\r\n"
        "VERIFY_TOTAL: %lu\r\nVERIFY_MATCH: %lu\r\nVERIFY_MISMATCH: %lu\r\n"
        "VERIFY_RAW_MISMATCH: %lu\r\nVERIFY_REJECTED: %lu\r\n"
        "VERIFY_SECOND_READ_ERRORS: %lu\r\nVERIFY_MAX_CYCLE_US: %lu\r\n"
        "VERIFY_UNCERTAIN_CONVERSION: %lu\r\n"
        "STATUS: %s\r\n",
        (unsigned long)(test_duration_us / 1000000U),
        (unsigned long)acq_session_id,
        (unsigned long)core.startup_us, (unsigned long)(core.startup_us + elapsed),
        session_mode == ACQ_MODE_DMA ? "DMA" : "POLL",
        (unsigned long)elapsed, elapsed_text,
        (unsigned long)core.ticks,
        (unsigned long)core.samples,
        (unsigned long)AcqCore_Rate(&core, core.stopped_us64),
        min, max, avg, std,
        (unsigned long)core.cnvrf_reads, (unsigned long)core.cnvrf_ready,
        (unsigned long)core.cnvrf_not_ready, (unsigned long)core.vshunt_reads,
        (unsigned long)core.spi_errors, (unsigned long)missed,
        (unsigned long)missed_timer, (unsigned long)core.freshness_skips,
        (unsigned long)core.overflows,
        (unsigned long)core.min_sample_period_us,
        (unsigned long)core.max_sample_period_us,
        (unsigned long)core.max_spi_transaction_us,
        (unsigned long)core.max_read_latency_us,
        (unsigned long)(session_capture_mode == ACQ_CAPTURE_SINGLE_SHOT ?
            alert_core.max_trigger_latency_us : core.max_read_latency_us),
        (unsigned long)core.max_read_latency_cycles,
        (unsigned long)core.max_service_us,
        (unsigned long)core.period_violations,
        (unsigned int)core.last_diag_alrt, (unsigned int)core.last_adc_config,
        Acq_StateName(), (unsigned long)core.stats_count,
        (unsigned long)dma_stats->starts, (unsigned long)dma_stats->completions,
        (unsigned long)dma_stats->busy, (unsigned long)dma_stats->errors,
        (unsigned long)dma_stats->timeouts,
        (unsigned long)INA229_ContentionCount(),
        (unsigned long)dma_stats->buffer_reuse_attempts,
        (unsigned long)dma_stats->callback_order_errors,
        (unsigned long)dma_stats->cs_state_errors,
        (unsigned long)dma_stats->max_transaction_us,
        (unsigned long)dma_stats->max_latency_us,
        (unsigned long)dma_stats->max_cycle_us,
        Acq_CaptureName(session_capture_mode),
        (unsigned long)alert_core.irq_count,
        (unsigned long)alert_core.min_period_us,
        (unsigned long)alert_core.max_period_us,
        (unsigned long)alert_core.timeouts,
        (unsigned long)alert_core.spurious,
        (unsigned long)alert_core.max_alert_to_read_us,
        (unsigned long)alert_core.max_trigger_to_alert_us,
        (unsigned long)alert_core.timeouts,
        (unsigned long)alert_core.missed_slots,
        (unsigned long)verify.total, (unsigned long)verify.match,
        (unsigned long)verify.mismatch, (unsigned long)verify.raw_mismatch,
        (unsigned long)verify.rejected, (unsigned long)verify.second_errors,
        (unsigned long)verify.max_cycle_us,
        (unsigned long)verify.uncertain,
        (elapsed64 >= test_duration_us &&
         core.samples >= expected - tolerance &&
         core.samples <= expected + tolerance &&
         missed <= tolerance && core.spi_errors == 0U &&
         core.overflows == 0U && core.period_violations == 0U) ?
         "OK" : "ERR_RATE_OR_DATA_LOSS");
}
static uint16_t Acq_ConversionUs(uint16_t adc, uint8_t shift)
{
    static const uint16_t times[8] = {50U, 84U, 150U, 280U, 540U, 1052U, 2074U, 4120U};
    return times[(adc >> shift) & 7U];
}

static uint16_t Acq_Averages(uint16_t adc)
{
    static const uint16_t counts[8] = {1U, 4U, 16U, 64U, 128U, 256U, 512U, 1024U};
    return counts[adc & 7U];
}

int Acq_FormatConfig(char *out, size_t size)
{
    const MeasurementProfile *profile = MeasurementProfile_Selected();
    int prefix;
    uint16_t config, adc, diag;
    RCC_ClkInitTypeDef clocks;
    uint32_t latency;
    uint32_t timer_clock;
    uint32_t timer_hz;
    if (INA229_ReadReg16(sensor, INA229_REG_CONFIG, &config) != INA229_OK ||
        INA229_ReadReg16(sensor, INA229_REG_ADC_CONFIG, &adc) != INA229_OK ||
        INA229_ReadReg16(sensor, INA229_REG_DIAG_ALRT, &diag) != INA229_OK)
        return snprintf(out, size, "ERR: SPI_ERROR\r\n");
    HAL_RCC_GetClockConfig(&clocks, &latency);
    timer_clock = HAL_RCC_GetPCLK1Freq();
    if (clocks.APB1CLKDivider != RCC_HCLK_DIV1) timer_clock *= 2U;
    timer_hz = timer_clock / (TIM3->PSC + 1U) / (TIM3->ARR + 1U);
    prefix = MeasurementProfile_Format(profile, out, size);
    if (prefix < 0 || (size_t)prefix >= size) return prefix;
    return prefix + snprintf(out + prefix, size - (size_t)prefix,
        "CONFIG: 0x%04X\r\nADC_CONFIG: 0x%04X\r\nDIAG_ALRT: 0x%04X\r\n"
        "FAST_ADC_CONFIG: 0x%04X\r\nTRIGGER_ADC_CONFIG: 0x%04X\r\n"
        "MODE: %u\r\nVSHCT_US: %u\r\nAVG: %u\r\nFAST_VSHCT_US: %u\r\nFAST_AVG: %u\r\n"
        "TIM2_PSC: %lu\r\nTIM2_ARR: %lu\r\nTIM3_PSC: %lu\r\nTIM3_ARR: %lu\r\n"
        "TIMER_HZ: %lu\r\nSPI_HZ: %lu\r\nACQ_MODE_SELECTED: %s\r\n"
        "ACQ_CAPTURE_SELECTED: %s\r\nSTATUS: OK\r\n",
        config, adc, diag, (unsigned int)profile->adc_config,
        (unsigned int)((profile->adc_config & 0x0FFFU) | 0x2000U),
        (unsigned int)(adc >> 12), (unsigned int)Acq_ConversionUs(adc, 6U),
        (unsigned int)Acq_Averages(adc), profile->vshct_us,
        profile->averages, (unsigned long)TIM2->PSC,
        (unsigned long)TIM2->ARR, (unsigned long)TIM3->PSC,
        (unsigned long)TIM3->ARR, (unsigned long)timer_hz,
        (unsigned long)SpiControl_GetHz(),
        acq_mode == ACQ_MODE_DMA ? "DMA" : "POLL",
        Acq_CaptureName(capture_mode));
}

int Acq_FormatProbe(char *out, size_t size, uint32_t wait_us)
{
    uint16_t original = 0U;
    uint16_t readback = 0U;
    uint16_t diag = 0U;
    uint32_t raw = 0U;
    uint32_t started_us = 0U;
    uint32_t elapsed_us = 0U;
    uint32_t started_ms;
    uint32_t read_errors = 0U;
    uint32_t write_errors = 0U;
    uint32_t restore_errors = 0U;
    bool have_original = false;
    bool have_readback = false;
    bool have_diag = false;
    bool have_raw = false;
    bool timer_timeout = false;
    const char *spi_status = "OK";
    const char *status;
    const char *written_text = "N/A";
    char readback_text[12] = "N/A";
    char diag_text[12] = "N/A";
    char raw_text[12] = "N/A";
    char code_text[16] = "N/A";
    char ready_text[8] = "N/A";

    if (core.running) return snprintf(out, size, "ERR: ACQ_ACTIVE\r\n");
    if (wait_us == 0U || wait_us > 1000U)
        return snprintf(out, size, "ERR: BAD_DELAY_US\r\n");

    if (INA229_ReadReg16(sensor, INA229_REG_ADC_CONFIG, &original) != INA229_OK)
    {
        ++read_errors;
        spi_status = "READ_ORIGINAL_ERROR";
        goto report;
    }
    have_original = true;
    if (INA229_WriteReg16(sensor, INA229_REG_ADC_CONFIG, 0x2A28U) != INA229_OK)
    {
        written_text = "FAILED";
        ++write_errors;
        spi_status = "WRITE_FAST_ERROR";
        goto restore;
    }
    written_text = "0x2A28";
    started_us = Acq_Now();
    if (INA229_ReadReg16(sensor, INA229_REG_ADC_CONFIG, &readback) == INA229_OK)
        have_readback = true;
    else
    {
        ++read_errors;
        spi_status = "READBACK_ERROR";
    }

    started_ms = HAL_GetTick();
    while ((uint32_t)(Acq_Now() - started_us) < wait_us)
    {
        if ((uint32_t)(HAL_GetTick() - started_ms) >= 10U)
        {
            timer_timeout = true;
            break;
        }
    }
    if (!timer_timeout)
    {
        if (INA229_ReadReg16(sensor, INA229_REG_DIAG_ALRT, &diag) == INA229_OK)
            have_diag = true;
        else
        {
            ++read_errors;
            if (spi_status[0] == 'O') spi_status = "READ_DIAG_ERROR";
        }
        elapsed_us = Acq_Now() - started_us;
        if (INA229_ReadVShunt(sensor, &raw) == INA229_OK)
            have_raw = true;
        else
        {
            ++read_errors;
            if (spi_status[0] == 'O') spi_status = "READ_VSHUNT_ERROR";
        }
    }
    else elapsed_us = Acq_Now() - started_us;

restore:
    if (have_original && INA229_WriteReg16(sensor, INA229_REG_ADC_CONFIG, original) != INA229_OK)
    {
        ++write_errors;
        ++restore_errors;
        if (spi_status[0] == 'O') spi_status = "RESTORE_ERROR";
    }
report:
    if (have_readback) (void)snprintf(readback_text, sizeof(readback_text), "0x%04X", readback);
    if (have_diag)
    {
        (void)snprintf(diag_text, sizeof(diag_text), "0x%04X", diag);
        (void)snprintf(ready_text, sizeof(ready_text), "%u",
                       (diag & INA229_DIAG_CNVRF) ? 1U : 0U);
    }
    if (have_raw)
    {
        (void)snprintf(raw_text, sizeof(raw_text), "0x%06lX", (unsigned long)raw);
        (void)snprintf(code_text, sizeof(code_text), "%ld", (long)AcqCore_Decode(raw));
    }
    if (read_errors != 0U || write_errors != 0U) status = "ERR_SPI";
    else if (timer_timeout) status = "ERR_TIMER_TIMEOUT";
    else if (readback != 0x2A28U) status = "ERR_READBACK_MISMATCH";
    else status = "OK"; /* CNVRF=0 is a valid timed diagnostic observation. */
    return snprintf(out, size,
        "ADC_CONFIG_WRITTEN: %s\r\nADC_CONFIG_READBACK: %s\r\n"
        "DIAG_ALRT_RAW: %s\r\nCNVRF: %s\r\nVSHUNT_RAW: %s\r\n"
        "VSHUNT_CODE: %s\r\nWAIT_REQUESTED_US: %lu\r\nELAPSED_US: %lu\r\nSPI_STATUS: %s\r\n"
        "SPI_READ_ERRORS: %lu\r\nSPI_WRITE_ERRORS: %lu\r\n"
        "SPI_RESTORE_ERRORS: %lu\r\nSTATUS: %s\r\n",
        written_text, readback_text, diag_text, ready_text, raw_text, code_text,
        (unsigned long)wait_us, (unsigned long)elapsed_us,
        spi_status, (unsigned long)read_errors,
        (unsigned long)write_errors, (unsigned long)restore_errors, status);
}

const char *Acq_StatusName(Acq_Status status)
{
    switch (status)
    {
    case ACQ_BUSY: return "BUSY";
    case ACQ_NOT_RUNNING: return "NOT_RUNNING";
    case ACQ_RANGE: return "ADCRANGE_NOT_ZERO";
    case ACQ_SPI_ERROR: return "SPI_ERROR";
    case ACQ_TIMER_ERROR: return "TIMER_ERROR";
    case ACQ_CONFIG_ERROR: return "ADC_CONFIG_READBACK";
    case ACQ_STARTUP_TIMEOUT: return "ADC_STARTUP_TIMEOUT";
    case ACQ_DIAGNOSTIC_MODE_UNSUPPORTED: return "REPEAT_REQUIRES_DMA_NON_FAST";
    default: return "OK";
    }
}
