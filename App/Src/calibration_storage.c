#include "calibration_storage.h"

#include <stddef.h>
#include <stdint.h>

#include "calibration_record.h"
#include "stm32f4xx_hal.h"

#define CAL_FLASH_START 0x08060000UL
#define CAL_FLASH_END   0x08080000UL
#define CAL_FLASH_BYTES (CAL_FLASH_END - CAL_FLASH_START)
#define CAL_SLOT_COUNT  (CAL_FLASH_BYTES / (CAL_RECORD_WORDS * sizeof(uint32_t)))

extern uint32_t __cal_flash_start;
extern uint32_t __cal_flash_end;

static bool CalibrationStorage_Available(void)
{
    uint16_t flash_kb = *(const volatile uint16_t *)FLASHSIZE_BASE;

    return flash_kb == 512U &&
           (uintptr_t)&__cal_flash_start == CAL_FLASH_START &&
           (uintptr_t)&__cal_flash_end == CAL_FLASH_END;
}

Calibration_Status CalibrationStorage_Load(Calibration_Parameters *parameters)
{
    Calibration_RecordScan scan;

    if (!CalibrationStorage_Available()) { return CAL_STORAGE_UNAVAILABLE; }
    CalibrationRecord_Scan((const volatile uint32_t *)CAL_FLASH_START, CAL_SLOT_COUNT, &scan);
    if (!scan.found) { return CAL_NO_RECORD; }
    *parameters = scan.latest;
    return CAL_OK;
}

Calibration_Status CalibrationStorage_Save(const Calibration_Parameters *parameters)
{
    Calibration_RecordScan scan;
    uint32_t record[CAL_RECORD_WORDS];
    uint32_t address;
    uint32_t sequence;
    uint32_t index;
    bool success = true;

    if (!CalibrationStorage_Available()) { return CAL_STORAGE_UNAVAILABLE; }
    CalibrationRecord_Scan((const volatile uint32_t *)CAL_FLASH_START, CAL_SLOT_COUNT, &scan);
    if (scan.occupied_unknown) { return CAL_STORAGE_UNAVAILABLE; }
    if (scan.free_slot < 0) { return CAL_STORAGE_FULL; }

    sequence = scan.found ? scan.sequence + 1U : 1U;
    if (sequence == 0U) { sequence = 1U; }
    CalibrationRecord_Encode(parameters, sequence, record);
    address = CAL_FLASH_START + (uint32_t)scan.free_slot *
              CAL_RECORD_WORDS * sizeof(uint32_t);
    if (HAL_FLASH_Unlock() != HAL_OK) { return CAL_STORAGE_ERROR; }

    /* The commit word is last: an interrupted write leaves the older record valid. */
    for (index = 0U; index < CAL_RECORD_WORDS; ++index)
    {
        uint32_t word_address = address + index * sizeof(uint32_t);
        if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_WORD, word_address, record[index]) != HAL_OK ||
            *(const volatile uint32_t *)word_address != record[index])
        {
            success = false;
            break;
        }
    }
    if (HAL_FLASH_Lock() != HAL_OK) { success = false; }
    if (!success) { return CAL_STORAGE_ERROR; }

    return CAL_OK;
}
