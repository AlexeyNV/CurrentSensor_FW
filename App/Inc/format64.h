#ifndef FORMAT64_H
#define FORMAT64_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define FORMAT64_UDEC_SIZE 21U
#define FORMAT64_SDEC_SIZE 22U

bool Format64_Unsigned(char *out, size_t size, uint64_t value);
bool Format64_Signed(char *out, size_t size, int64_t value);

#endif
