#ifndef PAIR_TRACE_CORE_H
#define PAIR_TRACE_CORE_H
#include "spi_dma_diagnostic.h"
#define PAIR_TRACE_CAPACITY 16U
#define PAIR_EDGE_HISTORY 32U
#define PAIR_PHASE_BINS 32U
typedef struct { uint32_t id, us; bool low; } Pair_Edge;
typedef struct {
    uint32_t seq, irq_id, alert_us, raw1, raw2, flags;
    SpiDiag_Result transfer[3]; /* DIAG, first VSHUNT, second VSHUNT */
    uint32_t new_edges, gap_edges, edge_count, history_missing;
    Pair_Edge edges[4];
    uint8_t valid_mask, failed_phase;
} Pair_Event;
enum {
    PAIR_UNCERTAIN = 1U, PAIR_NEW_IRQ = 2U, PAIR_NOMINAL_BOUNDARY = 4U,
    PAIR_HISTORY_MISSING = 8U, PAIR_RAW_RX_ERROR = 16U, PAIR_TIME_ORDER_ERROR = 32U
};
typedef struct {
    volatile bool active;
    bool in_pair;
    volatile uint32_t irq_count;
    Pair_Edge history[PAIR_EDGE_HISTORY];
    Pair_Event current, events[PAIR_TRACE_CAPACITY], failures[4];
    uint32_t event_count, events_not_stored, failure_count, failures_not_stored;
    uint32_t total, match, mismatch, raw_rx_errors, spi_errors, dma_timeouts, stopped_pairs;
    uint32_t capture_errors, time_order_errors;
    uint32_t conversion_us, adc_config, diag_control;
    uint32_t bins[4][PAIR_PHASE_BINS][2]; /* metric, 10 us bins (last overflow), match/mismatch */
} Pair_Trace;
void PairTrace_Start(Pair_Trace *c, uint32_t conversion, uint16_t adc, uint16_t diag);
void PairTrace_Irq(Pair_Trace *c, uint32_t us, bool low);
void PairTrace_Begin(Pair_Trace *c, uint32_t seq, uint32_t irq, uint32_t alert);
void PairTrace_Transfer(Pair_Trace *c, uint32_t phase, const SpiDiag_Result *r);
bool PairTrace_Finish(Pair_Trace *c, uint32_t raw1, uint32_t raw2);
void PairTrace_Fail(Pair_Trace *c, uint32_t phase, const SpiDiag_Result *r);
void PairTrace_Stop(Pair_Trace *c);
uint32_t PairTrace_Raw(const SpiDiag_Result *r);
int32_t PairTrace_Code(uint32_t raw);
#endif
