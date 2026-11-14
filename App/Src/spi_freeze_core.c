#include "spi_freeze_core.h"
#include <string.h>
enum { REG_CONFIG=0, REG_ADC=1, REG_SHUNT=4, REG_DIAG=11, REG_MANUF=62, REG_DEVICE=63 };
int32_t FreezeCore_Code(uint32_t raw)
{
    uint32_t code = (raw >> 4) & 0xFFFFFU;
    return (code & 0x80000U) ? (int32_t)code - 0x100000L : (int32_t)code;
}
static uint32_t Value(const SpiDiag_Result *r)
{
    return r->length == 4U ? ((uint32_t)r->rx[1] << 16) | ((uint32_t)r->rx[2] << 8) | r->rx[3] :
                            ((uint32_t)r->rx[1] << 8) | r->rx[2];
}
static bool Read(Freeze_Core *c, uint8_t reg, uint16_t *v)
{
    int32_t status = c->io.read16(c->io.ctx, reg, v);
    if (status == 0) return true;
    c->last_hal_status = status;
    ++c->hal_errors; ++c->spi_errors; return false;
}
static bool Write(Freeze_Core *c, uint8_t reg, uint16_t v)
{
    int32_t status = c->io.write16(c->io.ctx, reg, v);
    if (status == 0) return true;
    c->last_hal_status = status;
    ++c->hal_errors; ++c->spi_errors; return false;
}
static void Finish(Freeze_Core *c)
{
    uint16_t check;
    if (!c->finish_audited) {
        c->finish_audited = true;
        c->trigger_writes = c->io.adc_writes(c->io.ctx) - c->write_base;
        c->owner_errors = c->io.conflicts(c->io.ctx) - c->owner_base;
        if (c->completion_confirmed || c->ids)
            c->frozen_writes = c->io.adc_writes(c->io.ctx) - c->frozen_write_base;
        if (c->frozen_writes || c->owner_errors || (!c->ids && c->completion_confirmed && c->trigger_writes != 1U))
            ++c->audit_errors;
        c->ended_us = c->io.now(c->io.ctx);
    }
    if (c->backend) {
        if (!c->io.stop(c->io.ctx)) {
            ++c->restore_errors; c->phase = FREEZE_RESTORE_FAILED;
            c->error = FREEZE_ERR_RESTORE; return;
        }
        c->backend = false; c->pending = false;
    }
    if (c->diag_changed) {
        if (Write(c, REG_DIAG, (uint16_t)(c->saved_diag & 0xF000U)) &&
            Read(c, REG_DIAG, &check) && (check & 0xF000U) == (c->saved_diag & 0xF000U))
            c->diag_changed = false;
        else ++c->restore_errors;
    }
    if (c->adc_changed) {
        if (Write(c, REG_ADC, c->saved_adc) && Read(c, REG_ADC, &check) && check == c->saved_adc)
            c->adc_changed = false;
        else ++c->restore_errors;
    }
    if (c->adc_changed || c->diag_changed) {
        c->phase = FREEZE_RESTORE_FAILED; c->error = FREEZE_ERR_RESTORE; return;
    }
    c->busy = false;
    c->phase = c->audit_errors ? FREEZE_ERROR : c->outcome;
    if (c->audit_errors && c->error == FREEZE_ERR_NONE) c->error = FREEZE_ERR_AUDIT;
}
static void Error(Freeze_Core *c, Freeze_Error error)
{ c->error = error; c->outcome = FREEZE_ERROR; Finish(c); }
static void Event(Freeze_Core *c, const SpiDiag_Result *a, const SpiDiag_Result *b)
{
    Freeze_Event *e;
    if (c->event_count >= 16U) { ++c->events_dropped; return; }
    e = &c->events[c->event_count++];
    e->pair = c->phase == FREEZE_FIRST || c->phase == FREEZE_SECOND ? c->completed + 1U : 0U;
    e->phase = c->phase; e->reference = c->reference;
    e->a = *a; e->b = *b; e->raw1 = Value(a); e->raw2 = Value(b);
}
static void Pair(Freeze_Core *c, const SpiDiag_Result *second)
{
    uint32_t a = Value(&c->first), b = Value(second);
    c->last_raw1 = a; c->last_raw2 = b;
    bool bad_a = a != (c->ids ? 0x5449U : c->reference);
    bool bad_b = b != (c->ids ? 0x2291U : c->reference);
    bool mismatch = c->ids ? bad_a || bad_b : a != b;
    uint32_t cycle = second->end_us - c->first.start_us;
    if (cycle > c->max_pair_us) c->max_pair_us = cycle;
    if (mismatch) ++c->pair_mismatch; else ++c->pair_match;
    if (bad_a) ++c->first_mismatch;
    if (bad_b) ++c->second_mismatch;
    if (bad_a || bad_b) {
        ++c->reference_mismatch;
        if (!c->first_changed_pair) c->first_changed_pair = c->completed + 1U;
        ++c->changed_suffix;
    }
    else {
        if (c->first_changed_pair) ++c->recovered_after_change;
        c->changed_suffix = 0U;
    }
    if (bad_a != bad_b) ++c->only_one_bad;
    if (bad_a && bad_b) ++c->both_bad;
    if (!c->ids) {
        if (a == b && bad_a) {
            ++c->equal_not_reference;
            if (c->new_run && c->new_value == a) ++c->new_run;
            else { c->new_value = a; c->new_run = 1U; }
            if (c->new_run > c->longest_new_run) c->longest_new_run = c->new_run;
        }
        else c->new_run = 0U;
        if (FreezeCore_Code(a) != FreezeCore_Code(b)) ++c->code_mismatch;
        if (a & 15U) ++c->reserved_errors;
        if (b & 15U) ++c->reserved_errors;
    }
    else { if (bad_a) ++c->ids_bad_manufacturer; if (bad_b) ++c->ids_bad_device; }
    if (mismatch || bad_a || bad_b || (!c->ids && ((a | b) & 15U))) Event(c, &c->first, second);
    ++c->completed;
}
bool FreezeCore_Start(Freeze_Core *c, const Freeze_IO *io, uint32_t pairs, uint32_t vshct_us, bool ids)
{
    if (c->busy) return false;
    if (!io || !io->read16 || !io->write16 || !io->begin || !io->read || !io->poll ||
        !io->stop || !io->now || !io->adc_writes || !io->conflicts || !pairs || pairs > 100000U ||
        (vshct_us != 50U && vshct_us != 84U && vshct_us != 150U)) return false;
    memset(c, 0, sizeof(*c)); c->io = *io; c->busy = true; c->ids = ids;
    c->requested = pairs; c->vshct_us = vshct_us; c->outcome = FREEZE_DONE;
    c->started_us = io->now(io->ctx); c->write_base = io->adc_writes(io->ctx);
    c->owner_base = io->conflicts(io->ctx); c->frozen_write_base = c->write_base;
    if (!Read(c, REG_ADC, &c->saved_adc) || !Read(c, REG_DIAG, &c->saved_diag) ||
        !Read(c, REG_CONFIG, &c->config)) { Error(c, FREEZE_ERR_IO); return false; }
    if (ids) {
        if (!io->begin(io->ctx)) { Error(c, FREEZE_ERR_START); return false; }
        c->backend = true; c->phase = FREEZE_FIRST; return true;
    }
    /* Refuse an initial shutdown/triggered mode: restoring it would contradict
       the requirement to return this diagnostic to ordinary continuous mode. */
    if ((c->config & 0x10U) || (c->saved_adc >> 12) < 9U) {
        Error(c, FREEZE_ERR_CONFIG); return false;
    }
    c->diag_changed = true;
    if (!Write(c, REG_DIAG, (uint16_t)(c->saved_diag & 0x2000U))) {
        Error(c, FREEZE_ERR_IO); return false;
    }
    c->trigger_adc = (uint16_t)(0x2A28U | (vshct_us == 150U ? 0x80U : vshct_us == 84U ? 0x40U : 0U));
    c->adc_changed = true;
    if (!Write(c, REG_ADC, c->trigger_adc)) { Error(c, FREEZE_ERR_IO); return false; }
    c->trigger_us = io->now(io->ctx);
    c->timeout_us = (uint64_t)((c->config >> 6) & 255U) * 2000U + vshct_us + 2000U;
    c->next_poll_us = c->trigger_us; c->phase = FREEZE_WAIT_READY;
    return true;
}
void FreezeCore_Process(Freeze_Core *c)
{
    SpiDiag_Result result;
    uint32_t value, duration;
    uint8_t reg, length;
    uint64_t now;
    if (!c->busy || c->phase == FREEZE_RESTORE_FAILED) return;
    now = c->io.now(c->io.ctx);
    if (c->phase == FREEZE_WAIT_READY) {
        uint16_t diag;
        if (now - c->trigger_us > c->timeout_us) { Error(c, FREEZE_ERR_READY_TIMEOUT); return; }
        if (now < c->next_poll_us) return;
        c->next_poll_us = now + 50U;
        if (!Read(c, REG_DIAG, &diag)) { Error(c, FREEZE_ERR_IO); return; }
        if (!(diag & 2U)) return;
        c->completion_confirmed = true; c->ready_us = c->io.now(c->io.ctx);
        c->frozen_write_base = c->io.adc_writes(c->io.ctx);
        if (!c->io.begin(c->io.ctx)) { Error(c, FREEZE_ERR_START); return; }
        c->backend = true; c->phase = FREEZE_AUDIT_ADC;
    }
    if (!c->pending) {
        switch (c->phase) {
            case FREEZE_AUDIT_ADC: case FREEZE_END_ADC: reg = REG_ADC; length = 3U; break;
            case FREEZE_AUDIT_DIAG: case FREEZE_END_DIAG: reg = REG_DIAG; length = 3U; break;
            case FREEZE_REFERENCE: reg = REG_SHUNT; length = 4U; break;
            case FREEZE_FIRST: reg = c->ids ? REG_MANUF : REG_SHUNT; length = c->ids ? 3U : 4U; break;
            case FREEZE_SECOND: reg = c->ids ? REG_DEVICE : REG_SHUNT; length = c->ids ? 3U : 4U; break;
            default: Error(c, FREEZE_ERR_ARGUMENT); return;
        }
        if (!c->io.read(c->io.ctx, reg, length)) { ++c->spi_errors; Error(c, FREEZE_ERR_START); return; }
        c->pending = true; return;
    }
    if (!c->io.poll(c->io.ctx, &result)) return;
    c->pending = false; ++c->all_dma_reads;
    if (c->phase == FREEZE_FIRST || c->phase == FREEZE_SECOND) ++c->reads_total;
    if (result.started) {
        duration = result.end_us - result.start_us;
        if (duration > c->max_transaction_us) c->max_transaction_us = duration;
        if (c->have_previous_end) {
            duration = result.start_us - c->previous_end;
            if (duration > c->max_gap_us) c->max_gap_us = duration;
        }
        c->previous_end = result.end_us; c->have_previous_end = true;
    }
    if (result.status != SPI_DIAG_OK) {
        SpiDiag_Result empty = {0};
        ++c->spi_errors;
        if (result.status == SPI_DIAG_TIMEOUT) ++c->dma_timeouts; else ++c->dma_errors;
        if (c->phase == FREEZE_SECOND) Event(c, &c->first, &result);
        else Event(c, &result, &empty);
        Error(c, FREEZE_ERR_IO); return;
    }
    value = Value(&result);
    switch (c->phase) {
        case FREEZE_AUDIT_ADC:
            c->adc_start = (uint16_t)value;
            if (value != c->trigger_adc) { ++c->audit_errors; Error(c, FREEZE_ERR_AUDIT); return; }
            c->phase = FREEZE_AUDIT_DIAG; break;
        case FREEZE_AUDIT_DIAG:
            c->diag_start = (uint16_t)value;
            if (value & 2U) { ++c->audit_errors; Error(c, FREEZE_ERR_AUDIT); return; }
            c->phase = FREEZE_REFERENCE; break;
        case FREEZE_REFERENCE:
            c->reference = value; c->reference_valid = true;
            if (value & 15U) ++c->audit_errors;
            c->phase = FREEZE_FIRST; break;
        case FREEZE_FIRST: c->first = result; c->phase = FREEZE_SECOND; break;
        case FREEZE_SECOND:
            Pair(c, &result);
            c->phase = c->completed == c->requested ? FREEZE_END_ADC : FREEZE_FIRST; break;
        case FREEZE_END_ADC:
            c->adc_end = (uint16_t)value;
            if (value != (uint32_t)(c->ids ? c->saved_adc : c->trigger_adc)) ++c->audit_errors;
            if (c->ids) Finish(c); else c->phase = FREEZE_END_DIAG;
            break;
        case FREEZE_END_DIAG:
            c->diag_end = (uint16_t)value;
            if (value & 2U) ++c->audit_errors;
            Finish(c); break;
        default: Error(c, FREEZE_ERR_ARGUMENT); break;
    }
}
void FreezeCore_Stop(Freeze_Core *c, bool disconnected)
{
    if (!c->busy) return;
    if (c->phase != FREEZE_RESTORE_FAILED) {
        c->outcome = disconnected ? FREEZE_ERROR : FREEZE_STOPPED;
        c->error = disconnected ? FREEZE_ERR_DISCONNECT : FREEZE_ERR_NONE;
    }
    Finish(c);
}
