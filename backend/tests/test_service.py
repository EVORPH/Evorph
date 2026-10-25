from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.channel import ChannelSpec, ModelChannel
from agents.fixtures import ScriptedTransport, keeper_payload, reply, response, seeker_payload, synthetic_meter, usage
from agents.research import Research, fixed_cases
from agents.strategy import baseline as search_baseline
from evorph.candidates import baseline
from evorph.contracts import InputError, canonical, digest, parse_json
from evorph.evaluation import plan
from runtime.accounting import Accounting
from runtime.backup import create_backup, restore_backup
from runtime.store import ConflictError, Limits, Store
from service.http_api import MAX_BODY_BYTES, Request, dispatch, handler, resolve_client_key
from service.public_store import PublicService, ServiceLimits, review_event, RELEASABLE

PROVIDER = 'synthetic-provider'
MODEL = 'synthetic-research-model'
PRICE = {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000}
ORIGIN = 'https://research.invalid'


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-service-test-')
        self.limits = Limits(queue_total=20, queue_per_submitter=10, max_attempts=2, lease_seconds=180,
                             storage_bytes=128 * 1024 * 1024, min_free_bytes=0, artifact_bytes=16 * 1024 * 1024)
        self.root = Path(self.temp.name)
        self.clock = [1800000000.0]
        self.store = Store(self.root / 'state', self.limits, clock=lambda: self.clock[0])
        self.accounts = Accounting(self.store)
        self.accounts.configure(1000000, 5000000)
        spec = ChannelSpec(PROVIDER, MODEL, PRICE, 100000, 10000, 100, 30, 'synthetic-byte-meter.v1')
        self.seeker = ModelChannel(self.accounts, spec, ScriptedTransport([reply(response(
            PROVIDER, MODEL, seeker_payload(plan('prepare', 'confirm', 'legacy-transition'), 'Synthetic explanation.'),
            usage(10, 10)))]), synthetic_meter)
        child = {**baseline(), 'version_id': 'synthetic-repair', 'parent_id': 'ignore-retirement'}
        self.keeper = ModelChannel(self.accounts, spec, ScriptedTransport([reply(response(
            PROVIDER, MODEL, keeper_payload(child), usage(10, 10)))]), synthetic_meter)
        self.research = Research(self.store, self.seeker, self.keeper)
        self.batch_id = self.research.freeze('service-batch', baseline('ignore-retirement'), search_baseline(),
                                             search_attempts=1, reduction_attempts=0, evaluation_cases=fixed_cases())
        self.assertEqual(self.research.run(self.batch_id)['reason'], 'retained')
        self.service = PublicService(self.store, self.research, ServiceLimits(
            session_seconds=100, identity_per_window=2, write_per_window=2, read_per_window=100,
            window_seconds=60, page_limit=100), now=lambda: self.clock[0])
        self.service.approve(self.batch_id, 'synthetic-reviewer', self.reviewed())
        self.service.publish(self.batch_id)
        self.experiment_id = self.research.alias('experiment', self.batch_id)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def get(self, path, **query):
        return dispatch(self.service, Request('GET', path, query, client_key='client-1'), origin=ORIGIN)

    def post(self, path, body=b'', headers=None, client='client-1'):
        configured = {'origin': ORIGIN, 'content-type': 'application/json', **(headers or {})}
        return dispatch(self.service, Request('POST', path, {}, configured, body, client), origin=ORIGIN)

    def reviewed(self):
        return {event['record_id']: review_event(event) for event in self.research.journal(self.batch_id) if event['kind'] in RELEASABLE}

    def issued(self, response):
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        for key, value in response.headers:
            if key.lower() == 'set-cookie':
                jar.load(value)
        return {**response.body, 'credentials': {'token': jar['evorph_session'].value, 'csrf': response.body['csrf_token']}}

    def identity(self):
        created = self.post('/api/identities')
        self.assertEqual(created.status, 201)
        return self.issued(created)

    def write_headers(self, identity):
        return {'cookie': 'evorph_session=' + identity['credentials']['token'],
                'x-csrf-token': identity['credentials']['csrf'], 'origin': ORIGIN, 'content-type': 'application/json'}

    def test_reads_have_no_research_execution_or_spending_side_effects(self):
        counts = [self.store.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('model_calls', 'jobs')]
        with patch.object(self.research, 'run', side_effect=AssertionError('Read path must not execute research.')):
            page = self.get('/api/research/experiments/' + self.experiment_id + '/records', limit='3')
            self.assertEqual(page.status, 200)
            self.assertTrue(page.body['records'])
        self.assertEqual(counts, [self.store.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                                  for table in ('model_calls', 'jobs')])

    def test_pagination_covers_every_record_once_and_advances(self):
        seen, raw_cursor = [], None
        for _ in range(50):
            query = {'limit': '2'} if raw_cursor is None else {'limit': '2', 'cursor': raw_cursor}
            page = self.get('/api/research/experiments/' + self.experiment_id + '/records', **query)
            self.assertEqual(page.status, 200)
            seen.extend(record['sequence'] for record in page.body['records'])
            raw_cursor = page.body['next_cursor']
            if raw_cursor is None:
                break
        total = self.store.connection.execute('SELECT COUNT(*) FROM service_records').fetchone()[0]
        self.assertEqual((len(seen), len(set(seen)), total), (total, total, total))
        self.assertEqual(seen, sorted(seen))

    def test_after_sequence_returns_only_newer_records(self):
        page = self.get('/api/research/experiments/' + self.experiment_id + '/records', limit='100')
        midpoint = page.body['records'][2]['sequence']
        update = self.get('/api/research/experiments/' + self.experiment_id + '/records', after_sequence=str(midpoint))
        self.assertTrue(all(record['sequence'] > midpoint for record in update.body['records']))
        self.assertEqual(len(update.body['records']), len(page.body['records']) - 3)

    def test_only_approved_whole_records_are_published(self):
        releasable = len([event for event in self.research.journal(self.batch_id)
                          if event['kind'] in ('hypothesis', 'repair', 'verification', 'comparison', 'decision', 'contribution', 'finding', 'experiment')])
        approvals = self.store.connection.execute('SELECT COUNT(*) FROM service_approvals').fetchone()[0]
        published = self.store.connection.execute('SELECT COUNT(*) FROM service_records').fetchone()[0]
        self.assertEqual((approvals, published), (releasable, releasable))
        self.assertEqual(self.service.publish(self.batch_id)['published'], 0)
        payload = canonical(self.get('/api/research/experiments/' + self.experiment_id + '/records', limit='100').body).decode()
        internal = [self.batch_id]
        for table, column in [('model_calls', 'call_id'), ('jobs', 'job_id'), ('attempts', 'run_id'),
                              ('research_journal', 'record_id'), ('research_submissions', 'submission_id'),
                              ('research_issues', 'issue_id'), ('service_identities', 'owner_id')]:
            internal.extend(row[0] for row in self.store.connection.execute('SELECT ' + column + ' FROM ' + table))
        for value in filter(None, internal):
            self.assertNotIn(value, payload)
        for marker in ('micros', 'token_digest', 'price', 'authorization'):
            self.assertNotIn(marker, payload)

    def test_unapproved_events_are_not_projected(self):
        other = self.research.freeze('unpublished-batch', baseline('ignore-retirement'), search_baseline(),
                                     search_attempts=1, reduction_attempts=0, evaluation_cases=fixed_cases())
        self.service.publish(other)
        page = self.get('/api/research/experiments/' + self.research.alias('experiment', other) + '/records')
        self.assertEqual((page.status, page.body['records']), (200, []))
        with self.assertRaises(InputError):
            review_event({'kind': 'evaluation_set', 'body': {}})

    def test_approved_record_content_is_bound_and_immutable(self):
        event = next(item for item in self.research.journal(self.batch_id) if item['kind'] == 'hypothesis')
        altered = deepcopy(event)
        altered['body']['statement'] = 'synthetic-changed-text'
        with self.store.transaction():
            self.store.connection.execute('UPDATE service_approvals SET approval_digest=? WHERE record_id=?',
                                          (review_event(altered), event['record_id']))
        with self.assertRaises(ConflictError):
            self.service.approve(self.batch_id, 'synthetic-reviewer', self.reviewed())
        self.assertEqual(self.get('/api/research/experiments/' + self.experiment_id + '/records',
                                  limit='100').body['records'][0]['summary'], event['body']['statement'][:2000])

    def test_identity_recovery_rotates_and_revokes_previous_session(self):
        identity = self.identity()
        recovered = self.post('/api/identities/recovery', headers={'authorization': 'Bearer ' + identity['recovery']})
        self.assertEqual(recovered.status, 200)
        self.assertEqual(recovered.body['contributor_id'], identity['contributor_id'])
        self.assertIsNone(self.service.session(identity['credentials']['token']))
        self.assertIsNotNone(self.service.session(self.issued(recovered)['credentials']['token']))
        self.assertNotIn('owner_id', recovered.body)
        self.assertEqual(self.post('/api/identities/recovery', headers={'authorization': 'Bearer ' + identity['recovery']}).status, 400)
        self.assertEqual(self.post('/api/identities/recovery', headers={'authorization': 'Bearer invalid-format'}, client='other-source').status, 401)

    def test_submission_requires_session_origin_and_csrf_and_keeps_ownership(self):
        identity = self.identity()
        raw = canonical({'request_key': 'submission-1', 'challenge': plan('prepare'),
                         'target_version_id': self.research.alias('version', 'ignore-retirement')})
        headers = self.write_headers(identity)
        first = self.post('/api/challenges', raw, headers, client='write-1')
        self.assertEqual((first.status, first.body['state']), (202, 'queued'))
        self.assertEqual(self.post('/api/challenges', raw, {**headers, 'origin': 'null'}, client='write-2').status, 401)
        self.assertEqual(self.post('/api/challenges', raw, {'cookie': 'evorph_session=' + identity['credentials']['token'],
                                                           'origin': ORIGIN}, client='write-3').status, 401)
        self.assertEqual(self.post('/api/challenges', raw, client='write-4').status, 401)
        other = self.identity()
        self.assertEqual(self.post('/api/challenges', raw, self.write_headers(other), client='write-5').status, 202)
        path = '/api/challenges/' + first.body['submission_id']
        reader = dispatch(self.service, Request('GET', path, {}, headers, client_key='read-1'), origin=ORIGIN)
        self.assertEqual((reader.status, reader.body['target_version_id']),
                         (200, self.research.alias('version', 'ignore-retirement')))
        self.assertEqual(dispatch(self.service, Request('GET', path, client_key='read-2'), origin=ORIGIN).status, 401)
        stranger = dispatch(self.service, Request('GET', path, {}, self.write_headers(other), client_key='read-3'), origin=ORIGIN)
        self.assertEqual(stranger.status, 400)
        self.assertEqual(dispatch(self.service, Request('POST', path, {}, headers, b'{}', 'write-6'), origin=ORIGIN).status, 405)

    def test_duplicate_submission_is_idempotent_and_owner_bound(self):
        identity = self.identity()
        raw = canonical({'request_key': 'submission-1', 'challenge': plan('prepare'),
                         'target_version_id': self.research.alias('version', 'ignore-retirement')})
        headers = self.write_headers(identity)
        first = self.post('/api/challenges', raw, headers)
        self.assertEqual(self.post('/api/challenges', raw, headers).body, first.body)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0], 1)
        conflicting = canonical({'request_key': 'submission-1', 'challenge': plan('confirm'),
                                 'target_version_id': self.research.alias('version', 'ignore-retirement')})
        self.assertEqual(self.post('/api/challenges', conflicting, headers, client='write-conflict').status, 409)

    def test_write_allowance_is_exhausted_then_resets_with_the_window(self):
        identity = self.identity()
        raw = canonical({'request_key': 'submission-1', 'challenge': plan('prepare'),
                         'target_version_id': self.research.alias('version', 'ignore-retirement')})
        headers = self.write_headers(identity)
        self.assertEqual([self.post('/api/challenges', raw, headers).status for _ in range(3)], [202, 202, 429])
        self.clock[0] += 61
        self.assertEqual(self.post('/api/challenges', raw, headers).status, 202)

    def test_client_key_trusts_only_the_loopback_proxy_and_its_rightmost_hop(self):
        self.assertEqual(resolve_client_key('127.0.0.1', {'x-forwarded-for': '203.0.113.7, 198.51.100.9'}), '198.51.100.9')
        self.assertEqual(resolve_client_key('::1', {'x-forwarded-for': '2001:db8::1, 203.0.113.7'}), '203.0.113.7')
        self.assertEqual(resolve_client_key('127.0.0.1', {'x-forwarded-for': 'not-an-address'}), '127.0.0.1')
        self.assertEqual(resolve_client_key('127.0.0.1', {}), '127.0.0.1')
        self.assertEqual(resolve_client_key('203.0.113.5', {'x-forwarded-for': '198.51.100.9'}), '203.0.113.5')

    def test_published_verification_links_a_replayable_evidence_pack(self):
        records = self.get('/api/research/experiments/' + self.experiment_id + '/records').body['records']
        verification = [record for record in records if record.get('speaker') == 'verifier' and record.get('evidence')]
        self.assertTrue(verification)
        href = verification[0]['evidence'][0]['href']
        self.assertTrue(href.startswith('/api/research/experiments/' + self.experiment_id + '/evidence/'))
        pack = self.get(href).body
        self.assertEqual(pack['schema_version'], 'evidence-pack.v1')
        self.assertEqual(pack['evidence_id'], verification[0]['evidence'][0]['evidence_id'])
        self.assertEqual(pack['trace']['candidate_digest'], digest(canonical(pack['candidate'])))
        self.assertEqual(pack['trace']['plan_digest'], digest(canonical(pack['challenge'])))
        self.assertIn('trace_digest', pack)
        self.assertNotIn('micros', canonical(pack).decode())
        self.assertEqual(self.get('/api/research/experiments/' + self.experiment_id + '/evidence/evidence-nope').status, 400)

    def test_transport_boundaries_return_fixed_reasons_without_private_text(self):
        self.assertEqual(self.post('/api/challenges', b'x' * (MAX_BODY_BYTES + 1)).status, 413)
        self.assertEqual(dispatch(self.service, Request('GET', '/api/private'), origin=ORIGIN).status, 404)
        self.assertEqual(dispatch(self.service, Request('DELETE', '/api/research/experiments'), origin=ORIGIN).status, 405)
        self.assertEqual(self.get('/api/research/experiments/' + self.experiment_id + '/records', cursor='%%%').status, 400)
        self.assertEqual(self.get('/api/research/experiments/' + self.experiment_id + '/records', limit='0').status, 400)
        self.assertEqual(self.get('/api/research/experiments/' + self.experiment_id + '/records', limit='101').status, 400)
        with patch.object(self.service, 'list_experiments', side_effect=RuntimeError('synthetic-private-detail')):
            failed = self.get('/api/research/experiments')
        self.assertEqual((failed.status, failed.body), (500, {'error': 'service_unavailable'}))

    def test_experiment_listing_pages_and_reports_recorded_state(self):
        page = self.get('/api/research/experiments', limit='1')
        self.assertEqual(page.status, 200)
        self.assertEqual(page.body['experiments'][0]['experiment_id'], self.experiment_id)
        self.assertEqual(page.body['experiments'][0]['state'], 'completed')
        self.assertEqual(page.body['mode'], 'example')
        self.service.refresh_experiment(self.batch_id)
        self.assertEqual(self.get('/api/research/experiments', limit='1').body['experiments'][0]['state'], 'completed')

    def test_migration_applies_to_a_restored_copy_without_touching_runtime_schema(self):
        snapshot, restored_root = self.root / 'backup', self.root / 'restored'
        create_backup(self.store, snapshot)
        restore_backup(snapshot, restored_root, self.limits.storage_bytes)
        restored = Store(restored_root, self.limits, clock=lambda: self.clock[0])
        try:
            self.assertEqual(restored.connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0], 'runtime.v1')
            self.assertEqual(restored.connection.execute("SELECT value FROM metadata WHERE key='service_schema'").fetchone()[0], 'service.v2')
            accounts = Accounting(restored)
            research = Research(restored, ModelChannel(accounts, self.seeker.spec, ScriptedTransport(), synthetic_meter),
                                ModelChannel(accounts, self.keeper.spec, ScriptedTransport(), synthetic_meter))
            service = PublicService(restored, research, self.service.limits, now=lambda: self.clock[0])
            self.assertEqual(service.approve(self.batch_id, 'synthetic-reviewer', self.reviewed()), [])
            self.assertEqual(service.publish(self.batch_id)['published'], 0)
            page = service.list_records(self.experiment_id, limit=100)
            self.assertEqual(len(page['records']), self.store.connection.execute('SELECT COUNT(*) FROM service_records').fetchone()[0])
            identity = service.create_identity('restored-client')
            self.assertIsNotNone(service.session(identity['credentials']['token']))
            self.assertTrue(restored.integrity()['artifacts'] > 0)
            with self.assertRaises(InputError):
                PublicService(restored, research, ServiceLimits(100, 1, 1, 1, 60, 101), now=lambda: self.clock[0])
        finally:
            restored.close()

    def test_service_tables_can_be_added_to_an_older_restored_state(self):
        older_root = self.root / 'older'
        older = Store(older_root, self.limits, clock=lambda: self.clock[0])
        try:
            older.submit(plan('prepare'), baseline(), 'older-owner', 'older-request')
            snapshot, restored_root = self.root / 'older-backup', self.root / 'older-restored'
            create_backup(older, snapshot)
        finally:
            older.close()
        restore_backup(snapshot, restored_root, self.limits.storage_bytes)
        restored = Store(restored_root, self.limits, clock=lambda: self.clock[0])
        try:
            self.assertIsNone(restored.connection.execute("SELECT value FROM metadata WHERE key='service_schema'").fetchone())
            accounts = Accounting(restored)
            research = Research(restored, ModelChannel(accounts, self.seeker.spec, ScriptedTransport(), synthetic_meter),
                                ModelChannel(accounts, self.keeper.spec, ScriptedTransport(), synthetic_meter))
            service = PublicService(restored, research, self.service.limits, now=lambda: self.clock[0])
            self.assertEqual(restored.connection.execute("SELECT value FROM metadata WHERE key='service_schema'").fetchone()[0], 'service.v2')
            self.assertEqual(restored.connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0], 'runtime.v1')
            self.assertEqual(service.list_experiments()['experiments'], [])
            self.assertEqual(restored.get_job(restored.connection.execute('SELECT job_id FROM jobs').fetchone()[0])['state'], 'queued')
        finally:
            restored.close()

    def test_review_cookie_header_origin_and_recovery_limits(self):
        issued = self.post('/api/identities')
        identity = self.issued(issued)
        token = identity['credentials']['token']
        self.assertNotIn(token, canonical(issued.body).decode())
        self.assertNotIn(token, repr(issued))
        value = dict(issued.headers)['Set-Cookie']
        for attribute in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/'):
            self.assertIn(attribute, value)
        self.assertEqual(self.post('/api/identities', headers={'origin': 'null'}).status, 401)
        raw = canonical({'request_key': 'new', 'target_version_id': self.research.alias('version', 'ignore-retirement'), 'challenge': plan('prepare')})
        headers = self.write_headers(identity)
        headers.pop('x-csrf-token')
        headers['cookie'] += '; evorph_csrf=' + identity['credentials']['csrf']
        self.assertEqual(self.post('/api/challenges', raw, headers).status, 401)
        attempts = [self.post('/api/identities/recovery', headers={'authorization': 'Bearer invalid'}, client='recovery-source').status for _ in range(3)]
        self.assertEqual(attempts, [401, 401, 429])
        self.clock[0] += 101
        self.assertIsNone(self.service.session(token))

    def test_review_cursor_binding_and_bounded_query(self):
        from service.public_store import cursor
        page = self.service.list_records(self.experiment_id, limit=1)
        token = page['next_cursor']
        self.assertIsNotNone(token)
        self.assertTrue(self.service.list_records(self.experiment_id, token, limit=1)['records'])
        with self.assertRaises(InputError):
            self.service.list_records(self.experiment_id, token[:-1] + ('a' if token[-1] != 'a' else 'b'))
        other = self.research.freeze('cursor-other', baseline('ignore-retirement'), search_baseline(), search_attempts=1,
                                     reduction_attempts=0, evaluation_cases=fixed_cases())
        self.service.publish(other)
        with self.assertRaises(InputError):
            self.service.list_records(self.research.alias('experiment', other), token)
        with self.assertRaises(InputError):
            self.service.list_records(self.experiment_id, token, after_sequence=0)
        with self.assertRaises(InputError):
            self.service.list_experiments(token)
        forged = cursor({'kind': 'records', 'experiment_id': self.experiment_id, 'sequence': 'wrong'}, self.service._cursor_key)
        with self.assertRaises(InputError):
            self.service.list_records(self.experiment_id, forged)
        queries = []
        self.store.connection.set_trace_callback(queries.append)
        try:
            listing = self.service.list_experiments(limit=1)
        finally:
            self.store.connection.set_trace_callback(None)
        self.assertTrue(any('LIMIT 2' in query and 'service_experiments' in query for query in queries))
        following = self.service.list_experiments(listing['next_cursor'], limit=1)
        self.assertNotEqual(listing['experiments'][0]['experiment_id'], following['experiments'][0]['experiment_id'])

    def test_review_explicit_release_and_late_approval_increment(self):
        with self.assertRaises(TypeError):
            self.service.approve(self.batch_id, 'reviewer')
        with self.assertRaises(InputError):
            self.service.approve(self.batch_id, 'reviewer', {})
        wrong = dict(self.reviewed())
        wrong[next(iter(wrong))] = '0' * 64
        with self.assertRaises(ConflictError):
            self.service.approve(self.batch_id, 'reviewer', wrong)
        prior_sequence = self.service.list_records(self.experiment_id, limit=100)['records'][-1]['sequence']
        record_id = self.research.record(self.batch_id, 'late-review', 'hypothesis',
                                         {'call_id': 'synthetic-call', 'statement': 'Synthetic late statement.', 'challenge': plan('prepare')})
        self.assertEqual(self.service.publish(self.batch_id)['published'], 0)
        later_id = self.research.record(self.batch_id, 'later-review', 'hypothesis',
                                        {'call_id': 'synthetic-call-later', 'statement': 'Synthetic later statement.', 'challenge': plan('prepare')})
        later = next(event for event in self.research.journal(self.batch_id) if event['record_id'] == later_id)
        self.service.approve(self.batch_id, 'reviewer', {later_id: review_event(later)})
        self.assertEqual(self.service.publish(self.batch_id)['published'], 1)
        prior_sequence = self.service.list_records(self.experiment_id, limit=100)['records'][-1]['sequence']
        event = next(event for event in self.research.journal(self.batch_id) if event['record_id'] == record_id)
        self.service.approve(self.batch_id, 'reviewer', {record_id: review_event(event)})
        self.assertEqual(self.service.publish(self.batch_id)['published'], 1)
        update = self.service.list_records(self.experiment_id, after_sequence=prior_sequence)
        self.assertEqual([item['public_text'] for item in update['records']], ['Synthetic late statement.'])

    def test_review_v1_migration_restores_foreign_key_without_losing_rows(self):
        import sqlite3
        before = self.service.list_records(self.experiment_id)
        self.store.connection.executescript('BEGIN; ALTER TABLE service_records RENAME TO prior_records; '
            'CREATE TABLE service_records(public_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,sequence INTEGER NOT NULL,body BLOB NOT NULL,published_at REAL NOT NULL,UNIQUE(batch_id,sequence)); '
            'INSERT INTO service_records SELECT * FROM prior_records; DROP TABLE prior_records; '
            "UPDATE metadata SET value='service.v1' WHERE key='service_schema'; COMMIT;")
        migrated = PublicService(self.store, self.research, self.service.limits, now=lambda: self.clock[0])
        self.assertEqual(migrated.list_records(self.experiment_id), before)
        self.assertTrue(self.store.connection.execute('PRAGMA foreign_key_list(service_records)').fetchall())
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction():
                self.store.connection.execute('INSERT INTO service_records VALUES(?,?,?,?,?)', ('orphan', 'missing', 1, b'{}', 0))
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_review_simultaneous_recovery_has_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        identity = self.service.create_identity('initial-owner')
        barrier = Barrier(2)
        def recover(index):
            store = Store(self.store.root, self.limits, clock=lambda: self.clock[0])
            try:
                accounts = Accounting(store)
                research = Research(store, ModelChannel(accounts, self.seeker.spec, ScriptedTransport(), synthetic_meter),
                                    ModelChannel(accounts, self.keeper.spec, ScriptedTransport(), synthetic_meter))
                service = PublicService(store, research, self.service.limits, now=lambda: self.clock[0])
                barrier.wait(timeout=5)
                try:
                    service.recover_identity(identity['recovery'], 'recovery-' + str(index))
                    return 'recovered'
                except InputError:
                    return 'unavailable'
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(recover, index) for index in range(2)]
            self.assertEqual(sorted(future.result(timeout=10) for future in futures), ['recovered', 'unavailable'])
        self.assertIsNone(self.service.session(identity['credentials']['token']))
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM service_sessions WHERE revoked=0').fetchone()[0], 1)

    def test_review_real_http_framing_cookies_and_deadline(self):
        from http.server import HTTPServer
        import json
        from queue import Queue
        import socket
        from threading import Thread
        ready = Queue()
        def serve():
            store = Store(self.store.root, self.limits, clock=lambda: self.clock[0])
            server = None
            try:
                accounts = Accounting(store)
                research = Research(store, ModelChannel(accounts, self.seeker.spec, ScriptedTransport(), synthetic_meter),
                                    ModelChannel(accounts, self.keeper.spec, ScriptedTransport(), synthetic_meter))
                service = PublicService(store, research, self.service.limits, now=lambda: self.clock[0])
                server = HTTPServer(('127.0.0.1', 0), handler(service, origin=ORIGIN, request_timeout=1))
                ready.put(server)
                server.serve_forever(poll_interval=0.02)
            except BaseException as exc:
                ready.put(exc)
            finally:
                if server:
                    server.server_close()
                store.close()
        thread = Thread(target=serve, daemon=True)
        thread.start()
        server = ready.get(timeout=5)
        if isinstance(server, BaseException):
            raise server
        def request(raw):
            with socket.create_connection(server.server_address, timeout=3) as connection:
                connection.sendall(raw)
                chunks = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b''.join(chunks)
        try:
            valid = request(b'POST /api/identities HTTP/1.1\r\nHost: localhost\r\nOrigin: https://research.invalid\r\nContent-Type: application/json\r\nContent-Length: 0\r\n\r\n')
            headers, raw = valid.split(b'\r\n\r\n', 1)
            self.assertIn(b'201', headers.split(b'\r\n')[0])
            self.assertIn(b'HttpOnly; SameSite=Strict', headers)
            self.assertNotIn('credentials', json.loads(raw))
            self.assertNotIn('token', json.loads(raw))
            for framing in (b'Content-Length: -1', b'Content-Length: invalid',
                            b'Content-Length: 0\r\nContent-Length: 0', b'Transfer-Encoding: chunked'):
                response = request(b'POST /api/identities HTTP/1.1\r\nHost: localhost\r\n' + framing + b'\r\n\r\n')
                self.assertIn(b'400', response.split(b'\r\n')[0])
            oversized = request(b'POST /api/identities HTTP/1.1\r\nHost: localhost\r\nContent-Length: 65537\r\n\r\n')
            self.assertIn(b'413', oversized.split(b'\r\n')[0])
            self.assertEqual(request(b'POST /api/identities HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\nx'), b'')
            read = request(b'GET /api/research/experiments HTTP/1.1\r\nHost: localhost\r\n\r\n')
            self.assertIn(b'200', read.split(b'\r\n')[0])
        finally:
            server.shutdown()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_review_submission_query_tracks_actual_batch_state(self):
        identity = self.identity()
        headers = self.write_headers(identity)
        raw = canonical({'request_key': 'status-request', 'target_version_id': self.research.alias('version', 'ignore-retirement'),
                         'challenge': plan('prepare')})
        submitted = self.post('/api/challenges', raw, headers).body['submission_id']
        def status():
            return dispatch(self.service, Request('GET', '/api/challenges/' + submitted, {}, headers, client_key='status-reader'), origin=ORIGIN).body['state']
        self.assertEqual(status(), 'queued')
        internal = self.store.connection.execute("SELECT internal_id FROM research_aliases WHERE kind='submission' AND public_id=?", (submitted,)).fetchone()[0]
        self.seeker.transport.script.append(reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare', 'confirm', 'legacy-transition')), usage(10, 10))))
        repaired = {**baseline(), 'version_id': 'synthetic-repair', 'parent_id': 'ignore-retirement'}
        self.keeper.transport.script.append(reply(response(PROVIDER, MODEL, keeper_payload(repaired), usage(10, 10))))
        batch = self.research.freeze('status-batch', baseline('ignore-retirement'), search_baseline(), search_attempts=1,
                                     reduction_attempts=0, evaluation_cases=fixed_cases(), submission_ids=[internal])
        self.assertEqual(self.research.run(batch)['state'], 'completed')
        self.assertEqual(status(), 'completed')


if __name__ == '__main__':
    unittest.main()
