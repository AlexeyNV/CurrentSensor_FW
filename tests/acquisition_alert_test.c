#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "acquisition_alert_core.h"

static void normal_and_duplicate(void)
{
    AcqAlert_Core c;
    AcqAlertCore_Reset(&c);
    AcqAlertCore_Tick(&c, 1U, 100U);
    assert(c.state == ALERT_PENDING && c.slot == 1U);
    AcqAlertCore_Trigger(&c, 110U);
    assert(c.max_trigger_latency_us == 10U);
    AcqAlertCore_Irq(&c, 180U, true, true);
    assert(c.state == ALERT_READING && c.irq_count == 1U);
    assert(c.max_trigger_to_alert_us == 70U);
    AcqAlertCore_Irq(&c, 182U, true, true);
    assert(c.spurious == 1U && c.irq_count == 2U);
    assert(c.min_period_us == 2U && c.max_period_us == 2U);
    assert(AcqAlertCore_Read(&c, 190U));
    assert(c.max_alert_to_read_us == 10U);
    AcqAlertCore_Tick(&c, 2U, 200U);
    assert(c.missed_slots == 1U && c.slot == 1U);
    AcqAlertCore_Finish(&c, 210U);
    assert(c.state == ALERT_IDLE && c.max_cycle_us == 110U);
    AcqAlertCore_Tick(&c, 3U, 300U);
    assert(c.slot == 3U);
}

static void absent_and_stop(void)
{
    AcqAlert_Core c;
    AcqAlertCore_Reset(&c);
    AcqAlertCore_Tick(&c, 10U, 1000U);
    AcqAlertCore_Trigger(&c, 1010U);
    assert(!AcqAlertCore_Timeout(&c, 1510U, 500U));
    assert(AcqAlertCore_Timeout(&c, 1511U, 500U));
    assert(c.timeouts == 1U && c.state == ALERT_IDLE);
    AcqAlertCore_Irq(&c, 1520U, true, true);
    assert(c.spurious == 1U);
    AcqAlertCore_Tick(&c, 11U, 1600U);
    AcqAlertCore_Trigger(&c, 1610U);
    AcqAlertCore_Stop(&c);
    assert(c.state == ALERT_IDLE);
    AcqAlertCore_Irq(&c, 1700U, false, true);
    assert(c.spurious == 2U);
}

static void monitor_and_rollover(void)
{
    AcqAlert_Core c;
    AcqAlertCore_Reset(&c);
    AcqAlertCore_Irq(&c, 0xFFFFFFF0U, true, false);
    AcqAlertCore_Irq(&c, 0x00000022U, true, false);
    assert(c.irq_count == 2U && c.min_period_us == 50U);
    assert(c.max_period_us == 50U && c.spurious == 0U);
    AcqAlertCore_Irq(&c, 0x00000030U, false, false);
    assert(c.spurious == 1U && c.irq_count == 2U);
}

static void stop_during_second_read_or_disconnect(void)
{
    AcqAlert_Core c;
    AcqAlertCore_Reset(&c);
    AcqAlertCore_Tick(&c, 20U, 2000U);
    AcqAlertCore_Trigger(&c, 2015U);
    AcqAlertCore_Irq(&c, 2150U, true, true);
    assert(c.state == ALERT_READING);
    AcqAlertCore_Tick(&c, 21U, 2200U);
    assert(c.missed_slots == 1U);
    AcqAlertCore_Stop(&c); /* STOP or USB disconnect while DMA owns SPI */
    assert(c.state == ALERT_IDLE);
    AcqAlertCore_Tick(&c, 22U, 2300U);
    assert(c.state == ALERT_PENDING && c.slot == 22U);
}

int main(void)
{
    normal_and_duplicate();
    absent_and_stop();
    monitor_and_rollover();
    stop_during_second_read_or_disconnect();
    puts("acquisition alert tests: OK");
    return 0;
}
