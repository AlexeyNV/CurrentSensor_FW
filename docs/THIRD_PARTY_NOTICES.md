# Лицензии сторонних компонентов

MIT в корне применяется к собственному коду CurrentSensor. Она не заменяет
лицензии сторонних файлов, генератора CubeMX, библиотек и Python-зависимостей.
Их исходные заголовки и тексты лицензий сохранены.

| Компонент | Исходный текст условий |
|---|---|
| STM32F4 HAL | [Drivers/STM32F4xx_HAL_Driver/LICENSE.txt](../Drivers/STM32F4xx_HAL_Driver/LICENSE.txt): условия package license либо BSD-3-Clause согласно файлу |
| CMSIS | [Drivers/CMSIS/LICENSE.txt](../Drivers/CMSIS/LICENSE.txt) |
| CMSIS Device STM32F4 | [Drivers/CMSIS/Device/ST/STM32F4xx/LICENSE.txt](../Drivers/CMSIS/Device/ST/STM32F4xx/LICENSE.txt) |
| ST USB Device Library | [Middlewares/ST/STM32_USB_Device_Library/LICENSE.txt](../Middlewares/ST/STM32_USB_Device_Library/LICENSE.txt), SLA0044 согласно файлу |

Python устанавливает PySide6/Shiboken, NumPy, pyqtgraph, pyserial, psutil и их
зависимости отдельно через pip. Их лицензии поставляются в metadata/wheel
соответствующего пакета. Они не перелицензируются MIT данного проекта.
Релиз содержит исходники GUI, а не переупакованные Qt/Python binaries.
Для последующего распространения отдельного executable необходимо сохранить
применимые условия Qt/PySide и остальных включённых библиотек.

Схема и Gerber предоставлены автором проекта. Обозначения компонентов,
товарные знаки производителей и библиотечные модели принадлежат их владельцам.
