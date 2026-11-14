#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "format64.h"

static void unsigned_case(uint64_t value, const char *expected)
{
    char text[FORMAT64_UDEC_SIZE];
    assert(Format64_Unsigned(text, sizeof(text), value));
    assert(strcmp(text, expected) == 0);
}

static void test_values(void)
{
    char text[FORMAT64_SDEC_SIZE];
    unsigned_case(0U, "0");
    unsigned_case(1U, "1");
    unsigned_case(1000014U, "1000014");
    unsigned_case(UINT32_MAX, "4294967295");
    unsigned_case(4294967296ULL, "4294967296");
    unsigned_case(5402197401ULL, "5402197401");
    unsigned_case(UINT64_MAX, "18446744073709551615");
    assert(Format64_Signed(text, sizeof(text), INT64_MIN));
    assert(strcmp(text, "-9223372036854775808") == 0);
    assert(Format64_Signed(text, sizeof(text), -1));
    assert(strcmp(text, "-1") == 0);
    assert(!Format64_Unsigned(text, 2U, UINT64_MAX));
}

static void test_following_fields(void)
{
    char number[FORMAT64_UDEC_SIZE];
    char report[160];
    int length;
    assert(Format64_Unsigned(number, sizeof(number), 5402197401ULL));
    length = snprintf(report, sizeof(report),
        "ACQ_ELAPSED_US64: %s\r\nTIMER_TICKS: %lu\r\n"
        "SAMPLES: %lu\r\nRATE_HZ: %lu\r\nSTATUS: %s\r\n",
        number, 10000UL, 9999UL, 9999UL, "OK");
    assert(length > 0 && (size_t)length < sizeof(report));
    assert(strcmp(report,
        "ACQ_ELAPSED_US64: 5402197401\r\nTIMER_TICKS: 10000\r\n"
        "SAMPLES: 9999\r\nRATE_HZ: 9999\r\nSTATUS: OK\r\n") == 0);
}

int main(void)
{
    test_values();
    test_following_fields();
    puts("format64 tests passed");
    return 0;
}
