from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agents.channel import ChannelSpec, TransportDisconnected, TransportTimeout
from agents.fixtures import synthetic_meter
from agents.metering import CONTEXT_BOUND_BASIS, context_bound_meter
from agents.openrouter import BODY_LIMIT, CurlHTTP, OpenRouterChannel, micros, role_schemas
from agents.strategy import baseline as search_baseline
from evorph.candidates import baseline
from evorph.contracts import PUBLIC_MESSAGES, InputError, canonical, digest, parse_json
from evorph.evaluation import plan
from runtime.accounting import Accounting
from runtime.store import CapacityError, ConflictError, Limits, Store


class OfflineHTTP:
    enabled = True

    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def exchange(self, method, path, body, timeout):
        self.sent.append((method, path, body, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def completion(cost=0.000013, **updates):
    value = {'id': 'gen-synthetic1', 'model': 'synthetic/model', 'provider': 'Synthetic Provider',
             'choices': [{'finish_reason': 'stop', 'message': {'content': canonical({'plan': plan('prepare'), 'statement': 'Synthetic statement.'}).decode()}}],
             'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'completion_tokens_details': {'reasoning_tokens': 2}, 'cost': cost}}
    value.update(updates)
    return value


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-router-test-')
        self.limits = Limits(20, 10, 2, 180, 128 * 1024 * 1024, 0, 16 * 1024 * 1024)
        self.store = Store(Path(self.temp.name) / 'state', self.limits)
        self.accounts = Accounting(self.store)
        self.accounts.configure(1000000, 5000000)
        self.spec = ChannelSpec('synthetic-provider', 'synthetic/model',
                                {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000},
                                100000, 10000, 100, 10, 'synthetic-byte-meter.v1')
        self.segments = [{'name': 'task_rules', 'text': 'Synthetic fixed research instructions.'}]
        self.offline = patch('subprocess.run', side_effect=AssertionError('Unexpected live transport.'))
        self.offline.start()

    def tearDown(self):
        self.offline.stop()
        self.store.close()
        self.temp.cleanup()

    def channel(self, *responses, meter=synthetic_meter):
        http = OfflineHTTP(responses)
        return OpenRouterChannel(self.accounts, self.spec, http, provider_name='Synthetic Provider',
                                 schemas=role_schemas(), meter=meter), http

    def call(self, channel, key='test-request'):
        return channel.request('seeker', key, self.segments)

    def test_seeker_schema_admits_optional_strategy_and_stays_closed(self):
        seeker = role_schemas()['seeker']
        self.assertEqual(seeker['required'], ['plan', 'statement'])
        self.assertFalse(seeker['additionalProperties'])
        strategy = seeker['properties']['strategy']
        self.assertFalse(strategy['additionalProperties'])
        self.assertEqual(sorted(strategy['required']), sorted(['schema_version', 'spec_digest', 'version_id',
                                                                 'parent_id', 'message_order', 'mutation_order',
                                                                 'reduction_order']))
        self.assertEqual(strategy['properties']['schema_version'], {'const': 'search-strategy.v1'})
        order = strategy['properties']['message_order']
        self.assertEqual(order['minItems'], order['maxItems'])
        self.assertTrue(order['uniqueItems'])
        self.assertEqual(order['items']['enum'], sorted(PUBLIC_MESSAGES))
        self.assertEqual(strategy['properties']['reduction_order'], {'enum': ['forward', 'reverse']})

    def test_seeker_completion_carries_strategy_and_the_wire_schema_ships_it(self):
        proposed = {**search_baseline(), 'version_id': 'search-proposed-1', 'parent_id': 'search-baseline-v1'}
        value = completion()
        value['choices'][0]['message']['content'] = canonical(
            {'plan': plan('prepare'), 'statement': 'Synthetic statement.', 'strategy': proposed}).decode()
        channel, http = self.channel((200, canonical(value)))
        outcome = self.call(channel)
        self.assertEqual(outcome.kind, 'accepted')
        self.assertEqual(outcome.payload['strategy'], proposed)
        wire = parse_json(http.sent[0][2], 131072)
        self.assertIn('search-strategy.v1', canonical(wire).decode())

    def test_strategy_parent_ships_as_a_wire_const_without_touching_the_base_schema(self):
        channel, http = self.channel((200, canonical(completion())))
        outcome = channel.request('seeker', 'pinned-request', self.segments, strategy_parent='search-baseline-v1')
        self.assertEqual(outcome.kind, 'accepted')
        wire = parse_json(http.sent[0][2], 131072)
        strategy = wire['response_format']['json_schema']['schema']['properties']['strategy']
        self.assertEqual(strategy['properties']['parent_id'], {'const': 'search-baseline-v1'})
        base = channel.transport.schemas['seeker']['properties']['strategy']['properties']['parent_id']
        self.assertIn('anyOf', base)

    def test_wire_contract_pins_provider_and_measures_entire_payload(self):
        measured = []
        def meter(raw):
            measured.append(raw)
            return synthetic_meter(raw)
        channel, http = self.channel((200, canonical(completion())), meter=meter)
        outcome = self.call(channel)
        self.assertEqual(outcome.kind, 'accepted')
        self.assertEqual(measured, [http.sent[0][2]])
        wire = parse_json(measured[0], 131072)
        self.assertEqual(wire['provider']['only'], ['synthetic-provider'])
        self.assertFalse(wire['provider']['allow_fallbacks'])
        self.assertTrue(wire['provider']['require_parameters'])
        self.assertFalse(wire['stream'])
        self.assertEqual(wire['max_tokens'], 10000)
        self.assertEqual(wire['messages'][0]['role'], 'system')
        self.assertEqual(wire['response_format']['type'], 'json_schema')
        self.assertNotIn('task_key', wire)
        self.assertEqual(outcome.billed_micros, 13)
        self.assertEqual(self.accounts.totals(), {'settled_micros': 13, 'reserved_micros': 0})

    def test_disabled_transport_fails_before_any_reservation(self):
        http = CurlHTTP('synthetic-key')
        channel = OpenRouterChannel(self.accounts, self.spec, http, provider_name='Synthetic Provider',
                                    schemas=role_schemas(), meter=synthetic_meter)
        with self.assertRaises(InputError):
            self.call(channel)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

    def test_same_request_never_resends_and_schema_change_conflicts(self):
        channel, http = self.channel((200, canonical(completion())))
        first = self.call(channel)
        self.assertEqual(self.call(channel).call_id, first.call_id)
        channel.transport.schemas['seeker']['description'] = 'Changed wire context.'
        with self.assertRaises(ConflictError):
            self.call(channel)
        self.assertEqual(len(http.sent), 1)

    def test_errors_timeouts_and_missing_cost_remain_unknown(self):
        cases = [(429, canonical({'error': {'message': 'synthetic-private'}})), TransportTimeout(),
                 TransportDisconnected(), (200, canonical(completion(cost=None)))]
        for index, case in enumerate(cases):
            channel, http = self.channel(case)
            outcome = self.call(channel, 'case-' + str(index))
            self.assertEqual(outcome.state, 'usage_unknown')
            self.assertIsNone(outcome.billed_micros)
            self.assertEqual(self.call(channel, 'case-' + str(index)).kind, 'duplicate')
            self.assertEqual(len(http.sent), 1)
            self.assertNotIn('synthetic-private', repr(outcome))

    def test_route_substitution_does_not_settle_wrong_price(self):
        channel, _ = self.channel((200, canonical(completion(provider='Other Provider'))))
        outcome = self.call(channel)
        self.assertEqual((outcome.kind, outcome.state), ('substituted', 'usage_unknown'))
        self.assertEqual(self.store.connection.execute('SELECT paused FROM resource_policy').fetchone()[0], 1)

    def test_output_refusal_truncation_and_invalid_json_are_paid_not_executed(self):
        cases = [('length', {'content': '{'}, 'truncated'), ('content_filter', {'refusal': 'synthetic refusal'}, 'refused'),
                 ('stop', {'content': 'not-json'}, 'malformed')]
        for index, (stop, message, kind) in enumerate(cases):
            body = completion(id='gen-output' + str(index), choices=[{'finish_reason': stop, 'message': message}])
            channel, _ = self.channel((200, canonical(body)))
            outcome = self.call(channel, 'output-' + str(index))
            self.assertEqual((outcome.kind, outcome.state), (kind, 'settled'))
            self.assertIsNone(outcome.payload)
            self.assertEqual(outcome.billed_micros, 13)

    def test_money_rounding_and_overrun_preserve_actual_charge(self):
        self.assertEqual(micros('0.0000001'), 1)
        for invalid in (True, -1, 'NaN', 'Infinity', None, {}, '1e90'):
            with self.assertRaises(InputError):
                micros(invalid)
        channel, _ = self.channel((200, canonical(completion(cost=1))))
        outcome = self.call(channel)
        self.assertEqual(outcome.billed_micros, 1000000)
        with self.assertRaises(CapacityError):
            self.call(channel, 'next')

    def test_authoritative_receipt_settles_once_without_second_completion(self):
        data = {'id': 'gen-synthetic1', 'model': self.spec.model_id, 'provider_name': 'Synthetic Provider',
                'total_cost': 0.000027, 'native_tokens_prompt': 10, 'native_tokens_completion': 5,
                'native_tokens_reasoning': 2, 'is_byok': False, 'cancelled': False, 'finish_reason': 'stop'}
        channel, http = self.channel((200, canonical(completion(cost=None))), (200, canonical({'data': data})))
        first = self.call(channel)
        prior = self.accounts.get(first.call_id)['response_json']
        result = channel.reconcile(first.call_id)
        self.assertEqual(result['billed_micros'], 27)
        self.assertEqual(self.accounts.get(first.call_id)['response_json'], prior)
        self.assertEqual([entry[0] for entry in http.sent], ['POST', 'GET'])
        self.assertEqual(http.sent[1][1], '/api/v1/generation?id=gen-synthetic1')
        self.assertIsNotNone(self.store.connection.execute('SELECT body FROM router_receipts').fetchone())
        with self.assertRaises(ConflictError):
            channel.reconcile(first.call_id)
        self.assertEqual(len(http.sent), 2)

    def test_mismatched_or_unavailable_receipt_keeps_reservation(self):
        channel, http = self.channel((200, canonical(completion(cost=None))), (404, b'{}'),
                                    (200, canonical({'data': {'id': 'gen-other'}})))
        first = self.call(channel)
        self.assertEqual(channel.reconcile(first.call_id)['reason'], 'receipt_unavailable')
        with self.assertRaises(InputError):
            channel.reconcile(first.call_id)
        self.assertEqual(self.accounts.get(first.call_id)['state'], 'usage_unknown')
        self.assertIsNone(self.accounts.get(first.call_id)['actual_micros'])
        self.assertGreater(self.accounts.totals()['reserved_micros'], 0)

    def test_reused_generation_cannot_charge_second_local_request(self):
        channel, _ = self.channel((200, canonical(completion())), (200, canonical(completion())))
        self.call(channel)
        outcome = self.call(channel, 'another-request')
        self.assertEqual(outcome.state, 'usage_unknown')
        self.assertEqual(self.accounts.totals()['settled_micros'], 13)

    def test_transport_uses_stdin_deadline_and_bounded_output(self):
        key = 'synthetic-private-key'
        http = CurlHTTP(key, enabled=True)
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'{}\n200')) as run:
            self.assertEqual(http.exchange('POST', '/api/v1/chat/completions', b'{"input":"quote\\\""}', 5), (200, b'{}'))
            args, kwargs = run.call_args
            self.assertNotIn(key, ' '.join(args[0]))
            self.assertIn(key.encode(), kwargs['input'])
            self.assertIn('--max-time', args[0])
            self.assertIn('--max-filesize', args[0])
            self.assertNotIn('--location', args[0])
            self.assertEqual(kwargs['timeout'], 6)
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('curl', 5)):
            with self.assertRaises(TransportTimeout):
                http.exchange('GET', '/api/v1/generation?id=gen-test', b'', 5)
        with self.assertRaises(InputError):
            http.exchange('POST', '/api/v1/credits', b'{}', 5)
        with self.assertRaises(InputError):
            CurlHTTP('line\nbreak')

    def test_context_bound_meter_reserves_the_published_ceiling(self):
        wire = b'{"synthetic":"wire"}'
        measured = context_bound_meter(1000000)(wire)
        self.assertEqual(measured['tokens'], 1000000)
        self.assertEqual(measured['basis'], CONTEXT_BOUND_BASIS)
        self.assertEqual(measured['request_digest'], digest(wire))
        with self.assertRaises(InputError):
            context_bound_meter(1000000)('not-bytes')
        for invalid in (0, -1, 10000001):
            with self.assertRaises(InputError):
                context_bound_meter(invalid)

    def test_live_channel_reserves_the_context_bound_and_settles_actual_usage(self):
        spec = ChannelSpec('synthetic-provider', 'synthetic/model',
                           {'input_micros_per_million': 10000000, 'output_micros_per_million': 50000000},
                           1000000, 10000, 0, 10, CONTEXT_BOUND_BASIS, 'Synthetic Provider')
        self.accounts.configure(100000000, 500000000)
        http = OfflineHTTP([(200, canonical(completion()))])
        channel = OpenRouterChannel(self.accounts, spec, http, provider_name='Synthetic Provider',
                                    schemas=role_schemas(),
                                    meter=context_bound_meter(spec.max_input_tokens, spec.meter_basis))
        outcome = channel.request('seeker', 'bound-request', self.segments)
        self.assertEqual(outcome.kind, 'accepted')
        self.assertEqual(outcome.input_measurement, 1000000)
        self.assertEqual(outcome.input_bound, 1000000)
        self.assertEqual(outcome.usage['input_tokens'], 10)
        row = self.accounts.get(outcome.call_id)
        # The documented reservation ceiling: the whole context bound at the input
        # price plus the output cap, before the provider's actual usage is settled.
        self.assertEqual(row['reserved_micros'], 10500000)
        self.assertEqual(row['actual_micros'], 13)
        self.assertEqual(self.accounts.totals(), {'settled_micros': 13, 'reserved_micros': 0})

    def test_keeper_response_is_bound_to_existing_parent(self):
        self.store.submit(plan('prepare'), baseline(), 'synthetic-owner', 'parent')
        child = {**baseline(), 'version_id': 'synthetic-child', 'parent_id': 'baseline-v1'}
        body = completion(choices=[{'finish_reason': 'stop', 'message': {'content': canonical({'policy': child, 'statement': 'Synthetic repair.'}).decode()}}])
        channel, _ = self.channel((200, canonical(body)))
        outcome = channel.request('keeper', 'keeper-task', self.segments, expected_parent='baseline-v1')
        self.assertEqual(outcome.kind, 'accepted')
        self.assertEqual(outcome.payload['body'], child)


if __name__ == '__main__':
    unittest.main()
