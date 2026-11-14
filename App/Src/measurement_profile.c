#include "measurement_profile.h"

#include <ctype.h>
#include <stddef.h>
#include <stdio.h>

static const MeasurementProfile profiles[PROFILE_COUNT] = {
    {PROFILE_FAST, "FAST", 10000U, 100U, 50U, 1U, 0xAA28U},
    {PROFILE_BALANCED, "BALANCED", 5000U, 200U, 150U, 1U, 0xAAA8U},
    {PROFILE_PRECISION, "PRECISION", 2500U, 400U, 280U, 1U, 0xAAE8U},
    {PROFILE_LOW_NOISE, "LOW_NOISE", 1000U, 1000U, 540U, 1U, 0xAB28U}
};
static MeasurementProfile_Id selected = PROFILE_FAST;

int MeasurementProfile_Format(const MeasurementProfile *profile,
                              char *out, size_t size)
{
    return snprintf(out, size,
        "PROFILE_ID: %u\r\nPROFILE: %s\r\nSAMPLE_RATE_HZ: %lu\r\n"
        "SAMPLE_PERIOD_US: %u\r\nPROFILE_VSHCT_US: %u\r\n"
        "PROFILE_AVG: %u\r\nPROFILE_ADC_CONFIG: 0x%04X\r\n",
        (unsigned int)profile->id, profile->name,
        (unsigned long)profile->rate_hz, profile->period_us,
        profile->vshct_us, profile->averages, profile->adc_config);
}

const MeasurementProfile *MeasurementProfile_Get(MeasurementProfile_Id id)
{
    return (unsigned int)id < PROFILE_COUNT ? &profiles[id] : NULL;
}

const MeasurementProfile *MeasurementProfile_Selected(void)
{
    return MeasurementProfile_Get(selected);
}

bool MeasurementProfile_Select(MeasurementProfile_Id id)
{
    if ((unsigned int)id >= PROFILE_COUNT) return false;
    selected = id;
    return true;
}

const MeasurementProfile *MeasurementProfile_Find(const char *name)
{
    unsigned int i;
    if (name == NULL || *name == '\0') return NULL;
    for (i = 0U; i < PROFILE_COUNT; ++i)
    {
        const char *a = name;
        const char *b = profiles[i].name;
        while (*a != '\0' && *b != '\0' &&
               toupper((unsigned char)*a) == (unsigned char)*b)
        {
            ++a;
            ++b;
        }
        if (*a == '\0' && *b == '\0') return &profiles[i];
    }
    return NULL;
}

bool MeasurementProfile_TimerDividers(uint32_t timer_clock_hz,
                                      uint16_t period_us,
                                      uint16_t *psc, uint16_t *arr)
{
    if (psc == NULL || arr == NULL || period_us == 0U ||
        timer_clock_hz < 1000000U || timer_clock_hz % 1000000U != 0U ||
        timer_clock_hz / 1000000U > 65536U) return false;
    *psc = (uint16_t)(timer_clock_hz / 1000000U - 1U);
    *arr = (uint16_t)(period_us - 1U);
    return true;
}
