#ifndef NATIVE_CORE_H
#define NATIVE_CORE_H
#include <stdbool.h>
#include <stdint.h>
#define NATIVE_QUEUE_SIZE 16U
#define NATIVE_HIST_SIZE 256U
#define NATIVE_TRACE_SIZE 32U
typedef struct { uint32_t id, us; bool pin_low; } Native_Event;
typedef enum { NATIVE_IDLE, NATIVE_ACK, NATIVE_READ } Native_State;
typedef struct {
    volatile bool active;
    volatile uint32_t irq_count, head, tail, queue_drops, while_busy, gpio_high;
    volatile Native_State state;
    Native_Event queue[NATIVE_QUEUE_SIZE], current;
    uint32_t conversion_us, confirmed, not_ready, samples, uncertain, lost;
    uint32_t primary_reads;
    uint32_t stop_discarded;
    uint32_t duplicate_events, invalid_callbacks, dma_failures, rearm_failures;
    uint32_t last_event_us; bool have_last_event, committed;
    uint32_t start_us, first_us, start_events, end_events;
    uint32_t min_age, max_age, max_cycle, timer_lost, overbudget_cycles, start_failed;
    uint32_t histogram[NATIVE_HIST_SIZE];
    uint32_t trace_count;
    uint32_t ack_start, last_ack_end, zero_before_rearm, service_count;
    struct { uint32_t id, alert, start, end, ready, previous_end, pin_low; } service[32];
    struct { uint32_t sequence, event_id, alert, start, end, age, events_start, events_end, flags; }
        trace[NATIVE_TRACE_SIZE];
} Native_Core;
void NativeCore_Start(Native_Core *c, uint32_t conversion_us);
void NativeCore_Irq(Native_Core *c, uint32_t stamp, bool pin_low);
bool NativeCore_Take(Native_Core *c);
bool NativeCore_Ready(Native_Core *c, bool ready, uint32_t *sequence);
bool NativeCore_ReadyAt(Native_Core *c, bool ready, uint32_t diag_end, uint32_t *sequence);
void NativeCore_ReadStart(Native_Core *c, uint32_t stamp);
void NativeCore_Primary(Native_Core *c, uint32_t start, uint32_t end, uint32_t events_end);
void NativeCore_Accept(Native_Core *c);
void NativeCore_Complete(Native_Core *c, uint32_t finish);
void NativeCore_Failed(Native_Core *c, uint32_t finish);
void NativeCore_Stop(Native_Core *c);
void NativeCore_StopAt(Native_Core *c, uint32_t stamp);
uint32_t NativeCore_P99(const Native_Core *c);
#endif
