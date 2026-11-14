#include "alert_driven_core.h"
#include <string.h>
#include <limits.h>
static void Trace(AlertDriven_Core *c, uint32_t reason)
{
    if (c->trace_count < 32U) {
        uint32_t i = c->trace_count++;
        c->trace[i].timestamp = c->event_time;
        c->trace[i].slot = (uint32_t)(c->event_elapsed / c->period_us);
        c->trace[i].state = c->reserved ? 2U : 1U;
        c->trace[i].reason = reason;
    }
}
static uint32_t Bounded(uint64_t value)
{ return value > UINT32_MAX ? UINT32_MAX : (uint32_t)value; }
void AlertDriven_Start(AlertDriven_Core *c, uint64_t start, uint32_t period)
{ memset(c, 0, sizeof(*c)); c->start_us64 = start; c->period_us = period; }
void AlertDriven_Irq(AlertDriven_Core *c, uint32_t timestamp)
{ c->event_us = timestamp; ++c->irq_count; }
bool AlertDriven_Take(AlertDriven_Core *c, uint64_t now)
{
    uint32_t before, stamp, after, age;
    uint64_t event64;
    do { before = c->irq_count; stamp = c->event_us; after = c->irq_count; }
    while (before != after);
    if (after == c->handled) return false;
    age = (uint32_t)now - stamp;
    /* The IRQ may arrive after the caller sampled now64. Retry with a newer
       clock without consuming the mailbox or projecting it one wrap back. */
    if (age >= 0x80000000U || now < age) { ++c->future_snapshots; return false; }
    event64 = now - age;
    c->overruns += after - c->handled - 1U;
    c->handled = after; c->event_time = stamp;
    if (event64 < c->start_us64) { ++c->wrong_slot; return false; }
    c->event_elapsed = event64 - c->start_us64;
    return true;
}
bool AlertDriven_Ready(AlertDriven_Core *c, bool ready, uint32_t *slot)
{
    uint64_t candidate;
    ++c->rearmed;
    if (!ready) {
        ++c->already_used;
        if (c->last_rearm_us != 0U && c->event_time - c->last_rearm_us >= 0x80000000U)
            ++c->zero_before_rearm;
        return false; /* CNVRF=0 is not an SPI rearm failure or a lost slot. */
    }
    ++c->ready;
    candidate = c->event_elapsed / c->period_us;
    if (candidate < c->next_slot) { ++c->skipped; ++c->wrong_slot; Trace(c, 2U); return false; }
    if (c->accepted != 0U && (c->event_time == c->last_read_us ||
                            c->event_time - c->last_read_us >= 0x80000000U))
    { ++c->already_used; Trace(c, 1U); return false; }
    if (c->reserved) { ++c->while_dma; Trace(c, 3U); return false; }
    c->reserved_slot = candidate; c->reserved = true;
    *slot = (uint32_t)candidate; ++c->selected;
    return true;
}
void AlertDriven_Accept(AlertDriven_Core *c, uint32_t first_read_us)
{
    if (!c->reserved) return;
    c->next_slot = c->reserved_slot + 1U;
    ++c->accepted; c->last_read_us = first_read_us; c->reserved = false;
    c->missed_slots = Bounded(c->next_slot - c->accepted);
}
bool AlertDriven_ReadyAt(AlertDriven_Core *c, bool ready, uint32_t diag_us, uint32_t *slot)
{
    uint32_t event_slot = (uint32_t)(c->event_elapsed / c->period_us);
    uint32_t confirm_slot = (uint32_t)((c->event_elapsed +
                                      (uint32_t)(diag_us - c->event_time)) / c->period_us);
    bool selected = AlertDriven_Ready(c, ready, slot);
    if (ready && event_slot != confirm_slot) ++c->confirmation_slot_differences;
    if (c->service_count < 32U) {
        uint32_t i = c->service_count++;
        c->service[i].irq_us = c->event_time; c->service[i].diag_us = diag_us;
        c->service[i].irq_slot = event_slot; c->service[i].confirm_slot = confirm_slot;
        c->service[i].ready = ready; c->service[i].selected = selected;
        c->service[i].prior_read = c->last_read_us;
    }
    c->last_rearm_us = diag_us;
    return selected;
}
void AlertDriven_Finish(AlertDriven_Core *c, uint64_t stopped)
{
    uint64_t elapsed = stopped >= c->start_us64 ? stopped - c->start_us64 : 0U;
    uint64_t bins = elapsed / c->period_us + (elapsed % c->period_us != 0U);
    c->reserved = false;
    c->missed_slots = bins > c->accepted ? Bounded(bins - c->accepted) : 0U;
}
void AlertDriven_Expired(AlertDriven_Core *c)
{ ++c->expired; } /* latency observation: retain RAW; do not fill rejection trace */
void AlertDriven_Failed(AlertDriven_Core *c, bool start_failed)
{
    if (start_failed) ++c->start_failed;
    if (c->reserved) Trace(c, 5U);
    c->reserved = false; /* allow a later ready event to retry this slot */
}
