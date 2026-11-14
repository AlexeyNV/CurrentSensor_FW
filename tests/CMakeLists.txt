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
add_executable(pair_diagnostic_tests pair_diagnostic_test.c
    ../App/Src/pair_trace_core.c ../App/Src/pair_experiment_core.c
    ../App/Src/pair_diagnostic.c ../App/Src/format64.c)
target_include_directories(pair_diagnostic_tests PRIVATE support ../App/Inc)
target_compile_definitions(pair_diagnostic_tests PRIVATE PAIR_DIAG_HOST_TEST)
if(MSVC)
    target_compile_options(pair_diagnostic_tests PRIVATE /W4 /WX)
else()
    target_compile_options(pair_diagnostic_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME pair_diagnostic_tests COMMAND pair_diagnostic_tests)
add_executable(spi_freeze_tests spi_freeze_test.c ../App/Src/spi_freeze_core.c)
target_include_directories(spi_freeze_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(spi_freeze_tests PRIVATE /W4 /WX)
else()
    target_compile_options(spi_freeze_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME spi_freeze_tests COMMAND spi_freeze_tests)
add_test(NAME calibration_tests COMMAND calibration_tests)

add_executable(format64_tests format64_test.c ../App/Src/format64.c)
target_include_directories(format64_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(format64_tests PRIVATE /W4 /WX)
else()
    target_compile_options(format64_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME format64_tests COMMAND format64_tests)

add_executable(acquisition_tests acquisition_test.c ../App/Src/acquisition_core.c)
target_include_directories(acquisition_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(acquisition_tests PRIVATE /W4 /WX)
else()
    target_compile_options(acquisition_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME acquisition_tests COMMAND acquisition_tests)

add_executable(stream_tests stream_test.c ../App/Src/stream_core.c
    ../App/Src/acquisition_core.c)
target_include_directories(stream_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(stream_tests PRIVATE /W4 /WX)
else()
    target_compile_options(stream_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME stream_tests COMMAND stream_tests)

add_executable(stream_transport_tests stream_transport_test.c
    ../App/Src/stream_transport.c ../App/Src/stream_core.c
    ../App/Src/acquisition_core.c)
target_include_directories(stream_transport_tests PRIVATE support ../App/Inc)
if(MSVC)
    target_compile_options(stream_transport_tests PRIVATE /W4 /WX)
else()
    target_compile_options(stream_transport_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME stream_transport_tests COMMAND stream_transport_tests)

add_executable(anomaly_tests anomaly_test.c ../App/Src/anomaly_trace.c
    ../App/Src/acquisition_core.c)
target_include_directories(anomaly_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(anomaly_tests PRIVATE /W4 /WX)
else()
    target_compile_options(anomaly_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME anomaly_tests COMMAND anomaly_tests)

add_executable(acquisition_dma_tests acquisition_dma_test.c
    ../App/Src/acquisition_dma_core.c ../App/Src/acquisition_core.c
    ../App/Src/measurement_profile.c)
target_include_directories(acquisition_dma_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(acquisition_dma_tests PRIVATE /W4 /WX)
else()
    target_compile_options(acquisition_dma_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME acquisition_dma_tests COMMAND acquisition_dma_tests)

foreach(test_target acquisition_tests stream_tests stream_transport_tests anomaly_tests)
    target_sources(${test_target} PRIVATE ../App/Src/measurement_profile.c)
endforeach()

add_executable(measurement_profile_tests measurement_profile_test.c
    ../App/Src/measurement_profile.c ../App/Src/acquisition.c
    ../App/Src/acquisition_alert_core.c
    ../App/Src/alert_driven_core.c
    ../App/Src/native_core.c
    ../App/Src/format64.c
    ../App/Src/acquisition_core.c ../App/Src/acquisition_dma_core.c
    ../App/Src/anomaly_trace.c ../App/Src/stream_core.c)
target_include_directories(measurement_profile_tests PRIVATE support ../App/Inc)
if(MSVC)
    target_compile_options(measurement_profile_tests PRIVATE /W4 /WX)
else()
    target_compile_options(measurement_profile_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME measurement_profile_tests COMMAND measurement_profile_tests)

add_executable(acquisition_alert_tests acquisition_alert_test.c
    ../App/Src/acquisition_alert_core.c)
target_include_directories(acquisition_alert_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(acquisition_alert_tests PRIVATE /W4 /WX)
else()
    target_compile_options(acquisition_alert_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME acquisition_alert_tests COMMAND acquisition_alert_tests)

add_executable(alert_driven_tests alert_driven_test.c ../App/Src/alert_driven_core.c)
target_include_directories(alert_driven_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(alert_driven_tests PRIVATE /W4 /WX)
else()
    target_compile_options(alert_driven_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME alert_driven_tests COMMAND alert_driven_tests)

add_executable(native_tests native_test.c ../App/Src/native_core.c)
target_include_directories(native_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(native_tests PRIVATE /W4 /WX)
else()
    target_compile_options(native_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME native_tests COMMAND native_tests)

add_executable(timebase_tests timebase_test.c ../App/Src/timebase_core.c)
target_include_directories(timebase_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(timebase_tests PRIVATE /W4 /WX)
else()
    target_compile_options(timebase_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME timebase_tests COMMAND timebase_tests)

add_executable(spi_speed_tests spi_speed_test.c ../App/Src/spi_speed_core.c)
target_include_directories(spi_speed_tests PRIVATE ../App/Inc)
if(MSVC)
    target_compile_options(spi_speed_tests PRIVATE /W4 /WX)
else()
    target_compile_options(spi_speed_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME spi_speed_tests COMMAND spi_speed_tests)

add_executable(ina229_owner_tests ina229_owner_test.c ../App/Src/ina229.c)
target_include_directories(ina229_owner_tests PRIVATE support_spi ../App/Inc)
if(MSVC)
    target_compile_options(ina229_owner_tests PRIVATE /W4 /WX)
else()
    target_compile_options(ina229_owner_tests PRIVATE -Wall -Wextra -Werror)
endif()
add_test(NAME ina229_owner_tests COMMAND ina229_owner_tests)
