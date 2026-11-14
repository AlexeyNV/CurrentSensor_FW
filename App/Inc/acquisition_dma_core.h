#ifndef ACQUISITION_DMA_CORE_H
#define ACQUISITION_DMA_CORE_H

#include <stdbool.h>
#include <stdint.h>

typedef enum { ACQ_DMA_IDLE, ACQ_DMA_DIAG, ACQ_DMA_VSHUNT, ACQ_DMA_REPEAT,
               ACQ_DMA_VERIFY_FIRST, ACQ_DMA_VERIFY_SECOND } AcqDma_State;
typedef enum
{
    ACQ_DMA_RX_IGNORE,
    ACQ_DMA_RX_ERROR,
    ACQ_DMA_RX_TX_ERROR,
    ACQ_DMA_RX_TX_INCOMPLETE,
    ACQ_DMA_RX_COMPLETE
} AcqDma_RxDecision;

typedef struct
{
    AcqDma_State state;
    uint32_t slot;
    uint32_t tick_us;
    uint32_t transaction_us;
    uint32_t starts;
    uint32_t completions;
    uint32_t busy;
    uint32_t errors;
    uint32_t timeouts;
    uint32_t buffer_reuse_attempts;
    uint32_t callback_order_errors;
    uint32_t cs_state_errors;
    uint32_t max_transaction_us;
    uint32_t max_latency_us;
    uint32_t max_cycle_us;
    volatile uint32_t generation, completed_generation, completed_us;
} AcqDma_Core;
bool AcqDmaCore_WatchdogDue(const AcqDma_Core *dma, uint32_t generation,
                           uint32_t now_us, uint32_t limit_us);
bool AcqDmaCore_CommitOnce(bool *committed);

void AcqDmaCore_Init(AcqDma_Core *dma);
bool AcqDmaCore_Tick(AcqDma_Core *dma, uint32_t slot, uint32_t tick_us,
                     uint32_t start_us);
void AcqDmaCore_TransactionStarted(AcqDma_Core *dma, uint32_t now_us);
void AcqDmaCore_TransactionCompleted(AcqDma_Core *dma, uint32_t now_us);
bool AcqDmaCore_DiagReady(AcqDma_Core *dma, bool ready);
bool AcqDmaCore_SampleReady(AcqDma_Core *dma, uint32_t now_us);
void AcqDmaCore_Error(AcqDma_Core *dma);
void AcqDmaCore_Timeout(AcqDma_Core *dma);
void AcqDmaCore_Stop(AcqDma_Core *dma);
bool AcqDmaCore_RepeatCandidate(uint32_t raw, uint32_t threshold_ma,
                                 bool armed);
AcqDma_RxDecision AcqDmaCore_DecideRx(bool rx_error, bool rx_complete,
                                       bool tx_error, uint32_t tx_remaining);
typedef struct { bool code_match; bool raw_match; bool accept; } AcqDma_VerifyDecision;
AcqDma_VerifyDecision AcqDmaCore_Verify(uint32_t first_raw,
                                        uint32_t second_raw,
                                        bool same_conversion, bool reject);

#endif
