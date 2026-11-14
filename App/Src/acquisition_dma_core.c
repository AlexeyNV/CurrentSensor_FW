#include "acquisition_dma_core.h"

#include <string.h>

void AcqDmaCore_Init(AcqDma_Core *dma) { memset(dma, 0, sizeof(*dma)); }

bool AcqDmaCore_Tick(AcqDma_Core *dma, uint32_t slot, uint32_t tick_us,
                     uint32_t start_us)
{
    uint32_t latency;
    if (dma->state != ACQ_DMA_IDLE) { ++dma->busy; return false; }
    dma->slot = slot;
    dma->tick_us = tick_us;
    dma->state = ACQ_DMA_DIAG;
    latency = start_us - tick_us;
    if (latency > dma->max_latency_us) dma->max_latency_us = latency;
    return true;
}

void AcqDmaCore_TransactionStarted(AcqDma_Core *dma, uint32_t now_us)
{
    dma->transaction_us = now_us;
    ++dma->starts;
    ++dma->generation;
}

void AcqDmaCore_TransactionCompleted(AcqDma_Core *dma, uint32_t now_us)
{
    uint32_t duration = now_us - dma->transaction_us;
    ++dma->completions;
    dma->completed_generation = dma->generation;
    dma->completed_us = now_us;
    if (duration > dma->max_transaction_us) dma->max_transaction_us = duration;
}

bool AcqDmaCore_WatchdogDue(const AcqDma_Core *dma, uint32_t generation,
                           uint32_t now_us, uint32_t limit_us)
{
    uint32_t age = now_us - dma->transaction_us;
    return dma->state != ACQ_DMA_IDLE && dma->generation == generation &&
        dma->completed_generation != generation &&
        age < 0x80000000U && age >= limit_us;
}
bool AcqDmaCore_CommitOnce(bool *committed)
{
    if (*committed) return false;
    *committed = true;
    return true;
}

bool AcqDmaCore_DiagReady(AcqDma_Core *dma, bool ready)
{
    if (dma->state != ACQ_DMA_DIAG) return false;
    dma->state = ready ? ACQ_DMA_VSHUNT : ACQ_DMA_IDLE;
    return ready;
}

bool AcqDmaCore_SampleReady(AcqDma_Core *dma, uint32_t now_us)
{
    uint32_t duration;
    if (dma->state != ACQ_DMA_VSHUNT) return false;
    duration = now_us - dma->tick_us;
    if (duration > dma->max_cycle_us) dma->max_cycle_us = duration;
    dma->state = ACQ_DMA_IDLE;
    return true;
}

void AcqDmaCore_Error(AcqDma_Core *dma) { ++dma->errors; dma->state = ACQ_DMA_IDLE; }
void AcqDmaCore_Timeout(AcqDma_Core *dma) { ++dma->timeouts; dma->state = ACQ_DMA_IDLE; }
void AcqDmaCore_Stop(AcqDma_Core *dma) { dma->state = ACQ_DMA_IDLE; }

bool AcqDmaCore_RepeatCandidate(uint32_t raw, uint32_t threshold_ma,
                                 bool armed)
{
    uint32_t code = (raw >> 4) & 0xFFFFFU;
    int32_t signed_code = (code & 0x80000U) ? (int32_t)code - 0x100000L : (int32_t)code;
    uint32_t magnitude = (uint32_t)(signed_code < 0 ? -signed_code : signed_code);
    return armed && magnitude > threshold_ma * 16U;
}

AcqDma_RxDecision AcqDmaCore_DecideRx(bool rx_error, bool rx_complete,
                                       bool tx_error, uint32_t tx_remaining)
{
    if (rx_error) return ACQ_DMA_RX_ERROR;
    if (tx_error) return ACQ_DMA_RX_TX_ERROR;
    if (!rx_complete) return ACQ_DMA_RX_IGNORE;
    if (tx_remaining != 0U) return ACQ_DMA_RX_TX_INCOMPLETE;
    return ACQ_DMA_RX_COMPLETE;
}

AcqDma_VerifyDecision AcqDmaCore_Verify(uint32_t first_raw,
                                        uint32_t second_raw,
                                        bool same_conversion, bool reject)
{
    AcqDma_VerifyDecision result;
    result.code_match = ((first_raw >> 4) & 0xFFFFFU) ==
                        ((second_raw >> 4) & 0xFFFFFU);
    result.raw_match = (first_raw & 0xFFFFFFU) == (second_raw & 0xFFFFFFU);
    result.accept = result.code_match || !same_conversion || !reject;
    return result;
}
