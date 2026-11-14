#ifndef ACQUISITION_ALERT_CORE_H
#define ACQUISITION_ALERT_CORE_H

#include <stdbool.h>
#include <stdint.h>

typedef enum { ALERT_IDLE, ALERT_PENDING, ALERT_WAITING, ALERT_READING } Alert_State;

typedef struct
{
    volatile Alert_State state;
    volatile uint32_t irq_count;
    volatile uint32_t min_period_us;
    volatile uint32_t max_period_us;
    volatile uint32_t last_irq_us;
    volatile uint32_t irq_times[8];
    volatile uint32_t missed_slots;
    volatile uint32_t spurious;
    volatile uint32_t timeouts;
    volatile uint32_t trigger_us;
    volatile uint32_t alert_us;
    volatile uint32_t tick_us;
    volatile uint32_t slot;
    volatile uint32_t max_trigger_to_alert_us;
    volatile uint32_t max_trigger_latency_us;
    volatile uint32_t max_alert_to_read_us;
    volatile uint32_t max_cycle_us;
    volatile uint32_t last_sample_irq_count;
    volatile uint32_t last_events_between_samples;
    volatile uint32_t max_events_between_samples;
} AcqAlert_Core;

void AcqAlertCore_Reset(AcqAlert_Core *core);
void AcqAlertCore_Tick(AcqAlert_Core *core, uint32_t slot, uint32_t tick_us);
void AcqAlertCore_Trigger(AcqAlert_Core *core, uint32_t now_us);
void AcqAlertCore_Irq(AcqAlert_Core *core, uint32_t now_us, bool pin_low,
                      bool single_shot);
bool AcqAlertCore_Read(AcqAlert_Core *core, uint32_t now_us);
bool AcqAlertCore_Timeout(AcqAlert_Core *core, uint32_t now_us,
                          uint32_t timeout_us);
void AcqAlertCore_Finish(AcqAlert_Core *core, uint32_t now_us);
void AcqAlertCore_Stop(AcqAlert_Core *core);
void AcqAlertCore_Sample(AcqAlert_Core *core);

#endif
