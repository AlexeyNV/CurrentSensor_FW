#include "native_core.h"
#include <string.h>
#if defined(_MSC_VER)
#include <intrin.h>
#define NATIVE_BARRIER() _ReadWriteBarrier()
#elif defined(__GNUC__)
#define NATIVE_BARRIER() __asm__ volatile("" ::: "memory")
#else
#define NATIVE_BARRIER() ((void)0)
#endif
void NativeCore_Start(Native_Core *c, uint32_t conversion_us)
{
    memset(c, 0, sizeof(*c)); c->conversion_us = conversion_us; c->active = true;
}
void NativeCore_Irq(Native_Core *c, uint32_t stamp, bool pin_low)
{
    uint32_t head, id;
    if (!c->active) return;
    id = ++c->irq_count;
    if (!pin_low) ++c->gpio_high;
    if (c->state != NATIVE_IDLE) ++c->while_busy;
    head = c->head;
    if (head - c->tail >= NATIVE_QUEUE_SIZE) { ++c->queue_drops; return; }
    c->queue[head & (NATIVE_QUEUE_SIZE - 1U)] = (Native_Event){id, stamp, pin_low};
    NATIVE_BARRIER(); c->head = head + 1U;
}
bool NativeCore_Take(Native_Core *c)
{
    uint32_t tail = c->tail;
    if (!c->active || c->state != NATIVE_IDLE || tail == c->head) return false;
    NATIVE_BARRIER(); c->current = c->queue[tail & (NATIVE_QUEUE_SIZE - 1U)];
    NATIVE_BARRIER(); c->tail = tail + 1U;
    c->committed = false; c->first_us = 0U; c->state = NATIVE_ACK;
    return true;
}
bool NativeCore_Ready(Native_Core *c, bool ready, uint32_t *sequence)
{
    bool duplicate;
    if (!c->active) return false;
    if (c->state != NATIVE_ACK) { ++c->invalid_callbacks; return false; }
    duplicate = c->have_last_event && (c->current.us == c->last_event_us ||
                    c->current.us - c->last_event_us >= 0x80000000U);
    c->have_last_event = true; c->last_event_us = c->current.us;
    if (!ready) { ++c->not_ready; c->state = NATIVE_IDLE; return false; }
    ++c->confirmed;
    if (duplicate) { ++c->duplicate_events; ++c->lost; c->state = NATIVE_IDLE; return false; }
    *sequence = c->samples; /* accepted-result numbering, independent of IRQ/TIM3 */
    c->state = NATIVE_READ;
    return true;
}
void NativeCore_ReadStart(Native_Core *c, uint32_t stamp)
{ if (c->active) { c->start_us = stamp; c->start_events = c->irq_count; } }
bool NativeCore_ReadyAt(Native_Core *c, bool ready, uint32_t diag_end, uint32_t *sequence)
{
    uint32_t i = c->service_count;
    if (!c->active) return false;
    if (!ready && c->last_ack_end != 0U &&
        (c->current.us == c->last_ack_end || c->current.us - c->last_ack_end >= 0x80000000U))
        ++c->zero_before_rearm;
    if (i < 32U) {
        c->service[i].id = c->current.id; c->service[i].alert = c->current.us;
        c->service[i].start = c->ack_start; c->service[i].end = diag_end;
        c->service[i].ready = ready; c->service[i].previous_end = c->last_ack_end;
        c->service[i].pin_low = c->current.pin_low;
        c->service_count = i + 1U;
    }
    c->last_ack_end = diag_end;
    {
        bool selected = NativeCore_Ready(c, ready, sequence);
        if (!selected && c->active) NativeCore_Complete(c, diag_end);
        return selected;
    }
}
void NativeCore_Primary(Native_Core *c, uint32_t start, uint32_t end, uint32_t events_end)
{
    if (!c->active) return;
    c->start_us = start; c->first_us = end; c->end_events = events_end; ++c->primary_reads;
}
void NativeCore_Accept(Native_Core *c)
{
    uint32_t age, flags, n;
    if (!c->active) return;
    if (c->state != NATIVE_READ || c->committed) {
        ++c->invalid_callbacks; return;
    }
    c->committed = true;
    age = c->first_us - c->current.us;
    if (c->samples == 0U || age < c->min_age) c->min_age = age;
    if (age > c->max_age) c->max_age = age;
    ++c->histogram[age < NATIVE_HIST_SIZE - 1U ? age : NATIVE_HIST_SIZE - 1U];
    flags = 1U; /* association/atomicity is not established by the datasheet */
    if (age >= c->conversion_us) flags |= 2U;
    if (c->end_events != c->start_events) flags |= 4U;
    if (!c->current.pin_low) flags |= 8U;
    if (c->head != c->tail) flags |= 16U;
    n = c->trace_count;
    if (n < NATIVE_TRACE_SIZE) {
        c->trace[n].sequence = c->samples; c->trace[n].event_id = c->current.id;
        c->trace[n].alert = c->current.us; c->trace[n].start = c->start_us;
        c->trace[n].end = c->first_us; c->trace[n].age = age;
        c->trace[n].events_start = c->start_events; c->trace[n].events_end = c->end_events;
        c->trace[n].flags = flags; c->trace_count = n + 1U;
    }
    ++c->samples; ++c->uncertain;
}
void NativeCore_Complete(Native_Core *c, uint32_t finish)
{
    uint32_t cycle = finish - c->current.us;
    if (!c->active) return;
    if (cycle > c->max_cycle) c->max_cycle = cycle;
    if (cycle >= c->conversion_us) ++c->overbudget_cycles;
    c->state = NATIVE_IDLE;
}
void NativeCore_Failed(Native_Core *c, uint32_t finish)
{
    if (!c->active) return;
    ++c->dma_failures;
    if (c->state != NATIVE_IDLE && !c->committed) ++c->lost;
    NativeCore_Complete(c, finish);
}
void NativeCore_Stop(Native_Core *c)
{
    uint32_t discarded;
    c->active = false;
    discarded = c->head - c->tail;
    if (c->state != NATIVE_IDLE && !c->committed) ++discarded;
    c->lost += discarded; c->stop_discarded += discarded;
    c->tail = c->head; c->state = NATIVE_IDLE;
}
void NativeCore_StopAt(Native_Core *c, uint32_t stamp)
{
    if (c->active && c->state != NATIVE_IDLE) {
        uint32_t cycle = stamp - c->current.us;
        if (cycle > c->max_cycle) c->max_cycle = cycle;
        if (cycle >= c->conversion_us) ++c->overbudget_cycles;
    }
    NativeCore_Stop(c);
}
uint32_t NativeCore_P99(const Native_Core *c)
{
    uint32_t target, cumulative = 0U, i;
    if (c->samples == 0U) return 0U;
    target = (uint32_t)(((uint64_t)c->samples * 99U + 99U) / 100U);
    for (i = 0U; i < NATIVE_HIST_SIZE; ++i) {
        cumulative += c->histogram[i];
        if (cumulative >= target) return i == NATIVE_HIST_SIZE - 1U ? c->max_age : i;
    }
    return c->max_age;
}
