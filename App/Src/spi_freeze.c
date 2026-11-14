#include "spi_freeze.h"
#include "spi_freeze_core.h"
#include "pair_diagnostic.h"
#include "acquisition.h"
#include "acquisition_dma.h"
#include "spi_control.h"
#include "stream_transport.h"
#include "timebase.h"
#include "format64.h"
#include "tim.h"
#include <stdarg.h>
#include <stdio.h>

static Freeze_Core freeze;
static INA229_HandleTypeDef *sensor;
static uint32_t selected_vshct = 150U;
static uint32_t backend_starts, backend_completions, backend_max_us;
static uint32_t backend_callback_errors, backend_cs_errors, backend_buffer_reuse;
static bool backend_stop_confirmed;
static uint32_t polling_hal_error_status;
static int32_t Read16(void *ctx, uint8_t reg, uint16_t *v)
{
    INA229_Status status = INA229_ReadReg16((INA229_HandleTypeDef *)ctx, reg, v);
    if (status != INA229_OK) polling_hal_error_status = INA229_LastHalStatus();
    return status;
}
static int32_t Write16(void *ctx, uint8_t reg, uint16_t v)
{
    INA229_Status status = INA229_WriteReg16((INA229_HandleTypeDef *)ctx, reg, v);
    if (status != INA229_OK) polling_hal_error_status = INA229_LastHalStatus();
    return status;
}
static bool Begin(void *ctx) { (void)ctx; return AcqDma_DiagnosticBegin(); }
static bool ReadDma(void *ctx, uint8_t reg, uint8_t length)
{ (void)ctx; return AcqDma_DiagnosticRead(reg, length); }
static bool Poll(void *ctx, SpiDiag_Result *r)
{ (void)ctx; AcqDma_CheckPairTimeout(); return AcqDma_DiagnosticPoll(r); }
static bool StopDma(void *ctx)
{
    bool clean;
    const AcqDma_Core *d;
    (void)ctx; clean = AcqDma_Stop(); d = AcqDma_GetCore();
    backend_starts = d->starts; backend_completions = d->completions;
    backend_max_us = d->max_transaction_us;
    backend_callback_errors = d->callback_order_errors;
    backend_cs_errors = d->cs_state_errors; backend_buffer_reuse = d->buffer_reuse_attempts;
    backend_stop_confirmed = clean && AcqDma_IsStopped();
    return clean;
}
static uint64_t Now(void *ctx) { (void)ctx; return Timebase_NowUs64(); }
static uint32_t Writes(void *ctx) { (void)ctx; return INA229_AdcWriteAttempts(); }
static uint32_t Conflicts(void *ctx) { (void)ctx; return INA229_ContentionCount(); }
static const char *State(void)
{
    switch (freeze.phase) {
        case FREEZE_IDLE: return "IDLE";
        case FREEZE_DONE: return "DONE";
        case FREEZE_STOPPED: return "STOPPED";
        case FREEZE_ERROR: return "ERROR";
        case FREEZE_RESTORE_FAILED: return "RESTORE_FAILED";
        default: return "RUNNING";
    }
}
static const char *Reason(void)
{
    static const char *const reasons[] = {"NONE", "BUSY", "ARGUMENT", "IO_ERROR", "READY_TIMEOUT",
        "CONFIG_UNSUPPORTED", "DMA_START", "ADC_AUDIT", "RESTORE", "USB_DISCONNECT"};
    return freeze.error <= FREEZE_ERR_DISCONNECT ? reasons[freeze.error] : "UNKNOWN";
}
static bool Append(char *out, size_t size, size_t *used, const char *format, ...)
{
    va_list args; int n;
    if (*used >= size) return false;
    va_start(args, format); n = vsnprintf(out + *used, size - *used, format, args); va_end(args);
    if (n < 0 || (size_t)n >= size - *used) return false;
    *used += (size_t)n; return true;
}
void SpiFreeze_Init(INA229_HandleTypeDef *handle) { sensor = handle; }
bool SpiFreeze_IsBusy(void) { return freeze.busy; }
bool SpiFreeze_SetVshct(uint32_t us)
{
    if (freeze.busy || Acq_IsRunning() || StreamTransport_IsActive() ||
        (us != 50U && us != 84U && us != 150U)) return false;
    selected_vshct = us; return true;
}
const char *SpiFreeze_Start(uint32_t pairs, bool ids)
{
    Freeze_IO io = {sensor, Read16, Write16, Begin, ReadDma, Poll, StopDma, Now, Writes, Conflicts};
    if (freeze.busy) return "FREEZE_BUSY";
    if (PairDiag_IsBusy()) return "PAIR_ACTIVE";
    if (Acq_IsRunning() || StreamTransport_IsActive()) return "ACQ_OR_STREAM_ACTIVE";
    if (!sensor || !AcqDma_IsStopped() || (TIM3->CR1 & TIM_CR1_CEN) != 0U) return "SPI_OR_TIMER_BUSY";
    if (SpiControl_GetHz() != 6000000U) return "FREEZE_REQUIRES_SPI_6000000";
    if (pairs != 10000U && pairs != 100000U) return "BAD_PAIR_COUNT";
    backend_starts = backend_completions = backend_max_us = 0U;
    backend_callback_errors = backend_cs_errors = backend_buffer_reuse = 0U;
    backend_stop_confirmed = false;
    polling_hal_error_status = 0U;
    if (!FreezeCore_Start(&freeze, &io, pairs, selected_vshct, ids)) return Reason();
    return "OK";
}
void SpiFreeze_Process(void) { FreezeCore_Process(&freeze); }
void SpiFreeze_Stop(bool disconnected) { FreezeCore_Stop(&freeze, disconnected); }
int SpiFreeze_FormatShow(char *out, size_t size)
{
    return snprintf(out, size,
        "FREEZE_STATUS: %s\r\nFREEZE_SELECTED_VSHCT_US: %lu\r\nFREEZE_MODE: %s\r\n"
        "FREEZE_PAIRS_COMPLETED: %lu\r\nFREEZE_PAIRS_REQUESTED: %lu\r\n"
        "FREEZE_REASON: %s\r\nSTATUS: OK\r\n",
        State(), (unsigned long)selected_vshct, freeze.ids ? "IDS" : "VSHUNT",
        (unsigned long)freeze.completed, (unsigned long)freeze.requested, Reason());
}
int SpiFreeze_FormatReport(char *out, size_t size)
{
    uint64_t elapsed;
    char elapsed_text[FORMAT64_UDEC_SIZE], reference[16] = "N/A", code[20] = "N/A";
    char last1[16] = "N/A", last2[16] = "N/A";
    bool invariant, bad;
    size_t used = 0U;
    if (freeze.busy && freeze.phase != FREEZE_RESTORE_FAILED) return snprintf(out, size, "ERR: FREEZE_BUSY\r\n");
    elapsed = freeze.ended_us >= freeze.started_us ? freeze.ended_us - freeze.started_us : 0U;
    if (!Format64_Unsigned(elapsed_text, sizeof(elapsed_text), elapsed)) return -1;
    if (freeze.reference_valid) {
        (void)snprintf(reference, sizeof(reference), "0x%06lX", (unsigned long)freeze.reference);
        (void)snprintf(code, sizeof(code), "%ld", (long)FreezeCore_Code(freeze.reference));
    }
    if (freeze.completed != 0U) {
        (void)snprintf(last1, sizeof(last1), "0x%06lX", (unsigned long)freeze.last_raw1);
        (void)snprintf(last2, sizeof(last2), "0x%06lX", (unsigned long)freeze.last_raw2);
    }
    invariant = freeze.completed > freeze.requested ||
        freeze.pair_match + freeze.pair_mismatch != freeze.completed ||
        freeze.reads_total < 2U * freeze.completed || freeze.reads_total > 2U * freeze.completed + 2U ||
        freeze.first_mismatch > freeze.completed || freeze.second_mismatch > freeze.completed;
    bad = invariant || freeze.error != FREEZE_ERR_NONE || freeze.pair_mismatch ||
        freeze.reference_mismatch || freeze.reserved_errors || freeze.audit_errors || freeze.restore_errors;
    if (!Append(out, size, &used,
        "FREEZE_STATUS: %s\r\nFREEZE_MODE: %s\r\nFREEZE_REASON: %s\r\n"
        "FREEZE_VSHCT_US: %lu\r\nFREEZE_REFERENCE_RAW: %s\r\nFREEZE_REFERENCE_CODE: %s\r\n"
        "FREEZE_PAIRS_REQUESTED: %lu\r\nFREEZE_PAIRS_COMPLETED: %lu\r\nFREEZE_READS_TOTAL: %lu\r\n"
        "FREEZE_ALL_DMA_READS: %lu\r\nFREEZE_PAIR_MATCH: %lu\r\nFREEZE_PAIR_MISMATCH: %lu\r\n"
        "FREEZE_REFERENCE_MISMATCH: %lu\r\nFREEZE_REFERENCE_MISMATCH_1: %lu\r\nFREEZE_REFERENCE_MISMATCH_2: %lu\r\n"
        "FREEZE_EQUAL_BUT_NOT_REFERENCE: %lu\r\nFREEZE_ONLY_ONE_BAD: %lu\r\nFREEZE_BOTH_BAD: %lu\r\n"
        "FREEZE_CODE_MISMATCH: %lu\r\nFREEZE_RESERVED_BITS_ERRORS: %lu\r\n"
        "FREEZE_FIRST_CHANGED_PAIR: %lu\r\nFREEZE_REFERENCE_RECOVERIES: %lu\r\n"
        "FREEZE_CHANGED_SUFFIX_PAIRS: %lu\r\nFREEZE_LONGEST_NEW_VALUE_RUN: %lu\r\n",
        State(), freeze.ids ? "IDS" : "VSHUNT", Reason(), (unsigned long)freeze.vshct_us, reference, code,
        (unsigned long)freeze.requested, (unsigned long)freeze.completed, (unsigned long)freeze.reads_total,
        (unsigned long)freeze.all_dma_reads, (unsigned long)freeze.pair_match, (unsigned long)freeze.pair_mismatch,
        (unsigned long)freeze.reference_mismatch, (unsigned long)freeze.first_mismatch,
        (unsigned long)freeze.second_mismatch, (unsigned long)freeze.equal_not_reference,
        (unsigned long)freeze.only_one_bad, (unsigned long)freeze.both_bad,
        (unsigned long)freeze.code_mismatch, (unsigned long)freeze.reserved_errors,
        (unsigned long)freeze.first_changed_pair, (unsigned long)freeze.recovered_after_change,
        (unsigned long)freeze.changed_suffix, (unsigned long)freeze.longest_new_run)) return -1;
    if (!Append(out, size, &used, "FREEZE_NO_REFERENCE_RECOVERY_AFTER_FIRST_CHANGE: %u\r\n",
        (unsigned int)(freeze.first_changed_pair != 0U && freeze.recovered_after_change == 0U))) return -1;
    if (!Append(out, size, &used,
        "FREEZE_LAST_RAW1: %s\r\nFREEZE_LAST_RAW2: %s\r\n"
        "FREEZE_SAVED_ADC_CONFIG: 0x%04X\r\nFREEZE_SAVED_DIAG_CONTROL: 0x%04X\r\n"
        "FREEZE_TRIGGER_ADC_CONFIG: 0x%04X\r\nFREEZE_RESTORE_PENDING: %u\r\n",
        last1, last2, freeze.saved_adc, freeze.saved_diag & 0xF000U, freeze.trigger_adc,
        (unsigned int)(freeze.backend || freeze.adc_changed || freeze.diag_changed))) return -1;
    if (!Append(out, size, &used,
        "FREEZE_DMA_STOP_CONFIRMED: %u\r\nFREEZE_DMA_RESULT_PENDING: %u\r\n"
        "FREEZE_CALLBACK_ORDER_ERRORS: %lu\r\nFREEZE_CS_ERRORS: %lu\r\nFREEZE_BUFFER_REUSE_ATTEMPTS: %lu\r\n",
        (unsigned int)backend_stop_confirmed, (unsigned int)freeze.pending,
        (unsigned long)backend_callback_errors, (unsigned long)backend_cs_errors,
        (unsigned long)backend_buffer_reuse)) return -1;
    if (!Append(out, size, &used,
        "FREEZE_SPI_ERRORS: %lu\r\nFREEZE_DMA_ERRORS: %lu\r\nFREEZE_DMA_TIMEOUTS: %lu\r\n"
        "FREEZE_HAL_ERRORS: %lu\r\nFREEZE_LAST_INA229_STATUS: %ld\r\nFREEZE_LAST_HAL_ERROR_STATUS: %lu\r\n"
        "FREEZE_RESTORE_ERRORS: %lu\r\n"
        "FREEZE_MAX_TRANSACTION_US: %lu\r\nFREEZE_MAX_PAIR_US: %lu\r\nFREEZE_MAX_GAP_US: %lu\r\n"
        "FREEZE_TEST_ELAPSED_US64: %s\r\nFREEZE_SINGLE_SHOT_CONFIRMED: %u\r\n"
        "FREEZE_TRIGGER_WRITE_ATTEMPTS: %lu\r\nFREEZE_ADC_WRITES_WHILE_FROZEN: %lu\r\n"
        "FREEZE_ADC_START: 0x%04X\r\nFREEZE_ADC_END: 0x%04X\r\n"
        "FREEZE_DIAG_START: 0x%04X\r\nFREEZE_DIAG_END: 0x%04X\r\nFREEZE_AUDIT_ERRORS: %lu\r\n"
        "FREEZE_OWNER_ERRORS: %lu\r\nFREEZE_DMA_STARTS: %lu\r\nFREEZE_DMA_COMPLETIONS: %lu\r\n"
        "FREEZE_MANUFACTURER_EXPECTED: 0x5449\r\nFREEZE_DEVICE_EXPECTED: 0x2291\r\n"
        "FREEZE_MANUFACTURER_ERRORS: %lu\r\nFREEZE_DEVICE_ERRORS: %lu\r\n"
        "FREEZE_EVENT_COUNT: %lu\r\nFREEZE_EVENTS_NOT_STORED: %lu\r\n"
        "FREEZE_COUNTER_INVARIANT_ERRORS: %u\r\nSTATUS: %s\r\n",
        (unsigned long)freeze.spi_errors, (unsigned long)freeze.dma_errors, (unsigned long)freeze.dma_timeouts,
        (unsigned long)freeze.hal_errors, (long)freeze.last_hal_status,
        (unsigned long)polling_hal_error_status, (unsigned long)freeze.restore_errors,
        (unsigned long)(freeze.max_transaction_us > backend_max_us ? freeze.max_transaction_us : backend_max_us),
        (unsigned long)freeze.max_pair_us,
        (unsigned long)freeze.max_gap_us, elapsed_text, (unsigned int)freeze.completion_confirmed,
        (unsigned long)freeze.trigger_writes, (unsigned long)freeze.frozen_writes,
        freeze.adc_start, freeze.adc_end, freeze.diag_start, freeze.diag_end,
        (unsigned long)freeze.audit_errors, (unsigned long)freeze.owner_errors,
        (unsigned long)backend_starts, (unsigned long)backend_completions,
        (unsigned long)freeze.ids_bad_manufacturer, (unsigned long)freeze.ids_bad_device,
        (unsigned long)freeze.event_count, (unsigned long)freeze.events_dropped,
        (unsigned int)invariant, bad ? "ERR_FREEZE_DIAGNOSTIC" : "OK")) return -1;
    return (int)used;
}
static bool ResultLine(char *out, size_t size, size_t *used, const SpiDiag_Result *r, unsigned int read)
{
    return Append(out, size, used,
        " READ%u: RX=%02X,%02X,%02X,%02X LENGTH=%u START=%lu END=%lu STATUS=%u STARTED=%u DATA_VALID=%u GEN=%lu "
        "HAL_STATE=%lu HAL_ERROR=0x%08lX SPI_SR=0x%08lX DMA_FLAGS=0x%08lX CS_HIGH=%lu "
        "OWNER=%lu CALLBACK_ERRORS=%lu CS_ERRORS=%lu\r\n",
        read, r->rx[0], r->rx[1], r->rx[2], r->rx[3], (unsigned int)r->length, (unsigned long)r->start_us,
        (unsigned long)r->end_us, (unsigned int)r->status, (unsigned int)r->started,
        (unsigned int)(r->started && r->status == SPI_DIAG_OK && (r->length == 3U || r->length == 4U)),
        (unsigned long)r->generation, (unsigned long)r->hal_state, (unsigned long)r->hal_error,
        (unsigned long)r->spi_sr, (unsigned long)r->dma_flags, (unsigned long)r->cs_high,
        (unsigned long)r->owner_conflicts, (unsigned long)r->callback_errors, (unsigned long)r->cs_errors);
}
int SpiFreeze_FormatEvents(char *out, size_t size, uint32_t page)
{
    uint32_t first = page * 4U, i, last;
    size_t used = 0U;
    if (freeze.busy && freeze.phase != FREEZE_RESTORE_FAILED) return snprintf(out, size, "ERR: FREEZE_BUSY\r\n");
    if (page > 3U || (first >= freeze.event_count && page != 0U)) return snprintf(out, size, "ERR: BAD_PAGE\r\n");
    last = first + 4U < freeze.event_count ? first + 4U : freeze.event_count;
    if (!Append(out, size, &used,
                "FREEZE_EVENT_MODE: %s\r\nFREEZE_REFERENCE_VALID: %u\r\n"
                "FREEZE_EVENT_PAGE: %lu\r\nFREEZE_EVENT_COUNT: %lu\r\n",
                freeze.ids ? "IDS" : "VSHUNT", (unsigned int)freeze.reference_valid,
                (unsigned long)page, (unsigned long)freeze.event_count)) return -1;
    for (i = first; i < last; ++i) {
        const Freeze_Event *e = &freeze.events[i];
        char code1[20] = "N/A", code2[20] = "N/A";
        uint32_t base1 = freeze.ids ? 0x5449U : e->reference;
        uint32_t base2 = freeze.ids ? 0x2291U : e->reference;
        if (!freeze.ids) {
            (void)snprintf(code1, sizeof(code1), "%ld", (long)FreezeCore_Code(e->raw1));
            (void)snprintf(code2, sizeof(code2), "%ld", (long)FreezeCore_Code(e->raw2));
        }
        if (!Append(out, size, &used,
            "EVENT_%lu: PAIR=%lu PHASE=%lu BASE1=0x%06lX BASE2=0x%06lX RAW1=0x%06lX RAW2=0x%06lX "
            "CODE1=%s CODE2=%s XOR_REF1=0x%06lX XOR_REF2=0x%06lX XOR_12=0x%06lX\r\n",
            (unsigned long)i, (unsigned long)e->pair, (unsigned long)e->phase,
            (unsigned long)base1, (unsigned long)base2, (unsigned long)e->raw1, (unsigned long)e->raw2,
            code1, code2, (unsigned long)(base1 ^ e->raw1),
            (unsigned long)(base2 ^ e->raw2), (unsigned long)(e->raw1 ^ e->raw2)) ||
            !ResultLine(out, size, &used, &e->a, 1U) || !ResultLine(out, size, &used, &e->b, 2U)) return -1;
    }
    if (!Append(out, size, &used, "FREEZE_NEXT_PAGE: %s\r\nSTATUS: OK\r\n",
                last < freeze.event_count ? "AVAILABLE (spi freeze events N)" : "NONE")) return -1;
    return (int)used;
}
