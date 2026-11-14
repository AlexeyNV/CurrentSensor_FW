#include "pair_experiment_core.h"
#include <string.h>
static bool Read16(Pair_Experiment *p, uint8_t reg, uint16_t *value)
{
    p->last_status = p->io.read16(p->io.ctx, reg, value);
    if (p->last_status) ++p->io_errors;
    return p->last_status == 0;
}
static bool Write16(Pair_Experiment *p, uint8_t reg, uint16_t value)
{
    p->last_status = p->io.write16(p->io.ctx, reg, value);
    if (p->last_status) ++p->io_errors;
    return p->last_status == 0;
}
static void Finish(Pair_Experiment *p)
{
    uint16_t adc, diag;
    if (!p->stopped_trace) {
        p->stopped_trace = true; PairTrace_Stop(p->trace); p->end_us = p->io.now(p->io.ctx);
    }
    if (p->backend) {
        if (!p->io.stop(p->io.ctx)) { ++p->restore_errors; p->state = PAIR_EXP_RESTORE_FAILED; return; }
        p->backend = false; p->pending = false;
    }
    if (p->changed) {
        bool diag_ok = Write16(p, 11U, p->saved_diag & 0xF000U);
        bool adc_ok = Write16(p, 1U, p->saved_adc);
        if (!diag_ok || !adc_ok || !Read16(p, 1U, &adc) || adc != p->saved_adc ||
            !Read16(p, 11U, &diag) || (diag & 0xF000U) != (p->saved_diag & 0xF000U)) {
            ++p->restore_errors; p->state = PAIR_EXP_RESTORE_FAILED; return;
        }
        p->changed = false;
    }
    p->busy = false; p->state = p->outcome;
}
static void Error(Pair_Experiment *p) { p->outcome = PAIR_EXP_ERROR; Finish(p); }
static void Timeout(Pair_Experiment *p)
{
    Pair_WaitContext *c = &p->wait_context;
    uint32_t id = p->trace->irq_count;
    c->valid = 1U; c->state = p->state; c->previous = p->previous;
    c->stamp = (uint32_t)p->io.now(p->io.ctx); c->irq_count = id;
    c->last_irq_us = id ? p->trace->history[(id - 1U) & (PAIR_EDGE_HISTORY - 1U)].us : 0U;
    c->last_diag_us = p->last_diag_us; c->cnvrf = p->last_cnvrf;
    c->busy_edges = p->busy_edges; c->skipped_edges = p->skipped_edges; c->pairs = p->trace->total;
    if (p->capture_context) p->capture_context(c);
    ++p->wait_timeouts; Error(p);
}
static void WaitEdge(Pair_Experiment *p)
{
    p->previous = p->state;
    p->state = PAIR_EXP_EDGE;
    /* Baseline was latched BEFORE the acknowledgement request. Keep edges
       arriving after ACK, including before foreground completion processing. */
    p->edge_deadline = p->io.now(p->io.ctx) + p->trace->conversion_us * 8U + 2000U;
}
bool PairExperiment_Start(Pair_Experiment *p, Pair_Trace *trace, const Freeze_IO *io,
    uint32_t count, uint32_t ct, uint32_t first, uint32_t gap, bool latch)
{
    uint16_t config, check;
    if (p->busy || !trace || !io || !io->read16 || !io->write16 || !io->begin ||
        !io->read || !io->poll || !io->stop || !io->now || !count || count > 100000U ||
        (ct != 84U && ct != 150U) || first > 500U || gap > 500U) return false;
    memset(p, 0, sizeof(*p)); p->io = *io; p->trace = trace; p->busy = true;
    p->count = count; p->first_delay = first; p->gap_delay = gap;
    p->outcome = PAIR_EXP_DONE; p->start_us = io->now(io->ctx);
    PairTrace_Start(trace, ct, 0U, 0U); trace->active = false;
    p->limit_us = (uint64_t)(count + 1U) * (first + gap + ct * 3U + 1000U);
    if (!Read16(p, 0U, &config) || !Read16(p, 1U, &p->saved_adc) ||
        !Read16(p, 11U, &p->saved_diag)) { Error(p); return false; }
    if ((config & 0x10U) || (p->saved_adc >> 12) < 9U) { Error(p); return false; }
    p->adc = (uint16_t)(ct == 84U ? 0xAA68U : 0xAAA8U);
    p->diag = (uint16_t)((p->saved_diag & 0x2000U) | 0x4000U | (latch ? 0x8000U : 0U));
    /* Mark before writes: an unsuccessful transfer may still have changed INA. */
    p->changed = true;
    if (!Write16(p, 11U, p->diag) || !Write16(p, 1U, p->adc) ||
        !Read16(p, 1U, &check) || check != p->adc || !Read16(p, 11U, &check) ||
        (check & 0xF000U) != p->diag) { Error(p); return false; }
    trace->adc_config = p->adc; trace->diag_control = p->diag; trace->active = true;
    if (!io->begin(io->ctx)) { Error(p); return false; }
    p->backend = true; p->state = PAIR_EXP_BOOT; return true;
}
void PairExperiment_Process(Pair_Experiment *p)
{
    SpiDiag_Result r;
    uint64_t now;
    uint32_t phase = 0U;
    if (!p->busy || p->state == PAIR_EXP_RESTORE_FAILED) return;
    now = p->io.now(p->io.ctx);
    if (now - p->start_us > p->limit_us) { Timeout(p); return; }
    if (p->state == PAIR_EXP_EDGE) {
        uint32_t id = p->trace->irq_count;
        Pair_Edge edge;
        if (id == p->last_edge) {
            if (now > p->edge_deadline) Timeout(p);
            return;
        }
        edge = p->trace->history[(id - 1U) & (PAIR_EDGE_HISTORY - 1U)];
        if (edge.id != id || p->trace->irq_count != id) return;
        p->skipped_edges += id - p->last_edge - 1U;
        p->last_edge = id;
        PairTrace_Begin(p->trace, p->trace->total, id, edge.us);
        p->previous = p->state;
        p->state = PAIR_EXP_DIAG;
    }
    if (p->state == PAIR_EXP_DELAY_FIRST || p->state == PAIR_EXP_DELAY_SECOND) {
        uint32_t origin = p->state == PAIR_EXP_DELAY_FIRST ? p->trace->current.alert_us :
            p->trace->current.transfer[1].end_us;
        uint32_t delay = p->state == PAIR_EXP_DELAY_FIRST ? p->first_delay : p->gap_delay;
        if ((uint32_t)now - origin < delay) return;
        p->previous = p->state;
        p->state = p->state == PAIR_EXP_DELAY_FIRST ? PAIR_EXP_FIRST : PAIR_EXP_SECOND;
    }
    if (p->state == PAIR_EXP_FIRST) phase = 1U;
    if (p->state == PAIR_EXP_SECOND) phase = 2U;
    if (!p->pending) {
        uint8_t reg = phase == 0U ? 11U : 4U;
        if (p->state == PAIR_EXP_BOOT || p->state == PAIR_EXP_REARM) {
            uint32_t latest = p->trace->irq_count;
            p->busy_edges += latest - p->last_edge; p->last_edge = latest;
            ++p->rearm_reads;
        }
        if (!p->io.read(p->io.ctx, reg, phase == 0U ? 3U : 4U)) {
            ++p->io_errors; Error(p); return;
        }
        p->pending = true; return;
    }
    if (!p->io.poll(p->io.ctx, &r)) return;
    p->pending = false;
    if (r.status != SPI_DIAG_OK) {
        ++p->dma_errors;
        if (r.status == SPI_DIAG_TIMEOUT) ++p->dma_timeouts;
        if (!p->trace->in_pair) PairTrace_Begin(p->trace, UINT32_MAX, 0U, 0U);
        PairTrace_Fail(p->trace, phase, &r); Error(p); return;
    }
    if (phase == 0U) {
        p->last_diag_us = r.end_us; p->last_cnvrf = (r.rx[2] & 2U) != 0U;
    }
    if (p->state == PAIR_EXP_BOOT || p->state == PAIR_EXP_REARM) { WaitEdge(p); return; }
    PairTrace_Transfer(p->trace, phase, &r);
    if (phase == 0U) {
        uint16_t diag = (uint16_t)(((uint16_t)r.rx[1] << 8) | r.rx[2]);
        if (!(diag & 2U)) {
            ++p->not_ready; p->trace->in_pair = false; WaitEdge(p); return;
        }
        p->previous = p->state; p->state = PAIR_EXP_DELAY_FIRST;
    }
    else if (phase == 1U) { p->previous = p->state; p->state = PAIR_EXP_DELAY_SECOND; }
    else {
        (void)PairTrace_Finish(p->trace, PairTrace_Raw(&p->trace->current.transfer[1]), PairTrace_Raw(&r));
        if (p->trace->total == p->count) Finish(p);
        else { p->previous = p->state; p->state = PAIR_EXP_REARM; }
    }
}
void PairExperiment_Stop(Pair_Experiment *p, bool error)
{
    if (!p->busy) return;
    if (p->state != PAIR_EXP_RESTORE_FAILED) p->outcome = error ? PAIR_EXP_ERROR : PAIR_EXP_STOPPED;
    Finish(p);
}
