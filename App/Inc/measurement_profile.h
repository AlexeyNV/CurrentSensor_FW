#ifndef MEASUREMENT_PROFILE_H
#define MEASUREMENT_PROFILE_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

typedef enum
{
    PROFILE_FAST = 0,
    PROFILE_BALANCED,
    PROFILE_PRECISION,
    PROFILE_LOW_NOISE,
    PROFILE_COUNT
} MeasurementProfile_Id;

typedef struct
{
    MeasurementProfile_Id id;
    const char *name;
    uint32_t rate_hz;
    uint16_t period_us;
    uint16_t vshct_us;
    uint16_t averages;
    uint16_t adc_config;
} MeasurementProfile;

const MeasurementProfile *MeasurementProfile_Get(MeasurementProfile_Id id);
const MeasurementProfile *MeasurementProfile_Find(const char *name);
const MeasurementProfile *MeasurementProfile_Selected(void);
bool MeasurementProfile_Select(MeasurementProfile_Id id);
int MeasurementProfile_Format(const MeasurementProfile *profile,
                              char *out, size_t size);
bool MeasurementProfile_TimerDividers(uint32_t timer_clock_hz,
                                      uint16_t period_us,
                                      uint16_t *psc, uint16_t *arr);

#endif
