#include "pair_trace_core.h"
#include <string.h>
#if defined(_MSC_VER)
#include <intrin.h>
#define BARRIER() _ReadWriteBarrier()
#elif defined(__GNUC__)
#define BARRIER() __asm__ volatile("" ::: "memory")
#else
#define BARRIER() ((void)0)
#endif
uint32_t PairTrace_Raw(const SpiDiag_Result *r)
{ return ((uint32_t)r->rx[1] << 16) | ((uint32_t)r->rx[2] << 8) | r->rx[3]; }
int32_t PairTrace_Code(uint32_t raw)
{
    uint32_t code = (raw >> 4) & 0xFFFFFU;
    return (code & 0x80000U) ? (int32_t)code - 1048576L : (int32_t)code;
}
void PairTrace_Start(Pair_Trace *c, uint32_t conversion, uint16_t adc, uint16_t diag)
{
    memset(c, 0, sizeof(*c)); c->conversion_us = conversion;
    c->adc_config = adc; c->diag_control = diag; c->active = true;
}
void PairTrace_Irq(Pair_Trace *c, uint32_t us, bool low)
{
    uint32_t id;
    Pair_Edge *slot;
    if (!c->active) return;
    id = c->irq_count + 1U;
    slot = &c->history[(id - 1U) & (PAIR_EDGE_HISTORY - 1U)];
    slot->id = 0U; BARRIER(); slot->us = us; slot->low = low;
    BARRIER(); slot->id = id;
    BARRIER(); c->irq_count = id;
}
void PairTrace_Begin(Pair_Trace *c, uint32_t seq, uint32_t irq, uint32_t alert)
{
    if (!c->active) return;
    if (c->in_pair) ++c->stopped_pairs;
    memset(&c->current, 0, sizeof(c->current));
    c->current.seq = seq; c->current.irq_id = irq; c->current.alert_us = alert;
    c->current.flags = PAIR_UNCERTAIN; c->in_pair = true;
}
void PairTrace_Transfer(Pair_Trace *c, uint32_t phase, const SpiDiag_Result *r)
{
    if (!c->active || !c->in_pair || phase > 2U) return;
    c->current.transfer[phase] = *r;
    if (r->started && r->status == SPI_DIAG_OK && r->length == (phase == 0U ? 3U : 4U))
        c->current.valid_mask |= (uint8_t)(1U << phase);
    if (r->started && r->status == SPI_DIAG_OK && r->length == 4U) {
        if (phase == 1U) c->current.raw1 = PairTrace_Raw(r);
        if (phase == 2U) c->current.raw2 = PairTrace_Raw(r);
    }
}
static void Evidence(Pair_Trace *c)
{
    Pair_Event *e = &c->current;
    const SpiDiag_Result *a = &e->transfer[1], *b = &e->transfer[2];
    uint32_t count, n;
    e->new_edges = b->edge_end - a->edge_start;
    e->gap_edges = b->edge_start - a->edge_end;
    if (e->new_edges) e->flags |= PAIR_NEW_IRQ;
    if (c->conversion_us &&
        ((a->start_us - e->alert_us) / c->conversion_us != (b->end_us - e->alert_us) / c->conversion_us ||
         (a->start_us - e->alert_us) % c->conversion_us == 0U)) e->flags |= PAIR_NOMINAL_BOUNDARY;
    if (a->end_us - a->start_us >= 0x80000000U || b->start_us - a->end_us >= 0x80000000U ||
        b->end_us - b->start_us >= 0x80000000U || a->start_us - e->alert_us >= 0x80000000U)
        e->flags |= PAIR_TIME_ORDER_ERROR;
    count = e->new_edges;
    if (count > PAIR_EDGE_HISTORY) { e->history_missing = count - PAIR_EDGE_HISTORY; count = PAIR_EDGE_HISTORY; }
    /* No long IRQ masking: producer is lower-priority EXTI in Native callbacks.
       In the foreground experiment validate each ring entry across the copy. */
    for (n = 0U; n < count; ++n) {
        uint32_t id = b->edge_end - count + n + 1U;
        const Pair_Edge *source = &c->history[(id - 1U) & (PAIR_EDGE_HISTORY - 1U)];
        Pair_Edge edge = *source;
        BARRIER();
        if (edge.id != id || source->id != id) { ++e->history_missing; continue; }
        if (e->edge_count < 4U) e->edges[e->edge_count++] = edge;
        else ++e->history_missing;
    }
    if (e->history_missing) e->flags |= PAIR_HISTORY_MISSING;
}
bool PairTrace_Finish(Pair_Trace *c, uint32_t raw1, uint32_t raw2)
{
    Pair_Event *e = &c->current;
    uint32_t values[4], i;
    bool different;
    if (!c->active || !c->in_pair) return false;
    if (e->valid_mask != 7U) {
        ++c->capture_errors;
        if (c->failure_count < 4U) c->failures[c->failure_count++] = *e;
        else ++c->failures_not_stored;
        c->in_pair = false; return false;
    }
    e->raw1 = raw1; e->raw2 = raw2; different = raw1 != raw2;
    Evidence(c);
    if (e->flags & PAIR_TIME_ORDER_ERROR) ++c->time_order_errors;
    if (raw1 != PairTrace_Raw(&e->transfer[1]) || raw2 != PairTrace_Raw(&e->transfer[2])) {
        ++c->raw_rx_errors; e->flags |= PAIR_RAW_RX_ERROR;
    }
    ++c->total;
    if (different) ++c->mismatch; else ++c->match;
    values[0] = e->transfer[1].start_us - e->alert_us;
    values[1] = e->transfer[1].end_us - e->alert_us;
    values[2] = e->transfer[2].start_us - e->transfer[1].end_us;
    values[3] = e->transfer[2].end_us - e->alert_us;
    for (i = 0U; i < 4U; ++i) {
        uint32_t bin = values[i] / 10U;
        if (bin >= PAIR_PHASE_BINS) bin = PAIR_PHASE_BINS - 1U;
        ++c->bins[i][bin][different ? 1U : 0U];
    }
    if (different || (e->flags & (PAIR_RAW_RX_ERROR | PAIR_TIME_ORDER_ERROR))) {
        /* Preserve the first sixteen RAW disagreements specifically. Software
           invariant failures without a RAW disagreement use the failure trace. */
        if (different) {
            if (c->event_count < PAIR_TRACE_CAPACITY) c->events[c->event_count++] = *e;
            else ++c->events_not_stored;
        }
        else if (c->failure_count < 4U) c->failures[c->failure_count++] = *e;
        else ++c->failures_not_stored;
    }
    c->in_pair = false; return different;
}
void PairTrace_Fail(Pair_Trace *c, uint32_t phase, const SpiDiag_Result *r)
{
    if (!c->active || !c->in_pair) return;
    PairTrace_Transfer(c, phase, r); c->current.failed_phase = (uint8_t)(phase + 1U);
    ++c->spi_errors;
    if (r->status == SPI_DIAG_TIMEOUT) ++c->dma_timeouts;
    if (c->failure_count < 4U) c->failures[c->failure_count++] = c->current;
    else ++c->failures_not_stored;
    c->in_pair = false;
}
void PairTrace_Stop(Pair_Trace *c)
{
    c->active = false;
    if (c->in_pair) {
        ++c->stopped_pairs;
        if (c->failure_count < 4U) c->failures[c->failure_count++] = c->current;
        else ++c->failures_not_stored;
    }
    c->in_pair = false;
}
