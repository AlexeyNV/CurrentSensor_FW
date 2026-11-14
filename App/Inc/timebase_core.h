#ifndef TIMEBASE_CORE_H
#define TIMEBASE_CORE_H

#include <stdbool.h>
#include <stdint.h>

/* Return false when the overflow IRQ changed the high word during the read. */
bool TimebaseCore_Compose(uint32_t before, uint32_t counter,
                          bool update_pending, uint32_t after,
                          uint64_t *value);

#endif
