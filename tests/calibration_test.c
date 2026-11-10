#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "calibration.h"
#include "calibration_record.h"
#include "calibration_storage.h"

static int failures;
static int saves;
static bool flash_valid;
static Calibration_Parameters flash_parameters;

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    ++failures; } } while (0)

Calibration_Status CalibrationStorage_Load(Calibration_Parameters *parameters)
{
    if (!flash_valid) { return CAL_NO_RECORD; }
    *parameters = flash_parameters;
    return CAL_OK;
}

Calibration_Status CalibrationStorage_Save(const Calibration_Parameters *parameters)
{
    flash_parameters = *parameters;
    flash_valid = true;
    ++saves;
    return CAL_OK;
}

static void TestArithmetic(void)
{
    int64_t raw;
    int64_t calibrated;

    flash_valid = false;
    Calibration_Init();
    CHECK(Calibration_ComputeCurrent(256, false, &raw, &calibrated) == CAL_OK);
    CHECK(raw == 16000000LL && calibrated == raw);
    CHECK(Calibration_ComputeCurrent(-256, true, &raw, &calibrated) == CAL_OK);
    CHECK(raw == -4000000LL && calibrated == raw);

    CHECK(Calibration_SetOffset(-1000) == CAL_OK);
    CHECK(Calibration_SetGain(1100000U) == CAL_OK);
    Calibration_SetEnabled(true);
    CHECK(Calibration_ComputeCurrent(16, false, &raw, &calibrated) == CAL_OK);
    CHECK(raw == 1000000LL && calibrated == 2200000LL);
    CHECK(Calibration_ComputeCurrent(-16, true, &raw, &calibrated) == CAL_OK);
    CHECK(raw == -250000LL && calibrated == 825000LL);

    CHECK(Calibration_SetOffset(0) == CAL_OK);
    CHECK(Calibration_SetGain(900000U) == CAL_OK);
    CHECK(Calibration_ApplyRawCurrent(5, &calibrated) == CAL_OK && calibrated == 5);
    CHECK(Calibration_ApplyRawCurrent(-5, &calibrated) == CAL_OK && calibrated == -5);
    CHECK(Calibration_ApplyRawCurrent(4, &calibrated) == CAL_OK && calibrated == 4);
    CHECK(Calibration_ApplyRawCurrent(-4, &calibrated) == CAL_OK && calibrated == -4);

    CHECK(Calibration_SetOffset(100000) == CAL_OK);
    CHECK(Calibration_ApplyRawCurrent(INT64_MIN, &calibrated) == CAL_OVERFLOW);
    CHECK(Calibration_SetOffset(-100000) == CAL_OK);
    CHECK(Calibration_ApplyRawCurrent(INT64_MAX, &calibrated) == CAL_OVERFLOW);
    CHECK(Calibration_SetOffset(0) == CAL_OK);
    CHECK(Calibration_ApplyRawCurrent(INT64_MAX, &calibrated) == CAL_OK);
    CHECK(calibrated == 8301034833169298226LL);
    CHECK(Calibration_ApplyRawCurrent(INT64_MIN, &calibrated) == CAL_OK);
    CHECK(calibrated == -8301034833169298227LL);
    CHECK(Calibration_SetGain(1100000U) == CAL_OK);
    CHECK(Calibration_ApplyRawCurrent(INT64_MAX, &calibrated) == CAL_OVERFLOW);
    CHECK(Calibration_ComputeCurrent(524288, false, &raw, &calibrated) == CAL_OUT_OF_RANGE);
    CHECK(Calibration_ComputeCurrent(-524289, false, &raw, &calibrated) == CAL_OUT_OF_RANGE);

    Calibration_SetEnabled(false);
    CHECK(Calibration_ApplyRawCurrent(INT64_MIN, &calibrated) == CAL_OK);
    CHECK(calibrated == INT64_MIN);
}

static void TestParser(void)
{
    Calibration_Command command;
    Calibration_Command before;
    Calibration_Parameters previous;

    CHECK(Calibration_ParseCommand("cal show", &command) == CAL_OK && command.type == CAL_CMD_SHOW);
    CHECK(Calibration_ParseCommand("cal save", &command) == CAL_OK && command.type == CAL_CMD_SAVE);
    CHECK(Calibration_ParseCommand("cal load", &command) == CAL_OK && command.type == CAL_CMD_LOAD);
    CHECK(Calibration_ParseCommand("cal reset", &command) == CAL_OK && command.type == CAL_CMD_RESET);
    CHECK(Calibration_ParseCommand("cal enable 1", &command) == CAL_OK && command.enabled);
    CHECK(Calibration_ParseCommand("cal enable 0", &command) == CAL_OK && !command.enabled);
    CHECK(Calibration_ParseCommand("cal set offset_ua -100000", &command) == CAL_OK &&
          command.type == CAL_CMD_SET_OFFSET && command.offset_ua == -100000);
    CHECK(Calibration_ParseCommand("cal set offset_ua +100000", &command) == CAL_OK &&
          command.offset_ua == 100000);
    CHECK(Calibration_ParseCommand("cal set gain_ppm 1100000", &command) == CAL_OK &&
          command.type == CAL_CMD_SET_GAIN && command.gain_ppm == 1100000U);

    before = command;
    CHECK(Calibration_ParseCommand("cal set offset_ua -", &command) == CAL_BAD_NUMBER);
    CHECK(Calibration_ParseCommand("cal set offset_ua 12x", &command) == CAL_BAD_NUMBER);
    CHECK(Calibration_ParseCommand("cal set offset_ua 999999999999999", &command) == CAL_NUMBER_OVERFLOW);
    CHECK(Calibration_ParseCommand("cal set gain_ppm 4294967296", &command) == CAL_NUMBER_OVERFLOW);
    CHECK(Calibration_ParseCommand("cal set gain_ppm -1", &command) == CAL_BAD_NUMBER);
    CHECK(Calibration_ParseCommand("cal enable 2", &command) == CAL_UNKNOWN_COMMAND);
    CHECK(Calibration_ParseCommand("cal show ", &command) == CAL_UNKNOWN_COMMAND);
    CHECK(memcmp(&command, &before, sizeof(command)) == 0);

    previous = Calibration_GetState()->parameters;
    CHECK(Calibration_SetOffset(100001) == CAL_OUT_OF_RANGE);
    CHECK(Calibration_SetOffset(-100001) == CAL_OUT_OF_RANGE);
    CHECK(Calibration_SetGain(899999U) == CAL_OUT_OF_RANGE);
    CHECK(Calibration_SetGain(1100001U) == CAL_OUT_OF_RANGE);
    CHECK(Calibration_GetState()->parameters.offset_ua == previous.offset_ua &&
          Calibration_GetState()->parameters.gain_ppm == previous.gain_ppm &&
          Calibration_GetState()->parameters.enabled == previous.enabled);
}

static void TestState(void)
{
    const Calibration_State *state;
    int prior_saves;

    flash_valid = false;
    Calibration_Init();
    state = Calibration_GetState();
    CHECK(state->source == CAL_SOURCE_DEFAULT && !state->dirty);
    CHECK(state->parameters.offset_ua == 0 && state->parameters.gain_ppm == 1000000U &&
          !state->parameters.enabled);
    CHECK(Calibration_SetOffset(25) == CAL_OK);
    CHECK(state->source == CAL_SOURCE_RAM && state->dirty);
    CHECK(Calibration_SetOffset(0) == CAL_OK);
    CHECK(state->source == CAL_SOURCE_RAM && !state->dirty);
    Calibration_SetEnabled(true);
    CHECK(state->dirty);
    prior_saves = saves;
    CHECK(Calibration_Save(true) == CAL_BUSY_STREAMING && saves == prior_saves);
    CHECK(Calibration_Save(false) == CAL_OK && saves == prior_saves + 1);
    CHECK(state->source == CAL_SOURCE_FLASH && !state->dirty);
    Calibration_Reset();
    CHECK(state->source == CAL_SOURCE_DEFAULT && state->dirty && !state->parameters.enabled);
    CHECK(Calibration_Load() == CAL_OK && state->source == CAL_SOURCE_FLASH &&
          !state->dirty && state->parameters.enabled);
    Calibration_Init();
    CHECK(state->source == CAL_SOURCE_FLASH && state->parameters.enabled);
}

static void TestRecords(void)
{
    Calibration_Parameters profile = {-100000, 1100000U, true};
    Calibration_Parameters decoded;
    Calibration_Parameters factory = {0, 1000000U, false};
    Calibration_RecordScan scan;
    uint32_t sequence;
    uint32_t words[CAL_RECORD_WORDS];
    uint32_t journal[CAL_RECORD_WORDS * 4U];
    size_t i;

    CalibrationRecord_Encode(&factory, 1U, words);
    CHECK(words[7] == 0x729F5EEBU); /* Independently checked IEEE CRC32. */
    CHECK(CalibrationRecord_Decode(words, &decoded, &sequence));
    CHECK(sequence == 1U && decoded.offset_ua == 0 && !decoded.enabled);
    words[4] ^= 1U;
    CHECK(!CalibrationRecord_Decode(words, &decoded, &sequence));
    CalibrationRecord_Encode(&profile, 2U, words);
    CHECK(CalibrationRecord_Decode(words, &decoded, &sequence));
    CHECK(decoded.offset_ua == -100000 && decoded.gain_ppm == 1100000U && decoded.enabled);
    words[8] = 0xFFFFFFFFU;
    CHECK(!CalibrationRecord_Decode(words, &decoded, &sequence));

    for (i = 0U; i < CAL_RECORD_WORDS * 4U; ++i) { journal[i] = 0xFFFFFFFFU; }
    CalibrationRecord_Encode(&factory, 1U, &journal[0]);
    CalibrationRecord_Encode(&profile, 2U, &journal[CAL_RECORD_WORDS]);
    journal[CAL_RECORD_WORDS * 2U] = 0x43414C32U; /* Power loss before commit. */
    CalibrationRecord_Scan(journal, 4U, &scan);
    CHECK(scan.found && scan.sequence == 2U && scan.free_slot == 3);
    CHECK(scan.latest.offset_ua == -100000 && scan.latest.enabled);
    journal[CAL_RECORD_WORDS + 7U] ^= 1U; /* Corrupt newest CRC: use older record. */
    CalibrationRecord_Scan(journal, 4U, &scan);
    CHECK(scan.found && scan.sequence == 1U && scan.latest.offset_ua == 0);
    journal[7] ^= 1U;
    CalibrationRecord_Scan(journal, 4U, &scan);
    CHECK(!scan.found && scan.free_slot == 3 && !scan.occupied_unknown);
    journal[0] = 0x12345678U; /* Unrelated data must block a new save. */
    CalibrationRecord_Scan(journal, 4U, &scan);
    CHECK(scan.occupied_unknown);
}

int main(void)
{
    TestArithmetic();
    TestParser();
    TestState();
    TestRecords();
    if (failures == 0) { puts("calibration tests: PASS"); }
    return failures == 0 ? 0 : 1;
}
