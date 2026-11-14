#ifndef ALERT_DRIVEN_CORE_H
#define ALERT_DRIVEN_CORE_H
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    volatile uint32_t irq_count, event_us;
    uint32_t handled, event_time, ready, rearmed, rearm_failures;
    uint32_t skipped, overruns, selected, missed_slots;
    uint32_t max_start_us, max_complete_us, max_cycle_us;
    uint64_t start_us64, event_elapsed, next_slot;
    uint32_t period_us;
    uint32_t timer_lost;
    uint32_t already_used, wrong_slot, while_dma, expired, start_failed;
    uint32_t future_snapshots, accepted, last_read_us, last_rearm_us, zero_before_rearm;
    uint64_t reserved_slot;
    bool reserved;
    uint32_t trace_count;
    struct { uint32_t timestamp, slot, state, reason; } trace[32];
    uint32_t service_count, confirmation_slot_differences;
    struct { uint32_t irq_us, diag_us, irq_slot, confirm_slot, ready, selected, prior_read; }
        service[32];
} AlertDriven_Core;
void AlertDriven_Start(AlertDriven_Core *c, uint64_t start, uint32_t period);
void AlertDriven_Irq(AlertDriven_Core *c, uint32_t timestamp);
bool AlertDriven_Take(AlertDriven_Core *c, uint64_t now);
bool AlertDriven_Ready(AlertDriven_Core *c, bool ready, uint32_t *slot);
bool AlertDriven_ReadyAt(AlertDriven_Core *c, bool ready, uint32_t diag_us, uint32_t *slot);
void AlertDriven_Accept(AlertDriven_Core *c, uint32_t first_read_us);
void AlertDriven_Finish(AlertDriven_Core *c, uint64_t stopped);
void AlertDriven_Expired(AlertDriven_Core *c);
void AlertDriven_Failed(AlertDriven_Core *c, bool start_failed);
#endif
