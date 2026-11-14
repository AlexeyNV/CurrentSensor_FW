#ifndef SPI_DMA_DIAGNOSTIC_H
#define SPI_DMA_DIAGNOSTIC_H
#include <stdbool.h>
#include <stdint.h>
typedef enum { SPI_DIAG_OK, SPI_DIAG_IO_ERROR, SPI_DIAG_TIMEOUT } SpiDiag_Status;
typedef struct {
    uint8_t rx[4], length;
    SpiDiag_Status status;
    uint32_t start_us, end_us, generation, hal_state, hal_error, spi_sr, dma_flags;
    uint32_t cs_high, owner_conflicts, callback_errors, cs_errors;
    /* Optional read-path evidence, sampled before status/overrun clear. */
    uint32_t irq_us, spi_cr1, spi_cr2, dma_state, rx_cr, tx_cr, rx_left, tx_left;
    uint32_t cs_before, edge_start, edge_end, pin_start, pin_end, exti_pending;
    bool started;
} SpiDiag_Result;
#endif
