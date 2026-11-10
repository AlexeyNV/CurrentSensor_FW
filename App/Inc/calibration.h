#ifndef CALIBRATION_H
#define CALIBRATION_H

#include <stdbool.h>
#include <stdint.h>

#define CAL_OFFSET_MIN_UA (-100000)
#define CAL_OFFSET_MAX_UA 100000
#define CAL_GAIN_MIN_PPM 900000U
#define CAL_GAIN_MAX_PPM 1100000U

typedef struct
{
    int32_t offset_ua;
    uint32_t gain_ppm;
    bool enabled;
} Calibration_Parameters;

typedef enum
{
    CAL_SOURCE_DEFAULT,
    CAL_SOURCE_FLASH,
    CAL_SOURCE_RAM
} Calibration_Source;

typedef struct
{
    Calibration_Parameters parameters;
    Calibration_Source source;
    bool dirty;
} Calibration_State;

typedef enum
{
    CAL_OK,
    CAL_BAD_NUMBER,
    CAL_NUMBER_OVERFLOW,
    CAL_OUT_OF_RANGE,
    CAL_OVERFLOW,
    CAL_STORAGE_UNAVAILABLE,
    CAL_STORAGE_FULL,
    CAL_STORAGE_ERROR,
    CAL_NO_RECORD,
    CAL_BUSY_STREAMING,
    CAL_UNKNOWN_COMMAND
} Calibration_Status;

typedef enum
{
    CAL_CMD_SHOW,
    CAL_CMD_SET_OFFSET,
    CAL_CMD_SET_GAIN,
    CAL_CMD_ENABLE,
    CAL_CMD_SAVE,
    CAL_CMD_LOAD,
    CAL_CMD_RESET
} Calibration_CommandType;

typedef struct
{
    Calibration_CommandType type;
    int32_t offset_ua;
    uint32_t gain_ppm;
    bool enabled;
} Calibration_Command;

void Calibration_Init(void);
const Calibration_State *Calibration_GetState(void);
Calibration_Status Calibration_SetOffset(int32_t offset_ua);
Calibration_Status Calibration_SetGain(uint32_t gain_ppm);
void Calibration_SetEnabled(bool enabled);
void Calibration_Reset(void);
Calibration_Status Calibration_Save(bool streaming_active);
Calibration_Status Calibration_Load(void);
Calibration_Status Calibration_ApplyRawCurrent(int64_t raw_na, int64_t *cal_na);
Calibration_Status Calibration_ComputeCurrent(int32_t shunt_code, bool adc_range,
                                              int64_t *raw_na, int64_t *cal_na);
Calibration_Status Calibration_ParseCommand(const char *line, Calibration_Command *command);
const char *Calibration_StatusText(Calibration_Status status);
const char *Calibration_SourceText(Calibration_Source source);

#endif
