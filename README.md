# CurrentSensor_FW

Firmware 0.2.0 for the STM32F411RET6 board with an INA229 on SPI1 and a USB CDC virtual serial port. This stage supports single register reads and a manually entered software current correction. It does not run a calibration procedure or a measurement stream.

## Build and test

Use the STM32 GNU Arm toolchain:

```sh
make -j4
```

The firmware is in `build/CurrentSensor_FW.{elf,hex,bin}`. Host-side C tests use CMake and a native compiler:

```sh
cmake -S tests -B tests/build
cmake --build tests/build --config Release
ctest --test-dir tests/build --build-config Release --output-on-failure
```

Connect a terminal to the USB CDC port and send ASCII commands terminated by CR, LF, or CRLF:

| Command | Action |
| --- | --- |
| `help` | List commands |
| `ping` | Return `pong` |
| `info` | Show firmware, MCU, USB, and SPI information |
| `ina` | Read manufacturer and device IDs |
| `regs` | Read CONFIG, ADC_CONFIG, and DIAG_ALRT |
| `measure` | Read one set of VSHUNT, VBUS, and DIETEMP values |
| `cal show` | Show active software calibration |
| `cal set offset_ua <signed_integer>` | Set offset from -100000 to +100000 uA in RAM |
| `cal set gain_ppm <unsigned_integer>` | Set gain from 900000 to 1100000 ppm in RAM |
| `cal enable <0|1>` | Disable or enable correction in RAM |
| `cal save` | Append the current parameters to Flash |
| `cal load` | Reload the newest valid Flash record, or factory defaults if none exists |
| `cal reset` | Restore factory values in RAM; use `cal save` to make the reset persistent |

`cal show` after a first boot with an erased storage sector:

```text
CAL:
OFFSET_UA: 0
GAIN_PPM: 1000000
ENABLED: 0
SOURCE: DEFAULT
DIRTY: 0
STATUS: OK
```

`SOURCE` describes the last operation that established the active parameters: `DEFAULT` at factory startup, after `cal reset`, or after `cal load` with no valid record; `FLASH` after loading or saving a valid record; `RAM` after any `cal set` or `cal enable`, even if the value is unchanged. `DIRTY=1` means the active parameters differ from the most recently saved Flash values. When Flash has no valid record, the comparison is against factory defaults. Thus `SOURCE: DEFAULT` can have `DIRTY: 1` after `cal reset` if Flash contains other values. A successful `cal save` or `cal load` clears `DIRTY`.

Valid state-changing commands return `STATUS: OK`. Invalid input returns `ERR: BAD_NUMBER`, `ERR: NUMBER_OVERFLOW`, `ERR: OUT_OF_RANGE`, or `ERR: UNKNOWN_COMMAND`, without partially applying a value. Storage failures return `ERR: STORAGE_UNAVAILABLE`, `ERR: STORAGE_FULL`, or `ERR: STORAGE_ERROR`. Saving while a future measurement stream is active returns `ERR: BUSY_STREAMING`; the stream owner must set the explicit state through `App_SetStreamingActive()`. The command parser accepts exact command spelling and decimal integers; extra characters and trailing spaces are rejected. An overlong input line returns `ERR: LINE_TOO_LONG`.

## Measurement and correction

`measure` prints the full 24-bit `VSHUNT_RAW` and `VBUS_RAW`, signed 20-bit `VSHUNT_CODE`, 16-bit `DIETEMP_RAW`, `VSHUNT_mV`, original `CURRENT_mA`, corrected `CURRENT_CAL_mA`, `CAL_ENABLED`, `VBUS_V`, `DIETEMP_C`, and `STATUS`. The shunt is 0.005 Ohm. The firmware reads `CONFIG.ADCRANGE` for every measurement and uses these equations:

```text
VSHUNT_mV = VSHUNT_CODE * (ADCRANGE ? 0.000078125 : 0.0003125)
I_raw_nA = VSHUNT_CODE * (ADCRANGE ? 15625 : 62500)
I_cal_nA = enabled ? round_away_from_zero((I_raw_nA - offset_ua * 1000) * gain_ppm / 1000000) : I_raw_nA
VBUS_V = (VBUS_RAW >> 4) * 0.0001953125
DIETEMP_C = signed16(DIETEMP_RAW) * 0.0078125
```

The integer calculation rounds to the nearest nA, with half values away from zero, and checks for overflow. `CURRENT_mA` remains the original uncorrected current. With `enabled=0`, `CURRENT_CAL_mA` exactly equals `CURRENT_mA`. The displayed current has six decimal places in mA. The command only reads already converted registers; successive register reads are not an atomic ADC snapshot. An SPI error prints `STATUS: SPI_ERROR` or `STATUS: SPI_TIMEOUT` without measurement values. The INA229 `CURRENT` and `SHUNT_CAL` registers are not used for this correction. The INA229 register scales are from the [INA229 datasheet](https://www.ti.com/lit/ds/symlink/ina229.pdf).

## Flash storage and updates

The STM32F411RET6 has 512 KiB of Flash. The linker limits firmware to `0x08000000-0x0805FFFF` and reserves sector 7, `0x08060000-0x0807FFFF` (128 KiB), for calibration. The vector table and current firmware image are in the first sectors. Storage is enabled only when the device reports 512 KiB and the linker symbols match sector 7. See the [STM32F411 reference manual](https://www.st.com/resource/en/reference_manual/rm0383-stm32f411xce-advanced-armbased-32bit-mcus-stmicroelectronics.pdf).

`cal save` is the only firmware command that writes Flash. It appends a 36-byte record containing magic, format version, length, sequence, offset, gain, enabled, IEEE CRC32, and a final commit word. Loading scans all slots and selects the newest complete valid record. An interrupted save or corrupt CRC leaves the previous valid record usable; if none is valid, factory values are loaded. `cal load` and startup only read Flash. If an occupied slot has an unknown header, `cal save` returns `ERR: STORAGE_UNAVAILABLE` instead of writing alongside unrecognized data. There is no automatic sector erase: after 3640 record slots are used, `cal save` returns `ERR: STORAGE_FULL`, preserving the last valid record. Reclaiming the sector then requires a deliberate sector-7 erase, which deletes saved calibration.

For STM32CubeProgrammer DFU updates, program the `.elf` with **Full chip erase disabled** and leave **Skip flash memory erase before programming disabled**. Do not run `Erase all sectors` or select sector 7 for manual erase. Check the programmer log for the erased sector numbers and verify that sector 7 is absent. A full-chip erase removes saved calibration. Firmware updates performed with sector erase should leave sector 7 intact; verify this on the board with `cal show` after an update. The [STM32CubeProgrammer manual](https://www.st.com/resource/en/user_manual/dm00403500.pdf) describes these options.

Hardware testing of the calibration has been performed on the STM32F411RET6 + INA229 board, as reported by the project owner. Test measurements and accuracy figures are not recorded here. Continuous 10 kHz streaming, DMA, ALERT handling, ADC configuration changes, and automatic calibration remain outside this stage.
