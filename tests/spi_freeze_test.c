#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "spi_freeze_core.h"
typedef struct {
    uint64_t now, ready_at;
    uint16_t adc, diag, config;
    uint32_t writes, triggers, conflicts, starts, completions, reg, length, shunt_reads;
    uint32_t mutation, fail_generation;
    SpiDiag_Status fault;
    bool backend, pending, ready_consumed, missing_ready, fail_hal, fail_restore, fail_stop;
    uint32_t start, generation;
} Fake;
static uint32_t sample_value = 0xFFF120U;
static void init(Fake *f)
{ memset(f, 0, sizeof(*f)); f->adc = 0xFB68U; f->diag = 0x2000U; f->now = 0xFFFFFF00ULL; }
static uint64_t now(void *ctx) { return ((Fake *)ctx)->now++; }
static uint32_t writes(void *ctx) { return ((Fake *)ctx)->writes; }
static uint32_t conflicts(void *ctx) { return ((Fake *)ctx)->conflicts; }
static int32_t read16(void *ctx, uint8_t reg, uint16_t *v)
{
    Fake *f = ctx;
    if (f->fail_hal) { f->fail_hal = false; return 2; }
    if (reg == 0U) *v = f->config;
    else if (reg == 1U) *v = f->adc;
    else {
        assert(reg == 11U);
        *v = f->diag | 1U;
        if ((f->adc >> 12) == 2U && !f->missing_ready && !f->ready_consumed && f->now >= f->ready_at) {
            *v |= 2U; f->ready_consumed = true;
        }
    }
    return 0;
}
static int32_t write16(void *ctx, uint8_t reg, uint16_t v)
{
    Fake *f = ctx;
    assert(!f->backend); /* setup/restoration HAL must never race DMA ownership */
    if (reg == 11U) { f->diag = v & 0xF000U; return 0; }
    assert(reg == 1U); ++f->writes;
    if (v == 0xFB68U && f->fail_restore) return 1;
    f->adc = v;
    if ((v >> 12) == 2U) {
        ++f->triggers; f->ready_consumed = false;
        f->ready_at = f->now + 60U + (((v >> 6) & 7U) == 2U ? 150U : ((v >> 6) & 7U) == 1U ? 84U : 50U);
    }
    return 0;
}
static bool begin(void *ctx)
{ Fake *f = ctx; assert(!f->backend); f->backend = true; f->pending = false; return true; }
static bool read_dma(void *ctx, uint8_t reg, uint8_t length)
{
    Fake *f = ctx; assert(f->backend && !f->pending);
    assert(length == 3U || length == 4U);
    f->pending = true; f->reg = reg; f->length = length;
    f->start = (uint32_t)f->now; f->generation = ++f->starts;
    return true;
}
static bool poll(void *ctx, SpiDiag_Result *r)
{
    Fake *f = ctx;
    uint32_t value;
    if (!f->pending) return false;
    memset(r, 0, sizeof(*r)); f->pending = false;
    f->now += 6U; r->start_us = f->start; r->end_us = (uint32_t)f->now;
    r->generation = f->generation; r->length = (uint8_t)f->length; r->started = true;
    r->hal_state = 1U; r->cs_high = 1U; r->spi_sr = 2U; r->rx[0] = 0xA5U;
    r->status = f->generation == f->fail_generation ? f->fault : SPI_DIAG_OK;
    ++f->completions;
    if (f->reg == 4U) {
        uint32_t n = f->shunt_reads++;
        uint32_t pair = n == 0U ? 0U : (n - 1U) / 2U + 1U;
        bool second = n != 0U && ((n - 1U) & 1U) != 0U;
        value = sample_value;
        if (pair == 1U) {
            if (f->mutation == 1U && !second) value ^= 0x10U;
            if (f->mutation == 2U && second) value ^= 0x10U;
            if (f->mutation == 3U) value ^= 0x20000U;
            if (f->mutation == 4U && !second) value ^= 1U;
        }
        if (f->mutation == 5U && pair >= 10U) value ^= 0x20000U;
    }
    else if (f->reg == 62U) value = f->mutation == 6U ? 0x5448U : 0x5449U;
    else if (f->reg == 63U) value = 0x2291U;
    else { uint16_t v; assert(read16(f, (uint8_t)f->reg, &v) == 0); value = v; }
    if (f->length == 4U) { r->rx[1] = (uint8_t)(value >> 16); r->rx[2] = (uint8_t)(value >> 8); r->rx[3] = (uint8_t)value; }
    else { r->rx[1] = (uint8_t)(value >> 8); r->rx[2] = (uint8_t)value; }
    return true;
}
static bool stop(void *ctx)
{
    Fake *f = ctx;
    if (f->fail_stop) return false;
    f->backend = false; f->pending = false; return true;
}
static Freeze_IO io(Fake *f)
{
    Freeze_IO result = {f, read16, write16, begin, read_dma, poll, stop, now, writes, conflicts};
    return result;
}
static void run(Freeze_Core *c)
{
    uint32_t guard = 0U;
    while (c->busy && c->phase != FREEZE_RESTORE_FAILED && guard++ < 2000000U) FreezeCore_Process(c);
    assert(guard < 2000000U);
}
static void baseline_and_ids(void)
{
    Fake f; Freeze_Core c = {0}; Freeze_IO link;
    init(&f); link = io(&f);
    assert(FreezeCore_Start(&c, &link, 100000U, 150U, false)); run(&c);
    assert(c.phase == FREEZE_DONE && !c.busy && c.completed == 100000U);
    assert(c.reference == sample_value && c.pair_match == 100000U && c.pair_mismatch == 0U);
    assert(c.reads_total == 200000U && c.all_dma_reads == 200005U && c.event_count == 0U);
    assert(c.trigger_writes == 1U && c.frozen_writes == 0U && f.triggers == 1U);
    assert(f.writes == 2U && f.adc == 0xFB68U && f.diag == 0x2000U && !f.backend);
    assert(c.adc_start == 0x2AA8U && c.adc_end == c.adc_start && !(c.diag_end & 2U));
    assert(c.ended_us > 0x100000000ULL && c.max_transaction_us == 7U);
    assert(FreezeCore_Code(0xFFFFF0U) == -1);
    {
        uint64_t ended = c.ended_us;
        f.now += 10000000U;
        FreezeCore_Process(&c); FreezeCore_Stop(&c, true);
        assert(c.ended_us == ended && c.completed == 100000U && c.phase == FREEZE_DONE);
    }
    printf("freeze: %u pairs, %u reads, %u trigger, %u frozen writes\n",
        c.completed, c.reads_total, c.trigger_writes, c.frozen_writes);
    init(&f); link = io(&f);
    assert(FreezeCore_Start(&c, &link, 100000U, 150U, true)); run(&c);
    assert(c.completed == 100000U && c.pair_match == 100000U && c.all_dma_reads == 200001U);
    assert(c.last_raw1 == 0x5449U && c.last_raw2 == 0x2291U);
    assert(f.writes == 0U && f.triggers == 0U && c.ids_bad_manufacturer == 0U);
    for (uint32_t us = 50U; us <= 150U; us = us == 50U ? 84U : us == 84U ? 150U : 151U) {
        init(&f); link = io(&f);
        assert(FreezeCore_Start(&c, &link, 1U, us, false)); run(&c);
        assert(c.completion_confirmed && c.trigger_writes == 1U && c.frozen_writes == 0U);
    }
}
static void corruptions(void)
{
    for (uint32_t mutation = 1U; mutation <= 4U; ++mutation) {
        Fake f; Freeze_Core c = {0}; Freeze_IO link;
        init(&f); f.mutation = mutation; link = io(&f);
        assert(FreezeCore_Start(&c, &link, 2U, 150U, false)); run(&c);
        assert(c.completed == 2U && c.reference_mismatch == 1U && c.event_count == 1U);
        assert(c.events[0].pair == 1U && c.events[0].a.rx[0] == 0xA5U);
        assert(c.recovered_after_change == 1U && c.changed_suffix == 0U);
        if (mutation == 1U) assert(c.first_mismatch == 1U && c.second_mismatch == 0U && c.only_one_bad == 1U);
        if (mutation == 2U) assert(c.first_mismatch == 0U && c.second_mismatch == 1U);
        if (mutation == 3U) assert(c.pair_mismatch == 0U && c.equal_not_reference == 1U && c.both_bad == 1U);
        if (mutation == 4U) assert(c.reserved_errors == 1U && c.code_mismatch == 0U &&
                                  (c.events[0].raw1 ^ c.events[0].reference) == 1U);
    }
    {
        Fake f; Freeze_Core c = {0}; Freeze_IO link;
        init(&f); f.mutation = 5U; link = io(&f);
        assert(FreezeCore_Start(&c, &link, 100000U, 150U, false)); run(&c);
        assert(c.reference_mismatch == 99991U && c.event_count == 16U && c.events_dropped == 99975U);
        assert(c.events[0].pair == 10U && c.events[15].pair == 25U);
        assert(c.longest_new_run == 99991U && c.changed_suffix == 99991U && c.recovered_after_change == 0U);
        init(&f); f.mutation = 6U; link = io(&f);
        assert(FreezeCore_Start(&c, &link, 10U, 150U, true)); run(&c);
        assert(c.ids_bad_manufacturer == 10U && c.ids_bad_device == 0U && c.event_count == 10U);
        assert(c.last_raw1 == 0x5448U && c.events[0].raw1 == 0x5448U);
    }
}
static void failures_stop_and_restore(void)
{
    for (uint32_t which = 0U; which < 4U; ++which) {
        Fake f; Freeze_Core c = {0}; Freeze_IO link;
        init(&f); link = io(&f);
        if (which == 0U) f.missing_ready = true;
        if (which == 1U) f.fail_hal = true;
        if (which >= 2U) { f.fail_generation = which == 2U ? 4U : 5U; f.fault = which == 2U ? SPI_DIAG_TIMEOUT : SPI_DIAG_IO_ERROR; }
        (void)FreezeCore_Start(&c, &link, 10000U, 150U, false); run(&c);
        assert(c.phase == FREEZE_ERROR && f.adc == 0xFB68U && !f.backend);
        if (which == 0U) assert(c.error == FREEZE_ERR_READY_TIMEOUT && f.triggers == 1U);
        if (which == 1U) assert(c.hal_errors == 1U && f.triggers == 0U);
        if (which == 2U) assert(c.dma_timeouts == 1U && c.event_count == 1U);
        if (which == 3U) assert(c.dma_errors == 1U && c.event_count == 1U);
    }
    for (uint32_t phase = 0U; phase < 4U; ++phase) {
        Fake f; Freeze_Core c = {0}; Freeze_IO link; uint32_t count;
        init(&f); link = io(&f);
        assert(FreezeCore_Start(&c, &link, 10000U, 150U, false));
        assert(!FreezeCore_Start(&c, &link, 100000U, 50U, false));
        assert(c.requested == 10000U && f.triggers == 1U);
        if (phase != 0U) {
            Freeze_Phase target = phase == 1U ? FREEZE_FIRST : FREEZE_SECOND;
            for (count = 0U; !(c.phase == target && c.pending) && count < 10000U; ++count) FreezeCore_Process(&c);
            assert(count < 10000U);
        }
        FreezeCore_Stop(&c, phase == 3U);
        assert(!c.busy && !f.backend && f.adc == 0xFB68U && f.diag == 0x2000U);
        count = c.all_dma_reads;
        f.pending = true; FreezeCore_Process(&c); /* late callback/result after stop is ignored */
        assert(c.all_dma_reads == count);
        if (phase == 3U) assert(c.error == FREEZE_ERR_DISCONNECT);
    }
    {
        Fake f; Freeze_Core c = {0}; Freeze_IO link;
        init(&f); link = io(&f); f.fail_restore = true;
        assert(FreezeCore_Start(&c, &link, 1U, 150U, false)); run(&c);
        assert(c.busy && c.phase == FREEZE_RESTORE_FAILED && c.frozen_writes == 0U);
        assert(!FreezeCore_Start(&c, &link, 10000U, 150U, false));
        f.fail_restore = false; FreezeCore_Stop(&c, false);
        assert(!c.busy && f.adc == 0xFB68U && c.frozen_writes == 0U);
        init(&f); link = io(&f); f.fail_stop = true;
        assert(FreezeCore_Start(&c, &link, 1U, 150U, false)); run(&c);
        assert(c.busy && c.phase == FREEZE_RESTORE_FAILED && f.backend);
        f.fail_stop = false; FreezeCore_Stop(&c, false);
        assert(!c.busy && f.adc == 0xFB68U);
    }
}
static void unexpected_write(void)
{
    Fake f; Freeze_Core c = {0}; Freeze_IO link; uint32_t guard = 0U;
    init(&f); link = io(&f);
    assert(FreezeCore_Start(&c, &link, 2U, 150U, false));
    while (!c.reference_valid && guard++ < 10000U) FreezeCore_Process(&c);
    assert(c.reference_valid);
    ++f.writes; /* simulate an unexpected control-path attempt during the hold */
    run(&c);
    assert(c.frozen_writes == 1U && c.audit_errors != 0U && c.phase == FREEZE_ERROR);
    assert(c.spi_errors == 0U); /* ADC audit failure is not automatically blamed on SPI */
}
int main(void)
{
    baseline_and_ids(); corruptions(); failures_stop_and_restore(); unexpected_write();
    puts("SPI freeze tests: OK"); return 0;
}
