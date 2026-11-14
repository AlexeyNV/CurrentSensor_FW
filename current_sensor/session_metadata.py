"""Explicit profile provenance and compatibility with GUI v0.1.1 JSON."""
import copy


def new_session_metadata(connection, confirmed_profile, calibration, session_id, connection_profile=None):
    profile = copy.deepcopy(confirmed_profile)
    connected = copy.deepcopy(connection_profile if connection_profile is not None else connection.get('profile'))
    device = copy.deepcopy(connection)
    device['profile'] = copy.deepcopy(profile)  # Current at acquisition start.
    return dict(source_session=session_id, profile=copy.deepcopy(profile),
                connection_profile=connected, current_device_profile=copy.deepcopy(profile),
                session_profile=copy.deepcopy(profile), export_profile=copy.deepcopy(profile),
                profile_semantics='v2_confirmed_at_acquisition_start',
                device=device, calibration=copy.deepcopy(calibration),
                firmware_version=profile.get('firmware_version') or connection.get('info',{}).get('FW'),
                acq_mode=profile.get('acq_mode'),
                conversion=dict(adc_config=profile.get('adc_config'),
                                sample_period_us=profile.get('sample_period_us'),
                                vshct_us=profile.get('vshct_us'), avg=profile.get('avg'),
                                adcrange=profile.get('adcrange')),
                protocol_version=profile.get('protocol_version',1), acquisition_finished=False, session_end_verified=False)


def normalize_legacy(metadata):
    """Keep legacy fields; annotate ambiguous profile provenance without guessing."""
    result = copy.deepcopy(metadata)
    top = result.get('session_profile') or result.get('profile')
    nested = result.get('device', {}).get('profile')
    if not result.get('profile_semantics'):
        result['profile_semantics'] = 'legacy_top_level_profile_assumed_session'
        result['legacy_profile_fields'] = dict(profile='assumed acquisition profile',
                                                device_profile='connection time; may be stale')
        result['connection_profile'] = copy.deepcopy(nested)
    if top:
        result.setdefault('session_profile', copy.deepcopy(top))
        result.setdefault('export_profile', copy.deepcopy(top))
    result.setdefault('current_device_profile', copy.deepcopy(nested))
    return result
