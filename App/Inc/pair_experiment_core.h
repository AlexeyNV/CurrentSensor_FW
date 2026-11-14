#ifndef PAIR_EXPERIMENT_CORE_H
#define PAIR_EXPERIMENT_CORE_H
#include "pair_trace_core.h"
#include "spi_freeze_core.h" /* Common, injected register/DMA/time IO contract. */
typedef enum { PAIR_EXP_IDLE, PAIR_EXP_BOOT, PAIR_EXP_EDGE, PAIR_EXP_DIAG,
    PAIR_EXP_DELAY_FIRST, PAIR_EXP_FIRST, PAIR_EXP_DELAY_SECOND, PAIR_EXP_SECOND,
    PAIR_EXP_REARM, PAIR_EXP_DONE, PAIR_EXP_STOPPED, PAIR_EXP_ERROR, PAIR_EXP_RESTORE_FAILED } Pair_ExpState;
typedef struct {
    uint32_t valid, state, previous, stamp, last_irq_us, last_diag_us, irq_count, busy_edges, skipped_edges;
    uint32_t cnvrf, pairs, pin_low, exti_pending, spi_sr, dma_state, dma_flags;
} Pair_WaitContext;
typedef struct {
    Freeze_IO io;
    Pair_Trace *trace;
    Pair_ExpState state, outcome;
    bool busy, backend, changed, pending, stopped_trace;
    uint16_t saved_adc, saved_diag, adc, diag;
    uint32_t count, first_delay, gap_delay, last_edge, not_ready, skipped_edges, busy_edges;
    uint32_t io_errors, restore_errors, wait_timeouts;
    uint32_t dma_errors, dma_timeouts;
    Pair_ExpState previous;
    uint32_t rearm_reads, last_diag_us, last_cnvrf;
    Pair_WaitContext wait_context;
    void (*capture_context)(Pair_WaitContext *);
    int32_t last_status;
    uint64_t start_us, end_us, edge_deadline, limit_us;
} Pair_Experiment;
bool PairExperiment_Start(Pair_Experiment *p, Pair_Trace *trace, const Freeze_IO *io,
    uint32_t count, uint32_t ct, uint32_t first, uint32_t gap, bool latch);
void PairExperiment_Process(Pair_Experiment *p);
void PairExperiment_Stop(Pair_Experiment *p, bool error);
#endif
