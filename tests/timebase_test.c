#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "timebase_core.h"

int main(void)
{
    uint64_t value;
    assert(TimebaseCore_Compose(0U, 0xFFFFFFFEU, false, 0U, &value));
    assert(value == 0xFFFFFFFEULL);
    assert(TimebaseCore_Compose(0U, 1U, true, 0U, &value));
    assert(value == 0x100000001ULL);
    assert(!TimebaseCore_Compose(0U, 1U, true, 1U, &value));
    assert(TimebaseCore_Compose(1U, 1U, false, 1U, &value));
    assert(value == 0x100000001ULL);
    assert(TimebaseCore_Compose(1U, 0xFFFFFFFEU, true, 1U, &value));
    assert(value == 0x1FFFFFFFEULL);
    assert(TimebaseCore_Compose(1U, 1U, true, 1U, &value));
    assert(value == 0x200000001ULL);
    assert(value - 0x100000001ULL == 0x100000000ULL);
    puts("TIM2 64-bit rollover tests passed");
    return 0;
}
