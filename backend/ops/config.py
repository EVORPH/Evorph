"""Operator configuration; every value is bounded before it reaches a running service."""

from dataclasses import dataclass
from pathlib import Path

from agents.channel import ChannelSpec
from agents.metering import CONTEXT_BOUND_BASIS
from evorph.contracts import InputError, exact, integer, parse_json
from evorph.evidence import read_file
from runtime.store import Limits
from service.public_store import ServiceLimits

CONFIG_BYTES = 131072
CREDENTIAL_BYTES = 512
LOOPBACK = ('127.0.0.1', '::1')
MODES = ('example', 'live')
CHANNEL_BASE_KEYS = {'provider', 'model_id', 'price', 'max_input_tokens', 'max_output_tokens',
                     'margin_tokens', 'timeout_seconds', 'meter_basis'}
OFFLINE_CHANNEL_KEYS = CHANNEL_BASE_KEYS | {'script'}
LIVE_CHANNEL_KEYS = CHANNEL_BASE_KEYS | {'provider_name'}
TRANSPORT_KEYS = {'enabled', 'credential_path'}
SCRIPT_KINDS = {'response', 'rate_limited', 'timeout', 'disconnected'}
SCRIPT_LIMIT = 64
SERVICE_LIMIT_KEYS = {'session_seconds', 'identity_per_window', 'write_per_window',
                      'read_per_window', 'window_seconds', 'page_limit'}


@dataclass(frozen=True)
class OpsConfig:
    state: Path
    limits: Limits
    mode: str
    origin: str
    request_timeout: int
    service_limits: ServiceLimits
    listen_host: str
    listen_port: int
    channels: dict
    openssl: str
    transport_enabled: bool = False
    credential_path: str = None


def _path(value):
    if type(value) is not str or not value or len(value) > 4096:
        raise InputError('Expected a filesystem path.')
    path = Path(value)
    if not path.is_absolute():
        raise InputError('Expected an absolute path.')
    return path


def _openssl(value):
    if value is None:
        return None
    # A missing post-quantum capable binary must fail at start, not halfway through a research round.
    path = _path(value)
    if not path.is_file():
        raise InputError('Configured OpenSSL binary is unavailable.')
    return str(path)


def read_json(path):
    path = Path(path)
    return parse_json(read_file(path.parent, path.name, CONFIG_BYTES), CONFIG_BYTES)


def _channel(raw):
    """A channel is either a recorded fixture or a live route; the two never mix."""
    if 'script' in raw:
        exact(raw, OFFLINE_CHANNEL_KEYS)
        script = raw['script']
        if type(script) is not list or len(script) > SCRIPT_LIMIT:
            raise InputError('Recorded outcomes must be a bounded list.')
        for step in script:
            if type(step) is not dict or step.get('kind') not in SCRIPT_KINDS:
                raise InputError('Unknown recorded outcome.')
        transport = {'kind': 'offline', 'script': script}
        provider_name = ''
    else:
        exact(raw, LIVE_CHANNEL_KEYS)
        if raw['meter_basis'] != CONTEXT_BOUND_BASIS:
            raise InputError('Live channels require the provider context-bound meter.')
        provider_name = raw['provider_name']
        transport = {'kind': 'live'}
    spec = ChannelSpec(raw['provider'], raw['model_id'], raw['price'], raw['max_input_tokens'],
                       raw['max_output_tokens'], raw['margin_tokens'], raw['timeout_seconds'],
                       raw['meter_basis'], provider_name).validate()
    return spec, transport


def _transport(raw, kinds):
    exact(raw, TRANSPORT_KEYS)
    if type(raw['enabled']) is not bool:
        raise InputError('Transport state must be explicit.')
    path = raw['credential_path']
    if 'live' in kinds:
        # The credential is read only by the process that may spend; a missing key
        # must not take down read-only status or the public archive.
        _path(path)
    elif path is not None:
        raise InputError('Offline channels carry no credential.')
    return raw['enabled'], (None if path is None else str(_path(path)))


def load(path):
    config = read_json(path)
    exact(config, {'state', 'runtime_config', 'service', 'api', 'channels', 'openssl', 'transport'})
    state = _path(config['state'])
    if str(state) == '/':
        raise InputError('Expected a dedicated state directory.')
    runtime = read_json(_path(config['runtime_config']))
    exact(runtime, {'limits', 'spending'})
    exact(runtime['limits'], set(Limits.__dataclass_fields__))
    limits = Limits(**runtime['limits'])
    limits.validate()

    service = config['service']
    exact(service, {'mode', 'origin', 'request_timeout', 'limits'})
    if service['mode'] not in MODES:
        raise InputError('Public archive source mode must be example or live.')
    exact(service['limits'], SERVICE_LIMIT_KEYS)
    service_limits = ServiceLimits(**service['limits']).validate()
    integer(service['request_timeout'], 1, 3600)

    api = config['api']
    exact(api, {'listen_host', 'listen_port'})
    # Transport encryption and static files belong to the reverse proxy in front of this process.
    if api['listen_host'] not in LOOPBACK:
        raise InputError('Bind a loopback address; the reverse proxy publishes the service.')
    integer(api['listen_port'], 1, 65535)

    exact(config['channels'], {'seeker', 'keeper'})
    channels = {role: _channel(config['channels'][role]) for role in ('seeker', 'keeper')}
    kinds = {channels[role][1]['kind'] for role in ('seeker', 'keeper')}
    if len(kinds) != 1:
        raise InputError('Both research roles must use the same channel kind.')
    # The archive mode names where its records came from, so it has to agree with the channels.
    if (service['mode'] == 'live') != ('live' in kinds):
        raise InputError('Public archive mode does not match the configured channels.')
    enabled, credential_path = _transport(config['transport'], kinds)
    return OpsConfig(state, limits, service['mode'], service['origin'], service['request_timeout'],
                     service_limits, api['listen_host'], api['listen_port'], channels,
                     _openssl(config['openssl']), enabled, credential_path)
