#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "pair_experiment_core.h"
#include "pair_diagnostic.h"
#include "acquisition_dma.h"
#include "tim.h"

TIM_TypeDef fake_tim2, fake_tim3;
TIM_HandleTypeDef htim2 = {&fake_tim2}, htim3 = {&fake_tim3};
typedef struct {
    Pair_Trace *trace;
    uint64_t now, next_edge, start;
    uint32_t ct, generation, fail_generation, reg, length, edge_start, starts, completions;
    uint32_t callback_delay;
    uint16_t adc, diag;
    bool backend, pending, no_ready, missing_edges, fail_read, fail_restore, wrapper;
    bool held_alert, asserted;
    SpiDiag_Status fault;
} Fake;
static Fake *hardware;
static AcqDma_Core dma;
static bool other_busy;
static void Advance(Fake *f, uint32_t us)
{
    f->now += us;
    while (f->now >= f->next_edge) {
        if (!f->missing_edges && (f->adc >> 12) == 10U) {
            if (!f->held_alert || !f->asserted) {
                if (f->wrapper) PairDiag_Irq((uint32_t)f->next_edge, true);
                else PairTrace_Irq(f->trace, (uint32_t)f->next_edge, true);
            }
            f->asserted = true;
        }
        f->next_edge += f->ct;
    }
}
static void Init(Fake *f, Pair_Trace *trace)
{
    memset(f, 0, sizeof(*f)); f->trace = trace; f->now = 0xFFFFFF00ULL;
    f->ct = 84U; f->next_edge = f->now + 84U; f->adc = 0xFB68U;
}
static int32_t Read16(void *ctx, uint8_t reg, uint16_t *value)
{
    Fake *f = ctx;
    if (f->fail_read) return 1;
    *value = reg == 0U ? 0U : reg == 1U ? f->adc : (uint16_t)(f->diag | 3U);
    return 0;
}
static int32_t Write16(void *ctx, uint8_t reg, uint16_t value)
{
    Fake *f = ctx; assert(!f->backend);
    if (reg == 1U) {
        if (value == 0xFB68U && f->fail_restore) return 1;
        f->adc = value;
        if (value == 0xAA68U || value == 0xAAA8U) {
            f->ct = value == 0xAA68U ? 84U : 150U; f->next_edge = f->now + f->ct;
        }
    }
    else { assert(reg == 11U); f->diag = value & 0xF000U; }
    return 0;
}
static uint32_t Edges(Fake *f) { return f->wrapper ? PairDiag_EdgeCount() : f->trace->irq_count; }
static bool Begin(void *ctx) { Fake *f = ctx; f->backend = true; return true; }
static bool ReadDma(void *ctx, uint8_t reg, uint8_t length)
{
    Fake *f = ctx; assert(f->backend && !f->pending);
    f->reg = reg; f->length = length; f->start = f->now; f->pending = true;
    f->edge_start = Edges(f); f->generation = ++f->starts; return true;
}
static SpiDiag_Result Result(uint32_t raw, uint32_t start, uint32_t end, uint8_t length)
{
    SpiDiag_Result r = {0}; r.start_us = start; r.end_us = end; r.irq_us = end - 1U;
    r.started = true; r.length = length; r.cs_high = 1U; r.rx[0] = 0xA5U;
    if (length == 3U) { r.rx[1] = (uint8_t)(raw >> 8); r.rx[2] = (uint8_t)raw; }
    else { r.rx[1] = (uint8_t)(raw >> 16); r.rx[2] = (uint8_t)(raw >> 8); r.rx[3] = (uint8_t)raw; }
    return r;
}
static bool Poll(void *ctx, SpiDiag_Result *r)
{
    Fake *f = ctx; uint32_t raw;
    if (!f->pending || f->now - f->start < 6U + f->callback_delay) return false;
    f->pending = false; ++f->completions;
    raw = f->reg == 11U ? f->diag | (f->no_ready ? 1U : 3U) :
        (uint32_t)((f->start / f->ct) & 0xFFFFFU) << 4; /* real simulated register updates */
    *r = Result(raw, (uint32_t)f->start, (uint32_t)f->now, (uint8_t)f->length);
    r->generation = f->generation; r->edge_start = f->edge_start; r->edge_end = Edges(f);
    if (f->generation == f->fail_generation) r->status = f->fault;
    if (f->reg == 11U) f->asserted = false; /* acknowledgement releases the held line */
    return true;
}
void PairDiag_TestTimeoutHardware(Pair_WaitContext *c)
{ c->pin_low = hardware->asserted; c->exti_pending = 1U; c->spi_sr = 2U; }
static bool Stop(void *ctx) { Fake *f = ctx; f->backend = f->pending = false; return true; }
static uint64_t Now(void *ctx) { return ((Fake *)ctx)->now; }
static Freeze_IO IO(Fake *f)
{ Freeze_IO result = {f, Read16, Write16, Begin, ReadDma, Poll, Stop, Now, NULL, NULL}; return result; }

/* Production CLI/controller with only peripheral boundaries mocked. */
bool Acq_IsRunning(void) { return other_busy; }
bool StreamTransport_IsActive(void) { return false; }
bool SpiFreeze_IsBusy(void) { return false; }
uint32_t SpiControl_GetHz(void) { return 6000000U; }
INA229_Status INA229_ReadReg16(INA229_HandleTypeDef *s, uint8_t reg, uint16_t *v)
{ (void)s; return (INA229_Status)Read16(hardware, reg, v); }
INA229_Status INA229_WriteReg16(INA229_HandleTypeDef *s, uint8_t reg, uint16_t v)
{ (void)s; return (INA229_Status)Write16(hardware, reg, v); }
bool AcqDma_DiagnosticBegin(void) { return Begin(hardware); }
bool AcqDma_DiagnosticRead(uint8_t reg, uint8_t length) { return ReadDma(hardware, reg, length); }
bool AcqDma_DiagnosticPoll(SpiDiag_Result *r) { return Poll(hardware, r); }
void AcqDma_CheckPairTimeout(void) { }
bool AcqDma_Stop(void) { dma.starts = hardware->starts; dma.completions = hardware->completions; return Stop(hardware); }
bool AcqDma_IsStopped(void) { return !hardware->backend; }
const AcqDma_Core *AcqDma_GetCore(void) { return &dma; }
uint64_t Timebase_NowUs64(void) { return hardware->now; }

static void TraceTests(void)
{
    static Pair_Trace c;
    PairTrace_Start(&c, 84U, 0xAA68U, 0x4000U);
    for (uint32_t n = 0U; n < 75U; ++n) {
        uint32_t t = 0xFFFFFE00U + n * 84U, a = 0xFFF120U, b = n >= 50U ? a ^ 0x10U : a;
        SpiDiag_Result d = Result(0x4003U, t + 2U, t + 8U, 3U);
        SpiDiag_Result r1 = Result(a, t + 12U, t + 19U, 4U), r2 = Result(b, t + 22U, t + 29U, 4U);
        PairTrace_Irq(&c, t, true); r1.edge_start = r1.edge_end = r2.edge_start = r2.edge_end = c.irq_count;
        PairTrace_Begin(&c, n, n + 1U, t); PairTrace_Transfer(&c, 0U, &d); PairTrace_Transfer(&c, 1U, &r1);
        r1.rx[1] ^= 0xFFU; /* buffer reuse after capture cannot change the snapshot */
        PairTrace_Transfer(&c, 2U, &r2); assert(PairTrace_Finish(&c, a, b) == (n >= 50U));
    }
    assert(c.total == 75U && c.match == 50U && c.mismatch == 25U);
    assert(c.event_count == 16U && c.events_not_stored == 9U);
    assert(c.events[0].seq == 50U && c.events[15].seq == 65U && c.raw_rx_errors == 0U);
    assert(PairTrace_Code(c.events[0].raw1) < 0);
    for (uint32_t metric = 0U; metric < 4U; ++metric) {
        uint32_t sum = 0U;
        for (uint32_t bin = 0U; bin < PAIR_PHASE_BINS; ++bin) sum += c.bins[metric][bin][0] + c.bins[metric][bin][1];
        assert(sum == 75U);
    }
    /* Real register update / IRQ during first read and another during the gap. */
    {
        SpiDiag_Result d = Result(3U, 1U, 5U, 3U), a = Result(0x100U, 10U, 90U, 4U), b = Result(0x110U, 100U, 106U, 4U);
        PairTrace_Start(&c, 84U, 0xAA68U, 0x4000U); PairTrace_Begin(&c, 0U, 1U, 0U);
        PairTrace_Transfer(&c, 0U, &d); a.edge_start = c.irq_count;
        PairTrace_Irq(&c, 84U, true); a.edge_end = c.irq_count; PairTrace_Transfer(&c, 1U, &a);
        PairTrace_Irq(&c, 96U, true); b.edge_start = b.edge_end = c.irq_count;
        PairTrace_Transfer(&c, 2U, &b); assert(PairTrace_Finish(&c, 0x100U, 0x110U));
        assert(c.events[0].new_edges == 2U && c.events[0].gap_edges == 1U && c.events[0].edge_count == 2U);
        assert(c.events[0].edges[0].us == 84U && (c.events[0].flags & PAIR_NOMINAL_BOUNDARY));
        PairTrace_Begin(&c, 1U, 2U, 0U); PairTrace_Transfer(&c, 0U, &d);
        PairTrace_Transfer(&c, 1U, &a); PairTrace_Transfer(&c, 2U, &b);
        (void)PairTrace_Finish(&c, 0x101U, 0x110U); assert(c.raw_rx_errors == 1U);
        PairTrace_Begin(&c, 2U, 2U, 0U); PairTrace_Transfer(&c, 0U, &d);
        a.length = 3U; PairTrace_Transfer(&c, 1U, &a); PairTrace_Transfer(&c, 2U, &b);
        assert(!PairTrace_Finish(&c, 0U, 0U) && c.capture_errors == 1U);
        PairTrace_Begin(&c, 2U, 2U, 0U); PairTrace_Transfer(&c, 0U, &d);
        a.length = 4U; a.end_us = a.start_us - 1U;
        PairTrace_Transfer(&c, 1U, &a); PairTrace_Transfer(&c, 2U, &b);
        (void)PairTrace_Finish(&c, 0x100U, 0x110U); assert(c.time_order_errors == 1U);
        PairTrace_Stop(&c); PairTrace_Irq(&c, 120U, true); assert(c.irq_count == 2U);
        PairTrace_Transfer(&c, 2U, &b); assert(!PairTrace_Finish(&c, 0U, 0U)); /* late callback */
    }
}
static void Run(Pair_Experiment *p, Fake *f)
{
    uint32_t guard = 0U;
    while (p->busy && p->state != PAIR_EXP_RESTORE_FAILED && guard++ < 10000000U) {
        Advance(f, 1U); PairExperiment_Process(p);
    }
    assert(guard < 10000000U);
}
static void ExperimentTests(void)
{
    static Pair_Trace c; Pair_Experiment p = {0}; Fake f; Freeze_IO io;
    Init(&f, &c); io = IO(&f);
    assert(PairExperiment_Start(&p, &c, &io, 1000U, 84U, 0U, 0U, false)); Run(&p, &f);
    assert(p.state == PAIR_EXP_DONE && c.total == 1000U && c.mismatch == 0U);
    assert(f.adc == 0xFB68U && f.diag == 0U && !f.backend);
    assert(p.end_us > 0x100000000ULL);
    Init(&f, &c); io = IO(&f);
    assert(PairExperiment_Start(&p, &c, &io, 1000U, 84U, 65U, 30U, true)); Run(&p, &f);
    assert(c.mismatch > 0U && c.events[0].flags & PAIR_NOMINAL_BOUNDARY);
    assert(p.busy_edges > 0U && !f.backend && f.adc == 0xFB68U);
    for (uint32_t target = PAIR_EXP_BOOT; target <= PAIR_EXP_REARM; ++target) {
        uint32_t guard = 0U; Init(&f, &c); io = IO(&f);
        assert(PairExperiment_Start(&p, &c, &io, 100U, 150U, 20U, 10U, false));
        while ((uint32_t)p.state != target && guard++ < 10000U) { Advance(&f, 1U); PairExperiment_Process(&p); }
        assert(guard < 10000U); PairExperiment_Stop(&p, false);
        assert(p.state == PAIR_EXP_STOPPED && !f.pending && !f.backend && f.adc == 0xFB68U);
        uint32_t total = c.total; Advance(&f, 1000U); PairExperiment_Process(&p); assert(c.total == total);
    }
    for (uint32_t phase = 1U; phase <= 4U; ++phase) {
        Init(&f, &c); io = IO(&f); f.fail_generation = phase;
        f.fault = phase & 1U ? SPI_DIAG_IO_ERROR : SPI_DIAG_TIMEOUT;
        assert(PairExperiment_Start(&p, &c, &io, 100U, 84U, 0U, 0U, false)); Run(&p, &f);
        assert(p.state == PAIR_EXP_ERROR && f.adc == 0xFB68U && !f.backend);
        assert(p.dma_errors == 1U && c.spi_errors == 1U && c.failure_count == 1U);
        if (phase == 1U) assert(c.failures[0].seq == UINT32_MAX && c.failures[0].failed_phase == 1U);
    }
    Init(&f, &c); io = IO(&f); f.callback_delay = 30U;
    assert(PairExperiment_Start(&p, &c, &io, 100U, 84U, 0U, 10U, false)); Run(&p, &f);
    assert(c.total == 100U && c.spi_errors == 0U); /* delayed callback remains evidence, not correction */
    Init(&f, &c); io = IO(&f); f.missing_edges = true;
    assert(PairExperiment_Start(&p, &c, &io, 100U, 84U, 0U, 0U, false)); Run(&p, &f);
    assert(p.wait_timeouts && p.state == PAIR_EXP_ERROR);
    Init(&f, &c); io = IO(&f); f.no_ready = true;
    assert(PairExperiment_Start(&p, &c, &io, 1U, 84U, 0U, 0U, false)); Run(&p, &f);
    assert(p.wait_timeouts && p.not_ready > 0U && c.total == 0U);
    Init(&f, &c); io = IO(&f); f.fail_restore = true;
    assert(PairExperiment_Start(&p, &c, &io, 1U, 84U, 0U, 0U, false)); Run(&p, &f);
    assert(p.state == PAIR_EXP_RESTORE_FAILED && p.busy);
    f.fail_restore = false; PairExperiment_Stop(&p, false); assert(!p.busy && f.adc == 0xFB68U);
    Init(&f, &c); io = IO(&f); f.fail_read = true;
    assert(!PairExperiment_Start(&p, &c, &io, 1U, 84U, 0U, 0U, false)); assert(p.io_errors && !p.busy);
    for (uint32_t latched = 0U; latched < 2U; ++latched) {
        Init(&f, &c); io = IO(&f); f.held_alert = true;
        assert(PairExperiment_Start(&p, &c, &io, 10000U, 84U, 40U, 100U, latched != 0U));
        Run(&p, &f);
        assert(c.total == 10000U && p.state == PAIR_EXP_DONE && p.wait_timeouts == 0U);
        assert(p.rearm_reads == 10000U && p.busy_edges > 0U && c.mismatch > 0U);
    }
    /* Edge after ACK but before foreground processes its mailbox is preserved. */
    Init(&f, &c); io = IO(&f);
    assert(PairExperiment_Start(&p, &c, &io, 1U, 84U, 0U, 0U, false));
    PairExperiment_Process(&p); Advance(&f, 84U); PairExperiment_Process(&p);
    assert(p.state == PAIR_EXP_EDGE && c.irq_count != p.last_edge);
    PairExperiment_Process(&p); assert(p.state == PAIR_EXP_DIAG);
    PairExperiment_Stop(&p, false);
}
static void CliTests(void)
{
    Fake f; static char out[8192]; INA229_HandleTypeDef sensor = {0};
    const char *bad[] = {"delay -1 0", "delay 1", "delay 0 501", "delay 4294967296 0",
        "delay 0 0 tail", "test 0", "test 10000 tail", "vshct 50", "events 4294967296", "bins 4"};
    Init(&f, NULL); f.wrapper = true; hardware = &f; PairDiag_Init(&sensor);
    for (uint32_t n = 0U; n < sizeof(bad) / sizeof(bad[0]); ++n) {
        assert(PairDiag_Command(bad[n], out, sizeof(out)) > 0); assert(strstr(out, "ERR: BAD_COMMAND"));
    }
    assert(PairDiag_Command("marker on", out, sizeof(out)) > 0 && strstr(out, "ERR: MARKER_UNAVAILABLE"));
    assert(PairDiag_Command("delay 0 0", out, sizeof(out)) > 0);
    assert(PairDiag_Command("test 10000", out, sizeof(out)) > 0 && strstr(out, "STARTED"));
    assert(PairDiag_Command("test 100000", out, sizeof(out)) > 0 && strstr(out, "ERR: PAIR_ACTIVE"));
    for (uint32_t guard = 0U; PairDiag_IsBusy() && guard < 2000000U; ++guard) { Advance(&f, 1U); PairDiag_Process(); }
    assert(!PairDiag_IsBusy());
    assert(PairDiag_Command("report", out, sizeof(out)) > 0 && strstr(out, "PAIR_TOTAL: 10000\r\n") && strstr(out, "STATUS: OK"));
    assert(PairDiag_Command("bins 3", out, sizeof(out)) > 0 && strstr(out, "BIN_31"));
    other_busy = true;
    assert(PairDiag_Command("test 10000", out, sizeof(out)) > 0 && strstr(out, "ERR: ACQ_OR_DIAGNOSTIC_ACTIVE"));
    other_busy = false;
    PairDiag_NativeStart(true, 84U, 0xAA68U, 0x4000U);
    for (uint32_t n = 0U; n < 20U; ++n) {
        SpiDiag_Result d = Result(3U, 2U, 8U, 3U), a = Result(0x100U, 12U, 19U, 4U), b = Result(0x110U, 22U, 29U, 4U);
        PairDiag_Irq(n * 84U, true); PairDiag_Begin(n, n + 1U, 0U);
        PairDiag_NativeResult(0U, &d, 0U, 0U); PairDiag_NativeResult(1U, &a, 0x100U, 0U);
        PairDiag_NativeResult(2U, &b, 0x100U, 0x110U);
    }
    PairDiag_NativeStop();
    assert(PairDiag_Command("events 15", out, sizeof(out)) > 0 && strstr(out, "SEQ: 15\r\n") && strstr(out, "XOR: 0x000010"));
    assert(strlen(out) < sizeof(out) && strstr(out, "RX=A5,00,01,00"));
    assert(PairDiag_Command("events 16", out, sizeof(out)) > 0 && strstr(out, "NO_EVENT"));
    assert(PairDiag_Command("report", out, sizeof(out)) > 0 && strstr(out, "PAIR_EVENTS_NOT_STORED: 4"));
    PairDiag_NativeStart(false, 150U, 0xAAA8U, 0x4000U); assert(!PairDiag_Tracing());
    assert(PairDiag_Command("report", out, sizeof(out)) > 0 && strstr(out, "PAIR_EVENTS_NOT_STORED: 4"));
    Init(&f, NULL); f.wrapper = true; f.missing_edges = true;
    assert(PairDiag_Command("test 10000", out, sizeof(out)) > 0 && strstr(out, "STARTED"));
    for (uint32_t guard = 0U; PairDiag_IsBusy() && guard < 10000U; ++guard) { Advance(&f, 1U); PairDiag_Process(); }
    assert(!PairDiag_IsBusy());
    assert(PairDiag_Command("wait", out, sizeof(out)) > 0 && strstr(out, "PAIR_WAIT_CONTEXT_VALID: 1") &&
        strstr(out, "WAIT_EXTI_PENDING: 1"));
}
int main(void) { TraceTests(); ExperimentTests(); CliTests(); puts("pair diagnostic tests: OK"); return 0; }
