#ifndef TIMEBASE_H
#define TIMEBASE_H

#include <stdint.h>

void Timebase_Init(void);
void Timebase_UpdateIRQ(void);
uint64_t Timebase_NowUs64(void);

#endif
