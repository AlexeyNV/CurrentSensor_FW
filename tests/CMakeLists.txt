cmake_minimum_required(VERSION 3.15)
project(CurrentSensorCalibrationTests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(calibration_tests
    calibration_test.c
    ../App/Src/calibration.c
    ../App/Src/calibration_record.c)
target_include_directories(calibration_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(calibration_tests PRIVATE /W4 /WX)
else()
    target_compile_options(calibration_tests PRIVATE -Wall -Wextra -Werror)
endif()

enable_testing()
add_test(NAME calibration_tests COMMAND calibration_tests)
