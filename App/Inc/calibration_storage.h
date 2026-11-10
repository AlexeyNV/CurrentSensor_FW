#ifndef CALIBRATION_STORAGE_H
#define CALIBRATION_STORAGE_H

#include "calibration.h"

Calibration_Status CalibrationStorage_Load(Calibration_Parameters *parameters);
Calibration_Status CalibrationStorage_Save(const Calibration_Parameters *parameters);

#endif
