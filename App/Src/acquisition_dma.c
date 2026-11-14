#include "acquisition_dma.h"
#include "acquisition.h"
#include "app.h"
#include "pair_diagnostic.h"

#include <stdio.h>

#include "format64.h"
#include "tim.h"

#define DMA_RX_FLAGS (DMA_LIFCR_CFEIF0 | DMA_LIFCR_CDMEIF0 | \
                      DMA_LIFCR_CTEIF0 | DMA_LIFCR_CHTIF0 | DMA_LIFCR_CTCIF0)
#define DMA_TX_FLAGS (DMA_LIFCR_CFEIF3 | DMA_LIFCR_CDMEIF3 | \
                      DMA_LIFCR_CTEIF3 | DMA_LIFCR_CHTIF3 | DMA_LIFCR_CTCIF3)
#define DMA_RX_ERRORS (DMA_LISR_FEIF0 | DMA_LISR_DMEIF0 | DMA_LISR_TEIF0)
#define DMA_TX_ERRORS (DMA_LISR_FEIF3 | DMA_LISR_DMEIF3 | DMA_LISR_TEIF3)
#define DMA_TIMEOUT_US 80U
#define REPEAT_CAPTURE_CAPACITY 4U
#define VERIFY_CAPTURE_CAPACITY 16U

typedef struct
{
    uint32_t slot, alert_us, first_us, second_us;
    uint32_t first_raw, second_raw;
    uint32_t first_events, second_events, read_start_us;
    uint32_t second_start_us;
    bool first_valid, second_valid;
    uint16_t diag;
    uint8_t first_rx[4], second_rx[4];
    uint8_t status; /* 1 match, 2 code mismatch, 3 first error, 4 second error,
                       5 stopped, 6 only unused low bits differ */
} VerifyCapture;

typedef struct
{
    uint32_t slot;
    uint32_t tim2_us;
    uint32_t primary_raw;
    uint32_t repeat_raw;
    uint32_t primary_finish_us;
    uint32_t repeat_start_us;
    uint8_t primary_rx[4];
    uint8_t repeat_rx[4];
    uint8_t status; /* 0 pending, 1 OK, 2 SPI/DMA error, 3 timeout, 4 stopped */
} RepeatCapture;

static INA229_HandleTypeDef *device;
static Acq_Core *samples;
static Anomaly_Trace *trace;
static AcqDma_Core dma;
static uint8_t tx[4];
static uint8_t rx[4];
static bool active;
static bool settling;
static bool trace_active;
static bool repeat_enabled;
static bool repeat_active;
static bool repeat_armed;
static uint32_t repeat_threshold_ma = 50U;
static volatile uint32_t suspect_count;
static volatile uint32_t repeat_error_count;
static volatile uint8_t repeat_count;
static RepeatCapture repeats[REPEAT_CAPTURE_CAPACITY];
static bool pair_active;
static bool pair_second;
static bool pair_reject;
static bool pair_continuous;
static bool verify_continuous;
static bool alert_dma;
static bool alert_bootstrap;
static bool alert_second;
static uint32_t alert_guard_us;
static uint32_t verify_uncertain;
static VerifyCapture pair_current;
static VerifyCapture verify_captures[VERIFY_CAPTURE_CAPACITY];
static VerifyCapture verify_last_error;
static uint8_t verify_capture_count;
static uint32_t verify_error_events;
static uint32_t verify_total, verify_match, verify_mismatch;
static uint32_t verify_rejected, verify_second_errors;
static uint32_t verify_raw_mismatch;
static uint32_t verify_max_cycle_us;
static bool primary_committed;
static bool capture_stored;
static uint32_t duplicate_attempts;
static uint32_t successful_primary_reads;
static volatile uint32_t last_callback;
typedef struct {
    uint32_t type, slot, event, generation, start, timeout, spi_sr, rx_cr, tx_cr;
    uint32_t rx_remaining, tx_remaining, flags, callback, finish, completed_generation;
} TimeoutCapture;
static TimeoutCapture timeout_captures[16];
static uint32_t timeout_capture_count;
static uint32_t watchdog_max_critical_us;
static uint32_t timestamp_order_errors;
static uint32_t transfer_cs_us;
static bool diagnostic_mode, diagnostic_transfer;
static volatile bool diagnostic_ready;
static SpiDiag_Result diagnostic_result;
static SpiDiag_Result transfer_evidence;
static uint8_t transfer_length;
static uint32_t transfer_rx_flags, transfer_rx_irq_us, transfer_edge_start, transfer_pin_start;
static bool transfer_started, native_evidence;
static uint32_t Now(void);

/* Snapshot after CS rises on success, before any overrun clear or RX reuse.
   Fault snapshots precede recovery. This avoids holding CS low while copying. */
static void CaptureEvidence(SpiDiag_Status status, uint32_t cs_before)
{
    SpiDiag_Result *r = &transfer_evidence;
    uint8_t i;
    for (i = 0U; i < 4U; ++i) r->rx[i] = transfer_started && i < transfer_length ? rx[i] : 0U;
    r->length = transfer_length; r->status = status; r->started = transfer_started;
    r->start_us = transfer_cs_us; r->end_us = Now(); r->irq_us = transfer_rx_irq_us;
    r->generation = dma.generation; r->hal_state = device->hspi->State;
    r->hal_error = device->hspi->ErrorCode; r->spi_sr = SPI1->SR;
    r->spi_cr1 = SPI1->CR1; r->spi_cr2 = SPI1->CR2; r->dma_state = dma.state;
    r->dma_flags = transfer_rx_flags | DMA2->LISR;
    r->rx_cr = DMA2_Stream0->CR; r->tx_cr = DMA2_Stream3->CR;
    r->rx_left = DMA2_Stream0->NDTR; r->tx_left = DMA2_Stream3->NDTR;
    r->cs_before = cs_before;
    r->cs_high = (device->cs_port->ODR & device->cs_pin) != 0U;
    r->owner_conflicts = INA229_ContentionCount(); r->callback_errors = dma.callback_order_errors;
    r->cs_errors = dma.cs_state_errors; r->edge_start = transfer_edge_start;
    r->edge_end = PairDiag_EdgeCount(); r->pin_start = transfer_pin_start;
    r->pin_end = (INA_ALERT_GPIO_Port->IDR & INA_ALERT_Pin) == 0U;
    r->exti_pending = (EXTI->PR & INA_ALERT_Pin) != 0U;
    PairDiag_SnapshotDuration(Now() - r->end_us);
}
static void EvidenceCsHigh(void)
{
    transfer_evidence.end_us = Now();
    transfer_evidence.cs_high = (device->cs_port->ODR & device->cs_pin) != 0U;
}
static void DiagnosticResult(void)
{
    diagnostic_result = transfer_evidence;
    diagnostic_transfer = false; pair_active = false;
    __DMB(); diagnostic_ready = true;
}

static void CommitPrimary(void)
{
    Acq_Sample sample;
    if (!AcqDmaCore_CommitOnce(&primary_committed)) { ++duplicate_attempts; return; }
    if (samples->samples != 0U &&
        (pair_current.first_us == samples->previous_sample_us ||
         pair_current.first_us - samples->previous_sample_us >= 0x80000000U))
        ++timestamp_order_errors;
    AcqCore_SampleSpi(samples, pair_current.first_raw, pair_current.first_us,
        pair_current.first_us - dma.tick_us, dma.slot, pair_current.first_rx[0]);
    Acq_AlertSampleSaved();
    if (trace_active) {
        sample.raw = pair_current.first_raw; sample.time_us = pair_current.first_us;
        sample.slot = dma.slot; sample.spi_command_rx = pair_current.first_rx[0];
        sample.spi_command_rx_valid = true;
        AnomalyTrace_Record(trace, &sample);
    }
}

static void StoreVerify(void)
{
    if (Acq_IsNativeSession()) {
        if (!capture_stored && (pair_current.status == 2U || pair_current.status == 6U) &&
            verify_capture_count < VERIFY_CAPTURE_CAPACITY) {
            verify_captures[verify_capture_count++] = pair_current;
            capture_stored = true;
        }
        return;
    }
    if (pair_current.status == 2U || pair_current.status == 6U || pair_current.status == 8U)
    {
        if (verify_capture_count < VERIFY_CAPTURE_CAPACITY)
            verify_captures[verify_capture_count++] = pair_current;
    }
    else
    {
        ++verify_error_events;
        verify_last_error = pair_current;
    }
}

static void FinishPair(void)
{
    uint32_t cycle = Now() - dma.tick_us;
    if (cycle > verify_max_cycle_us) verify_max_cycle_us = cycle;
    if (Acq_IsNativeSession()) {
        if (pair_current.status == 0U) pair_current.status = 1U;
        StoreVerify();
    }
    AcqDmaCore_Stop(&dma);
    pair_active = false;
    if (alert_dma)
    {
        Acq_AlertDrivenComplete(pair_current.first_us, Now());
        alert_dma = false;
    }
    else if (!pair_continuous) Acq_AlertPairComplete(Now());
}

static uint32_t Now(void) { return __HAL_TIM_GET_COUNTER(&htim2); }
static void CsLow(void) { device->cs_port->BSRR = (uint32_t)device->cs_pin << 16; }
static void CsHigh(void) { device->cs_port->BSRR = device->cs_pin; }

static void ClearOverrun(void)
{
    volatile uint32_t discarded = SPI1->DR;
    discarded = SPI1->SR;
    (void)discarded;
}

static void DisableRequests(void)
{
    SPI1->CR2 &= ~(SPI_CR2_RXDMAEN | SPI_CR2_TXDMAEN);
    DMA2_Stream0->CR &= ~DMA_SxCR_EN;
    DMA2_Stream3->CR &= ~DMA_SxCR_EN;
}

static void Fail(bool timeout)
{
    bool was_repeat;
    bool primary_accepted = pair_active && pair_continuous;
    bool was_single_pair = pair_active && !pair_continuous;
    bool was_alert_diag = alert_dma && dma.state == ACQ_DMA_DIAG;
    bool was_alert = alert_dma;
    bool was_diagnostic = diagnostic_transfer;
    if (dma.state == ACQ_DMA_IDLE) return;
    if (was_diagnostic) {
        transfer_rx_flags |= DMA2->LISR;
    }
    if (was_diagnostic || native_evidence) CaptureEvidence(timeout ? SPI_DIAG_TIMEOUT : SPI_DIAG_IO_ERROR,
        (device->cs_port->ODR & device->cs_pin) != 0U);
    if (timeout && timeout_capture_count < 16U) {
        TimeoutCapture *e = &timeout_captures[timeout_capture_count++];
        e->type = dma.state; e->slot = dma.slot; e->event = Acq_AlertEventCount();
        e->generation = dma.generation; e->start = dma.transaction_us; e->timeout = Now();
        e->spi_sr = SPI1->SR; e->rx_cr = DMA2_Stream0->CR; e->tx_cr = DMA2_Stream3->CR;
        e->rx_remaining = DMA2_Stream0->NDTR; e->tx_remaining = DMA2_Stream3->NDTR;
        e->flags = DMA2->LISR; e->callback = last_callback;
        e->finish = dma.completed_us; e->completed_generation = dma.completed_generation;
    }
    if (pair_active && !was_diagnostic)
    {
        if (dma.state == ACQ_DMA_VERIFY_SECOND && !pair_continuous && !pair_reject)
        {
            CommitPrimary();
            primary_accepted = true;
        }
        pair_current.status = dma.state == ACQ_DMA_VERIFY_SECOND ? 4U : 3U;
        if (dma.state == ACQ_DMA_VERIFY_SECOND)
        {
            ++verify_second_errors;
            if (pair_reject) ++verify_rejected;
        }
        StoreVerify();
        pair_active = false;
    }
    was_repeat = dma.state == ACQ_DMA_REPEAT;
    if (was_repeat && repeat_count != 0U)
    {
        repeats[repeat_count - 1U].status = timeout ? 3U : 2U;
        ++repeat_error_count;
    }
    HAL_NVIC_DisableIRQ(DMA2_Stream0_IRQn);
    HAL_NVIC_DisableIRQ(DMA2_Stream3_IRQn);
    TIM2->DIER &= ~TIM_DIER_CC1IE;
    settling = false;
    DisableRequests();
    DMA2->LIFCR = DMA_RX_FLAGS | DMA_TX_FLAGS;
    HAL_NVIC_ClearPendingIRQ(DMA2_Stream0_IRQn);
    HAL_NVIC_ClearPendingIRQ(DMA2_Stream3_IRQn);
    CsHigh();
    if (was_diagnostic || native_evidence) EvidenceCsHigh();
    ClearOverrun();
    if (timeout) AcqDmaCore_Timeout(&dma);
    else AcqDmaCore_Error(&dma);
    if (!was_diagnostic) AcqCore_SpiError(samples);
    if (was_alert) Acq_AlertDrivenFailed(false);
    if (was_alert_diag) Acq_AlertDrivenRearmFailure();
    else if (!was_diagnostic && !was_alert && !was_repeat && !primary_accepted) AcqCore_Missed(samples);
    alert_dma = false;
    alert_bootstrap = false;
    if (native_evidence && !was_diagnostic)
        PairDiag_NativeFailure(transfer_evidence.dma_state == ACQ_DMA_DIAG ? 0U :
            transfer_evidence.dma_state == ACQ_DMA_VERIFY_FIRST ? 1U : 2U, &transfer_evidence);
    if (was_diagnostic) DiagnosticResult();
    else if (was_single_pair) Acq_AlertPairComplete(Now());
    if (active)
    {
        HAL_NVIC_EnableIRQ(DMA2_Stream0_IRQn);
        HAL_NVIC_EnableIRQ(DMA2_Stream3_IRQn);
    }
}

static bool StartTransfer(uint8_t reg, uint8_t length)
{
    uint32_t now = Now();
    uint8_t i;
    native_evidence = !diagnostic_mode && Acq_IsNativeSession() && PairDiag_Tracing();
    transfer_length = length; transfer_started = false; transfer_rx_irq_us = 0U; transfer_rx_flags = 0U;
    transfer_cs_us = now;
    if ((DMA2_Stream0->CR & DMA_SxCR_EN) != 0U ||
        (DMA2_Stream3->CR & DMA_SxCR_EN) != 0U ||
        (SPI1->SR & (SPI_SR_BSY | SPI_SR_MODF)) != 0U ||
        (SPI1->CR1 & SPI_CR1_SPE) == 0U ||
        device->hspi->State != HAL_SPI_STATE_READY)
    {
        if (((DMA2_Stream0->CR | DMA2_Stream3->CR) & DMA_SxCR_EN) != 0U)
            ++dma.buffer_reuse_attempts;
        ++dma.busy;
        return false;
    }
    if ((device->cs_port->ODR & device->cs_pin) == 0U)
    {
        ++dma.cs_state_errors;
        return false;
    }
    for (i = 0U; i < length; ++i) { tx[i] = 0U; rx[i] = 0U; }
    tx[0] = (uint8_t)((reg << 2) | 1U);
    ClearOverrun();
    DMA2->LIFCR = DMA_RX_FLAGS | DMA_TX_FLAGS;
    DMA2_Stream0->M0AR = (uint32_t)rx;
    DMA2_Stream0->NDTR = length;
    DMA2_Stream3->M0AR = (uint32_t)tx;
    DMA2_Stream3->NDTR = length;
    AcqDmaCore_TransactionStarted(&dma, now);
    transfer_started = true;
    if (diagnostic_transfer || native_evidence) {
        transfer_edge_start = PairDiag_EdgeCount();
        transfer_pin_start = (INA_ALERT_GPIO_Port->IDR & INA_ALERT_Pin) == 0U;
    }
    transfer_cs_us = (diagnostic_mode || Acq_IsNativeSession()) ? Now() : now;
    /* Publish timing before enabling DMA: the 3-byte ACK may complete before
       the foreground caller resumes, and the callback can start VSHUNT. */
    if (!diagnostic_mode && Acq_IsNativeSession() && reg == INA229_REG_DIAG_ALRT)
        Acq_NativeAckStart(transfer_cs_us);
    CsLow();
    DMA2_Stream0->CR |= DMA_SxCR_EN;
    DMA2_Stream3->CR |= DMA_SxCR_EN;
    SPI1->CR2 |= SPI_CR2_RXDMAEN | SPI_CR2_TXDMAEN;
    return true;
}

void AcqDma_Init(INA229_HandleTypeDef *sensor, Acq_Core *sample_core,
                 Anomaly_Trace *anomaly)
{
    device = sensor;
    samples = sample_core;
    trace = anomaly;
    __HAL_RCC_DMA2_CLK_ENABLE();
    DMA2_Stream0->CR = (DMA_SxCR_CHSEL_0 | DMA_SxCR_CHSEL_1 | DMA_SxCR_PL_1 |
                        DMA_SxCR_MINC | DMA_SxCR_TCIE | DMA_SxCR_TEIE |
                        DMA_SxCR_DMEIE);
    DMA2_Stream0->PAR = (uint32_t)&SPI1->DR;
    DMA2_Stream0->FCR = 0U;
    DMA2_Stream3->CR = (DMA_SxCR_CHSEL_0 | DMA_SxCR_CHSEL_1 | DMA_SxCR_PL_1 |
                        DMA_SxCR_MINC | DMA_SxCR_DIR_0 | DMA_SxCR_TEIE |
                        DMA_SxCR_DMEIE);
    DMA2_Stream3->PAR = (uint32_t)&SPI1->DR;
    DMA2_Stream3->FCR = 0U;
    HAL_NVIC_SetPriority(DMA2_Stream0_IRQn, 4U, 0U);
    HAL_NVIC_SetPriority(DMA2_Stream3_IRQn, 4U, 0U);
    HAL_NVIC_SetPriority(TIM2_IRQn, 4U, 0U);
    AcqDmaCore_Init(&dma);
}

bool AcqDma_Start(bool trace_enabled)
{
    diagnostic_mode = false; diagnostic_transfer = false; diagnostic_ready = false;
    AcqDmaCore_Init(&dma);
    if (device == NULL || device->hspi == NULL ||
        device->hspi->State != HAL_SPI_STATE_READY ||
        (DMA2_Stream0->CR & DMA_SxCR_EN) != 0U ||
        (DMA2_Stream3->CR & DMA_SxCR_EN) != 0U)
    {
        ++dma.errors;
        return false;
    }
    trace_active = trace_enabled;
    repeat_active = repeat_enabled && samples->profile_id != PROFILE_FAST;
    repeat_armed = true;
    suspect_count = 0U;
    repeat_error_count = 0U;
    repeat_count = 0U;
    pair_active = false;
    alert_dma = false;
    verify_uncertain = 0U;
    alert_bootstrap = false;
    pair_continuous = false;
    verify_continuous = false;
    verify_total = verify_match = verify_mismatch = verify_rejected = 0U;
    verify_second_errors = verify_max_cycle_us = 0U;
    verify_raw_mismatch = 0U;
    verify_capture_count = 0U;
    verify_error_events = 0U;
    verify_last_error = (VerifyCapture){0};
    primary_committed = false; duplicate_attempts = 0U;
    successful_primary_reads = 0U; timeout_capture_count = 0U; last_callback = 0U;
    watchdog_max_critical_us = 0U;
    timestamp_order_errors = 0U;
    settling = false;
    DMA2->LIFCR = DMA_RX_FLAGS | DMA_TX_FLAGS;
    HAL_NVIC_ClearPendingIRQ(DMA2_Stream0_IRQn);
    HAL_NVIC_ClearPendingIRQ(DMA2_Stream3_IRQn);
    HAL_NVIC_EnableIRQ(DMA2_Stream0_IRQn);
    HAL_NVIC_EnableIRQ(DMA2_Stream3_IRQn);
    INA229_SetDmaOwner(true);
    active = true;
    return true;
}

bool AcqDma_Stop(void)
{
    uint32_t started = Now();
    uint32_t irq_state = __get_PRIMASK();
    bool clean;
    __disable_irq();
    active = false;
    HAL_NVIC_DisableIRQ(DMA2_Stream0_IRQn);
    HAL_NVIC_DisableIRQ(DMA2_Stream3_IRQn);
    TIM2->DIER &= ~TIM_DIER_CC1IE;
    __set_PRIMASK(irq_state);
    if (dma.state == ACQ_DMA_REPEAT && repeat_count != 0U)
        repeats[repeat_count - 1U].status = 4U;
    if (pair_active && !diagnostic_mode) { pair_current.status = 5U; StoreVerify(); }
    pair_active = false; diagnostic_transfer = false; diagnostic_ready = false;
    settling = false;
    DisableRequests();
    while (((DMA2_Stream0->CR | DMA2_Stream3->CR) & DMA_SxCR_EN) != 0U ||
           (SPI1->SR & SPI_SR_BSY) != 0U)
    {
        if (Now() - started >= 100U) break;
    }
    clean = ((DMA2_Stream0->CR | DMA2_Stream3->CR) & DMA_SxCR_EN) == 0U &&
            (SPI1->SR & SPI_SR_BSY) == 0U;
    CsHigh();
    ClearOverrun();
    AcqDmaCore_Stop(&dma);
    repeat_active = false;
    if (!clean)
    {
        ++dma.timeouts;
        App_UsbDmaAbortError();
    }
    if ((SPI1->CR1 & SPI_CR1_SPE) == 0U && HAL_SPI_Init(device->hspi) != HAL_OK)
        clean = false;
    if (clean) INA229_SetDmaOwner(false);
    return clean;
}

void AcqDma_Tick(uint32_t tick_us)
{
    uint32_t now = Now();
    uint32_t slot;
    if (!active) return;
    if (dma.state != ACQ_DMA_IDLE)
    {
        ++dma.busy;
        AcqCore_Missed(samples);
        if (now - dma.tick_us >= DMA_TIMEOUT_US &&
            dma.state != ACQ_DMA_IDLE) Fail(true);
        return;
    }
    slot = samples->ticks + samples->timer_lost - 1U;
    if (!AcqDmaCore_Tick(&dma, slot, tick_us, now)) return;
    AcqCore_RecordReadLatency(samples, now - tick_us, 0U);
    AcqCore_DiagAttempt(samples);
    if (!StartTransfer(INA229_REG_DIAG_ALRT, 3U)) Fail(false);
}

static void FinishTransaction(void)
{
    uint32_t now = Now();
    uint16_t diag;
    uint32_t raw;
    Acq_Sample sample;
    if ((DMA2->LISR & DMA_TX_ERRORS) != 0U)
    {
        ++dma.callback_order_errors;
        Fail(false);
        return;
    }
    if ((SPI1->SR & (SPI_SR_OVR | SPI_SR_MODF)) != 0U)
    {
        Fail(false);
        return;
    }
    settling = false;
    if ((device->cs_port->ODR & device->cs_pin) != 0U)
    {
        ++dma.cs_state_errors;
        Fail(false);
        return;
    }
    TIM2->DIER &= ~TIM_DIER_CC1IE;
    CsHigh();
    if (diagnostic_transfer || native_evidence) CaptureEvidence(SPI_DIAG_OK, 0U);
    ClearOverrun();
    AcqDmaCore_TransactionCompleted(&dma, now);
    if (diagnostic_transfer) {
        DiagnosticResult();
        AcqDmaCore_Stop(&dma);
        return;
    }
    AcqCore_RecordSpi(samples, now - dma.transaction_us);
    if (dma.state == ACQ_DMA_VERIFY_FIRST || dma.state == ACQ_DMA_VERIFY_SECOND)
    {
        uint8_t i;
        raw = ((uint32_t)rx[1] << 16) | ((uint32_t)rx[2] << 8) | rx[3];
        if (dma.state == ACQ_DMA_VERIFY_FIRST)
        {
            ++successful_primary_reads;
            pair_current.first_raw = raw;
            pair_current.first_valid = true;
            pair_current.first_us = now;
            pair_current.first_events = Acq_AlertEventCount();
            Acq_NativePrimary(pair_current.read_start_us, now, pair_current.first_events);
            for (i = 0U; i < 4U; ++i) pair_current.first_rx[i] = rx[i];
            if (native_evidence) PairDiag_NativeResult(1U, &transfer_evidence, raw, 0U);
            if (alert_dma && (now - dma.tick_us >= alert_guard_us ||
                (samples->samples != 0U &&
                 ((uint32_t)(dma.tick_us - samples->previous_sample_us) >= 0x80000000U ||
                  dma.tick_us == samples->previous_sample_us))))
            {
                Acq_AlertDrivenStale();
                /* Observe latency without silently rejecting a confirmed RAW. */
            }
            if (pair_second)
            {
                dma.state = ACQ_DMA_VERIFY_SECOND;
                AcqCore_VShuntAttempt(samples);
                if (!StartTransfer(INA229_REG_VSHUNT, 4U))
                { if (alert_dma) Acq_AlertDrivenFailed(true); Fail(false); }
                else pair_current.second_start_us = transfer_cs_us;
                return;
            }
            if (alert_dma && AcqDmaCore_RepeatCandidate(raw, 50U, true))
            { pair_current.status = 8U; StoreVerify(); }
            CommitPrimary();
            FinishPair();
            return;
        }
        pair_current.second_raw = raw;
        pair_current.second_valid = true;
        pair_current.second_us = now;
        pair_current.second_events = Acq_AlertEventCount();
        for (i = 0U; i < 4U; ++i) pair_current.second_rx[i] = rx[i];
        if (native_evidence) PairDiag_NativeResult(2U, &transfer_evidence, pair_current.first_raw, raw);
        ++verify_total;
        if (alert_dma || pair_continuous) ++verify_uncertain;
        AcqDma_VerifyDecision decision = AcqDmaCore_Verify(
            pair_current.first_raw, raw, !pair_continuous && !alert_dma, pair_reject);
        if (decision.code_match)
        {
            ++verify_match;
            pair_current.status = decision.raw_match ? 1U : 6U;
            if (!decision.raw_match)
            {
                ++verify_raw_mismatch;
                StoreVerify();
            }
        }
        else
        {
            ++verify_mismatch;
            pair_current.status = 2U;
            StoreVerify();
        }
        if (alert_dma && decision.raw_match &&
            AcqDmaCore_RepeatCandidate(pair_current.first_raw, 50U, true))
        { pair_current.status = 8U; StoreVerify(); }
        if (!decision.accept)
        {
            ++verify_rejected;
            AcqCore_Missed(samples);
        }
        else if (!pair_continuous)
        {
            CommitPrimary();
        }
        FinishPair();
        return;
    }
    if (dma.state == ACQ_DMA_REPEAT)
    {
        RepeatCapture *entry = &repeats[repeat_count - 1U];
        uint8_t i;
        for (i = 0U; i < 4U; ++i) entry->repeat_rx[i] = rx[i];
        entry->repeat_raw = ((uint32_t)rx[1] << 16) |
                            ((uint32_t)rx[2] << 8) | rx[3];
        entry->status = 1U;
        AcqDmaCore_Stop(&dma);
        return;
    }
    if (dma.state == ACQ_DMA_DIAG)
    {
        diag = ((uint16_t)rx[1] << 8) | rx[2];
        AcqCore_DiagResult(samples, diag);
        if (alert_dma)
        {
            uint32_t slot;
            if (native_evidence && !alert_bootstrap) PairDiag_NativeResult(0U, &transfer_evidence, 0U, 0U);
            if (alert_bootstrap)
            {
                alert_bootstrap = false; alert_dma = false;
                AcqDmaCore_Stop(&dma); return;
            }
            if (!Acq_AlertDrivenReady(diag, &slot))
            {
                AcqDmaCore_Stop(&dma); alert_dma = false; return;
            }
            dma.state = ACQ_DMA_IDLE;
            Acq_AlertDrivenSpiStart(Now());
            if (!AcqDma_StartPair(slot, dma.tick_us, dma.tick_us, alert_second, false))
                alert_dma = false;
            else { pair_current.diag = diag; pair_current.read_start_us = transfer_cs_us; }
            return;
        }
        if (!AcqDmaCore_DiagReady(&dma, (diag & 2U) != 0U))
        {
            AcqCore_Missed(samples);
            return;
        }
        if (!AcqCore_FreshnessAllowsRead(samples, dma.tick_us))
        {
            AcqDmaCore_Stop(&dma);
            return;
        }
        AcqCore_VShuntAttempt(samples);
        Acq_AlertReadStarted(Now());
        if (!StartTransfer(INA229_REG_VSHUNT, 4U)) Fail(false);
        return;
    }
    if (dma.state != ACQ_DMA_VSHUNT) return;
    raw = ((uint32_t)rx[1] << 16) | ((uint32_t)rx[2] << 8) | rx[3];
    sample.raw = raw;
    sample.time_us = now;
    sample.slot = dma.slot;
    sample.spi_command_rx = rx[0];
    sample.spi_command_rx_valid = true;
    AcqCore_SampleSpi(samples, raw, now, now - dma.tick_us, dma.slot, rx[0]);
    Acq_AlertSampleSaved();
    if (trace_active) AnomalyTrace_Record(trace, &sample);
    (void)AcqDmaCore_SampleReady(&dma, now);
    if (verify_continuous)
    {
        uint8_t i;
        pair_active = true;
        pair_continuous = true;
        pair_second = true;
        pair_reject = false;
        pair_current = (VerifyCapture){0};
        pair_current.slot = sample.slot;
        pair_current.alert_us = 0U;
        pair_current.first_raw = raw;
        pair_current.first_valid = true;
        pair_current.first_us = now;
        for (i = 0U; i < 4U; ++i) pair_current.first_rx[i] = rx[i];
        dma.state = ACQ_DMA_VERIFY_SECOND;
        AcqCore_VShuntAttempt(samples);
        if (!StartTransfer(INA229_REG_VSHUNT, 4U)) Fail(false);
        return;
    }
    if (repeat_active)
    {
        bool above = AcqDmaCore_RepeatCandidate(raw, repeat_threshold_ma, true);
        if (above) ++suspect_count;
        if (above && repeat_armed && repeat_count < REPEAT_CAPTURE_CAPACITY)
        {
            RepeatCapture *entry = &repeats[repeat_count++];
            uint8_t i;
            entry->slot = sample.slot;
            entry->tim2_us = sample.time_us;
            entry->primary_raw = raw;
            entry->primary_finish_us = now;
            entry->status = 0U;
            for (i = 0U; i < 4U; ++i) entry->primary_rx[i] = rx[i];
            dma.state = ACQ_DMA_REPEAT;
            repeat_armed = false;
            entry->repeat_start_us = Now();
            if (!StartTransfer(INA229_REG_VSHUNT, 4U)) Fail(false);
        }
        else if (!above) repeat_armed = true;
    }
}

static void CompleteOrSettle(void)
{
    if ((SPI1->SR & (SPI_SR_BSY | SPI_SR_TXE)) != SPI_SR_TXE)
    {
        settling = true;
        TIM2->CCR1 = Now() + 2U;
        TIM2->SR = ~TIM_SR_CC1IF;
        TIM2->DIER |= TIM_DIER_CC1IE;
        return;
    }
    FinishTransaction();
}

void AcqDma_RxIRQ(void)
{
    last_callback = 1U;
    uint32_t flags = DMA2->LISR;
    if (diagnostic_transfer || native_evidence) {
        transfer_rx_flags = flags; transfer_rx_irq_us = Now();
    }
    AcqDma_RxDecision decision;
    DMA2->LIFCR = DMA_RX_FLAGS;
    if (!active || dma.state == ACQ_DMA_IDLE) return;
    decision = AcqDmaCore_DecideRx((flags & DMA_RX_ERRORS) != 0U,
        (flags & DMA_LISR_TCIF0) != 0U,
        (DMA2->LISR & DMA_TX_ERRORS) != 0U,
        DMA2_Stream3->NDTR);
    if (decision == ACQ_DMA_RX_IGNORE) return;
    if (decision != ACQ_DMA_RX_COMPLETE)
    {
        if (decision == ACQ_DMA_RX_TX_ERROR ||
            decision == ACQ_DMA_RX_TX_INCOMPLETE) ++dma.callback_order_errors;
        Fail(false);
        return;
    }
    SPI1->CR2 &= ~(SPI_CR2_RXDMAEN | SPI_CR2_TXDMAEN);
    CompleteOrSettle();
}

void AcqDma_TxIRQ(void)
{
    last_callback = 2U;
    uint32_t flags = DMA2->LISR;
    DMA2->LIFCR = DMA_TX_FLAGS;
    if (active && dma.state != ACQ_DMA_IDLE && (flags & DMA_TX_ERRORS) != 0U)
        Fail(false);
}

void AcqDma_SettleIRQ(void)
{
    last_callback = 3U;
    TIM2->SR = ~TIM_SR_CC1IF;
    if (!active || !settling) { TIM2->DIER &= ~TIM_DIER_CC1IE; return; }
    if (Now() - dma.transaction_us >= DMA_TIMEOUT_US) { Fail(true); return; }
    CompleteOrSettle();
}

const AcqDma_Core *AcqDma_GetCore(void) { return &dma; }

bool AcqDma_IsStopped(void)
{
    return !active && !INA229_DmaOwned() && dma.state == ACQ_DMA_IDLE &&
        ((DMA2_Stream0->CR | DMA2_Stream3->CR) & DMA_SxCR_EN) == 0U &&
        (SPI1->SR & SPI_SR_BSY) == 0U;
}

bool AcqDma_StartPair(uint32_t slot, uint32_t tick_us, uint32_t alert_us,
                      bool second_read, bool reject)
{
    uint32_t now = Now();
    if (!active || pair_active || dma.state != ACQ_DMA_IDLE) return false;
    pair_active = true;
    pair_continuous = false;
    pair_second = second_read;
    pair_reject = reject;
    pair_current = (VerifyCapture){0};
    primary_committed = false;
    capture_stored = false;
    pair_current.slot = slot;
    pair_current.alert_us = alert_us;
    dma.slot = slot;
    dma.tick_us = tick_us;
    dma.state = ACQ_DMA_VERIFY_FIRST;
    if (now - tick_us > dma.max_latency_us) dma.max_latency_us = now - tick_us;
    AcqCore_VShuntAttempt(samples);
    if (!StartTransfer(INA229_REG_VSHUNT, 4U))
    { if (alert_dma) Acq_AlertDrivenFailed(true); Fail(false); return false; }
    return true;
}

bool AcqDma_PairActive(void) { return pair_active; }
bool AcqDma_StartAlert(uint32_t event_us, bool second_read, uint32_t guard_us)
{
    if (!active || dma.state != ACQ_DMA_IDLE) return false;
    alert_dma = true; alert_second = second_read; alert_guard_us = guard_us;
    dma.tick_us = event_us; dma.state = ACQ_DMA_DIAG;
    AcqCore_DiagAttempt(samples);
    if (!StartTransfer(INA229_REG_DIAG_ALRT, 3U))
    { Acq_AlertDrivenFailed(true); Fail(false); return false; }
    return true;
}
bool AcqDma_ArmAlert(void)
{
    alert_bootstrap = true;
    if (!AcqDma_StartAlert(Now(), false, 0U))
    { alert_bootstrap = false; return false; }
    return true;
}
void AcqDma_SetContinuousVerify(bool enabled) { verify_continuous = enabled; }
AcqDma_VerifySummary AcqDma_GetVerifySummary(void)
{
    AcqDma_VerifySummary summary = {verify_total, verify_match, verify_mismatch,
        verify_raw_mismatch, verify_rejected, verify_second_errors,
        verify_max_cycle_us, verify_uncertain};
    return summary;
}

void AcqDma_CheckPairTimeout(void)
{
    uint32_t irq_state = __get_PRIMASK();
    uint32_t begin, now, generation, rx_enabled = 0U, tx_enabled = 0U, tim_enabled = 0U;
    bool claimed = false;
    __disable_irq();
    begin = Now(); generation = dma.generation; now = Now();
    /* State, generation and clock must belong to one snapshot. Pending RX TC
       is serviced by its IRQ, rather than reclassified as a timeout. */
    if (active && (pair_active || alert_dma) &&
        AcqDmaCore_WatchdogDue(&dma, generation, now, DMA_TIMEOUT_US) &&
        (DMA2->LISR & DMA_LISR_TCIF0) == 0U)
    {
        rx_enabled = NVIC_GetEnableIRQ(DMA2_Stream0_IRQn);
        tx_enabled = NVIC_GetEnableIRQ(DMA2_Stream3_IRQn);
        tim_enabled = NVIC_GetEnableIRQ(TIM2_IRQn);
        HAL_NVIC_DisableIRQ(DMA2_Stream0_IRQn);
        HAL_NVIC_DisableIRQ(DMA2_Stream3_IRQn);
        HAL_NVIC_DisableIRQ(TIM2_IRQn);
        claimed = true;
    }
    now = Now() - begin;
    if (now > watchdog_max_critical_us) watchdog_max_critical_us = now;
    __set_PRIMASK(irq_state);
    if (!claimed) return;
    /* Only SPI's owning callbacks are masked during recovery; EXTI/USB remain
       enabled. They cannot mutate this DMA transaction. */
    Fail(true);
    if (!rx_enabled) HAL_NVIC_DisableIRQ(DMA2_Stream0_IRQn);
    if (!tx_enabled) HAL_NVIC_DisableIRQ(DMA2_Stream3_IRQn);
    if (tim_enabled) HAL_NVIC_EnableIRQ(TIM2_IRQn);
}

bool AcqDma_DiagnosticBegin(void)
{
    if (!AcqDma_IsStopped() || !AcqDma_Start(false)) return false;
    diagnostic_mode = true;
    return true;
}
bool AcqDma_DiagnosticRead(uint8_t reg, uint8_t length)
{
    if (!active || !diagnostic_mode || diagnostic_transfer || diagnostic_ready ||
        dma.state != ACQ_DMA_IDLE || reg > 0x3FU || (length != 3U && length != 4U)) return false;
    transfer_cs_us = Now();
    diagnostic_transfer = true;
    /* Arm the existing generation-aware watchdog through its pair-active gate.
       Diagnostic completion/error branches never enter acquisition commit logic. */
    pair_active = true; dma.state = length == 4U ? ACQ_DMA_VERIFY_FIRST : ACQ_DMA_DIAG;
    dma.tick_us = transfer_cs_us; dma.slot = 0U;
    if (!StartTransfer(reg, length)) Fail(false); /* result mailbox carries error */
    return true;
}
bool AcqDma_DiagnosticPoll(SpiDiag_Result *result)
{
    if (!diagnostic_ready || result == NULL) return false;
    __DMB(); *result = diagnostic_result;
    diagnostic_ready = false;
    return true;
}

int AcqDma_FormatTimeouts(char *out, size_t size)
{
    int n = snprintf(out, size,
        "TIMEOUT_CAPTURE_COUNT: %lu\r\nPRIMARY_DUPLICATE_ATTEMPTS: %lu\r\n"
        "SUCCESSFUL_PRIMARY_VSHUNT: %lu\r\nWATCHDOG_MAX_CRITICAL_US: %lu\r\n"
        "TIMESTAMP_ORDER_ERRORS: %lu\r\n",
        (unsigned long)timeout_capture_count, (unsigned long)duplicate_attempts,
        (unsigned long)successful_primary_reads, (unsigned long)watchdog_max_critical_us,
        (unsigned long)timestamp_order_errors);
    size_t used;
    uint32_t i;
    if (n < 0 || (size_t)n >= size) return -1;
    used = (size_t)n;
    for (i = 0U; i < timeout_capture_count; ++i) {
        const TimeoutCapture *e = &timeout_captures[i];
        n = snprintf(out + used, size - used,
            "TIMEOUT_%lu: TYPE=%lu SLOT=%lu EVENT=%lu GEN=%lu START=%lu NOW=%lu "
            "SPI_SR=0x%08lX RX_CR=0x%08lX TX_CR=0x%08lX RX_N=%lu TX_N=%lu "
            "FLAGS=0x%08lX LAST_CB=%lu LAST_FINISH=%lu FINISHED_GEN=%lu\r\n",
            (unsigned long)i, (unsigned long)e->type, (unsigned long)e->slot,
            (unsigned long)e->event, (unsigned long)e->generation, (unsigned long)e->start,
            (unsigned long)e->timeout, (unsigned long)e->spi_sr, (unsigned long)e->rx_cr,
            (unsigned long)e->tx_cr, (unsigned long)e->rx_remaining, (unsigned long)e->tx_remaining,
            (unsigned long)e->flags, (unsigned long)e->callback, (unsigned long)e->finish,
            (unsigned long)e->completed_generation);
        if (n < 0 || (size_t)n >= size - used) return -1;
        used += (size_t)n;
    }
    n = snprintf(out + used, size - used, "STATUS: OK\r\n");
    if (n < 0 || (size_t)n >= size - used) return -1;
    return (int)(used + (size_t)n);
}

int AcqDma_FormatVerify(char *out, size_t size)
{
    size_t used = 0U;
    int n;
    uint8_t i;
    n = snprintf(out, size,
        "VERIFY_TOTAL: %lu\r\nVERIFY_MATCH: %lu\r\nVERIFY_MISMATCH: %lu\r\n"
        "VERIFY_RAW_MISMATCH: %lu\r\n"
        "VERIFY_UNCERTAIN_CONVERSION: %lu\r\n"
        "VERIFY_REJECTED: %lu\r\nVERIFY_SECOND_READ_ERRORS: %lu\r\n"
        "VERIFY_MAX_CYCLE_US: %lu\r\nVERIFY_CAPTURE_COUNT: %u\r\n"
        "VERIFY_ERROR_EVENTS: %lu\r\nVERIFY_LAST_ERROR_SLOT: %lu\r\n"
        "VERIFY_LAST_ERROR_STATUS: %u\r\n",
        (unsigned long)verify_total, (unsigned long)verify_match,
        (unsigned long)verify_mismatch, (unsigned long)verify_raw_mismatch,
        (unsigned long)verify_uncertain,
        (unsigned long)verify_rejected,
        (unsigned long)verify_second_errors, (unsigned long)verify_max_cycle_us,
        (unsigned int)verify_capture_count,
        (unsigned long)verify_error_events,
        (unsigned long)verify_last_error.slot,
        (unsigned int)verify_last_error.status);
    if (n < 0 || (size_t)n >= size) return -1;
    used = (size_t)n;
    for (i = 0U; i < verify_capture_count; ++i)
    {
        const VerifyCapture *e = &verify_captures[i];
        n = snprintf(out + used, size - used,
            "VERIFY_%u: SLOT=%lu ALERT_US=%lu READ1_US=%lu READ2_US=%lu "
            "RAW1=0x%06lX CODE1=%ld RX1=%02X,%02X,%02X,%02X "
            "RAW2=0x%06lX CODE2=%ld RX2=%02X,%02X,%02X,%02X STATUS=%u "
            "DIAG=0x%04X START_US=%lu READ_DELTA_US=%lu EVENTS1=%lu EVENTS2=%lu "
            "FIRST_VALID=%u SECOND_VALID=%u SECOND_START_US=%lu UPDATE_POSSIBLE=%u\r\n",
            (unsigned int)i, (unsigned long)e->slot, (unsigned long)e->alert_us,
            (unsigned long)e->first_us, (unsigned long)e->second_us,
            (unsigned long)e->first_raw, (long)AcqCore_Decode(e->first_raw),
            e->first_rx[0], e->first_rx[1], e->first_rx[2], e->first_rx[3],
            (unsigned long)e->second_raw, (long)AcqCore_Decode(e->second_raw),
            e->second_rx[0], e->second_rx[1], e->second_rx[2], e->second_rx[3],
            (unsigned int)e->status, e->diag, (unsigned long)e->read_start_us,
            (unsigned long)(e->second_us - e->first_us),
            (unsigned long)e->first_events, (unsigned long)e->second_events,
            (unsigned int)e->first_valid, (unsigned int)e->second_valid,
            (unsigned long)e->second_start_us,
            (unsigned int)(Acq_IsNativeSession() || pair_continuous));
        if (n < 0 || (size_t)n >= size - used) return -1;
        used += (size_t)n;
    }
    return (int)used;
}

bool AcqDma_RepeatConfigure(bool enabled, uint32_t threshold_ma)
{
    if (active || !AcqDma_IsStopped() || threshold_ma == 0U ||
        threshold_ma > 32767U) return false;
    repeat_enabled = enabled;
    repeat_threshold_ma = threshold_ma;
    return true;
}

bool AcqDma_RepeatEnabled(void) { return repeat_enabled; }
uint32_t AcqDma_RepeatThresholdMa(void) { return repeat_threshold_ma; }

int AcqDma_FormatRepeat(char *out, size_t size)
{
    char whole_text[FORMAT64_UDEC_SIZE];
    char fractional_text[4];
    size_t used = 0U;
    uint8_t i;
    uint64_t per_million_milli = samples != NULL && samples->samples != 0U ?
        ((uint64_t)suspect_count * 1000000000ULL) / samples->samples : 0U;
    if (!Format64_Unsigned(whole_text, sizeof(whole_text),
                           per_million_milli / 1000ULL)) return -1;
    fractional_text[0] = (char)('0' + (per_million_milli / 100ULL) % 10ULL);
    fractional_text[1] = (char)('0' + (per_million_milli / 10ULL) % 10ULL);
    fractional_text[2] = (char)('0' + per_million_milli % 10ULL);
    fractional_text[3] = '\0';
    int n = snprintf(out, size,
        "REPEAT_ENABLED: %u\r\nREPEAT_ACTIVE: %u\r\nREPEAT_THRESHOLD_MA: %lu\r\n"
        "SUSPECT_SAMPLES: %lu\r\nSUSPECT_PER_MILLION: %s.%s\r\n"
        "REPEAT_CAPTURES: %u\r\nREPEAT_ERRORS: %lu\r\n"
        "REPEAT_LIMIT: %u\r\nNEW_CONVERSION_BETWEEN_READS: POSSIBLE_UNOBSERVED\r\n",
        repeat_enabled ? 1U : 0U, repeat_active ? 1U : 0U,
        (unsigned long)repeat_threshold_ma, (unsigned long)suspect_count,
        whole_text, fractional_text,
        (unsigned int)repeat_count, (unsigned long)repeat_error_count,
        REPEAT_CAPTURE_CAPACITY);
    if (n < 0 || (size_t)n >= size) return n;
    used = (size_t)n;
    for (i = 0U; i < repeat_count; ++i)
    {
        const RepeatCapture *e = &repeats[i];
        n = snprintf(out + used, size - used,
            "REPEAT_%u: SLOT=%lu TIM2_US=%lu ORIGINAL_RX=%02X,%02X,%02X,%02X "
            "ORIGINAL_RAW=0x%06lX ORIGINAL_CODE=%ld "
            "BETWEEN_US=%lu SPI_DMA_STATUS=%s",
            (unsigned int)i, (unsigned long)e->slot, (unsigned long)e->tim2_us,
            (unsigned int)e->primary_rx[0], (unsigned int)e->primary_rx[1],
            (unsigned int)e->primary_rx[2], (unsigned int)e->primary_rx[3],
            (unsigned long)e->primary_raw, (long)AcqCore_Decode(e->primary_raw),
            (unsigned long)(e->repeat_start_us - e->primary_finish_us),
            e->status == 1U ? "OK" : e->status == 2U ? "ERROR" :
            e->status == 3U ? "TIMEOUT" : e->status == 4U ? "STOPPED" : "PENDING");
        if (n < 0) return n;
        if ((size_t)n >= size - used) return (int)(used + (size_t)n);
        used += (size_t)n;
        if (e->status == 1U)
            n = snprintf(out + used, size - used,
                " SECOND_RX=%02X,%02X,%02X,%02X SECOND_RAW=0x%06lX SECOND_CODE=%ld\r\n",
                (unsigned int)e->repeat_rx[0], (unsigned int)e->repeat_rx[1],
                (unsigned int)e->repeat_rx[2], (unsigned int)e->repeat_rx[3],
                (unsigned long)e->repeat_raw, (long)AcqCore_Decode(e->repeat_raw));
        else n = snprintf(out + used, size - used, " SECOND_RX=NA SECOND_RAW=NA SECOND_CODE=NA\r\n");
        if (n < 0) return n;
        if ((size_t)n >= size - used) return (int)(used + (size_t)n);
        used += (size_t)n;
    }
    n = snprintf(out + used, size - used, "STATUS: OK\r\n");
    return n < 0 ? n : (int)used + n;
}
