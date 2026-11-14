#include "acquisition_alert_core.h"

#include <string.h>

void AcqAlertCore_Reset(AcqAlert_Core *core)
{
    memset(core, 0, sizeof(*core));
    core->state = ALERT_IDLE;
}

void AcqAlertCore_Tick(AcqAlert_Core *core, uint32_t slot, uint32_t tick_us)
{
    if (core->state == ALERT_IDLE)
    {
        core->slot = slot;
        core->tick_us = tick_us;
        core->state = ALERT_PENDING;
    }
    else ++core->missed_slots;
}

void AcqAlertCore_Trigger(AcqAlert_Core *core, uint32_t now_us)
{
    uint32_t latency;
    if (core->state != ALERT_PENDING) return;
    latency = now_us - core->tick_us;
    if (latency > core->max_trigger_latency_us)
        core->max_trigger_latency_us = latency;
    core->trigger_us = now_us;
    core->state = ALERT_WAITING;
}

void AcqAlertCore_Irq(AcqAlert_Core *core, uint32_t now_us, bool pin_low,
                      bool single_shot)
{
    uint32_t count = core->irq_count;
    uint32_t period;
    if (!pin_low) { ++core->spurious; return; }
    if (count != 0U)
    {
        period = now_us - core->last_irq_us;
        if (core->min_period_us == 0U || period < core->min_period_us)
            core->min_period_us = period;
        if (period > core->max_period_us) core->max_period_us = period;
    }
    core->last_irq_us = now_us;
    core->irq_times[count & 7U] = now_us;
    core->irq_count = count + 1U;
    if (single_shot)
    {
        if (core->state == ALERT_WAITING)
        {
            uint32_t elapsed = now_us - core->trigger_us;
            core->alert_us = now_us;
            core->state = ALERT_READING;
            if (elapsed > core->max_trigger_to_alert_us)
                core->max_trigger_to_alert_us = elapsed;
        }
        else ++core->spurious;
    }
}

bool AcqAlertCore_Read(AcqAlert_Core *core, uint32_t now_us)
{
    uint32_t elapsed;
    if (core->state != ALERT_READING) return false;
    elapsed = now_us - core->alert_us;
    if (elapsed > core->max_alert_to_read_us)
        core->max_alert_to_read_us = elapsed;
    return true;
}

bool AcqAlertCore_Timeout(AcqAlert_Core *core, uint32_t now_us,
                          uint32_t timeout_us)
{
    if (core->state != ALERT_WAITING ||
        (uint32_t)(now_us - core->trigger_us) <= timeout_us) return false;
    ++core->timeouts;
    core->state = ALERT_IDLE;
    return true;
}

void AcqAlertCore_Finish(AcqAlert_Core *core, uint32_t now_us)
{
    uint32_t elapsed = now_us - core->tick_us;
    if (elapsed > core->max_cycle_us) core->max_cycle_us = elapsed;
    core->state = ALERT_IDLE;
}

void AcqAlertCore_Stop(AcqAlert_Core *core) { core->state = ALERT_IDLE; }

void AcqAlertCore_Sample(AcqAlert_Core *core)
{
    uint32_t count = core->irq_count;
    uint32_t events = count - core->last_sample_irq_count;
    core->last_sample_irq_count = count;
    core->last_events_between_samples = events;
    if (events > core->max_events_between_samples)
        core->max_events_between_samples = events;
}
