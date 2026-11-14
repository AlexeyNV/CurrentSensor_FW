#include "format64.h"

bool Format64_Unsigned(char *out, size_t size, uint64_t value)
{
    char reverse[20];
    size_t count = 0U;
    size_t i;
    if (out == NULL) return false;
    do
    {
        reverse[count++] = (char)('0' + value % 10U);
        value /= 10U;
    } while (value != 0U);
    if (size <= count) return false;
    for (i = 0U; i < count; ++i) out[i] = reverse[count - 1U - i];
    out[count] = '\0';
    return true;
}

bool Format64_Signed(char *out, size_t size, int64_t value)
{
    uint64_t magnitude;
    if (out == NULL || size == 0U) return false;
    if (value >= 0) return Format64_Unsigned(out, size, (uint64_t)value);
    if (size < 2U) return false;
    /* Avoid negating INT64_MIN in signed arithmetic. */
    magnitude = (uint64_t)(-(value + 1)) + 1U;
    out[0] = '-';
    return Format64_Unsigned(out + 1U, size - 1U, magnitude);
}
