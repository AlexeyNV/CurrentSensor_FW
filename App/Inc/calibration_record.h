#ifndef CALIBRATION_RECORD_H
#define CALIBRATION_RECORD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "calibration.h"

#define CAL_RECORD_WORDS 9U

typedef struct
{
    bool found;
    Calibration_Parameters latest;
    uint32_t sequence;
    int32_t free_slot;
    bool occupied_unknown;
} Calibration_RecordScan;

void CalibrationRecord_Encode(const Calibration_Parameters *parameters, uint32_t sequence,
                              uint32_t words[CAL_RECORD_WORDS]);
bool CalibrationRecord_Decode(const uint32_t words[CAL_RECORD_WORDS],
                              Calibration_Parameters *parameters, uint32_t *sequence);
void CalibrationRecord_Scan(const volatile uint32_t *words, size_t slot_count,
                            Calibration_RecordScan *scan);

#endif
