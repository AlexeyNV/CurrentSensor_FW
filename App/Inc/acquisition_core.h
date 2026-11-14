#ifndef ACQUISITION_CORE_H
#define ACQUISITION_CORE_H

#include <stdbool.h>
#include <stdint.h>
#include "measurement_profile.h"

#define ACQ_RING_CAPACITY 1024U

typedef bool (*AcqCore_WriteConfigFn)(void *context, uint16_t value);

typedef struct
{
    uint32_t raw;
    uint32_t time_us;
    uint32_t slot;
    uint8_t spi_command_rx;
    bool spi_command_rx_valid;
} Acq_Sample;

typedef struct
{
    Acq_Sample ring[ACQ_RING_CAPACITY];
    volatile uint16_t head;
    volatile uint16_t tail;
    volatile bool running;
    bool config_saved;
    uint16_t saved_adc_config;
    uint32_t started_us;
    uint32_t stopped_us;
    uint64_t started_us64;
    uint64_t stopped_us64;
    uint16_t period_us;
    uint16_t freshness_guard_us;
    MeasurementProfile_Id profile_id;
    uint32_t startup_us;
    volatile uint32_t ticks;
    uint32_t handled_ticks;
    uint32_t samples;
    uint32_t missed;
    uint32_t missed_timer_slots;
    uint32_t freshness_skips;
    volatile uint32_t timer_lost;
    uint32_t spi_errors;
    uint32_t overflows;
    volatile uint32_t period_violations;
    uint32_t cnvrf_reads;
    uint32_t cnvrf_ready;
    uint32_t cnvrf_not_ready;
    uint32_t vshunt_reads;
    uint16_t last_diag_alrt;
    uint16_t last_adc_config;
    uint32_t max_service_us;
    uint32_t max_spi_transaction_us;
    uint32_t min_sample_period_us;
    uint32_t max_sample_period_us;
    uint32_t previous_sample_us;
    uint32_t max_read_latency_us;
    uint32_t max_read_latency_cycles;
    volatile uint32_t previous_tick_us;
    int32_t min_code;
    int32_t max_code;
    int64_t sum_code;
    uint64_t sum_square;
    uint32_t stats_count;
} Acq_Core;

void AcqCore_Start(Acq_Core *core, uint64_t now_us);
void AcqCore_Stop(Acq_Core *core, uint64_t now_us);
void AcqCore_SaveConfig(Acq_Core *core, uint16_t value);
bool AcqCore_RestoreConfig(Acq_Core *core, AcqCore_WriteConfigFn write_config,
                           void *context);
void AcqCore_Tick(Acq_Core *core, uint32_t now_us);
bool AcqCore_Schedule(Acq_Core *core, uint32_t ticks, uint32_t tick_us,
                      uint32_t now_us, uint32_t period_us);
bool AcqCore_FreshnessAllowsRead(Acq_Core *core, uint32_t tick_us);
void AcqCore_DiagAttempt(Acq_Core *core);
void AcqCore_DiagResult(Acq_Core *core, uint16_t diag_alrt);
void AcqCore_RecordSpi(Acq_Core *core, uint32_t duration_us);
void AcqCore_RecordReadLatency(Acq_Core *core, uint32_t latency_us,
                               uint32_t latency_cycles);
void AcqCore_VShuntAttempt(Acq_Core *core);
void AcqCore_Missed(Acq_Core *core);
void AcqCore_MissedTimerSlot(Acq_Core *core);
void AcqCore_SpiError(Acq_Core *core);
void AcqCore_Sample(Acq_Core *core, uint32_t raw, uint32_t now_us,
                    uint32_t service_us, uint32_t slot);
void AcqCore_SampleSpi(Acq_Core *core, uint32_t raw, uint32_t now_us,
                       uint32_t service_us, uint32_t slot,
                       uint8_t command_rx);
bool AcqCore_Pop(Acq_Core *core, Acq_Sample *sample);
int32_t AcqCore_Decode(uint32_t raw);
uint32_t AcqCore_Rate(const Acq_Core *core, uint64_t now_us);
uint64_t AcqCore_StdCodeMilli(const Acq_Core *core);
bool AcqCore_ParseProbeDelay(const char *text, uint32_t *delay_us);

#endif
