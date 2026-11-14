"""GUI streaming presets. Firmware profile lists remain the profile authority."""


def native_capable(firmware):
    return bool(firmware and (firmware.startswith('0.5.') or firmware == '1.0.0'))


def selection_error(mode, firmware, name=None, profiles=None):
    if mode not in ('dma', 'poll', 'native'):
        return 'Unsupported streaming preset. Select DMA, POLL or Native DMA.'
    if mode == 'native' and firmware and not native_capable(firmware):
        return 'Native CSF4 v2 requires FW 0.5.x or 1.0.0; use continuous DMA/POLL on this firmware.'
    if mode == 'poll' and name == 'FAST' and firmware in ('0.5.0', '1.0.0'):
        return ('POLL/FAST streaming failed the FW 0.5.0 hardware STOP test (missing END, slot losses). '
                'Use DMA/FAST or Native DMA. POLL acquisition diagnostics remain available in CLI.')
    if name and profiles is not None and name not in [p['name'] for p in profiles]:
        return f'Profile {name} was not advertised by the device.'
    return None
