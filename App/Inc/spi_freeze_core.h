#ifndef SPI_FREEZE_CORE_H
#define SPI_FREEZE_CORE_H
#include <stdbool.h>
#include <stdint.h>
#include "spi_dma_diagnostic.h"
typedef enum {
    FREEZE_IDLE, FREEZE_WAIT_READY, FREEZE_AUDIT_ADC, FREEZE_AUDIT_DIAG,
    FREEZE_REFERENCE, FREEZE_FIRST, FREEZE_SECOND, FREEZE_END_ADC, FREEZE_END_DIAG,
    FREEZE_DONE, FREEZE_STOPPED, FREEZE_ERROR, FREEZE_RESTORE_FAILED
} Freeze_Phase;
typedef enum {
    FREEZE_ERR_NONE, FREEZE_ERR_BUSY, FREEZE_ERR_ARGUMENT, FREEZE_ERR_IO,
    FREEZE_ERR_READY_TIMEOUT, FREEZE_ERR_CONFIG, FREEZE_ERR_START,
    FREEZE_ERR_AUDIT, FREEZE_ERR_RESTORE, FREEZE_ERR_DISCONNECT
} Freeze_Error;
typedef struct {
    void *ctx;
    int32_t (*read16)(void *, uint8_t, uint16_t *);
    int32_t (*write16)(void *, uint8_t, uint16_t);
    bool (*begin)(void *);
    bool (*read)(void *, uint8_t, uint8_t);
    bool (*poll)(void *, SpiDiag_Result *);
    bool (*stop)(void *);
    uint64_t (*now)(void *);
    uint32_t (*adc_writes)(void *);
    uint32_t (*conflicts)(void *);
} Freeze_IO;
typedef struct {
    uint32_t pair, reference, raw1, raw2, phase;
    SpiDiag_Result a, b;
} Freeze_Event;
typedef struct {
    Freeze_IO io;
    Freeze_Phase phase, outcome;
    Freeze_Error error;
    bool busy, ids, pending, backend, adc_changed, diag_changed, reference_valid;
    bool completion_confirmed, finish_audited;
    uint16_t saved_adc, saved_diag, config, trigger_adc, adc_start, adc_end, diag_start, diag_end;
    uint32_t vshct_us, requested, completed, reads_total, all_dma_reads, reference;
    uint32_t last_raw1, last_raw2;
    uint32_t pair_match, pair_mismatch, reference_mismatch, first_mismatch, second_mismatch;
    uint32_t equal_not_reference, only_one_bad, both_bad, code_mismatch, reserved_errors;
    uint32_t first_changed_pair, recovered_after_change, changed_suffix, longest_new_run;
    uint32_t new_run, new_value, ids_bad_manufacturer, ids_bad_device;
    uint32_t spi_errors, dma_errors, dma_timeouts, hal_errors, audit_errors, restore_errors;
    uint32_t max_transaction_us, max_pair_us, max_gap_us, previous_end;
    bool have_previous_end;
    uint32_t trigger_writes, write_base, frozen_write_base, frozen_writes, owner_base, owner_errors;
    uint64_t started_us, ended_us, trigger_us, ready_us, timeout_us, next_poll_us;
    int32_t last_hal_status;
    SpiDiag_Result first;
    Freeze_Event events[16];
    uint32_t event_count, events_dropped;
} Freeze_Core;
bool FreezeCore_Start(Freeze_Core *c, const Freeze_IO *io, uint32_t pairs,
                       uint32_t vshct_us, bool ids);
void FreezeCore_Process(Freeze_Core *c);
void FreezeCore_Stop(Freeze_Core *c, bool disconnected);
int32_t FreezeCore_Code(uint32_t raw);
#endif
