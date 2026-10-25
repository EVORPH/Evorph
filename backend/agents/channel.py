from copy import deepcopy
from dataclasses import dataclass, field, replace
import sqlite3
import unicodedata

from evorph import candidates
from evorph.contracts import (SPEC, SPEC_DIGEST, InputError, b64, canonical, digest, exact,
                             identifier, integer, parse_json, validate_plan)
from runtime.accounting import Accounting
from runtime.store import CapacityError, ConflictError
from .strategy import validate as validate_strategy

ROLES = ('seeker', 'keeper')
SEGMENTS = ('task_rules', 'public_evidence', 'approved_history')
OUTCOMES = ('accepted', 'rate_limited', 'timeout', 'truncated', 'refused', 'malformed',
            'usage_unknown', 'rejected', 'substituted', 'duplicate', 'cancelled', 'input_rejected')
REQUEST_SCHEMA = 'model-request.v1'
RESPONSE_SCHEMA = 'model-response.v1'
STOP_REASONS = ('complete', 'max_output_tokens')
# Base64 evidence must fit the ledger's existing one-megabyte JSON limit.
MAX_RESPONSE_BYTES = 524288
MAX_REQUEST_BYTES = 131072
STATEMENT_CHARS = 2000


class TransportError(RuntimeError):
    """A recorded provider outcome that returned no usable response body."""


class TransportRateLimited(TransportError):
    def __init__(self, confirmed_no_usage):
        super().__init__('Provider rejected the request before generation.')
        if type(confirmed_no_usage) is not bool:
            raise InputError('Rejection without usage must be explicitly confirmed.')
        self.confirmed_no_usage = confirmed_no_usage


class TransportTimeout(TransportError):
    """The provider did not answer inside the configured deadline."""


class TransportDisconnected(TransportError):
    """The connection ended without a response; spending is unconfirmed."""


@dataclass(frozen=True)
class ChannelSpec:
    provider: str
    model_id: str
    price: dict
    max_input_tokens: int
    max_output_tokens: int
    margin_tokens: int
    timeout_seconds: int
    meter_basis: str
    provider_name: str = ''

    def validate(self):
        identifier(self.provider)
        if (type(self.model_id) is not str or not self.model_id or len(self.model_id) > 200
                or any(ord(char) < 32 for char in self.model_id)):
            raise InputError('Invalid channel model.')
        if self.provider_name and (type(self.provider_name) is not str or len(self.provider_name) > 120
                                   or any(ord(char) < 32 for char in self.provider_name)):
            raise InputError('Invalid channel provider name.')
        integer(self.max_input_tokens, 1, 10000000)
        integer(self.max_output_tokens, 1, 10000000)
        integer(self.margin_tokens, 0, 10000000 - self.max_input_tokens)
        integer(self.timeout_seconds, 1, 3600)
        identifier(self.meter_basis)
        Accounting.estimate(self.price, self.max_input_tokens + self.margin_tokens, self.max_output_tokens)
        return self


@dataclass(frozen=True)
class Outcome:
    kind: str
    call_id: str
    reason: str
    state: str
    input_measurement: int = 0
    input_bound: int = 0
    billed_micros: int = field(default=None, repr=False)
    usage: dict = field(default=None, repr=False)
    payload: dict = field(default=None, repr=False)


def bill(price, input_tokens, output_tokens):
    exact(price, {'input_micros_per_million', 'output_micros_per_million'})
    for value in price.values():
        integer(value, 0, 2**53 - 1)
    integer(input_tokens, 0, 10000000)
    integer(output_tokens, 0, 10000000)
    numerator = input_tokens * price['input_micros_per_million'] + output_tokens * price['output_micros_per_million']
    amount = (numerator + 999999) // 1000000
    integer(amount, 0, 2**53 - 1)
    return amount


def statement(value):
    if type(value) is not str or not value or len(value) > STATEMENT_CHARS:
        raise InputError('Public statement exceeds bounds.')
    if any(unicodedata.category(char).startswith('C') for char in value):
        raise InputError('Public statement must be plain text.')
    return value


def role_output(role, payload, expected_parent=None):
    strategy = None
    if role == 'seeker':
        fields = {'plan', 'statement'}
        if type(payload) is dict and 'strategy' in payload:
            fields.add('strategy')
        exact(payload, fields)
        body = validate_plan(payload['plan'])
        if 'strategy' in payload:
            strategy = validate_strategy(payload['strategy'])
    elif role == 'keeper':
        exact(payload, {'policy', 'statement'})
        body = candidates.validate(payload['policy'])
        identifier(expected_parent)
        if body['parent_id'] != expected_parent:
            raise InputError('Candidate is not bound to the approved parent version.')
    else:
        raise InputError('Unknown research role.')
    result = {'role': role, 'body': body, 'statement': statement(payload['statement'])}
    if strategy is not None:
        result['strategy'] = strategy
    return result


class ModelChannel:
    """Single fixed provider path; every send is reserved and dispatched through the private ledger."""

    def __init__(self, accounting, spec, transport, meter):
        if not isinstance(accounting, Accounting):
            raise InputError('Channel requires the existing private ledger.')
        spec.validate()
        if transport is None or not callable(getattr(transport, 'send', None)):
            raise InputError('Model transport is not configured.')
        if not callable(meter):
            raise InputError('Input metering is not configured.')
        self.accounting, self.spec = accounting, replace(spec, price=deepcopy(spec.price))
        self.transport, self.meter = transport, meter

    def segments(self, role, task_key, segments):
        if role not in ROLES:
            raise InputError('Unknown research role.')
        identifier(task_key)
        if type(segments) is not list or not segments or len(segments) > len(SEGMENTS):
            raise InputError('Model input requires bounded task segments.')
        body, seen = [], set()
        for segment in segments:
            exact(segment, {'name', 'text'})
            if segment['name'] not in SEGMENTS or segment['name'] in seen:
                raise InputError('Model input is outside the approved allowlist.')
            seen.add(segment['name'])
            text = segment['text']
            if type(text) is not str or not text or len(text) > SPEC['limits']['input_bytes']:
                raise InputError('Model input segment exceeds bounds.')
            body.append({'name': segment['name'], 'text': text})
        if 'task_rules' not in seen:
            raise InputError('Task rules are required.')
        return body

    def prepared(self, role, task_key, segments, expected_parent=None):
        body = self.segments(role, task_key, segments)
        parent_digest = None
        if role == 'keeper' or expected_parent is not None:
            identifier(expected_parent)
            parent = self.accounting.store.connection.execute(
                'SELECT content_digest,body FROM versions WHERE version_id=?', (expected_parent,)).fetchone()
            if not parent or digest(parent['body']) != parent['content_digest']:
                raise InputError('Approved parent version is unavailable.')
            parent_digest = parent['content_digest']
        envelope = {'schema_version': REQUEST_SCHEMA, 'provider': self.spec.provider,
                    'model_id': self.spec.model_id, 'role': role, 'task_key': task_key,
                    'spec_digest': SPEC_DIGEST, 'structured_output': True, 'allow_fallback': False,
                    'expected_parent': expected_parent, 'parent_digest': parent_digest,
                    'max_output_tokens': self.spec.max_output_tokens, 'segments': body}
        raw = canonical(envelope)
        if len(raw) > MAX_REQUEST_BYTES:
            raise InputError('Model request exceeds encoding bounds.')
        measurement = self.meter(raw)
        exact(measurement, {'tokens', 'basis', 'request_digest'})
        measured = measurement['tokens']
        integer(measured, 1, 10000000)
        if measurement['basis'] != self.spec.meter_basis or measurement['request_digest'] != digest(raw):
            raise InputError('Input measurement is not bound to this request.')
        return {'allowed': measured <= self.spec.max_input_tokens, 'raw': raw, 'measured': measured,
                'bound': measured + self.spec.margin_tokens,
                'request_key': 'req-' + digest(canonical({'role': role, 'task_key': task_key})),
                'context': digest(canonical({'request_digest': digest(raw), 'measurement': measurement}))}

    def request(self, role, task_key, segments, expected_parent=None):
        prepared = self.prepared(role, task_key, segments, expected_parent)
        measured, bound = prepared['measured'], prepared['bound']
        if not prepared['allowed']:
            return self.outcome('input_rejected', '', 'input_ceiling', 'not_reserved', measured, bound, 0)
        call_id = self.accounting.reserve(prepared['request_key'], role, self.spec.model_id,
                                          self.spec.price, bound, self.spec.max_output_tokens,
                                          prepared['context'])
        row = self.accounting.get(call_id)
        if row['state'] == 'cancelled_before_send':
            return self.outcome('cancelled', call_id, 'confirmed_unsent', row['state'], measured, bound, 0)
        if row['state'] != 'reserved':
            return self.outcome('duplicate', call_id, 'already_dispatched', row['state'], measured, bound, row['actual_micros'])
        request_data = parse_json(row['request_json'])
        self.accounting.store.capacity(1048576 * 4)
        try:
            self.accounting.dispatch(call_id)
        except ConflictError:
            row = self.accounting.get(call_id)
            return self.outcome('duplicate', call_id, 'already_dispatched', row['state'], measured, bound, row['actual_micros'])
        try:
            response = self.transport.send(prepared['raw'], self.spec.timeout_seconds)
        except TransportRateLimited as rejection:
            if rejection.confirmed_no_usage:
                return self.unspent(call_id, 'rate_limited', measured, bound)
            return self.unconfirmed(call_id, 'rate_limited', 'unconfirmed_rejection', measured, bound)
        except (TransportTimeout, TimeoutError):
            return self.unconfirmed(call_id, 'timeout', 'deadline_reached', measured, bound)
        except (TransportDisconnected, OSError):
            return self.unconfirmed(call_id, 'usage_unknown', 'disconnected', measured, bound)
        except Exception:
            return self.unconfirmed(call_id, 'usage_unknown', 'transport_error', measured, bound)
        except BaseException:
            self.accounting.mark_unknown(call_id)
            raise
        if type(response) is bytes:
            saved = {'kind': 'channel-response.v1', 'body_b64': b64(response[:MAX_RESPONSE_BYTES]),
                     'complete': len(response) <= MAX_RESPONSE_BYTES, 'received_bytes': len(response),
                     'body_digest': digest(response)}
        else:
            saved = {'kind': 'channel-response.v1', 'complete': False, 'reason': 'unexpected_response_type'}
        if not self.save_response(call_id, saved):
            return self.unconfirmed(call_id, 'usage_unknown', 'storage_unavailable', measured, bound)
        return self.classify(call_id, response, role, expected_parent, measured, bound, request_data)

    def outcome(self, kind, call_id, reason, state, measured, bound, billed=None, usage=None, payload=None):
        if kind not in OUTCOMES:
            raise InputError('Unknown channel outcome.')
        return Outcome(kind, call_id, reason, state, measured, bound, billed, usage, payload)

    def save_response(self, call_id, response):
        try:
            self.accounting.record_response(call_id, response)
        except (CapacityError, OSError, sqlite3.Error):
            return False
        return True

    def unspent(self, call_id, kind, measured, bound):
        rejection = {'schema_version': RESPONSE_SCHEMA, 'kind': 'provider_rejection', 'reason': kind,
                     'confirmed_no_usage': True}
        if not self.save_response(call_id, rejection):
            return self.unconfirmed(call_id, 'usage_unknown', 'storage_unavailable', measured, bound)
        self.accounting.settle(call_id, 0)
        return self.outcome(kind, call_id, 'confirmed_no_usage', 'settled', measured, bound, 0)

    def unconfirmed(self, call_id, kind, reason, measured, bound):
        self.accounting.mark_unknown(call_id)
        return self.outcome(kind, call_id, reason, self.accounting.get(call_id)['state'], measured, bound)

    def classify(self, call_id, response, role, expected_parent, measured, bound, request_data):
        if type(response) is not bytes or len(response) > MAX_RESPONSE_BYTES:
            return self.unconfirmed(call_id, 'malformed', 'response_out_of_bounds', measured, bound)
        try:
            envelope = parse_json(response, MAX_RESPONSE_BYTES)
            exact(envelope, {'schema_version', 'provider', 'model_id', 'stop_reason', 'refusal', 'payload', 'usage'})
            if envelope['schema_version'] != RESPONSE_SCHEMA:
                raise InputError('Unknown response schema.')
            if envelope['stop_reason'] not in STOP_REASONS or type(envelope['refusal']) is not bool:
                raise InputError('Invalid response status.')
        except InputError:
            return self.unconfirmed(call_id, 'malformed', 'unparseable_response', measured, bound)
        if envelope['provider'] != self.spec.provider or envelope['model_id'] != self.spec.model_id:
            self.accounting.pause()
            return self.unconfirmed(call_id, 'substituted', 'provider_or_model_mismatch', measured, bound)
        try:
            usage = self.usage(envelope['usage'])
        except InputError:
            return self.unconfirmed(call_id, 'malformed', 'invalid_usage', measured, bound)
        if usage is None:
            return self.unconfirmed(call_id, 'usage_unknown', 'missing_usage', measured, bound)
        try:
            billed = self.settlement_amount(call_id, usage, request_data)
        except InputError:
            self.accounting.pause()
            return self.unconfirmed(call_id, 'usage_unknown', 'unrepresentable_usage', measured, bound)
        self.accounting.settle(call_id, billed)
        if usage['input_tokens'] > request_data['max_input'] or usage['output_tokens'] > request_data['max_output']:
            self.accounting.pause()
            return self.outcome('rejected', call_id, 'usage_exceeds_bounds', 'settled', measured, bound, billed, usage)
        if envelope['refusal']:
            reason = 'refusal_with_payload' if envelope['payload'] is not None else 'provider_refusal'
            return self.outcome('refused', call_id, reason, 'settled', measured, bound, billed, usage)
        if envelope['stop_reason'] == 'max_output_tokens':
            return self.outcome('truncated', call_id, 'output_limit', 'settled', measured, bound, billed, usage)
        if envelope['payload'] is None:
            return self.outcome('rejected', call_id, 'empty_payload', 'settled', measured, bound, billed, usage)
        try:
            payload = role_output(role, envelope['payload'], expected_parent)
            if role == 'keeper':
                prior = self.accounting.store.connection.execute(
                    'SELECT content_digest FROM versions WHERE version_id=?', (payload['body']['version_id'],)).fetchone()
                if prior and prior['content_digest'] != digest(canonical(payload['body'])):
                    raise InputError('Candidate version identifier is already assigned.')
        except (ValueError, TypeError, RecursionError):
            return self.outcome('malformed', call_id, 'invalid_payload', 'settled', measured, bound, billed, usage)
        return self.outcome('accepted', call_id, 'validated', 'settled', measured, bound, billed, usage, payload)

    def settlement_amount(self, call_id, usage, request_data):
        return bill(request_data['price'], usage['input_tokens'], usage['output_tokens'])

    @staticmethod
    def usage(value):
        if value is None:
            return None
        exact(value, {'input_tokens', 'output_tokens', 'reasoning_tokens'})
        for tokens in value.values():
            integer(tokens, 0, 10000000)
        if value['reasoning_tokens'] > value['output_tokens']:
            raise InputError('Reasoning is already included in total output.')
        return value
