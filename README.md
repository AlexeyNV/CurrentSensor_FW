# CurrentSensor_FW

Firmware for the STM32F411RET6 current sensor board with an INA229 on SPI1 and a USB CDC virtual serial port. The current stage is hardware bring-up: checking USB communication and reading INA229 registers without changing its reset configuration.

Build with the STM32 GNU Arm toolchain and `make`:

```sh
make clean
make
```

The output is in `build/CurrentSensor_FW.{elf,hex,bin}`. Connect a serial terminal to the USB CDC port and send a command followed by CR, LF, or CRLF:

- `help`: list commands
- `ping`: return `pong`
- `info`: firmware and interface information
- `ina`: read manufacturer and device IDs, including die and revision IDs
- `regs`: read CONFIG, ADC_CONFIG, and DIAG_ALRT

Commands run in the main loop. An INA229 SPI error is reported as `STATUS: SPI_ERROR` or `STATUS: SPI_TIMEOUT`. Hardware operation must be checked on the board; a successful build only verifies the firmware image.
