#include "pair_diagnostic.h"
#include "pair_experiment_core.h"
#include "acquisition.h"
#include "acquisition_dma.h"
#include "spi_freeze.h"
#include "spi_control.h"
#include "stream_transport.h"
#include "timebase.h"
#include "format64.h"
#include "tim.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>

static Pair_Trace trace;
static Pair_Experiment experiment;
static INA229_HandleTypeDef *sensor;
static bool native_observing, phase_source, marker_enabled;
static uint32_t ct_us = 84U, delay_us, gap_us, not_ready, marker_pulses;
static bool latch;
static uint32_t stop_starts, stop_completions;
static uint32_t max_snapshot_us, max_observer_us;
#ifdef PAIR_DIAG_HOST_TEST
extern void PairDiag_TestTimeoutHardware(Pair_WaitContext *);
#endif
static void TimeoutHardware(Pair_WaitContext *c)
{
#ifdef PAIR_DIAG_HOST_TEST
    PairDiag_TestTimeoutHardware(c);
#else
    c->pin_low = (INA_ALERT_GPIO_Port->IDR & INA_ALERT_Pin) == 0U;
    c->exti_pending = (EXTI->PR & INA_ALERT_Pin) != 0U;
    c->spi_sr = SPI1->SR; c->dma_flags = DMA2->LISR;
    c->dma_state = AcqDma_GetCore()->state;
#endif
}

static int32_t Read16(void *ctx, uint8_t reg, uint16_t *value)
{ return INA229_ReadReg16(ctx, reg, value); }
static int32_t Write16(void *ctx, uint8_t reg, uint16_t value)
{ return INA229_WriteReg16(ctx, reg, value); }
static bool Begin(void *ctx) { (void)ctx; return AcqDma_DiagnosticBegin(); }
static bool ReadDma(void *ctx, uint8_t reg, uint8_t length)
{ (void)ctx; return AcqDma_DiagnosticRead(reg, length); }
static bool Poll(void *ctx, SpiDiag_Result *r)
{ (void)ctx; AcqDma_CheckPairTimeout(); return AcqDma_DiagnosticPoll(r); }
static bool StopDma(void *ctx)
{
    bool clean;
    (void)ctx; clean = AcqDma_Stop();
    stop_starts = AcqDma_GetCore()->starts; stop_completions = AcqDma_GetCore()->completions;
    return clean;
}
static uint64_t Now(void *ctx) { (void)ctx; return Timebase_NowUs64(); }
static void Marker(void)
{
#if defined(PAIR_DIAG_PA8_CONFIRMED) && PAIR_DIAG_PA8_CONFIRMED == 1
    if (marker_enabled) {
        GPIOA->BSRR = GPIO_PIN_8;
        TIM2->DIER &= ~TIM_DIER_CC2IE;
        TIM2->SR = ~TIM_SR_CC2IF;
        TIM2->CCR2 = TIM2->CNT + 5U;
        TIM2->DIER |= TIM_DIER_CC2IE; ++marker_pulses;
    }
#endif
}
void PairDiag_MarkerIRQ(void)
{
#if defined(PAIR_DIAG_PA8_CONFIRMED) && PAIR_DIAG_PA8_CONFIRMED == 1
    if ((TIM2->SR & TIM_SR_CC2IF) && (TIM2->DIER & TIM_DIER_CC2IE)) {
        GPIOA->BSRR = (uint32_t)GPIO_PIN_8 << 16;
        TIM2->DIER &= ~TIM_DIER_CC2IE; TIM2->SR = ~TIM_SR_CC2IF;
    }
#endif
}
static bool MarkerSet(bool enabled)
{
    if (!enabled) {
#if defined(PAIR_DIAG_PA8_CONFIRMED) && PAIR_DIAG_PA8_CONFIRMED == 1
        if (marker_enabled) {
            TIM2->DIER &= ~TIM_DIER_CC2IE; TIM2->SR = ~TIM_SR_CC2IF;
            GPIOA->BSRR = (uint32_t)GPIO_PIN_8 << 16;
            GPIOA->MODER &= ~(3UL << 16); /* leave as input */
        }
#endif
        marker_enabled = false; return true;
    }
#if defined(PAIR_DIAG_PA8_CONFIRMED) && PAIR_DIAG_PA8_CONFIRMED == 1
    if (!marker_enabled) {
        GPIO_InitTypeDef pin = {0};
        uint32_t mode = (GPIOA->MODER >> 16) & 3U;
        if ((mode != 0U && mode != 3U) || (GPIOA->AFR[1] & 15U)) return false;
        if ((TIM2->DIER & (TIM_DIER_CC2IE | TIM_DIER_CC2DE)) ||
            (TIM2->CCER & TIM_CCER_CC2E) || (TIM2->CCMR1 & 0xFF00U)) return false;
        __HAL_RCC_GPIOA_CLK_ENABLE(); GPIOA->BSRR = (uint32_t)GPIO_PIN_8 << 16;
        pin.Pin = GPIO_PIN_8; pin.Mode = GPIO_MODE_OUTPUT_PP;
        pin.Pull = GPIO_NOPULL; pin.Speed = GPIO_SPEED_FREQ_LOW; HAL_GPIO_Init(GPIOA, &pin);
        marker_enabled = true;
    }
    return true;
#else
    return false; /* No schematic/board-level authorization for PA8 in this repo. */
#endif
}
void PairDiag_Init(INA229_HandleTypeDef *handle) { sensor = handle; }
bool PairDiag_Tracing(void) { return trace.active; }
uint32_t PairDiag_EdgeCount(void) { return trace.irq_count; }
void PairDiag_SnapshotDuration(uint32_t us)
{ if (trace.active && us > max_snapshot_us) max_snapshot_us = us; }
void PairDiag_Irq(uint32_t us, bool low) { PairTrace_Irq(&trace, us, low); }
void PairDiag_NativeStart(bool enabled, uint32_t ct, uint16_t adc, uint16_t diag)
{
    native_observing = enabled;
    if (enabled) {
        phase_source = false; not_ready = 0U; PairTrace_Start(&trace, ct, adc, diag);
        stop_starts = stop_completions = 0U;
        max_snapshot_us = max_observer_us = 0U;
    }
}
void PairDiag_NativeStop(void)
{
    if (native_observing) {
        PairTrace_Stop(&trace);
        stop_starts = AcqDma_GetCore()->starts; stop_completions = AcqDma_GetCore()->completions;
    }
    native_observing = false;
}
void PairDiag_Begin(uint32_t seq, uint32_t irq, uint32_t alert)
{ if (native_observing) PairTrace_Begin(&trace, seq, irq, alert); }
void PairDiag_NativeResult(uint32_t phase, const SpiDiag_Result *r, uint32_t raw1, uint32_t raw2)
{
    uint32_t begin;
    if (!native_observing) return;
    begin = TIM2->CNT;
    PairTrace_Transfer(&trace, phase, r);
    if (phase == 0U && !(r->rx[2] & 2U)) { ++not_ready; trace.in_pair = false; }
    if (phase == 2U && PairTrace_Finish(&trace, raw1, raw2)) Marker();
    begin = TIM2->CNT - begin;
    if (begin > max_observer_us) max_observer_us = begin;
}
void PairDiag_NativeFailure(uint32_t phase, const SpiDiag_Result *r)
{
    if (!native_observing) return;
    if (!trace.in_pair) PairTrace_Begin(&trace, UINT32_MAX, 0U, 0U); /* bootstrap, no selected result */
    PairTrace_Fail(&trace, phase, r);
}
bool PairDiag_IsBusy(void) { return experiment.busy; }
void PairDiag_Process(void)
{
    uint32_t mismatches = trace.mismatch;
    if (experiment.busy) PairExperiment_Process(&experiment);
    if (trace.mismatch != mismatches) Marker();
}
void PairDiag_Stop(bool error) { PairExperiment_Stop(&experiment, error); }
static bool Append(char *out, size_t size, size_t *used, const char *format, ...)
{
    va_list args; int n;
    if (*used >= size) return false;
    va_start(args, format); n = vsnprintf(out + *used, size - *used, format, args); va_end(args);
    if (n < 0 || (size_t)n >= size - *used) return false;
    *used += (size_t)n; return true;
}
static const char *State(void)
{
    if (!phase_source) return trace.active ? "NATIVE_RUNNING" : "NATIVE_STOPPED";
    switch (experiment.state) {
        case PAIR_EXP_DONE: return "DONE";
        case PAIR_EXP_STOPPED: return "STOPPED";
        case PAIR_EXP_ERROR: return "ERROR";
        case PAIR_EXP_RESTORE_FAILED: return "RESTORE_FAILED";
        default: return experiment.busy ? "RUNNING" : "IDLE";
    }
}
static int Report(char *out, size_t size)
{
    char elapsed[FORMAT64_UDEC_SIZE];
    bool invariant = trace.total != trace.match + trace.mismatch ||
        trace.mismatch != trace.event_count + trace.events_not_stored;
    bool bad = trace.raw_rx_errors || trace.capture_errors || trace.time_order_errors ||
        trace.spi_errors || (phase_source && experiment.restore_errors);
    uint64_t duration = phase_source && experiment.end_us >= experiment.start_us ?
        experiment.end_us - experiment.start_us : 0U;
    if (trace.active) return snprintf(out, size, "ERR: PAIR_ACTIVE\r\n");
    if (!Format64_Unsigned(elapsed, sizeof(elapsed), duration)) return -1;
    return snprintf(out, size,
        "PAIR_SOURCE: %s\r\nPAIR_STATE: %s\r\nPAIR_VSHCT_US: %lu\r\n"
        "PAIR_ADC_CONFIG: 0x%04lX\r\nPAIR_DIAG_CONTROL: 0x%04lX\r\nPAIR_ALATCH: %lu\r\n"
        "PAIR_TOTAL: %lu\r\nPAIR_MATCH: %lu\r\nPAIR_MISMATCH: %lu\r\nPAIR_RAW_RX_ERRORS: %lu\r\n"
        "PAIR_CAPTURE_ERRORS: %lu\r\nPAIR_TIME_ORDER_ERRORS: %lu\r\n"
        "PAIR_EVENTS: %lu\r\nPAIR_EVENTS_NOT_STORED: %lu\r\nPAIR_FAILURES: %lu\r\nPAIR_FAILURES_NOT_STORED: %lu\r\n"
        "PAIR_SPI_DMA_ERRORS: %lu\r\nPAIR_DMA_TIMEOUTS: %lu\r\nPAIR_STOPPED_PARTIAL: %lu\r\n"
        "PAIR_IRQ_COUNT: %lu\r\nPAIR_NOT_READY: %lu\r\nPAIR_UPDATE_POSSIBLE: 1\r\n"
        "PAIR_FIRST_DELAY_US: %lu\r\nPAIR_GAP_DELAY_US: %lu\r\nPAIR_BUSY_EDGES: %lu\r\nPAIR_SKIPPED_EDGES: %lu\r\n"
        "PAIR_IO_ERRORS: %lu\r\nPAIR_RESTORE_ERRORS: %lu\r\nPAIR_WAIT_TIMEOUTS: %lu\r\n"
        "PAIR_ELAPSED_US64: %s\r\nPAIR_DMA_STARTS: %lu\r\nPAIR_DMA_COMPLETIONS: %lu\r\n"
        "PAIR_MAX_SNAPSHOT_US: %lu\r\nPAIR_MAX_NATIVE_OBSERVER_US: %lu\r\nPAIR_COUNTER_INVARIANT_ERRORS: %u\r\n"
        "PAIR_MARKER_ENABLED: %u\r\nPAIR_MARKER_PULSES_LIFETIME: %lu\r\nSTATUS: %s\r\n",
        phase_source ? "PHASE_EXPERIMENT" : "NATIVE_DIAGNOSTIC", State(), (unsigned long)trace.conversion_us,
        (unsigned long)trace.adc_config, (unsigned long)trace.diag_control, (unsigned long)(trace.diag_control >> 15),
        (unsigned long)trace.total, (unsigned long)trace.match, (unsigned long)trace.mismatch,
        (unsigned long)trace.raw_rx_errors, (unsigned long)trace.capture_errors, (unsigned long)trace.time_order_errors,
        (unsigned long)trace.event_count, (unsigned long)trace.events_not_stored,
        (unsigned long)trace.failure_count, (unsigned long)trace.failures_not_stored,
        (unsigned long)(phase_source ? experiment.dma_errors : trace.spi_errors),
        (unsigned long)(phase_source ? experiment.dma_timeouts : trace.dma_timeouts), (unsigned long)trace.stopped_pairs,
        (unsigned long)trace.irq_count, (unsigned long)(phase_source ? experiment.not_ready : not_ready),
        (unsigned long)(phase_source ? experiment.first_delay : 0U), (unsigned long)(phase_source ? experiment.gap_delay : 0U),
        (unsigned long)(phase_source ? experiment.busy_edges : 0U), (unsigned long)(phase_source ? experiment.skipped_edges : 0U),
        (unsigned long)(phase_source ? experiment.io_errors : 0U), (unsigned long)(phase_source ? experiment.restore_errors : 0U),
        (unsigned long)(phase_source ? experiment.wait_timeouts : 0U), elapsed,
        (unsigned long)stop_starts, (unsigned long)stop_completions,
        (unsigned long)max_snapshot_us, (unsigned long)max_observer_us, (unsigned int)invariant, (unsigned int)marker_enabled,
        (unsigned long)marker_pulses, bad || invariant || (phase_source && experiment.state != PAIR_EXP_DONE &&
        experiment.state != PAIR_EXP_STOPPED) ? "ERR_PAIR_TEST" : "OK");
}
static int WaitReport(char *out, size_t size)
{
    const Pair_WaitContext *c = &experiment.wait_context;
    return snprintf(out, size,
        "PAIR_WAIT_CONTEXT_VALID: %lu\r\nWAIT_STATE: %lu\r\nWAIT_PREVIOUS_STATE: %lu\r\n"
        "WAIT_TIM2_US: %lu\r\nWAIT_LAST_IRQ_US: %lu\r\nWAIT_LAST_DIAG_US: %lu\r\n"
        "WAIT_PB1_LOW: %lu\r\nWAIT_EXTI_PENDING: %lu\r\nWAIT_IRQ_COUNT: %lu\r\n"
        "WAIT_BUSY_EDGES: %lu\r\nWAIT_SKIPPED_EDGES: %lu\r\nWAIT_CNVRF: %lu\r\n"
        "WAIT_SPI_SR: 0x%08lX\r\nWAIT_DMA_STATE: %lu\r\nWAIT_DMA_FLAGS: 0x%08lX\r\n"
        "WAIT_PAIRS: %lu\r\nPAIR_REARM_READS: %lu\r\nSTATUS: OK\r\n",
        (unsigned long)c->valid, (unsigned long)c->state, (unsigned long)c->previous,
        (unsigned long)c->stamp, (unsigned long)c->last_irq_us, (unsigned long)c->last_diag_us,
        (unsigned long)c->pin_low, (unsigned long)c->exti_pending, (unsigned long)c->irq_count,
        (unsigned long)c->busy_edges, (unsigned long)c->skipped_edges, (unsigned long)c->cnvrf,
        (unsigned long)c->spi_sr, (unsigned long)c->dma_state, (unsigned long)c->dma_flags,
        (unsigned long)c->pairs, (unsigned long)experiment.rearm_reads);
}
static int EventReport(char *out, size_t size, uint32_t index, bool failure)
{
    const Pair_Event *e;
    uint32_t n, count = failure ? trace.failure_count : trace.event_count;
    size_t used = 0U;
    if (trace.active) return snprintf(out, size, "ERR: PAIR_ACTIVE\r\n");
    if (index >= count) return snprintf(out, size, "PAIR_EVENT_COUNT: %lu\r\nERR: NO_EVENT\r\n", (unsigned long)count);
    e = failure ? &trace.failures[index] : &trace.events[index];
    if (!Append(out, size, &used,
        "PAIR_EVENT: %lu\r\nPAIR_FAILURE: %u\r\nSEQ: %lu\r\nIRQ_ID: %lu\r\nALERT_US: %lu\r\n"
        "RAW1: 0x%06lX\r\nRAW2: 0x%06lX\r\nCODE1: %ld\r\nCODE2: %ld\r\nXOR: 0x%06lX\r\n"
        "VALID_MASK: %u\r\nFAILED_PHASE: %u\r\nFLAGS: 0x%02lX\r\nUPDATE_POSSIBLE: 1\r\n"
        "NOMINAL_BOUNDARY_BETWEEN_READS: %u\r\nALERT_TO_FIRST_START_US: %lu\r\n"
        "ALERT_TO_FIRST_END_US: %lu\r\nINTER_READ_GAP_US: %lu\r\nALERT_TO_SECOND_END_US: %lu\r\n"
        "NEW_ALERTS_DURING_READS: %lu\r\nNEW_ALERTS_IN_GAP: %lu\r\nALERT_TIMESTAMPS_MISSING: %lu\r\n"
        "ALATCH: %lu\r\nADC_CONFIG: 0x%04lX\r\nDIAG_ALRT: 0x%04X\r\nCNVRF: %u\r\n",
        (unsigned long)index, (unsigned int)failure, (unsigned long)e->seq, (unsigned long)e->irq_id,
        (unsigned long)e->alert_us, (unsigned long)e->raw1, (unsigned long)e->raw2,
        (long)PairTrace_Code(e->raw1), (long)PairTrace_Code(e->raw2), (unsigned long)(e->raw1 ^ e->raw2),
        (unsigned int)e->valid_mask, (unsigned int)e->failed_phase, (unsigned long)e->flags,
        (unsigned int)((e->flags & PAIR_NOMINAL_BOUNDARY) != 0U),
        (unsigned long)(e->transfer[1].start_us - e->alert_us),
        (unsigned long)(e->transfer[1].end_us - e->alert_us),
        (unsigned long)(e->transfer[2].start_us - e->transfer[1].end_us),
        (unsigned long)(e->transfer[2].end_us - e->alert_us),
        (unsigned long)e->new_edges, (unsigned long)e->gap_edges, (unsigned long)e->history_missing,
        (unsigned long)(trace.diag_control >> 15), (unsigned long)trace.adc_config,
        (unsigned int)(((uint16_t)e->transfer[0].rx[1] << 8) | e->transfer[0].rx[2]),
        (unsigned int)((e->transfer[0].rx[2] & 2U) != 0U))) return -1;
    for (n = 0U; n < 3U; ++n) {
        const SpiDiag_Result *r = &e->transfer[n];
        if (!Append(out, size, &used,
            "TRANSFER_%lu: START=%lu END=%lu RX_IRQ=%lu RX=%02X,%02X,%02X,%02X LENGTH=%u "
            "STATUS=%u STARTED=%u VALID=%u GEN=%lu HAL_STATE=%lu HAL_ERROR=0x%08lX SPI_SR=0x%08lX "
            "SPI_CR1=0x%08lX SPI_CR2=0x%08lX DMA_STATE=%lu DMA_FLAGS=0x%08lX RX_CR=0x%08lX "
            "TX_CR=0x%08lX RX_LEFT=%lu TX_LEFT=%lu CS_BEFORE=%lu CS_AFTER=%lu "
            "GPIO_START_LOW=%lu GPIO_END_LOW=%lu EXTI_PENDING=%lu EDGE_START=%lu EDGE_END=%lu "
            "OWNER=%lu CALLBACK_ERRORS=%lu CS_ERRORS=%lu\r\n",
            (unsigned long)n, (unsigned long)r->start_us, (unsigned long)r->end_us, (unsigned long)r->irq_us,
            r->rx[0], r->rx[1], r->rx[2], r->rx[3], (unsigned int)r->length,
            (unsigned int)r->status, (unsigned int)r->started, (unsigned int)((e->valid_mask & (1U << n)) != 0U),
            (unsigned long)r->generation,
            (unsigned long)r->hal_state, (unsigned long)r->hal_error, (unsigned long)r->spi_sr,
            (unsigned long)r->spi_cr1, (unsigned long)r->spi_cr2, (unsigned long)r->dma_state,
            (unsigned long)r->dma_flags, (unsigned long)r->rx_cr, (unsigned long)r->tx_cr,
            (unsigned long)r->rx_left, (unsigned long)r->tx_left, (unsigned long)r->cs_before,
            (unsigned long)r->cs_high, (unsigned long)r->pin_start, (unsigned long)r->pin_end,
            (unsigned long)r->exti_pending, (unsigned long)r->edge_start, (unsigned long)r->edge_end,
            (unsigned long)r->owner_conflicts, (unsigned long)r->callback_errors,
            (unsigned long)r->cs_errors)) return -1;
    }
    for (n = 0U; n < e->edge_count; ++n)
        if (!Append(out, size, &used, "NEW_ALERT_%lu: ID=%lu US=%lu LOW=%u\r\n", (unsigned long)n,
            (unsigned long)e->edges[n].id, (unsigned long)e->edges[n].us, (unsigned int)e->edges[n].low)) return -1;
    if (!Append(out, size, &used, "STATUS: OK\r\n")) return -1;
    return (int)used;
}
static int Bins(char *out, size_t size, uint32_t metric)
{
    size_t used = 0U; uint32_t i;
    if (trace.active) return snprintf(out, size, "ERR: PAIR_ACTIVE\r\n");
    if (!Append(out, size, &used, "PAIR_METRIC: %lu\r\nPAIR_BIN_WIDTH_US: 10\r\n", (unsigned long)metric)) return -1;
    for (i = 0U; i < PAIR_PHASE_BINS; ++i)
        if (!Append(out, size, &used, "BIN_%lu: FROM_US=%lu MATCH=%lu MISMATCH=%lu\r\n",
            (unsigned long)i, (unsigned long)(i * 10U), (unsigned long)trace.bins[metric][i][0],
            (unsigned long)trace.bins[metric][i][1])) return -1;
    if (!Append(out, size, &used, "STATUS: OK\r\n")) return -1;
    return (int)used;
}
/* Strict decimal parsing: reject signs, empty tokens, overflow and trailing text. */
static bool Number(const char **s, uint32_t *value)
{
    uint32_t v = 0U; const char *p = *s;
    if (*p < '0' || *p > '9') return false;
    while (*p >= '0' && *p <= '9') {
        uint32_t d = (uint32_t)(*p++ - '0');
        if (v > (UINT32_MAX - d) / 10U) return false;
        v = v * 10U + d;
    }
    *s = p; *value = v; return true;
}
int PairDiag_Command(const char *args, char *out, size_t size)
{
    const char *s; uint32_t a, b;
    bool occupied = Acq_IsRunning() || StreamTransport_IsActive() || SpiFreeze_IsBusy();
    if (strcmp(args, "show") == 0)
        return snprintf(out, size, "PAIR_STATE: %s\r\nPAIR_SELECTED_VSHCT_US: %lu\r\nPAIR_SELECTED_FIRST_US: %lu\r\n"
            "PAIR_SELECTED_GAP_US: %lu\r\nPAIR_SELECTED_LATCH: %u\r\nPAIR_TOTAL: %lu\r\nSTATUS: OK\r\n",
            State(), (unsigned long)ct_us, (unsigned long)delay_us, (unsigned long)gap_us,
            (unsigned int)latch, (unsigned long)trace.total);
    if (occupied) return snprintf(out, size, "ERR: ACQ_OR_DIAGNOSTIC_ACTIVE\r\n");
    if (strcmp(args, "stop") == 0) {
        PairDiag_Stop(false);
        return snprintf(out, size, experiment.busy ? "ERR: PAIR_RESTORE\r\n" : "STATUS: OK\r\n");
    }
    if (experiment.busy && experiment.state != PAIR_EXP_RESTORE_FAILED)
        return snprintf(out, size, "ERR: PAIR_ACTIVE\r\n");
    if (strcmp(args, "report") == 0) return Report(out, size);
    if (strcmp(args, "wait") == 0) return WaitReport(out, size);
    s = args;
    if (strncmp(s, "events", 6U) == 0 || strncmp(s, "failures", 8U) == 0) {
        bool failed = strncmp(s, "failures", 8U) == 0; s += failed ? 8U : 6U; a = 0U;
        if (*s && (*s++ != ' ' || !Number(&s, &a) || *s)) goto bad;
        return EventReport(out, size, a, failed);
    }
    if (strncmp(args, "bins ", 5U) == 0) {
        s = args + 5U; if (!Number(&s, &a) || *s || a > 3U) goto bad;
        return Bins(out, size, a);
    }
    if (experiment.busy) return snprintf(out, size, "ERR: PAIR_RESTORE\r\n");
    if (strncmp(args, "delay ", 6U) == 0) {
        s = args + 6U;
        if (!Number(&s, &a) || *s++ != ' ' || !Number(&s, &b) || *s || a > 500U || b > 500U) goto bad;
        delay_us = a; gap_us = b;
    }
    else if (strcmp(args, "vshct 84") == 0 || strcmp(args, "vshct 150") == 0)
        ct_us = strcmp(args, "vshct 84") == 0 ? 84U : 150U;
    else if (strcmp(args, "latch 0") == 0 || strcmp(args, "latch 1") == 0)
        latch = args[6] == '1';
    else if (strcmp(args, "marker on") == 0 || strcmp(args, "marker off") == 0) {
        if (!MarkerSet(strcmp(args, "marker on") == 0))
            return snprintf(out, size, "ERR: MARKER_UNAVAILABLE_OR_PIN_BUSY\r\n");
    }
    else if (strcmp(args, "test 10000") == 0 || strcmp(args, "test 100000") == 0) {
        Freeze_IO io = {sensor, Read16, Write16, Begin, ReadDma, Poll, StopDma, Now, NULL, NULL};
        if (!sensor || !AcqDma_IsStopped() || (TIM3->CR1 & TIM_CR1_CEN) || SpiControl_GetHz() != 6000000U)
            return snprintf(out, size, "ERR: PAIR_REQUIRES_STOPPED_SPI_6000000\r\n");
        phase_source = true; native_observing = false; stop_starts = stop_completions = 0U;
        max_snapshot_us = max_observer_us = 0U;
        if (!PairExperiment_Start(&experiment, &trace, &io, strcmp(args, "test 10000") == 0 ? 10000U : 100000U,
                                 ct_us, delay_us, gap_us, latch))
            return snprintf(out, size, "ERR: PAIR_START_OR_RESTORE\r\n");
        experiment.capture_context = TimeoutHardware;
        return snprintf(out, size, "PAIR: STARTED\r\nSTATUS: OK\r\n");
    }
    else goto bad;
    return snprintf(out, size, "STATUS: OK\r\n");
bad:
    return snprintf(out, size, "ERR: BAD_COMMAND\r\n");
}
