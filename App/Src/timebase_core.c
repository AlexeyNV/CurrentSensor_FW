#include "timebase_core.h"

bool TimebaseCore_Compose(uint32_t before, uint32_t counter,
                          bool update_pending, uint32_t after,
                          uint64_t *value)
{
    if (value == 0 || before != after) return false;
    /* UIF may be pending while interrupts are briefly masked. A low counter
       belongs to the new epoch; a high counter was sampled before rollover. */
    if (update_pending && counter < 0x80000000U) ++before;
    *value = ((uint64_t)before << 32) | counter;
    return true;
}
