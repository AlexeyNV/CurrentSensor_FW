#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "acquisition_dma_core.h"
#include "acquisition_core.h"

static void test_ready_and_not_ready(void)
{
    AcqDma_Core dma;
    AcqDmaCore_Init(&dma);
    assert(AcqDmaCore_Tick(&dma, 0U, 100U, 103U));
    AcqDmaCore_TransactionStarted(&dma, 103U);
    AcqDmaCore_TransactionCompleted(&dma, 110U);
    assert(dma.completions == 1U && dma.max_latency_us == 3U);
    assert(!AcqDmaCore_DiagReady(&dma, false));
    assert(dma.state == ACQ_DMA_IDLE);
    assert(AcqDmaCore_Tick(&dma, 1U, 200U, 205U));
    AcqDmaCore_TransactionStarted(&dma, 205U);
    AcqDmaCore_TransactionCompleted(&dma, 213U);
    assert(AcqDmaCore_DiagReady(&dma, true));
    AcqDmaCore_TransactionStarted(&dma, 214U);
    AcqDmaCore_TransactionCompleted(&dma, 225U);
    assert(AcqDmaCore_SampleReady(&dma, 225U));
    assert(dma.starts == 3U && dma.completions == 3U);
    assert(dma.max_transaction_us == 11U && dma.max_cycle_us == 25U);
}

static void test_busy_error_timeout_stop(void)
{
    AcqDma_Core dma;
    AcqDmaCore_Init(&dma);
    assert(AcqDmaCore_Tick(&dma, 0U, 0U, 0U));
    assert(!AcqDmaCore_Tick(&dma, 1U, 100U, 101U));
    assert(dma.busy == 1U && dma.state == ACQ_DMA_DIAG);
    AcqDmaCore_Error(&dma);
    assert(dma.errors == 1U && dma.state == ACQ_DMA_IDLE);
    assert(AcqDmaCore_Tick(&dma, 2U, 200U, 200U));
    AcqDmaCore_Timeout(&dma);
    assert(dma.timeouts == 1U && dma.state == ACQ_DMA_IDLE);
    assert(AcqDmaCore_Tick(&dma, 3U, 300U, 300U));
    assert(AcqDmaCore_DiagReady(&dma, true));
    AcqDmaCore_Stop(&dma);
    assert(!AcqDmaCore_SampleReady(&dma, 320U));
    assert(dma.state == ACQ_DMA_IDLE);
}

static void test_repeat_threshold_and_sign(void)
{
    assert(!AcqDmaCore_RepeatCandidate(0U, 50U, true));
    assert(!AcqDmaCore_RepeatCandidate(800U << 4, 50U, true));
    assert(AcqDmaCore_RepeatCandidate(801U << 4, 50U, true));
    assert(AcqDmaCore_RepeatCandidate(0xFFCDF0U, 50U, true));
    assert(!AcqDmaCore_RepeatCandidate(801U << 4, 50U, false));
}

static void test_rx_tx_callback_order(void)
{
    assert(AcqDmaCore_DecideRx(false, false, false, 2U) == ACQ_DMA_RX_IGNORE);
    assert(AcqDmaCore_DecideRx(true, true, false, 0U) == ACQ_DMA_RX_ERROR);
    assert(AcqDmaCore_DecideRx(false, true, true, 0U) == ACQ_DMA_RX_TX_ERROR);
    assert(AcqDmaCore_DecideRx(false, true, false, 1U) == ACQ_DMA_RX_TX_INCOMPLETE);
    assert(AcqDmaCore_DecideRx(false, true, false, 0U) == ACQ_DMA_RX_COMPLETE);
}

static void test_repeat_state_and_stop(void)
{
    AcqDma_Core dma;
    AcqDmaCore_Init(&dma);
    assert(AcqDmaCore_Tick(&dma, 0U, 100U, 100U));
    assert(AcqDmaCore_DiagReady(&dma, true));
    assert(AcqDmaCore_SampleReady(&dma, 125U));
    dma.state = ACQ_DMA_REPEAT;
    AcqDmaCore_TransactionStarted(&dma, 126U);
    assert(!AcqDmaCore_Tick(&dma, 1U, 200U, 200U));
    assert(dma.busy == 1U && dma.state == ACQ_DMA_REPEAT);
    AcqDmaCore_Stop(&dma);
    assert(dma.state == ACQ_DMA_IDLE);
}

static void test_verify_scope_and_raw_bits(void)
{
    AcqDma_VerifyDecision d;
    d = AcqDmaCore_Verify(0x123450U, 0x123450U, true, true);
    assert(d.code_match && d.raw_match && d.accept);
    d = AcqDmaCore_Verify(0x123450U, 0x12345FU, true, true);
    assert(d.code_match && !d.raw_match && d.accept);
    d = AcqDmaCore_Verify(0x123450U, 0x123460U, true, true);
    assert(!d.code_match && !d.accept);
    d = AcqDmaCore_Verify(0x123450U, 0x123460U, true, false);
    assert(!d.code_match && d.accept);
    d = AcqDmaCore_Verify(0x123450U, 0x123460U, false, true);
    assert(!d.code_match && d.accept); /* continuous reads may span conversions */
}

int main(void)
{
    {
        static Acq_Core samples;
        Acq_Sample sample;
        AcqCore_Start(&samples, 0U);
        for (uint32_t i = 0U; i < 1000U; ++i) {
            bool committed = false;
            uint32_t stamp = 0xFFFFFF00U + i * 100U;
            /* Competing success and secondary-error retention paths. */
            for (uint32_t path = 0U; path < 2U; ++path)
                if (AcqDmaCore_CommitOnce(&committed))
                    AcqCore_SampleSpi(&samples, i << 4, stamp, 20U, i, 0U);
            assert(AcqCore_Pop(&samples, &sample));
            assert(sample.slot == i && sample.time_us == stamp);
            assert(!AcqCore_Pop(&samples, &sample));
        }
        assert(samples.samples == 1000U && samples.stats_count == 1000U);
        assert(samples.min_sample_period_us == 100U && samples.max_sample_period_us == 100U);
    }
    {
        AcqDma_Core d;
        uint32_t observed;
        bool committed = false;
        AcqDmaCore_Init(&d); d.state = ACQ_DMA_VERIFY_FIRST;
        AcqDmaCore_TransactionStarted(&d, 100U); observed = d.generation;
        assert(!AcqDmaCore_WatchdogDue(&d, observed, 179U, 80U));
        AcqDmaCore_TransactionCompleted(&d, 179U); /* callback immediately before check */
        assert(!AcqDmaCore_WatchdogDue(&d, observed, 180U, 80U));
        d.state = ACQ_DMA_VERIFY_SECOND;
        AcqDmaCore_TransactionStarted(&d, 181U);
        assert(!AcqDmaCore_WatchdogDue(&d, observed, 300U, 80U)); /* old generation */
        assert(!AcqDmaCore_WatchdogDue(&d, d.generation, 180U, 80U)); /* old clock */
        assert(AcqDmaCore_WatchdogDue(&d, d.generation, 261U, 80U));
        AcqDmaCore_Timeout(&d); /* timeout claims before callback; callback must ignore idle */
        assert(d.state == ACQ_DMA_IDLE);
        assert(!AcqDmaCore_WatchdogDue(&d, d.generation, 262U, 80U));
        assert(AcqDmaCore_CommitOnce(&committed)); /* valid primary retained on error */
        assert(!AcqDmaCore_CommitOnce(&committed)); /* late completion cannot count twice */
        d.state = ACQ_DMA_DIAG;
        AcqDmaCore_TransactionStarted(&d, 0xFFFFFFF0U);
        assert(AcqDmaCore_WatchdogDue(&d, d.generation, 0x40U, 80U));
        AcqDmaCore_Stop(&d);
        assert(!AcqDmaCore_WatchdogDue(&d, d.generation, 0x41U, 80U));
    }
    test_ready_and_not_ready();
    test_busy_error_timeout_stop();
    test_repeat_threshold_and_sign();
    test_rx_tx_callback_order();
    test_repeat_state_and_stop();
    test_verify_scope_and_raw_bits();
    puts("acquisition DMA state tests passed");
    return 0;
}
