#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "acquisition.h"
#include "acquisition_dma.h"
#include "stream_core.h"
#include "tim.h"
#include "timebase.h"
#include "spi_control.h"
#include "pair_diagnostic.h"

static uint32_t native_diag_sessions;
void PairDiag_Irq(uint32_t us, bool low) { (void)us; (void)low; }
void PairDiag_Begin(uint32_t seq, uint32_t irq, uint32_t us) { (void)seq; (void)irq; (void)us; }
void PairDiag_NativeStart(bool enabled, uint32_t ct, uint16_t adc, uint16_t diag)
{ (void)ct; (void)adc; (void)diag; if (enabled) ++native_diag_sessions; }
void PairDiag_NativeStop(void) { }

/* Exercise the production acquisition controller; mock only the peripherals. */
TIM_TypeDef fake_tim2, fake_tim3;
TIM_HandleTypeDef htim2 = {&fake_tim2}, htim3 = {&fake_tim3};
CoreDebug_Type fake_debug;
DWT_Type fake_dwt;
static uint32_t now_us, write_us, diag_reads, shunt_reads, clock_hz = 48000000U;
static uint16_t adc = 0xFB68U;
static uint16_t diag_control;
static bool fail_diag_write, fail_diag_read, wrong_diag_readback;
static bool pending, dma_active, fail_write, fail_readback, wrong_readback;
static bool dma_pair_active;
static bool dma_owned;
static uint32_t pair_starts;
static uint32_t alert_starts;
static bool no_ready, fail_restore, timer_fail;
static AcqDma_Core dma;
static Acq_Core *mock_sample_core;
static INA229_HandleTypeDef sensor;

uint32_t Test_TimerCounter(TIM_HandleTypeDef *timer)
{
    return timer->Instance == TIM2 ? now_us++ : timer->Instance->CNT;
}
void Timebase_Init(void) { (void)HAL_TIM_Base_Start(&htim2); }
uint64_t Timebase_NowUs64(void) { return now_us++; }
uint32_t SpiControl_GetHz(void) { return 6000000U; }
uint32_t INA229_ContentionCount(void) { return 0U; }
void INA229_SetDmaOwner(bool owned) { dma_owned = owned; }
bool AcqDma_RepeatEnabled(void) { return false; }
uint32_t HAL_GetTick(void) { return now_us / 1000U; }
void HAL_NVIC_SetPriority(int irq, uint32_t p, uint32_t s) { (void)irq; (void)p; (void)s; }
void HAL_NVIC_DisableIRQ(int irq) { (void)irq; }
void HAL_NVIC_EnableIRQ(int irq) { (void)irq; }
void HAL_NVIC_ClearPendingIRQ(int irq) { (void)irq; pending = false; }
void HAL_RCC_GetClockConfig(RCC_ClkInitTypeDef *clocks, uint32_t *latency)
{ clocks->APB1CLKDivider = 2U; *latency = 0U; }
uint32_t HAL_RCC_GetPCLK1Freq(void) { return clock_hz; }
int HAL_TIM_Base_Start(TIM_HandleTypeDef *timer) { timer->Instance->CR1 |= 1U; return HAL_OK; }
int HAL_TIM_Base_Start_IT(TIM_HandleTypeDef *timer)
{
    assert(!pending && (timer->Instance->SR & TIM_FLAG_UPDATE) == 0U);
    assert(timer->Instance->CNT == 0U);
    if (timer_fail) return 1;
    timer->Instance->DIER |= TIM_IT_UPDATE;
    return HAL_TIM_Base_Start(timer);
}
int HAL_TIM_Base_Stop_IT(TIM_HandleTypeDef *timer)
{ timer->Instance->CR1 = 0U; timer->Instance->DIER = 0U; return HAL_OK; }

INA229_Status INA229_WriteReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t value)
{
    (void)handle;
    if (reg == INA229_REG_DIAG_ALRT)
    {
        if (fail_diag_write) return INA229_ERROR;
        diag_control = (uint16_t)(value & 0xF000U);
        return INA229_OK;
    }
    assert(reg == INA229_REG_ADC_CONFIG);
    assert(!dma_owned); /* foreground SPI must not race a DMA transaction */
    if ((value != 0xFB68U && fail_write) || (value == 0xFB68U && fail_restore))
        return INA229_ERROR;
    adc = value;
    write_us = now_us;
    return INA229_OK;
}
INA229_Status INA229_ReadReg16(INA229_HandleTypeDef *handle, uint8_t reg, uint16_t *value)
{
    (void)handle;
    if (reg == INA229_REG_CONFIG) *value = 0U;
    else if (reg == INA229_REG_ADC_CONFIG)
    {
        if (adc != 0xFB68U && fail_readback) return INA229_ERROR;
        *value = adc != 0xFB68U && wrong_readback ? 0U : adc;
    }
    else
    {
        assert(reg == INA229_REG_DIAG_ALRT);
        if (fail_diag_read) return INA229_ERROR;
        ++diag_reads;
        *value = (wrong_diag_readback && diag_control != 0U ? 0U : diag_control) |
                 (!no_ready && now_us - write_us >=
            MeasurementProfile_Selected()->vshct_us ? 2U : 0U);
    }
    return INA229_OK;
}
INA229_Status INA229_ReadVShunt(INA229_HandleTypeDef *handle, uint32_t *value)
{
    (void)handle;
    assert(now_us - write_us >= MeasurementProfile_Selected()->vshct_us);
    ++shunt_reads;
    *value = 0x123450U;
    return INA229_OK;
}
void AcqDma_Init(INA229_HandleTypeDef *handle, Acq_Core *core, Anomaly_Trace *trace)
{ (void)handle; mock_sample_core = core; (void)trace; AcqDmaCore_Init(&dma); }
bool AcqDma_Start(bool enabled)
{ (void)enabled; dma_active = true; dma_owned = true; return true; }
bool AcqDma_Stop(void)
{ dma_active = false; dma_pair_active = false; dma_owned = false;
  AcqDmaCore_Stop(&dma); return true; }
bool AcqDma_IsStopped(void) { return !dma_active && dma.state == ACQ_DMA_IDLE; }
void AcqDma_Tick(uint32_t tick_us) { (void)AcqDmaCore_Tick(&dma, 0U, tick_us, now_us); }
const AcqDma_Core *AcqDma_GetCore(void) { return &dma; }
void AcqDma_SetContinuousVerify(bool enabled) { (void)enabled; }
AcqDma_VerifySummary AcqDma_GetVerifySummary(void)
{ AcqDma_VerifySummary summary = {0}; return summary; }
bool AcqDma_StartPair(uint32_t slot, uint32_t tick_us, uint32_t alert_us,
                      bool second_read, bool reject)
{ (void)slot; (void)tick_us; (void)alert_us; (void)second_read; (void)reject;
  assert(dma_active); ++pair_starts; dma_pair_active = true; return true; }
bool AcqDma_PairActive(void) { return dma_pair_active; }
bool AcqDma_ArmAlert(void) { return true; }
bool AcqDma_StartAlert(uint32_t time, bool second, uint32_t guard)
{ (void)time; (void)second; (void)guard; ++alert_starts;
  dma.state = ACQ_DMA_DIAG; return true; }
void AcqDma_CheckPairTimeout(void) { }
int AcqDma_FormatVerify(char *out, size_t size)
{ return snprintf(out, size, "VERIFY_TOTAL: 0\r\n"); }

static void tick(uint16_t period)
{
    now_us += period;
    HAL_TIM_PeriodElapsedCallback(&htim3);
    Acq_Process();
}

static void table_and_sessions(void)
{
    static const uint16_t periods[] = {100U, 200U, 400U, 1000U};
    static const uint16_t times[] = {50U, 150U, 280U, 540U};
    static const uint16_t configs[] = {0xAA28U, 0xAAA8U, 0xAAE8U, 0xAB28U};
    static const uint16_t encodings[] = {0U, 2U, 3U, 4U};
    static Stream_Core stream;
    uint8_t frame[STREAM_FRAME_MAX];
    unsigned int i;
    assert(MeasurementProfile_Selected()->id == PROFILE_FAST);
    assert(MeasurementProfile_Find("bAlAnCeD")->id == PROFILE_BALANCED);
    assert(MeasurementProfile_Find("unknown") == NULL);
    assert(MeasurementProfile_Find("fast extra") == NULL);
    assert(MeasurementProfile_Get((MeasurementProfile_Id)-1) == NULL);
    assert(!MeasurementProfile_Select(PROFILE_COUNT));
    assert(Acq_SetMode(ACQ_MODE_POLL));
    for (i = 0U; i < PROFILE_COUNT; ++i)
    {
        const MeasurementProfile *p = MeasurementProfile_Get((MeasurementProfile_Id)i);
        uint16_t psc, arr;
        Acq_Sample sample;
        char report[2048];
        assert(p->period_us == periods[i] && p->vshct_us == times[i]);
        assert(p->rate_hz * p->period_us == 1000000U && p->averages == 1U);
        assert(p->adc_config == configs[i] && ((p->adc_config >> 6) & 7U) == encodings[i]);
        assert((p->adc_config >> 12) == 10U && (p->adc_config & 7U) == 0U);
        assert(MeasurementProfile_TimerDividers(96000000U, p->period_us, &psc, &arr));
        assert(psc == 95U && arr == p->period_us - 1U);
        pending = true; TIM3->SR |= TIM_FLAG_UPDATE; TIM3->CNT = 37U;
        assert(Acq_SelectProfile(p->id) == ACQ_OK);
        assert(TIM3->PSC == psc && TIM3->ARR == arr && TIM3->CNT == 0U && !pending);
        assert(TIM2->PSC == 95U && TIM2->ARR == UINT32_MAX && TIM2->CR1 == 1U);
        diag_reads = 0U; shunt_reads = 0U;
        Acq_SetStreamMode(true);
        assert(Acq_Start(0U) == ACQ_OK && adc == configs[i]);
        assert(diag_reads == 1U && shunt_reads == 0U && Acq_GetCore()->samples == 0U);
        assert(Acq_GetCore()->startup_us >= 2U * times[i] + 100U);
        assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_BUSY);
        no_ready = true; tick(p->period_us); no_ready = false;
        assert(Acq_GetCore()->samples == 0U && Acq_GetCore()->missed == 1U);
        tick(p->period_us);
        assert(Acq_PopSample(&sample) && sample.slot == 1U && sample.raw == 0x123450U);
        StreamCore_Init(&stream);
        assert(StreamCore_Push(&stream, &sample));
        assert(StreamCore_BuildData(&stream, frame, sizeof(frame), now_us, true) != 0U);
        assert((uint16_t)(frame[24] | ((uint16_t)frame[25] << 8)) == p->period_us);
        assert(Acq_FormatStatus(report, sizeof(report)) < (int)sizeof(report));
        assert(strstr(report, p->name) != NULL);
        assert(Acq_Stop() == ACQ_OK && adc == 0xFB68U && !Acq_IsRunning());
        /* A new selection cannot relabel the retained stream header/session. */
        assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_OK);
        assert(stream.profile == p && Acq_GetCore()->profile_id == p->id);
        StreamCore_CommitData(&stream);
        assert(StreamCore_BuildConfig(&stream, frame, sizeof(frame)) != 0U);
        assert((uint16_t)(frame[32] | ((uint16_t)frame[33] << 8)) == configs[i]);
    }
}

static void errors_and_stop(void)
{
    uint16_t psc, arr;
    assert(!MeasurementProfile_TimerDividers(96500000U, 100U, &psc, &arr));
    clock_hz = 48250000U;
    assert(Acq_SelectProfile(PROFILE_BALANCED) == ACQ_TIMER_ERROR);
    assert(MeasurementProfile_Selected()->id == PROFILE_FAST);
    clock_hz = 48000000U;
    fail_write = true;
    assert(Acq_Start(0U) == ACQ_SPI_ERROR); fail_write = false;
    assert(adc == 0xFB68U && !Acq_IsRunning());
    fail_readback = true;
    assert(Acq_Start(0U) == ACQ_SPI_ERROR); fail_readback = false;
    assert(adc == 0xFB68U);
    wrong_readback = true;
    assert(Acq_Start(0U) == ACQ_CONFIG_ERROR); wrong_readback = false;
    assert(adc == 0xFB68U);
    no_ready = true;
    assert(Acq_Start(0U) == ACQ_STARTUP_TIMEOUT); no_ready = false;
    assert(adc == 0xFB68U);
    timer_fail = true;
    assert(Acq_Start(0U) == ACQ_TIMER_ERROR); timer_fail = false;
    assert(adc == 0xFB68U && !Acq_IsRunning());
    wrong_readback = true; fail_restore = true;
    assert(Acq_Start(0U) == ACQ_CONFIG_ERROR);
    assert(Acq_SelectProfile(PROFILE_LOW_NOISE) == ACQ_BUSY);
    assert(Acq_Start(0U) == ACQ_BUSY);
    wrong_readback = false; fail_restore = false;
    assert(Acq_Stop() == ACQ_OK && adc == 0xFB68U);
    assert(Acq_SetMode(ACQ_MODE_DMA));
    assert(Acq_Start(0U) == ACQ_OK);
    tick(100U);
    assert(dma.state == ACQ_DMA_DIAG);
    assert(Acq_Stop() == ACQ_OK && dma.state == ACQ_DMA_IDLE && !dma_active);
    assert(adc == 0xFB68U && Acq_GetCore()->samples == 0U);
    assert(Acq_SelectProfile(PROFILE_LOW_NOISE) == ACQ_OK);
    dma_active = true;
    assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_BUSY);
    dma_active = false;
}

static void per_profile_losses_and_overflow(void)
{
    static Acq_Core core;
    unsigned int profile;
    for (profile = 0U; profile < PROFILE_COUNT; ++profile)
    {
        uint32_t i;
        uint32_t period = MeasurementProfile_Get((MeasurementProfile_Id)profile)->period_us;
        assert(MeasurementProfile_Select((MeasurementProfile_Id)profile));
        AcqCore_Start(&core, 0U);
        AcqCore_Tick(&core, period);
        AcqCore_Tick(&core, 4U * period);
        assert(core.timer_lost == 2U && core.period_violations == 1U);
        assert(AcqCore_Schedule(&core, core.ticks, 4U * period, 4U * period + 2U, period));
        assert(core.missed_timer_slots == 1U && core.missed == 1U);
        for (i = 0U; i < ACQ_RING_CAPACITY; ++i)
            AcqCore_Sample(&core, 0U, i * period, 20U, i);
        assert(core.overflows == 1U && core.samples == ACQ_RING_CAPACITY);
    }
}

static void first_slot_is_a_real_conversion(void)
{
    Acq_Sample sample;
    assert(Acq_SetMode(ACQ_MODE_POLL));
    assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_OK);
    Acq_SetStreamMode(true);
    assert(Acq_Start(0U) == ACQ_OK);
    assert(Acq_GetCore()->startup_us >= 3U * 50U + 120U);
    tick(100U);
    assert(Acq_GetCore()->cnvrf_not_ready == 0U);
    assert(Acq_GetCore()->missed == 0U);
    assert(Acq_PopSample(&sample) && sample.slot == 0U);
    assert(Acq_Stop() == ACQ_OK);
}

static void experimental_mode_guards(void)
{
    assert(Acq_GetCaptureMode() == ACQ_CAPTURE_CONTINUOUS);
    assert(Acq_GetVerifyPolicy() == ACQ_VERIFY_OFF);
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_ALERT_MONITOR));
    assert(!Acq_SetVerifyPolicy(ACQ_VERIFY_REJECT));
    assert(!Acq_SetCaptureMode(ACQ_CAPTURE_SINGLE_SHOT)); /* POLL selected */
    assert(Acq_SetMode(ACQ_MODE_DMA));
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_SINGLE_SHOT));
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_REJECT));
    assert(!Acq_SetCaptureMode(ACQ_CAPTURE_CONTINUOUS));
    assert(!Acq_SetMode(ACQ_MODE_POLL));
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_OFF));
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_CONTINUOUS));
}

static void single_shot_alert_controller(void)
{
    char report[2048];
    uint32_t first_trigger;
    Acq_SetStreamMode(false);
    assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_OK);
    assert(Acq_SetMode(ACQ_MODE_DMA));
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_SINGLE_SHOT));
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_DIAGNOSTIC));
    assert(Acq_Start(0U) == ACQ_OK);
    assert(adc == 0x0A28U && diag_control == 0x4000U);
    pair_starts = 0U;
    tick(100U);
    assert(adc == 0x2A28U);
    first_trigger = write_us;
    tick(100U);
    assert(write_us == first_trigger); /* the next tick does not retrigger */
    assert(Acq_GetCore()->missed >= 1U);
    now_us += 100U;
    Acq_AlertIRQ(now_us, true);
    Acq_AlertIRQ(now_us + 1U, true);
    Acq_Process();
    assert(pair_starts == 1U && dma_pair_active);
    assert(Acq_FormatAlertReport(report, sizeof(report)) < (int)sizeof(report));
    assert(strstr(report, "ALERT_SPURIOUS: 1") != NULL);
    dma_pair_active = false;
    Acq_AlertPairComplete(now_us + 10U);
    tick(100U);
    assert(write_us != first_trigger);
    now_us += 1000U;
    Acq_Process(); /* no ALERT for second trigger */
    assert(Acq_FormatAlertReport(report, sizeof(report)) < (int)sizeof(report));
    assert(strstr(report, "TRIGGER_TIMEOUTS: 1") != NULL);
    assert(Acq_Stop() == ACQ_OK);
    assert(adc == 0xFB68U && diag_control == 0U);
    {
        uint32_t stopped_ticks = Acq_GetCore()->ticks;
        HAL_TIM_PeriodElapsedCallback(&htim3); /* late pending IRQ after STOP */
        assert(Acq_GetCore()->ticks == stopped_ticks);
    }

    assert(Acq_Start(0U) == ACQ_OK);
    tick(100U);
    now_us += 100U;
    Acq_AlertIRQ(now_us, true);
    Acq_Process();
    assert(dma_pair_active); /* simulate STOP during second DMA */
    assert(Acq_Stop() == ACQ_OK);
    assert(adc == 0xFB68U && diag_control == 0U && !dma_pair_active);
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_OFF));
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_CONTINUOUS));
}

static void alert_configuration_errors(void)
{
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_ALERT_MONITOR));
    fail_diag_read = true;
    assert(Acq_Start(0U) == ACQ_SPI_ERROR);
    fail_diag_read = false;
    assert(!Acq_IsRunning() && adc == 0xFB68U);

    wrong_diag_readback = true;
    assert(Acq_Start(0U) == ACQ_CONFIG_ERROR);
    wrong_diag_readback = false;
    assert(diag_control == 0U && adc == 0xFB68U);

    fail_diag_write = true;
    assert(Acq_Start(0U) == ACQ_SPI_ERROR);
    assert(Acq_Start(0U) == ACQ_BUSY); /* restoration remains pending */
    fail_diag_write = false;
    assert(Acq_Stop() == ACQ_OK);
    assert(diag_control == 0U && adc == 0xFB68U);
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_CONTINUOUS));
}

static void alert_driven_controller(void)
{
    uint32_t slot;
    uint32_t starts;
    assert(Acq_SetMode(ACQ_MODE_DMA));
    assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_OK);
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_ALERT_DRIVEN));
    assert(!Acq_SetMode(ACQ_MODE_POLL));
    assert(!Acq_SetVerifyPolicy(ACQ_VERIFY_REJECT));
    assert(!Acq_SetAlertConversion(51U));
    assert(Acq_SetAlertConversion(84U));
    Acq_SetStreamMode(true);
    assert(Acq_Start(0U) == ACQ_DIAGNOSTIC_MODE_UNSUPPORTED);
    Acq_SetStreamMode(false);
    assert(Acq_Start(0U) == ACQ_OK && adc == 0xAA68U);
    assert(!Acq_SetAlertConversion(50U));
    starts = alert_starts;
    tick(100U);
    assert(alert_starts == starts && dma.state == ACQ_DMA_IDLE);
    Acq_AlertIRQ(now_us, false); /* GPIO-high does not erase the captured edge */
    Acq_Process();
    assert(alert_starts == starts + 1U && dma.state == ACQ_DMA_DIAG);
    assert(Acq_AlertDrivenReady(2U, &slot));
    assert(Acq_Stop() == ACQ_OK && adc == 0xFB68U && diag_control == 0U);
    assert(Acq_SetAlertConversion(0U));
    assert(Acq_Start(0U) == ACQ_OK && adc == 0xAA28U);
    assert(Acq_AlertEventCount() == 0U);
    assert(Acq_Stop() == ACQ_OK);
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_CONTINUOUS));
}

static void native_controller(void)
{
    static char report[8192];
    uint32_t seq, starts, first;
    uint32_t previous_diag_sessions = native_diag_sessions;
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_OFF));
    assert(Acq_SetMode(ACQ_MODE_DMA));
    assert(Acq_SelectProfile(PROFILE_BALANCED) == ACQ_OK);
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_NATIVE));
    assert(!Acq_SetMode(ACQ_MODE_POLL));
    assert(!Acq_SetVerifyPolicy(ACQ_VERIFY_REJECT));
    assert(Acq_SetNativeLatch(true));
    assert(Acq_SetAlertConversion(0U));
    Acq_SetStreamMode(true);
    assert(Acq_Start(0U) == ACQ_DIAGNOSTIC_MODE_UNSUPPORTED);
    Acq_SetStreamMode(false);
    assert(Acq_Start(0U) == ACQ_OK && adc == 0xAAA8U && diag_control == 0xC000U);
    assert(!Acq_SetNativeLatch(false));
    starts = alert_starts;
    tick(200U);
    assert(alert_starts == starts && dma.state == ACQ_DMA_IDLE);
    Acq_AlertIRQ(now_us, true);
    Acq_Process();
    assert(alert_starts == starts + 1U);
    assert(Acq_AlertDrivenReady(2U, &seq) && seq == 0U);
    first = now_us + 20U;
    Acq_AlertDrivenSpiStart(now_us + 5U);
    Acq_NativePrimary(now_us + 5U, first, Acq_AlertEventCount());
    AcqCore_SampleSpi(mock_sample_core, 0x123450U, first, 20U, seq, 0U);
    Acq_AlertSampleSaved();
    Acq_AlertDrivenComplete(first, first + 5U);
    AcqDmaCore_Stop(&dma);
    now_us = first + 10U;
    assert(Acq_Stop() == ACQ_OK && adc == 0xFB68U && diag_control == 0U);
    assert(Acq_FormatTest(report, sizeof(report)) < (int)sizeof(report));
    assert(strstr(report, "ACQ_CAPTURE_MODE: NATIVE") != NULL);
    assert(strstr(report, "NATIVE_SAMPLES: 1\r\n") != NULL);
    assert(strstr(report, "NATIVE_COUNTER_INVARIANT_ERRORS: 0") != NULL);
    assert(strstr(report, "NATIVE_CONVERSION_US: 150") != NULL);
    assert(Acq_FormatNativeTrace(report, sizeof(report), false) < (int)sizeof(report));
    assert(strstr(report, "SEQ=0 IRQ_ID=1") != NULL);
    assert(Acq_SetNativeLatch(false));
    assert(Acq_SelectProfile(PROFILE_FAST) == ACQ_OK);
    assert(Acq_SetAlertConversion(84U));
    assert(Acq_Start(0U) == ACQ_OK && adc == 0xAA68U && diag_control == 0x4000U);
    Acq_AlertIRQ(now_us, true);
    Acq_AlertIRQ(now_us + 10U, true);
    assert(Acq_Stop() == ACQ_OK);
    assert(native_diag_sessions == previous_diag_sessions); /* ordinary Native did not enable observer */
    assert(Acq_FormatNativeReport(report, sizeof(report)) < (int)sizeof(report));
    assert(strstr(report, "NATIVE_EVENTS_LOST: 2") != NULL);
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_DIAGNOSTIC));
    assert(Acq_Start(0U) == ACQ_OK);
    assert(native_diag_sessions == previous_diag_sessions + 1U);
    assert(Acq_Stop() == ACQ_OK);
    assert(Acq_SetVerifyPolicy(ACQ_VERIFY_OFF));
    assert(Acq_SetNativeLatch(true));
    Acq_SetStreamMode(true);
    assert(Acq_Start(0U) == ACQ_OK && adc == 0xAA68U && diag_control == 0xC000U);
    assert(Acq_Stop() == ACQ_OK && adc == 0xFB68U);
    Acq_SetStreamMode(false);
    assert(Acq_SetAlertConversion(0U));
    assert(Acq_SetCaptureMode(ACQ_CAPTURE_CONTINUOUS));
}

int main(void)
{
    TIM2->PSC = 95U; TIM2->ARR = UINT32_MAX;
    Acq_Init(&sensor);
    table_and_sessions();
    errors_and_stop();
    first_slot_is_a_real_conversion();
    per_profile_losses_and_overflow();
    experimental_mode_guards();
    single_shot_alert_controller();
    alert_configuration_errors();
    alert_driven_controller();
    native_controller();
    puts("measurement profile/controller tests passed");
    return 0;
}
