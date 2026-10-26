from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re
import subprocess

from evorph.contracts import InputError, b64, canonical, digest, exact, identifier, integer, parse_json, unb64
from runtime.store import ConflictError
from .channel import (MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ModelChannel, RESPONSE_SCHEMA,
                      TransportDisconnected, TransportTimeout)

BODY_LIMIT = 262144
HOST = 'https://openrouter.ai'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS router_generations (
 call_id TEXT PRIMARY KEY REFERENCES model_calls, generation_id TEXT NOT NULL UNIQUE,
 response_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS router_receipts (
 call_id TEXT PRIMARY KEY REFERENCES router_generations, body BLOB NOT NULL
);
'''


def generation_id(value):
    if type(value) is not str or len(value) > 128 or not re.fullmatch(r'gen-[0-9A-Za-z-]+', value):
        raise InputError('Invalid generation identifier.')
    return value


def micros(value):
    if type(value) not in (int, float, str) or len(str(value)) > 80:
        raise InputError('Invalid provider cost.')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= Decimal(2**53 - 1) / 1000000:
            raise InvalidOperation()
        result = int((amount * 1000000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError):
        raise InputError('Invalid provider cost.') from None
    integer(result, 0, 2**53 - 1)
    return result


def role_schemas():
    from evorph.candidates import EFFECTS, FACTS
    from evorph.contracts import MUTATIONS, PUBLIC_MESSAGES, SPEC_DIGEST

    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}

    ident = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'}
    public_statement = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
    delivery = {'kind': {'const': 'deliver'}, 'message_ref': {'enum': sorted(PUBLIC_MESSAGES)}}
    event = {'anyOf': [obj(delivery), obj({**delivery, 'kind': {'const': 'mutate'}, 'mutation': {'enum': sorted(MUTATIONS)}}),
                       obj({'kind': {'const': 'duplicate'}, 'event_index': {'type': 'integer', 'minimum': 0, 'maximum': 31}}),
                       obj({'kind': {'const': 'advance'}, 'ticks': {'type': 'integer', 'minimum': 1, 'maximum': 1000}})]}
    challenge = obj({'schema_version': {'const': 'challenge.v1'}, 'spec_digest': {'const': SPEC_DIGEST},
                     'events': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': event}})

    # Strict structured output rejects self-referencing $defs, so the condition tree is
    # inlined to a fixed depth. Depth 3 covers the rule shapes the fixed baseline and its
    # single-fault repairs use, and three copies still fit the request encoding bound.
    def condition(depth):
        leaf = [obj({'fact': {'enum': sorted(FACTS)}}), obj({'constant': {'type': 'boolean'}})]
        if depth <= 0:
            return {'anyOf': leaf}
        node = condition(depth - 1)
        return {'anyOf': leaf + [obj({'not': node}),
                                 obj({'all': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': node}}),
                                 obj({'any': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': node}})]}

    rule = obj({'when': condition(3), 'effect': {'enum': sorted(EFFECTS)}})
    policy = obj({'schema_version': {'const': 'policy.v1'}, 'spec_digest': {'const': SPEC_DIGEST},
                  'version_id': ident, 'parent_id': ident, 'rules': obj({name: rule for name in ('prepare', 'confirm', 'operation')})})
    keeper = obj({'policy': policy, 'statement': public_statement})
    # The strategy is optional so a round may keep the inherited search method, but the
    # object itself is closed: orders must be permutations of the fixed capabilities.
    def order(names):
        return {'type': 'array', 'minItems': len(names), 'maxItems': len(names),
                'items': {'enum': sorted(names)}, 'uniqueItems': True}

    strategy = obj({'schema_version': {'const': 'search-strategy.v1'}, 'spec_digest': {'const': SPEC_DIGEST},
                    'version_id': ident, 'parent_id': {'anyOf': [ident, {'type': 'null'}]},
                    'message_order': order(PUBLIC_MESSAGES), 'mutation_order': order(MUTATIONS),
                    'reduction_order': {'enum': ['forward', 'reverse']}})
    seeker = {'type': 'object', 'additionalProperties': False,
              'properties': {'plan': challenge, 'statement': public_statement, 'strategy': strategy},
              'required': ['plan', 'statement']}
    return {'seeker': seeker, 'keeper': keeper}


class CurlHTTP:
    def __init__(self, api_key, *, enabled=False):
        if type(api_key) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', api_key):
            raise InputError('Invalid service credential encoding.')
        if type(enabled) is not bool:
            raise InputError('Explicit transport state required.')
        self._api_key, self.enabled = api_key, enabled

    def exchange(self, method, path, body, timeout):
        if not self.enabled:
            raise InputError('Provider transport is not enabled.')
        integer(timeout, 1, 3600)
        if not ((method == 'POST' and path == '/api/v1/chat/completions') or
                (method == 'GET' and path.startswith('/api/v1/generation?id=') and
                 generation_id(path.removeprefix('/api/v1/generation?id=')))):
            raise InputError('Unexpected provider endpoint.')
        if type(body) is not bytes or len(body) > MAX_REQUEST_BYTES:
            raise InputError('Provider request exceeds bounds.')
        # Credentials and request text go through stdin, never argv or a temporary file.
        config = ('url = "' + HOST + path + '"\nrequest = "' + method +
                  '"\nheader = "Authorization: Bearer ' + self._api_key +
                  '"\nheader = "Content-Type: application/json"\nheader = "Accept: application/json"\n')
        if body:
            text = body.decode('ascii').replace('\\', '\\\\').replace('"', '\\"')
            config += 'data-binary = "' + text + '"\n'
        try:
            result = subprocess.run(['curl', '-q', '--silent', '--show-error', '--proto', '=https',
                                     '--max-time', str(timeout), '--max-filesize', str(BODY_LIMIT),
                                     '--write-out', '\n%{http_code}', '--config', '-'],
                                    input=config.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=timeout + 1, check=False)
        except subprocess.TimeoutExpired:
            raise TransportTimeout() from None
        except OSError:
            raise TransportDisconnected() from None
        if result.returncode == 28:
            raise TransportTimeout()
        if result.returncode or len(result.stdout) > BODY_LIMIT + 4:
            raise TransportDisconnected()
        try:
            raw, code = result.stdout.rsplit(b'\n', 1)
            status = int(code)
            integer(status, 100, 599)
        except (ValueError, InputError):
            raise TransportDisconnected() from None
        return status, raw


class OpenRouterTransport:
    def __init__(self, spec, http, schemas):
        self.spec, self.http, self.schemas = deepcopy(spec), http, deepcopy(schemas)
        exact(schemas, {'seeker', 'keeper'})
        for schema in schemas.values():
            if (type(schema) is not dict or schema.get('type') != 'object'
                    or schema.get('additionalProperties') is not False):
                raise InputError('Strict role output schemas are required.')

    def prepare(self, raw):
        envelope = parse_json(raw, MAX_REQUEST_BYTES)
        if (envelope['model_id'] != self.spec.model_id or envelope['provider'] != self.spec.provider
                or envelope['allow_fallback'] is not False or envelope['structured_output'] is not True):
            raise InputError('Provider route differs from the fixed channel.')
        role = envelope['role']
        rules = next(item['text'] for item in envelope['segments'] if item['name'] == 'task_rules')
        context = {'spec_digest': envelope['spec_digest'], 'expected_parent': envelope['expected_parent'],
                   'parent_digest': envelope['parent_digest'],
                   'data': [item for item in envelope['segments'] if item['name'] != 'task_rules']}
        schema = self.schemas[role]
        strategy_parent = envelope.get('strategy_parent')
        if strategy_parent is not None:
            # The inherited search method's identity is fixed input, not a model choice:
            # pin it the same way the wire schema pins spec_digest.
            if role != 'seeker':
                raise InputError('Only the seeker request pins a strategy parent.')
            identifier(strategy_parent)
            schema = deepcopy(schema)
            schema['properties']['strategy']['properties']['parent_id'] = {'const': strategy_parent}
        request = {'model': self.spec.model_id, 'stream': False, 'max_tokens': envelope['max_output_tokens'],
                   'provider': {'only': [self.spec.provider], 'order': [self.spec.provider],
                                'allow_fallbacks': False, 'require_parameters': True, 'data_collection': 'deny',
                                'max_price': {'prompt': str(Decimal(self.spec.price['input_micros_per_million']) / 1000000),
                                              'completion': str(Decimal(self.spec.price['output_micros_per_million']) / 1000000)}},
                   'messages': [{'role': 'system', 'content': rules},
                                {'role': 'user', 'content': canonical(context).decode()}],
                   'response_format': {'type': 'json_schema', 'json_schema':
                                       {'name': role, 'strict': True, 'schema': schema}}}
        body = canonical(request)
        if len(body) > MAX_REQUEST_BYTES:
            raise InputError('Provider request exceeds encoding bounds.')
        return body

    def send(self, raw, timeout):
        status, response = self.http.exchange('POST', '/api/v1/chat/completions', self.prepare(raw), timeout)
        if type(response) is not bytes or len(response) > BODY_LIMIT:
            raise TransportDisconnected()
        return canonical({'http_status': status, 'body_b64': b64(response)})


class OpenRouterChannel(ModelChannel):
    def __init__(self, accounting, spec, http, *, provider_name, schemas, meter):
        if type(provider_name) is not str or not provider_name or len(provider_name) > 120:
            raise InputError('Expected provider response name is required.')
        transport = OpenRouterTransport(spec, http, schemas)
        self.provider_name = provider_name
        self._wire_meter = meter
        super().__init__(accounting, spec, transport, self._measure)
        accounting.store.connection.executescript(SCHEMA)

    def _measure(self, raw):
        wire = self.transport.prepare(raw)
        measurement = self._wire_meter(wire)
        exact(measurement, {'tokens', 'basis', 'request_digest'})
        if measurement['request_digest'] != digest(wire):
            raise InputError('Provider measurement is not bound to the wire request.')
        return {**measurement, 'request_digest': digest(raw)}

    def prepared(self, *args, **kwargs):
        prepared = super().prepared(*args, **kwargs)
        prepared['context'] = digest(canonical({'context': prepared['context'],
                                               'wire': digest(self.transport.prepare(prepared['raw'])),
                                               'provider_name': self.provider_name}))
        return prepared

    def request(self, *args, **kwargs):
        if self.transport.http.enabled is not True:
            raise InputError('Provider transport is not enabled.')
        return super().request(*args, **kwargs)

    def settlement_amount(self, call_id, usage, request_data):
        return request_data['provider_cost_micros']

    def _document(self, response):
        saved = parse_json(response, MAX_RESPONSE_BYTES)
        exact(saved, {'http_status', 'body_b64'})
        integer(saved['http_status'], 100, 599)
        body = parse_json(unb64(saved['body_b64'], BODY_LIMIT), BODY_LIMIT)
        if type(body) is not dict:
            raise InputError('Invalid provider response.')
        return saved['http_status'], body

    def classify(self, call_id, response, role, expected_parent, measured, bound, request_data):
        try:
            status, body = self._document(response)
        except InputError:
            return self.unconfirmed(call_id, 'malformed', 'provider_response', measured, bound)
        if status != 200 or body.get('error') is not None:
            kind = 'rate_limited' if status == 429 else 'rejected'
            return self.unconfirmed(call_id, kind, 'provider_error', measured, bound)
        try:
            gen = generation_id(body.get('id'))
            if body.get('model') != self.spec.model_id or body.get('provider') != self.provider_name:
                self.accounting.pause()
                return self.unconfirmed(call_id, 'substituted', 'provider_or_model_mismatch', measured, bound)
            with self.accounting.store.transaction():
                row = self.accounting.store.connection.execute('SELECT * FROM router_generations WHERE call_id=?', (call_id,)).fetchone()
                if row and (row['generation_id'] != gen or row['response_digest'] != digest(response)):
                    raise InputError('Generation binding changed.')
                self.accounting.store.connection.execute('INSERT OR IGNORE INTO router_generations VALUES(?,?,?)',
                                                         (call_id, gen, digest(response)))
                owner = self.accounting.store.connection.execute('SELECT call_id FROM router_generations WHERE generation_id=?', (gen,)).fetchone()
                if owner is None or owner[0] != call_id:
                    raise InputError('Generation belongs to another call.')
            receipt = self.accounting.store.connection.execute('SELECT body FROM router_receipts WHERE call_id=?', (call_id,)).fetchone()
            if receipt:
                data = self._document(receipt['body'])[1]['data']
                reported = {'input_tokens': data['native_tokens_prompt'], 'output_tokens': data['native_tokens_completion'],
                            'reasoning_tokens': data['native_tokens_reasoning']}
                cost = micros(data['total_cost'])
            else:
                usage = body.get('usage')
                if type(usage) is not dict:
                    raise InputError('Missing provider usage.')
                details = usage.get('completion_tokens_details')
                if type(details) is not dict:
                    raise InputError('Missing reasoning usage.')
                reported = {'input_tokens': usage['prompt_tokens'], 'output_tokens': usage['completion_tokens'],
                            'reasoning_tokens': details['reasoning_tokens']}
                cost = micros(usage['cost'])
            self.usage(reported)
        except (InputError, KeyError, TypeError):
            return self.unconfirmed(call_id, 'usage_unknown', 'provider_usage_unconfirmed', measured, bound)
        choices = body.get('choices')
        malformed = not (type(choices) is list and len(choices) == 1 and type(choices[0]) is dict)
        choice = choices[0] if not malformed else {}
        message = choice.get('message')
        message = message if type(message) is dict else {}
        refusal = bool(message.get('refusal')) or choice.get('finish_reason') == 'content_filter'
        stop = choice.get('finish_reason')
        payload = None
        if not refusal and stop == 'stop':
            try:
                if message.get('tool_calls') or type(message.get('content')) is not str:
                    raise InputError('Unexpected provider output.')
                payload = parse_json(message['content'], 65536)
            except (InputError, TypeError):
                payload = {'invalid_provider_output': True}
        elif stop not in ('length', 'content_filter'):
            payload = {'invalid_provider_output': True}
        normalized = {'schema_version': RESPONSE_SCHEMA, 'provider': self.spec.provider, 'model_id': self.spec.model_id,
                      'stop_reason': 'max_output_tokens' if stop == 'length' else 'complete',
                      'refusal': refusal, 'payload': payload, 'usage': reported}
        return super().classify(call_id, canonical(normalized), role, expected_parent, measured, bound,
                                {**request_data, 'provider_cost_micros': cost})

    def reconcile(self, call_id):
        row = self.accounting.get(call_id)
        if row['state'] not in ('usage_unknown', 'response_saved') or row['response_json'] is None:
            raise ConflictError('Call has no reconcilable response.')
        archived = parse_json(row['response_json'], 1048576)
        if archived.get('complete') is not True:
            raise InputError('Complete provider evidence is unavailable.')
        response = unb64(archived['body_b64'], MAX_RESPONSE_BYTES)
        if digest(response) != archived['body_digest']:
            raise InputError('Provider evidence integrity mismatch.')
        status, original = self._document(response)
        gen = generation_id(original.get('id'))
        request = parse_json(row['request_json'])
        if status != 200 or original.get('model') != self.spec.model_id or original.get('provider') != self.provider_name:
            raise InputError('Provider evidence does not match the selected route.')
        binding = self.accounting.store.connection.execute('SELECT * FROM router_generations WHERE call_id=?', (call_id,)).fetchone()
        if not binding or binding['generation_id'] != gen or binding['response_digest'] != digest(response):
            raise InputError('Generation binding is unavailable.')
        status, raw = self.transport.http.exchange('GET', '/api/v1/generation?id=' + gen, b'', self.spec.timeout_seconds)
        if status != 200:
            return {'call_id': call_id, 'state': row['state'], 'reason': 'receipt_unavailable'}
        if type(raw) is not bytes or len(raw) > BODY_LIMIT:
            raise InputError('Provider receipt exceeds bounds.')
        receipt = parse_json(raw, BODY_LIMIT)
        data = receipt.get('data') if type(receipt) is dict else None
        if (type(data) is not dict or data.get('id') != gen or data.get('model') != row['model_id']
                or data.get('provider_name') != self.provider_name or data.get('is_byok') is not False
                or data.get('cancelled') is not False or data.get('finish_reason') not in ('stop', 'length', 'content_filter')):
            raise InputError('Provider receipt does not confirm this generation.')
        try:
            cost = micros(data['total_cost'])
            self.usage({'input_tokens': data['native_tokens_prompt'], 'output_tokens': data['native_tokens_completion'],
                        'reasoning_tokens': data['native_tokens_reasoning']})
        except (KeyError, InputError):
            raise InputError('Provider receipt is incomplete.') from None
        evidence = canonical({'http_status': status, 'body_b64': b64(raw)})
        with self.accounting.store.transaction():
            current = self.accounting.get(call_id)
            if current['state'] not in ('usage_unknown', 'response_saved'):
                raise ConflictError('Call state changed during reconciliation.')
            prior = self.accounting.store.connection.execute('SELECT body FROM router_receipts WHERE call_id=?', (call_id,)).fetchone()
            if prior and prior['body'] != evidence:
                raise ConflictError('Provider receipt is immutable.')
            self.accounting.store.capacity(len(evidence) * 4)
            self.accounting.store.connection.execute('INSERT OR IGNORE INTO router_receipts VALUES(?,?)', (call_id, evidence))
            self.accounting.store.connection.execute("UPDATE model_calls SET state='settled',actual_micros=? WHERE call_id=?", (cost, call_id))
            if (cost > current['reserved_micros'] or data['native_tokens_prompt'] > request['max_input']
                    or data['native_tokens_completion'] > request['max_output']):
                self.accounting.store.connection.execute('UPDATE resource_policy SET paused=1 WHERE policy_id=1')
        return {'call_id': call_id, 'state': 'settled', 'reason': 'provider_receipt', 'billed_micros': cost}
