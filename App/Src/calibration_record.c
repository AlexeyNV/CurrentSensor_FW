#include "calibration_record.h"

#include <limits.h>

#define CAL_RECORD_MAGIC   0x43414C32U /* CAL2 */
#define CAL_RECORD_VERSION 1U
#define CAL_RECORD_COMMIT  0xC01117EDU

static uint32_t CalibrationRecord_Crc32(const uint32_t *words)
{
    uint32_t crc = 0xFFFFFFFFU;
    size_t index;
    uint32_t byte_index;
    uint32_t bit;

    for (index = 0U; index < 7U; ++index)
    {
        for (byte_index = 0U; byte_index < 4U; ++byte_index)
        {
            crc ^= (words[index] >> (byte_index * 8U)) & 0xFFU;
            for (bit = 0U; bit < 8U; ++bit)
            {
                crc = (crc >> 1) ^ ((crc & 1U) ? 0xEDB88320U : 0U);
            }
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

void CalibrationRecord_Encode(const Calibration_Parameters *parameters, uint32_t sequence,
                              uint32_t words[CAL_RECORD_WORDS])
{
    words[0] = CAL_RECORD_MAGIC;
    words[1] = CAL_RECORD_VERSION;
    words[2] = CAL_RECORD_WORDS * sizeof(uint32_t);
    words[3] = sequence;
    words[4] = (uint32_t)parameters->offset_ua;
    words[5] = parameters->gain_ppm;
    words[6] = parameters->enabled ? 1U : 0U;
    words[7] = CalibrationRecord_Crc32(words);
    words[8] = CAL_RECORD_COMMIT;
}

bool CalibrationRecord_Decode(const uint32_t words[CAL_RECORD_WORDS],
                              Calibration_Parameters *parameters, uint32_t *sequence)
{
    int64_t offset;

    if (words[0] != CAL_RECORD_MAGIC || words[1] != CAL_RECORD_VERSION ||
        words[2] != CAL_RECORD_WORDS * sizeof(uint32_t) || words[3] == 0U ||
        words[6] > 1U || words[7] != CalibrationRecord_Crc32(words) ||
        words[8] != CAL_RECORD_COMMIT)
    {
        return false;
    }
    offset = words[4] <= INT32_MAX ? (int64_t)words[4] :
        (int64_t)words[4] - 0x100000000LL;
    if (offset < CAL_OFFSET_MIN_UA || offset > CAL_OFFSET_MAX_UA ||
        words[5] < CAL_GAIN_MIN_PPM || words[5] > CAL_GAIN_MAX_PPM)
    {
        return false;
    }
    parameters->offset_ua = (int32_t)offset;
    parameters->gain_ppm = words[5];
    parameters->enabled = words[6] != 0U;
    *sequence = words[3];
    return true;
}

void CalibrationRecord_Scan(const volatile uint32_t *words, size_t slot_count,
                            Calibration_RecordScan *scan)
{
    size_t slot;
    size_t index;
    uint32_t record[CAL_RECORD_WORDS];
    uint32_t sequence;
    Calibration_Parameters parameters;
    bool erased;

    scan->found = false;
    scan->sequence = 0U;
    scan->free_slot = -1;
    scan->occupied_unknown = false;
    for (slot = 0U; slot < slot_count; ++slot)
    {
        erased = true;
        for (index = 0U; index < CAL_RECORD_WORDS; ++index)
        {
            record[index] = words[slot * CAL_RECORD_WORDS + index];
            if (record[index] != 0xFFFFFFFFU) { erased = false; }
        }
        if (erased)
        {
            if (scan->free_slot < 0) { scan->free_slot = (int32_t)slot; }
        }
        else if (record[0] != CAL_RECORD_MAGIC)
        {
            scan->occupied_unknown = true;
        }
        else if (CalibrationRecord_Decode(record, &parameters, &sequence))
        {
            if (!scan->found || (sequence != scan->sequence &&
                (uint32_t)(sequence - scan->sequence) < 0x80000000U))
            {
                scan->latest = parameters;
                scan->sequence = sequence;
                scan->found = true;
            }
        }
    }
}
