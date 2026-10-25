from http.client import HTTPConnection
from pathlib import Path
import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest

from agents.channel import ModelChannel
from agents.fixtures import ScriptedTransport, reply, response, seeker_payload, synthetic_meter, usage
from agents.metering import CONTEXT_BOUND_BASIS
from agents.openrouter import OpenRouterTransport
from agents.research import Research, fixed_cases
from agents.strategy import baseline as method
from evorph.candidates import baseline
from evorph.contracts import InputError, canonical
from evorph.evaluation import plan
from ops.__main__ import SCHEDULE_MISSING, main, run_scheduler, serve_api
from ops.compose import build, publisher
from ops.config import load

LIMITS = {'queue_total': 20, 'queue_per_submitter': 10, 'max_attempts': 2, 'lease_seconds': 180,
          'storage_bytes': 134217728, 'min_free_bytes': 0, 'artifact_bytes': 16777216}
CHANNEL = {'provider': 'synthetic-provider', 'model_id': 'synthetic-research-model',
           'price': {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000},
           'max_input_tokens': 100000, 'max_output_tokens': 10000, 'margin_tokens': 100,
           'timeout_seconds': 30, 'meter_basis': 'synthetic-byte-meter.v1', 'script': []}
LIVE_CHANNEL = {'provider': 'synthetic-provider', 'model_id': 'synthetic-research-model',
                'provider_name': 'Synthetic Provider',
                'price': {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000},
                'max_input_tokens': 1000000, 'max_output_tokens': 10000, 'margin_tokens': 0,
                'timeout_seconds': 30, 'meter_basis': CONTEXT_BOUND_BASIS}
ORIGIN = 'https://research.invalid'


def free_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-ops-test-')
        self.root = Path(self.temp.name)
        self.write('runtime.json', {'limits': LIMITS, 'spending': None})
        self.raw = {
            'state': str(self.root / 'state'),
            'runtime_config': str(self.root / 'runtime.json'),
            'openssl': None,
            'service': {'mode': 'example', 'origin': ORIGIN, 'request_timeout': 30,
                        'limits': {'session_seconds': 3600, 'identity_per_window': 20, 'write_per_window': 50,
                                   'read_per_window': 1000, 'window_seconds': 60, 'page_limit': 100}},
            'api': {'listen_host': '127.0.0.1', 'listen_port': free_port()},
            'transport': {'enabled': False, 'credential_path': None},
            'channels': {'seeker': dict(CHANNEL), 'keeper': dict(CHANNEL)}}
        self.path = self.write('ops.json', self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload))
        return path

    def candidate(self, mutate):
        raw = json.loads(json.dumps(self.raw))
        mutate(raw)
        return self.write('candidate.json', raw)

    def test_sample_configuration_loads(self):
        config = load(self.path)
        self.assertEqual(config.mode, 'example')
        self.assertEqual(config.listen_host, '127.0.0.1')
        self.assertEqual(config.limits.storage_bytes, LIMITS['storage_bytes'])
        self.assertEqual(sorted(config.channels), ['keeper', 'seeker'])
        self.assertIsNone(config.openssl)

    def test_unknown_configuration_key_is_rejected(self):
        with self.assertRaises(InputError):
            load(self.candidate(lambda raw: raw.update({'extra': 1})))

    def test_live_archive_mode_requires_live_channels(self):
        def mutate(raw):
            raw['service']['mode'] = 'live'
        with self.assertRaises(InputError):
            load(self.candidate(mutate))

    def test_live_channels_require_live_archive_mode(self):
        def mutate(raw):
            raw['channels'] = {'seeker': dict(LIVE_CHANNEL), 'keeper': dict(LIVE_CHANNEL)}
            raw['transport'] = {'enabled': True, 'credential_path': str(self.root / 'absent-key')}
        with self.assertRaises(InputError):
            load(self.candidate(mutate))

    def test_mixed_channel_kinds_are_rejected(self):
        def mutate(raw):
            raw['channels']['keeper'] = dict(LIVE_CHANNEL)
        with self.assertRaises(InputError):
            load(self.candidate(mutate))

    def test_offline_channels_carry_no_credential(self):
        def mutate(raw):
            raw['transport'] = {'enabled': False, 'credential_path': str(self.root / 'absent-key')}
        with self.assertRaises(InputError):
            load(self.candidate(mutate))

    def test_live_channels_cannot_claim_a_precise_meter(self):
        def mutate(raw):
            raw['channels']['seeker']['meter_basis'] = 'synthetic-byte-meter.v1'
        with self.assertRaises(InputError):
            self.live(mutate)

    def live(self, mutate=None):
        raw = json.loads(json.dumps(self.raw))
        raw['service']['mode'] = 'live'
        raw['channels'] = {'seeker': dict(LIVE_CHANNEL), 'keeper': dict(LIVE_CHANNEL)}
        raw['transport'] = {'enabled': True, 'credential_path': str(self.root / 'absent-key')}
        if mutate is not None:
            mutate(raw)
        return load(self.write('live.json', raw))

    def test_reader_processes_never_need_the_credential(self):
        config = self.live()
        self.assertEqual(config.mode, 'live')
        runtime = build(config)
        try:
            self.assertEqual(runtime.service.mode, 'live')
            for role in ('seeker', 'keeper'):
                channel = runtime.research.channels[role]
                self.assertIs(type(channel.transport), OpenRouterTransport)
                self.assertFalse(channel.transport.http.enabled)
        finally:
            runtime.close()

    def test_inert_live_channel_cannot_spend(self):
        runtime = build(self.live())
        try:
            with self.assertRaises(InputError):
                runtime.research.channels['seeker'].request(
                    'seeker', 'inert-request', [{'name': 'task_rules', 'text': 'Synthetic rules.'}])
            self.assertEqual(runtime.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)
        finally:
            runtime.close()

    def test_execution_process_fails_at_start_without_the_credential(self):
        with self.assertRaises(InputError):
            build(self.live(), execute=True)

    def test_execution_process_reads_a_usable_credential(self):
        key = self.root / 'provider.key'
        key.write_text('synthetic-live-key\n')
        config = self.live(lambda raw: raw['transport'].__setitem__('credential_path', str(key)))
        runtime = build(config, execute=True)
        try:
            self.assertTrue(runtime.research.channels['keeper'].transport.http.enabled)
        finally:
            runtime.close()

    def test_research_rejects_an_unreviewed_transport(self):
        runtime = build(load(self.path))
        try:
            class Arbitrary:
                def send(self, raw, timeout):
                    raise AssertionError('An unreviewed transport must never send.')

            spec = runtime.research.channels['seeker'].spec
            seeker = ModelChannel(runtime.accounting, spec, Arbitrary(), synthetic_meter)
            with self.assertRaises(InputError):
                Research(runtime.store, seeker, runtime.research.channels['keeper'])
        finally:
            runtime.close()

    def test_live_channels_cannot_write_an_example_archive(self):
        runtime = build(self.live())
        try:
            channels = runtime.research.channels
            with self.assertRaises(InputError):
                Research(runtime.store, channels['seeker'], channels['keeper'], mode='example')
        finally:
            runtime.close()

    def test_research_roles_must_share_one_reviewed_transport(self):
        runtime = build(self.live())
        try:
            spec = runtime.research.channels['keeper'].spec
            offline = ModelChannel(runtime.accounting, spec, ScriptedTransport([]), synthetic_meter)
            with self.assertRaises(InputError):
                Research(runtime.store, runtime.research.channels['seeker'], offline, mode='live')
        finally:
            runtime.close()

    def test_finished_batch_is_projected_and_unreleasable_records_stay_private(self):
        runtime = build(load(self.path))
        try:
            research, service = runtime.research, runtime.service
            batch = research.freeze('template', baseline(), method(), search_attempts=1,
                                    reduction_attempts=0, evaluation_cases=fixed_cases())
            first = research.record(batch, 'step-1', 'decision', {
                'outcome': 'deferred', 'candidate_id': None, 'parent_id': None, 'supporting': [],
                'reason': 'no_verified_discrepancy'})
            research.record(batch, 'step-2', 'evaluation_set', {'cases': [], 'digest': 'synthetic-digest'})
            research.record(batch, 'step-3', 'decision', {
                'outcome': 'deferred', 'candidate_id': None, 'parent_id': None, 'supporting': [first],
                'reason': 'no_verified_discrepancy'})
            report = publisher(service, research)(batch)
            self.assertEqual(report, {'state': 'published', 'withheld_count': 0, 'reason_categories': []})
            experiment_id = research.alias('experiment', batch)
            listed = service.list_experiments()
            self.assertEqual([item['experiment_id'] for item in listed['experiments']], [experiment_id])
            self.assertEqual(listed['mode'], 'example')
            page = service.list_records(experiment_id)
            self.assertEqual(len(page['records']), 2)
            self.assertTrue(all(record['type'] == 'decision' for record in page['records']))
            bodies = json.dumps(page['records'])
            self.assertNotIn('evaluation_set', bodies)
            self.assertNotIn('synthetic-digest', bodies)
            # Projecting twice must not duplicate the public archive.
            publisher(service, research)(batch)
            self.assertEqual(len(service.list_records(experiment_id)['records']), 2)
        finally:
            runtime.close()

    def test_schedule_reports_filtered_public_statement_without_its_text(self):
        runtime = build(load(self.path))
        try:
            runtime.accounting.configure(1000000, 5000000)
            research = runtime.research
            template = research.freeze('template', baseline(), method(), search_attempts=1,
                                       reduction_attempts=0, evaluation_cases=fixed_cases())
            runtime.scheduler.configure(template, interval_seconds=60, check_seconds=5)
            payload = seeker_payload(plan('prepare'), 'Synthetic authorization marker.')
            research.channels['seeker'].transport.script = [reply(response(
                'synthetic-provider', 'synthetic-research-model', payload, usage(10, 10)))]
            runtime.scheduler.enable()
            outcome = runtime.scheduler.tick()
            self.assertEqual(outcome['state'], 'completed')
            reports = runtime.scheduler.publication_status()['items']
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]['state'], 'partial')
            self.assertEqual(reports[0]['withheld_count'], 1)
            self.assertEqual(reports[0]['reason_categories'], ['private_marker'])
            records = runtime.service.list_records(research.alias('experiment', outcome['batch_id']))
            self.assertFalse(any(record['type'] == 'hypothesis' for record in records['records']))
            self.assertNotIn('Synthetic authorization marker.', canonical(reports).decode())
            self.assertNotIn('Synthetic authorization marker.', canonical(records).decode())
        finally:
            runtime.close()

    def test_public_bind_is_rejected(self):
        def mutate(raw):
            raw['api']['listen_host'] = '0.0.0.0'
        with self.assertRaises(InputError):
            load(self.candidate(mutate))

    def test_missing_openssl_binary_is_rejected(self):
        def mutate(raw):
            raw['openssl'] = str(self.root / 'absent-openssl')
        with self.assertRaises(InputError):
            load(self.candidate(mutate))

    def test_compose_keeps_offline_transports(self):
        runtime = build(load(self.path))
        try:
            self.assertEqual(runtime.service.mode, 'example')
            for role in ('seeker', 'keeper'):
                transport = runtime.research.channels[role].transport
                self.assertIs(type(transport), ScriptedTransport)
                self.assertEqual(transport.sends, 0)
        finally:
            runtime.close()

    def test_restart_does_not_unpause_spending(self):
        config = load(self.path)
        runtime = build(config)
        try:
            runtime.accounting.configure(1000000, 5000000)
            runtime.accounting.pause()
        finally:
            runtime.close()
        reopened = build(config)
        try:
            row = reopened.store.connection.execute(
                'SELECT paused FROM resource_policy WHERE policy_id=1').fetchone()
            self.assertEqual(row['paused'], 1)
        finally:
            reopened.close()

    def test_absent_spending_policy_blocks_the_schedule(self):
        runtime = build(load(self.path))
        try:
            row = runtime.store.connection.execute(
                'SELECT paused FROM resource_policy WHERE policy_id=1').fetchone()
            self.assertTrue(row is None or row['paused'] == 1)
        finally:
            runtime.close()

    def test_scheduler_worker_requires_a_stored_schedule(self):
        self.assertEqual(run_scheduler(load(self.path), stop=threading.Event()), SCHEDULE_MISSING)

    def test_command_line_status_reports_example_mode(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(['status', '--config', str(self.path)])
        self.assertEqual(code, 0)
        reported = json.loads(buffer.getvalue())
        self.assertEqual(reported['mode'], 'example')
        self.assertIsNone(reported['schedule'])
        self.assertEqual(reported['pending_tasks'], 0)
        self.assertEqual(reported['publications']['items'], [])
        self.assertEqual(reported['publications']['summary']['total'], 0)
        self.assertIsNone(reported['publications']['next_before'])

    def test_api_serves_public_read_and_stops_on_request(self):
        config = load(self.path)
        stop = threading.Event()
        server = threading.Thread(target=serve_api, args=(config,), kwargs={'stop': stop}, daemon=True)
        server.start()
        body = None
        deadline = time.time() + 10
        while time.time() < deadline and body is None:
            try:
                connection = HTTPConnection(config.listen_host, config.listen_port, timeout=5)
                connection.request('GET', '/api/research/experiments')
                response = connection.getresponse()
                body = json.loads(response.read().decode())
                self.assertEqual(response.status, 200)
                connection.close()
            except OSError:
                time.sleep(0.1)
        self.assertIsNotNone(body, 'api never accepted a connection')
        self.assertEqual(body['mode'], 'example')
        self.assertEqual(body['experiments'], [])
        stop.set()
        server.join(timeout=10)
        self.assertFalse(server.is_alive())


if __name__ == '__main__':
    unittest.main()
