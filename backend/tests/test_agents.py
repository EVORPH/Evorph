from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from evorph.candidates import baseline
from evorph.contracts import SPEC_DIGEST, InputError, canonical, digest, parse_json, unb64
from evorph.evaluation import plan
from agents.channel import MAX_RESPONSE_BYTES, ChannelSpec, ModelChannel, bill
from agents.fixtures import (ScriptedTransport, disconnected, keeper_payload, limited, reply,
                             response, seeker_payload, synthetic_meter, timed_out, usage)
from runtime.accounting import Accounting
from runtime.store import CapacityError, ConflictError, Limits, Store

PRICE = {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000}
PROVIDER = 'synthetic-provider'
MODEL = 'synthetic/model-a'


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-agents-test-')
        limits = Limits(queue_total=4, queue_per_submitter=2, max_attempts=2, lease_seconds=180,
                        storage_bytes=64 * 1024 * 1024, min_free_bytes=0, artifact_bytes=16 * 1024 * 1024)
        self.now = 1800000000.0
        self.store = Store(Path(self.temp.name) / 'state', limits, clock=lambda: self.now)
        self.accounts = Accounting(self.store)
        self.accounts.configure(100000, 500000)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def spec(self, **overrides):
        configured = {'provider': PROVIDER, 'model_id': MODEL, 'price': PRICE, 'max_input_tokens': 1000,
                      'max_output_tokens': 50, 'margin_tokens': 5, 'timeout_seconds': 30,
                      'meter_basis': 'synthetic-byte-meter.v1'}
        return ChannelSpec(**{**configured, **overrides})

    def channel(self, script=(), spec=None):
        transport = ScriptedTransport(script)
        return ModelChannel(self.accounts, spec or self.spec(), transport, synthetic_meter), transport

    def segments(self, text='one two three four five'):
        return [{'name': 'task_rules', 'text': text}]

    def ledger(self, call_id):
        return self.accounts.get(call_id)

    def test_successful_call_is_bounded_settled_and_validated(self):
        channel, transport = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare', 'confirm')),
                                                         usage(4, 6, 2)))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason, result.state), ('accepted', 'validated', 'settled'))
        self.assertEqual(result.payload['body'], plan('prepare', 'confirm'))
        self.assertEqual(result.payload['statement'], 'Synthetic seeker statement.')
        measured = len(transport.sent[0]['raw'])
        self.assertEqual((result.input_measurement, result.input_bound), (measured, measured + 5))
        self.assertEqual(self.ledger(result.call_id)['actual_micros'], 10)
        self.assertEqual(self.accounts.totals(), {'settled_micros': 10, 'reserved_micros': 0})
        self.assertEqual(transport.sends, 1)

    def test_reasoning_tokens_are_not_billed_twice(self):
        self.assertEqual(bill(PRICE, 4, 6), 10)
        self.assertEqual(Accounting.estimate(PRICE, 4, 6), 10)
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 6, 6)))])
        self.assertEqual(channel.request('seeker', 'task-1', self.segments()).billed_micros, 10)

    def test_request_envelope_pins_spec_and_forbids_fallback(self):
        channel, transport = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(1, 1)))])
        channel.request('seeker', 'task-1', self.segments())
        sent = parse_json(transport.sent[0]['raw'])
        self.assertEqual(transport.sent[0]['timeout_seconds'], 30)
        self.assertEqual(sent['schema_version'], 'model-request.v1')
        self.assertEqual((sent['provider'], sent['model_id'], sent['role']), (PROVIDER, MODEL, 'seeker'))
        self.assertEqual(sent['spec_digest'], SPEC_DIGEST)
        self.assertTrue(sent['structured_output'])
        self.assertFalse(sent['allow_fallback'])
        self.assertEqual(sent['max_output_tokens'], 50)
        self.assertEqual(sent['segments'], [{'name': 'task_rules', 'text': 'one two three four five'}])

    def test_confirmed_rate_limit_costs_nothing(self):
        channel, transport = self.channel([limited(True)])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason, result.state), ('rate_limited', 'confirmed_no_usage', 'settled'))
        self.assertEqual(result.billed_micros, 0)
        self.assertEqual(self.ledger(result.call_id)['actual_micros'], 0)
        self.assertIsNotNone(self.ledger(result.call_id)['response_json'])
        self.assertEqual(self.accounts.totals(), {'settled_micros': 0, 'reserved_micros': 0})
        self.assertEqual(transport.sends, 1)

    def test_unconfirmed_rejection_leaves_usage_unknown(self):
        channel, _ = self.channel([limited(False)])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.state), ('rate_limited', 'usage_unknown'))
        self.assertEqual(self.ledger(result.call_id)['actual_micros'], None)
        self.assertEqual(self.accounts.totals()['reserved_micros'], result.input_bound + 50)

    def test_timeout_and_disconnect_never_read_as_free(self):
        first, _ = self.channel([timed_out()])
        timeout = first.request('seeker', 'task-timeout', self.segments())
        second, _ = self.channel([disconnected()])
        dropped = second.request('seeker', 'task-disconnect', self.segments())
        self.assertEqual((timeout.kind, timeout.state), ('timeout', 'usage_unknown'))
        self.assertEqual((dropped.kind, dropped.state), ('usage_unknown', 'usage_unknown'))
        self.assertEqual(self.accounts.totals(), {'settled_micros': 0,
                                                'reserved_micros': timeout.input_bound + dropped.input_bound + 100})
        with self.store.worker_lock():
            self.store.recover()
        self.assertEqual(self.ledger(timeout.call_id)['state'], 'usage_unknown')
        self.assertEqual(self.ledger(dropped.call_id)['state'], 'usage_unknown')

    def test_repeated_request_never_sends_twice(self):
        channel, transport = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 6)))])
        first = channel.request('seeker', 'task-1', self.segments())
        again = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual(first.kind, 'accepted')
        self.assertEqual((again.kind, again.reason, again.call_id), ('duplicate', 'already_dispatched', first.call_id))
        self.assertEqual(transport.sends, 1)
        self.assertEqual(self.accounts.totals()['settled_micros'], 10)

    def test_cancelled_reservation_is_not_dispatched(self):
        channel, transport = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(1, 1)))])
        prepared = channel.prepared('seeker', 'task-1', self.segments())
        call_id = self.accounts.reserve(prepared['request_key'], 'seeker', MODEL, PRICE,
                                        prepared['bound'], 50, prepared['context'])
        self.accounts.cancel_before_send(call_id)
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.call_id, result.state), ('cancelled', call_id, 'cancelled_before_send'))
        self.assertEqual(transport.sends, 0)
        self.assertEqual(self.accounts.totals(), {'settled_micros': 0, 'reserved_micros': 0})

    def test_truncated_output_is_paid_and_discarded(self):
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 50, 10),
                                                  stop_reason='max_output_tokens'))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason, result.state), ('truncated', 'output_limit', 'settled'))
        self.assertIsNone(result.payload)
        self.assertEqual(result.billed_micros, 54)
        self.assertEqual(self.ledger(result.call_id)['actual_micros'], 54)

    def test_refusal_is_recorded_without_payload(self):
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, None, usage(4, 1), refusal=True))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason, result.state), ('refused', 'provider_refusal', 'settled'))
        self.assertIsNone(result.payload)
        self.assertEqual(result.billed_micros, 5)
        other, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 1), refusal=True))])
        self.assertEqual(other.request('seeker', 'task-2', self.segments()).reason, 'refusal_with_payload')

    def test_malformed_responses_are_not_settled(self):
        cases = [(b'synthetic-not-json', 'unparseable_response'),
                 (response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 2, 9)), 'invalid_usage'),
                 (response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 2), without_usage_field=True),
                  'unparseable_response'),
                 (response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 2), schema='model-response.v2'),
                  'unparseable_response'),
                 (response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 2), stop_reason='paused'),
                  'unparseable_response'),
                 (b'x' * (MAX_RESPONSE_BYTES + 1), 'response_out_of_bounds')]
        for index, (raw, reason) in enumerate(cases):
            channel, _ = self.channel([reply(raw)])
            result = channel.request('seeker', 'task-' + str(index), self.segments())
            self.assertEqual((result.kind, result.reason, result.state), ('malformed', reason, 'usage_unknown'))
            self.assertIsNone(result.payload)
            row = self.ledger(result.call_id)
            self.assertEqual(row['actual_micros'], None)
            self.assertIsNotNone(row['response_json'])
            self.assertEqual(row['state'], result.state)
            self.assertLessEqual(len(row['response_json']), 1048576)

    def test_missing_usage_stays_unsettled_with_evidence_saved(self):
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), None))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason, result.state), ('usage_unknown', 'missing_usage', 'usage_unknown'))
        row = self.ledger(result.call_id)
        self.assertIsNotNone(row['response_json'])
        self.assertIsNone(row['actual_micros'])
        self.assertEqual(self.accounts.totals(), {'settled_micros': 0, 'reserved_micros': result.input_bound + 50})

    def test_input_ceiling_blocks_before_reservation(self):
        channel, transport = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(1, 1)))])
        segments = self.segments('word ' * 1200)
        result = channel.request('seeker', 'task-1', segments)
        self.assertEqual((result.kind, result.reason, result.state), ('input_rejected', 'input_ceiling', 'not_reserved'))
        self.assertEqual(result.call_id, '')
        self.assertEqual(result.input_measurement, len(channel.prepared('seeker', 'task-1', segments)['raw']))
        self.assertEqual(transport.sends, 0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

    def test_segment_allowlist_and_role_are_enforced(self):
        channel, _ = self.channel()
        for segments in ([{'name': 'private_notes', 'text': 'one'}],
                         [{'name': 'task_rules', 'text': 'one'}, {'name': 'task_rules', 'text': 'two'}],
                         [{'name': name, 'text': 'one'} for name in ('task_rules', 'public_evidence', 'approved_history', 'task_rules')],
                         [], [{'name': 'task_rules', 'text': 5}], [{'name': 'task_rules'}]):
            with self.assertRaises(InputError):
                channel.request('seeker', 'task-1', segments)
        with self.assertRaises(InputError):
            channel.request('auditor', 'task-1', self.segments())
        with self.assertRaises(InputError):
            channel.request('seeker', 'not an identifier', self.segments())

    def test_substituted_provider_or_model_is_rejected(self):
        channel, _ = self.channel([reply(response('other-provider', MODEL, seeker_payload(plan('prepare')), usage(4, 6)))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason, result.state), ('substituted', 'provider_or_model_mismatch', 'usage_unknown'))
        self.assertIsNone(result.payload)
        self.assertIsNone(result.billed_micros)
        self.assertEqual(self.store.connection.execute('SELECT paused FROM resource_policy').fetchone()[0], 1)
        self.accounts.configure(100000, 500000)
        other, _ = self.channel([reply(response(PROVIDER, 'synthetic/model-b', None, None))])
        fallback = other.request('seeker', 'task-2', self.segments())
        self.assertEqual((fallback.kind, fallback.state), ('substituted', 'usage_unknown'))

    def test_keeper_candidate_must_bind_approved_parent(self):
        self.store.submit(plan('prepare'), baseline(), 'synthetic-participant', 'synthetic-parent')
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, keeper_payload(baseline()), usage(4, 6)))])
        result = channel.request('keeper', 'task-1', self.segments(), expected_parent='baseline-v1')
        self.assertEqual((result.kind, result.reason, result.state), ('malformed', 'invalid_payload', 'settled'))
        self.assertIsNone(result.payload)
        child = {**baseline(), 'version_id': 'synthetic-child', 'parent_id': 'baseline-v1'}
        other, transport = self.channel([reply(response(PROVIDER, MODEL, keeper_payload(child), usage(4, 6)))])
        accepted = other.request('keeper', 'task-2', self.segments(), expected_parent='baseline-v1')
        self.assertEqual(accepted.kind, 'accepted')
        self.assertEqual(accepted.payload['body'], child)
        self.assertEqual(parse_json(transport.sent[0]['raw'])['expected_parent'], 'baseline-v1')

    def test_overrun_is_recorded_and_pauses_further_calls(self):
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 2000)))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.billed_micros), ('rejected', 2004))
        self.assertIsNone(result.payload)
        self.assertEqual(self.ledger(result.call_id)['actual_micros'], 2004)
        self.assertEqual(self.store.connection.execute('SELECT paused FROM resource_policy').fetchone()[0], 1)
        with self.assertRaises(CapacityError):
            channel.request('seeker', 'task-2', self.segments())

    def test_channel_requires_ledger_transport_and_meter(self):
        transport = ScriptedTransport()
        with self.assertRaises(InputError):
            ModelChannel(self.store, self.spec(), transport, synthetic_meter)
        with self.assertRaises(InputError):
            ModelChannel(self.accounts, self.spec(), None, synthetic_meter)
        with self.assertRaises(InputError):
            ModelChannel(self.accounts, self.spec(), object(), synthetic_meter)
        with self.assertRaises(InputError):
            ModelChannel(self.accounts, self.spec(), transport, None)
        for overrides in ({'margin_tokens': -1}, {'meter_basis': ''}, {'model_id': ''},
                          {'timeout_seconds': 0}, {'max_input_tokens': 0}, {'provider': 'not an identifier'}):
            with self.assertRaises(InputError):
                self.spec(**overrides).validate()

    def test_channel_modules_import_no_network_path(self):
        forbidden = {'socket', 'ssl', 'urllib', 'http', 'requests', 'subprocess', 'ftplib', 'smtplib', 'asyncio'}
        for name in ('channel.py', 'fixtures.py'):
            source = (Path(__file__).resolve().parents[1] / 'agents' / name).read_text()
            for line in source.splitlines():
                match = re.match(r'\s*(?:from|import)\s+([A-Za-z0-9_.]+)', line)
                if match and match.group(1).split('.')[0] in forbidden:
                    self.fail('Offline channel imported a network module: ' + name)

    def test_review_changed_input_cannot_resend_same_task(self):
        channel, transport = self.channel([timed_out(), timed_out()])
        first = channel.request('seeker', 'task-1', self.segments())
        with self.assertRaises(ConflictError):
            channel.request('seeker', 'task-1', self.segments('changed approved text'))
        self.assertEqual(transport.sends, 1)
        self.assertEqual(self.ledger(first.call_id)['state'], 'usage_unknown')

    def test_review_missing_parent_is_rejected_before_send(self):
        channel, transport = self.channel([timed_out()])
        with self.assertRaises(InputError):
            channel.request('keeper', 'task-1', self.segments())
        self.assertEqual(transport.sends, 0)

    def test_review_substitution_keeps_cost_unknown_and_pauses(self):
        channel, _ = self.channel([reply(response('other-provider', MODEL, None, usage(4, 6)))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual(self.ledger(result.call_id)['state'], 'usage_unknown')
        self.assertIsNone(self.ledger(result.call_id)['actual_micros'])
        self.assertIsNone(result.billed_micros)
        self.assertEqual(self.store.connection.execute('SELECT paused FROM resource_policy').fetchone()[0], 1)

    def test_review_unknown_result_matches_durable_state(self):
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, None, None))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual(result.state, self.ledger(result.call_id)['state'])
        self.assertIsNone(result.billed_micros)

    def test_review_transport_exception_is_private_and_unsettled(self):
        channel, transport = self.channel()
        def fail(raw, timeout):
            raise RuntimeError('synthetic-private-credential')
        transport.send = fail
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual(result.state, 'usage_unknown')
        self.assertNotIn('synthetic-private-credential', repr(result))
        self.assertEqual(self.ledger(result.call_id)['state'], result.state)

    def test_review_meter_receives_complete_wire_request(self):
        channel, _ = self.channel()
        measured = []
        def meter(raw):
            measured.append(raw)
            return synthetic_meter(raw)
        channel.meter = meter
        prepared = channel.prepared('seeker', 'task-1', self.segments())
        self.assertEqual(measured, [prepared['raw']])

    def test_review_pause_blocks_already_reserved_send(self):
        call_id = self.accounts.reserve('synthetic-pending', 'seeker', MODEL, PRICE, 10, 10, 'context')
        self.accounts.pause()
        with self.assertRaises(CapacityError):
            self.accounts.dispatch(call_id)
        self.assertEqual(self.ledger(call_id)['state'], 'reserved')

    def test_review_malformed_response_retains_bounded_evidence(self):
        channel, _ = self.channel([reply(b'synthetic-broken-json')])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertIsNotNone(self.ledger(result.call_id)['response_json'])
        self.assertEqual(self.ledger(result.call_id)['state'], 'usage_unknown')

    def test_review_price_mutation_does_not_reprice_dispatched_request(self):
        price = dict(PRICE)
        channel, transport = self.channel(spec=self.spec(price=price))
        def reply_after_change(raw, timeout):
            price['output_micros_per_million'] = 2000000
            return response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 6))
        transport.send = reply_after_change
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual(result.billed_micros, 10)

    def test_changed_route_and_parent_cannot_bypass_task_identity(self):
        first, _ = self.channel([timed_out()])
        first.request('seeker', 'task-1', self.segments())
        other, transport = self.channel(spec=self.spec(provider='synthetic-other'))
        with self.assertRaises(ConflictError):
            other.request('seeker', 'task-1', self.segments())
        self.assertEqual(transport.sends, 0)
        for index, version in enumerate(('synthetic-parent-a', 'synthetic-parent-b')):
            candidate = {**baseline(), 'version_id': version}
            self.store.submit(plan('prepare'), candidate, 'synthetic-owner-' + str(index), 'parent-request')
        keeper, sent = self.channel([timed_out()])
        keeper.request('keeper', 'task-keeper', self.segments(), expected_parent='synthetic-parent-a')
        with self.assertRaises(ConflictError):
            keeper.request('keeper', 'task-keeper', self.segments(), expected_parent='synthetic-parent-b')
        self.assertEqual(sent.sends, 1)

    def test_meter_evidence_and_total_encoding_are_checked_before_reserve(self):
        channel, transport = self.channel()
        cases = [{'tokens': 1, 'basis': 'different-meter', 'request_digest': '0' * 64},
                 {'tokens': 1, 'basis': channel.spec.meter_basis, 'request_digest': '0' * 64}]
        for receipt in cases:
            channel.meter = lambda raw: receipt
            with self.assertRaises(InputError):
                channel.request('seeker', 'task-1', self.segments())
        for count in (0, -1, True, 1.5, 10000001):
            channel.meter = lambda raw: {**synthetic_meter(raw), 'tokens': count}
            with self.assertRaises(InputError):
                channel.request('seeker', 'task-1', self.segments())
        channel.meter = synthetic_meter
        with self.assertRaises(InputError):
            channel.request('seeker', 'task-1', self.segments('\U0001f600' * 20000))
        self.assertEqual(transport.sends, 0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

    def test_competing_connections_have_one_dispatch_winner(self):
        barrier = Barrier(2)
        sent = []
        def run():
            store = Store(self.store.root, self.store.limits, clock=lambda: self.now)
            try:
                accounts = Accounting(store)
                original = accounts.dispatch
                def dispatch(call_id):
                    barrier.wait(timeout=5)
                    original(call_id)
                accounts.dispatch = dispatch
                transport = ScriptedTransport()
                def send(raw, timeout):
                    sent.append(raw)
                    return response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 6))
                transport.send = send
                channel = ModelChannel(accounts, self.spec(), transport, synthetic_meter)
                return channel.request('seeker', 'concurrent-task', self.segments())
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(run) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(result.kind for result in results), ['accepted', 'duplicate'])
        self.assertEqual(len(sent), 1)
        self.assertEqual(len({result.call_id for result in results}), 1)
        self.assertEqual(self.accounts.totals(), {'settled_micros': 10, 'reserved_micros': 0})

    def test_saved_response_survives_interrupted_settlement_without_resend(self):
        raw = response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 6))
        channel, transport = self.channel([reply(raw)])
        with patch.object(self.accounts, 'settle', side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                channel.request('seeker', 'task-1', self.segments())
        root, limits = self.store.root, self.store.limits
        self.store.close()
        self.store = Store(root, limits, clock=lambda: self.now)
        self.accounts = Accounting(self.store)
        channel.accounting = self.accounts
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.state), ('duplicate', 'response_saved'))
        self.assertEqual(transport.sends, 1)
        saved = parse_json(self.ledger(result.call_id)['response_json'], 1048576)
        self.assertEqual(unb64(saved['body_b64'], MAX_RESPONSE_BYTES), raw)
        self.assertEqual(saved['body_digest'], digest(raw))
        self.assertIsNone(self.ledger(result.call_id)['actual_micros'])
        self.assertGreater(self.accounts.totals()['reserved_micros'], 0)

    def test_recovered_dispatched_request_remains_unknown_and_single_send(self):
        channel, transport = self.channel([timed_out()])
        prepared = channel.prepared('seeker', 'task-1', self.segments())
        call_id = self.accounts.reserve(prepared['request_key'], 'seeker', MODEL, PRICE,
                                        prepared['bound'], 50, prepared['context'])
        self.accounts.dispatch(call_id)
        with self.store.worker_lock():
            self.store.recover()
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.state), ('duplicate', 'usage_unknown'))
        self.assertEqual(transport.sends, 0)
        with self.assertRaises(ConflictError):
            self.accounts.settle(call_id, 0)
        self.assertGreater(self.accounts.totals()['reserved_micros'], 0)

    def test_response_storage_failure_never_releases_cost_or_leaks_error(self):
        channel, transport = self.channel([reply(response(PROVIDER, MODEL, None, usage(4, 6)))])
        with patch.object(self.accounts, 'record_response', side_effect=OSError('synthetic-private-path')):
            result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.reason, result.state), ('storage_unavailable', 'usage_unknown'))
        self.assertEqual(transport.sends, 1)
        self.assertIsNone(result.billed_micros)
        self.assertIsNone(self.ledger(result.call_id)['actual_micros'])
        self.assertNotIn('synthetic-private-path', repr(result))

    def test_capacity_failure_before_send_keeps_reservation_cancellable(self):
        channel, transport = self.channel([timed_out()])
        original = self.store.capacity
        def capacity(amount=0):
            if amount == 1048576 * 4:
                raise CapacityError('Synthetic capacity boundary.')
            original(amount)
        with patch.object(self.store, 'capacity', side_effect=capacity):
            with self.assertRaises(CapacityError):
                channel.request('seeker', 'task-1', self.segments())
        row = self.store.connection.execute('SELECT call_id,state FROM model_calls').fetchone()
        self.assertEqual(row['state'], 'reserved')
        self.assertEqual(transport.sends, 0)
        self.accounts.cancel_before_send(row['call_id'])
        self.assertEqual(self.accounts.totals()['reserved_micros'], 0)

    def test_reduced_allocation_blocks_pending_dispatch(self):
        call_id = self.accounts.reserve('synthetic-pending', 'seeker', MODEL, PRICE, 10, 10, 'context')
        self.accounts.configure(10, 10)
        with self.assertRaises(CapacityError):
            self.accounts.dispatch(call_id)
        self.assertEqual(self.ledger(call_id)['state'], 'reserved')

    def test_output_cap_violation_pauses_even_without_monetary_overrun(self):
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 51)))])
        result = channel.request('seeker', 'task-1', self.segments())
        self.assertEqual((result.kind, result.reason), ('rejected', 'usage_exceeds_bounds'))
        self.assertIsNone(result.payload)
        self.assertLess(result.billed_micros, self.ledger(result.call_id)['reserved_micros'])
        self.assertEqual(self.store.connection.execute('SELECT paused FROM resource_policy').fetchone()[0], 1)

    def test_external_payload_cannot_expand_capabilities_or_change_spec(self):
        invalid = []
        for events in ([{'kind': 'execute', 'command': 'synthetic-command'}],
                       [{'kind': 'deliver', 'message_ref': 'private-key-material'}],
                       [{'kind': 'duplicate', 'event_index': 0}],
                       [{'kind': 'advance', 'ticks': True}]):
            invalid.append(seeker_payload({**plan('prepare'), 'events': events}))
        invalid.append(seeker_payload({**plan('prepare'), 'spec_digest': '0' * 64}))
        invalid.append({**seeker_payload(plan('prepare')), 'private_reasoning': 'synthetic-private-text'})
        for index, payload in enumerate(invalid):
            channel, _ = self.channel([reply(response(PROVIDER, MODEL, payload, usage(4, 6)))])
            result = channel.request('seeker', 'invalid-' + str(index), self.segments())
            self.assertEqual((result.kind, result.reason), ('malformed', 'invalid_payload'))
            self.assertIsNone(result.payload)
            self.assertEqual(result.billed_micros, 10)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_untrusted_json_stays_bounded_with_private_evidence(self):
        cases = [b'{}', b'{"duplicate":1,"duplicate":2}', b'\xff', b'[' * 1100 + b']' * 1100]
        valid = response(PROVIDER, MODEL, seeker_payload(plan('prepare')), usage(4, 6))
        cases.append(valid.replace(b'"events":[', b'"events":[1e999,'))
        for index, raw in enumerate(cases):
            channel, _ = self.channel([reply(raw)])
            result = channel.request('seeker', 'malformed-' + str(index), self.segments())
            self.assertEqual(result.kind, 'malformed')
            self.assertIsNone(result.payload)
            saved = parse_json(self.ledger(result.call_id)['response_json'], 1048576)
            self.assertEqual(unb64(saved['body_b64'], MAX_RESPONSE_BYTES), raw)

    def test_oversized_and_unicode_responses_keep_bounded_raw_evidence(self):
        import json
        envelope = parse_json(response(PROVIDER, MODEL, seeker_payload(plan('prepare'), '\u754c' * 1800), usage(4, 6)))
        raw = json.dumps(envelope, ensure_ascii=False).encode('utf-8')
        channel, _ = self.channel([reply(raw)])
        result = channel.request('seeker', 'unicode-response', self.segments())
        self.assertEqual(result.kind, 'accepted')
        saved = parse_json(self.ledger(result.call_id)['response_json'], 1048576)
        self.assertEqual(unb64(saved['body_b64'], MAX_RESPONSE_BYTES), raw)
        other, _ = self.channel([reply(b'x' * (MAX_RESPONSE_BYTES + 1))])
        oversized = other.request('seeker', 'oversized-response', self.segments())
        row = self.ledger(oversized.call_id)
        saved = parse_json(row['response_json'], 1048576)
        self.assertFalse(saved['complete'])
        self.assertEqual(len(unb64(saved['body_b64'], MAX_RESPONSE_BYTES)), MAX_RESPONSE_BYTES)
        self.assertLessEqual(len(row['response_json']), 1048576)
        self.assertIsNone(row['actual_micros'])

    def test_private_result_repr_and_error_paths_do_not_publish_free_text(self):
        sentinel = 'synthetic-private-marker'
        channel, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare'), sentinel), usage(4, 6)))])
        result = channel.request('seeker', 'private-result', self.segments())
        self.assertEqual(result.payload['statement'], sentinel)
        self.assertNotIn(sentinel, repr(result))
        for index, text in enumerate(('text\x7f', 'text\u202e', 'text\ud800')):
            other, _ = self.channel([reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare'), text), usage(4, 6)))])
            invalid = other.request('seeker', 'control-' + str(index), self.segments())
            self.assertEqual((invalid.kind, invalid.reason), ('malformed', 'invalid_payload'))
            self.assertIsNone(invalid.payload)


if __name__ == '__main__':
    unittest.main()
