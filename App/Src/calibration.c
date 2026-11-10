#include "calibration.h"

#include <limits.h>
#include <stddef.h>
#include <string.h>

#include "calibration_storage.h"

static const Calibration_Parameters factory = {0, 1000000U, false};
static Calibration_State state;
static Calibration_Parameters persisted;

static bool Calibration_Equal(const Calibration_Parameters *a, const Calibration_Parameters *b)
{
    return a->offset_ua == b->offset_ua && a->gain_ppm == b->gain_ppm &&
           a->enabled == b->enabled;
}

static void Calibration_UpdateDirty(void)
{
    state.dirty = !Calibration_Equal(&state.parameters, &persisted);
}

void Calibration_Init(void)
{
    state.parameters = factory;
    persisted = factory;
    state.source = CAL_SOURCE_DEFAULT;
    state.dirty = false;
    (void)Calibration_Load();
}

const Calibration_State *Calibration_GetState(void)
{
    return &state;
}

Calibration_Status Calibration_SetOffset(int32_t offset_ua)
{
    if (offset_ua < CAL_OFFSET_MIN_UA || offset_ua > CAL_OFFSET_MAX_UA)
    {
        return CAL_OUT_OF_RANGE;
    }
    state.parameters.offset_ua = offset_ua;
    state.source = CAL_SOURCE_RAM;
    Calibration_UpdateDirty();
    return CAL_OK;
}

Calibration_Status Calibration_SetGain(uint32_t gain_ppm)
{
    if (gain_ppm < CAL_GAIN_MIN_PPM || gain_ppm > CAL_GAIN_MAX_PPM)
    {
        return CAL_OUT_OF_RANGE;
    }
    state.parameters.gain_ppm = gain_ppm;
    state.source = CAL_SOURCE_RAM;
    Calibration_UpdateDirty();
    return CAL_OK;
}

void Calibration_SetEnabled(bool enabled)
{
    state.parameters.enabled = enabled;
    state.source = CAL_SOURCE_RAM;
    Calibration_UpdateDirty();
}

void Calibration_Reset(void)
{
    state.parameters = factory;
    state.source = CAL_SOURCE_DEFAULT;
    Calibration_UpdateDirty();
}

Calibration_Status Calibration_Save(bool streaming_active)
{
    Calibration_Status result;

    if (streaming_active)
    {
        return CAL_BUSY_STREAMING;
    }
    result = CalibrationStorage_Save(&state.parameters);
    if (result == CAL_OK)
    {
        persisted = state.parameters;
        state.source = CAL_SOURCE_FLASH;
        state.dirty = false;
    }
    return result;
}

Calibration_Status Calibration_Load(void)
{
    Calibration_Parameters loaded;
    Calibration_Status result = CalibrationStorage_Load(&loaded);

    if (result == CAL_NO_RECORD)
    {
        state.parameters = factory;
        persisted = factory;
        state.source = CAL_SOURCE_DEFAULT;
        state.dirty = false;
        return CAL_OK;
    }
    if (result == CAL_OK)
    {
        state.parameters = loaded;
        persisted = loaded;
        state.source = CAL_SOURCE_FLASH;
        state.dirty = false;
    }
    return result;
}

Calibration_Status Calibration_ApplyRawCurrent(int64_t raw_na, int64_t *cal_na)
{
    int64_t offset_na;
    int64_t adjusted;
    uint64_t magnitude;
    uint64_t whole;
    uint64_t fractional;
    uint64_t rounded;
    uint64_t limit;
    bool negative;

    if (cal_na == NULL)
    {
        return CAL_OUT_OF_RANGE;
    }
    if (!state.parameters.enabled)
    {
        *cal_na = raw_na;
        return CAL_OK;
    }
    offset_na = (int64_t)state.parameters.offset_ua * 1000LL;
    if ((offset_na > 0 && raw_na < INT64_MIN + offset_na) ||
        (offset_na < 0 && raw_na > INT64_MAX + offset_na))
    {
        return CAL_OVERFLOW;
    }
    adjusted = raw_na - offset_na;
    negative = adjusted < 0;
    magnitude = negative ? (uint64_t)(-(adjusted + 1)) + 1U : (uint64_t)adjusted;
    limit = negative ? (uint64_t)INT64_MAX + 1U : (uint64_t)INT64_MAX;
    whole = magnitude / 1000000U;
    fractional = ((magnitude % 1000000U) * state.parameters.gain_ppm + 500000U) /
                 1000000U; /* Half away from zero. */
    if (whole > limit / state.parameters.gain_ppm)
    {
        return CAL_OVERFLOW;
    }
    rounded = whole * state.parameters.gain_ppm;
    if (fractional > limit - rounded) { return CAL_OVERFLOW; }
    rounded += fractional;
    *cal_na = negative ?
        ((rounded == (uint64_t)INT64_MAX + 1U) ? INT64_MIN : -(int64_t)rounded) :
        (int64_t)rounded;
    return CAL_OK;
}

Calibration_Status Calibration_ComputeCurrent(int32_t shunt_code, bool adc_range,
                                              int64_t *raw_na, int64_t *cal_na)
{
    int64_t calculated;

    if (raw_na == NULL || cal_na == NULL || shunt_code < -524288 || shunt_code > 524287)
    {
        return CAL_OUT_OF_RANGE;
    }
    calculated = (int64_t)shunt_code * (adc_range ? 15625LL : 62500LL);
    if (Calibration_ApplyRawCurrent(calculated, cal_na) != CAL_OK)
    {
        return CAL_OVERFLOW;
    }
    *raw_na = calculated;
    return CAL_OK;
}

static Calibration_Status Calibration_ParseMagnitude(const char *text, uint64_t limit,
                                                     uint64_t *value)
{
    uint64_t result = 0U;
    uint32_t digit;

    if (*text == '\0')
    {
        return CAL_BAD_NUMBER;
    }
    while (*text != '\0')
    {
        if (*text < '0' || *text > '9')
        {
            return CAL_BAD_NUMBER;
        }
        digit = (uint32_t)(*text - '0');
        if (result > (limit - digit) / 10U)
        {
            return CAL_NUMBER_OVERFLOW;
        }
        result = result * 10U + digit;
        ++text;
    }
    *value = result;
    return CAL_OK;
}

Calibration_Status Calibration_ParseCommand(const char *line, Calibration_Command *command)
{
    static const char offset_prefix[] = "cal set offset_ua ";
    static const char gain_prefix[] = "cal set gain_ppm ";
    uint64_t magnitude;
    bool negative;
    const char *number;
    Calibration_Status result;
    Calibration_Command parsed = {0};

    if (line == NULL || command == NULL)
    {
        return CAL_UNKNOWN_COMMAND;
    }
    if (strcmp(line, "cal show") == 0) { parsed.type = CAL_CMD_SHOW; }
    else if (strcmp(line, "cal save") == 0) { parsed.type = CAL_CMD_SAVE; }
    else if (strcmp(line, "cal load") == 0) { parsed.type = CAL_CMD_LOAD; }
    else if (strcmp(line, "cal reset") == 0) { parsed.type = CAL_CMD_RESET; }
    else if (strcmp(line, "cal enable 0") == 0 || strcmp(line, "cal enable 1") == 0)
    {
        parsed.type = CAL_CMD_ENABLE;
        parsed.enabled = line[11] == '1';
    }
    else if (strncmp(line, offset_prefix, sizeof(offset_prefix) - 1U) == 0)
    {
        number = line + sizeof(offset_prefix) - 1U;
        negative = *number == '-';
        if (*number == '-' || *number == '+') { ++number; }
        result = Calibration_ParseMagnitude(number,
            negative ? (uint64_t)INT32_MAX + 1U : INT32_MAX, &magnitude);
        if (result != CAL_OK) { return result; }
        parsed.type = CAL_CMD_SET_OFFSET;
        parsed.offset_ua = negative ?
            ((magnitude == (uint64_t)INT32_MAX + 1U) ? INT32_MIN : -(int32_t)magnitude) :
            (int32_t)magnitude;
    }
    else if (strncmp(line, gain_prefix, sizeof(gain_prefix) - 1U) == 0)
    {
        result = Calibration_ParseMagnitude(line + sizeof(gain_prefix) - 1U,
                                            UINT32_MAX, &magnitude);
        if (result != CAL_OK) { return result; }
        parsed.type = CAL_CMD_SET_GAIN;
        parsed.gain_ppm = (uint32_t)magnitude;
    }
    else
    {
        return CAL_UNKNOWN_COMMAND;
    }
    *command = parsed;
    return CAL_OK;
}

const char *Calibration_StatusText(Calibration_Status status)
{
    switch (status)
    {
    case CAL_OK: return "OK";
    case CAL_BAD_NUMBER: return "BAD_NUMBER";
    case CAL_NUMBER_OVERFLOW: return "NUMBER_OVERFLOW";
    case CAL_OUT_OF_RANGE: return "OUT_OF_RANGE";
    case CAL_OVERFLOW: return "OVERFLOW";
    case CAL_STORAGE_UNAVAILABLE: return "STORAGE_UNAVAILABLE";
    case CAL_STORAGE_FULL: return "STORAGE_FULL";
    case CAL_STORAGE_ERROR: return "STORAGE_ERROR";
    case CAL_NO_RECORD: return "NO_RECORD";
    case CAL_BUSY_STREAMING: return "BUSY_STREAMING";
    default: return "UNKNOWN_COMMAND";
    }
}

const char *Calibration_SourceText(Calibration_Source source)
{
    switch (source)
    {
    case CAL_SOURCE_FLASH: return "FLASH";
    case CAL_SOURCE_RAM: return "RAM";
    default: return "DEFAULT";
    }
}
