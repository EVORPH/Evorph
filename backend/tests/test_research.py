from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agents.channel import ChannelSpec, ModelChannel
from agents.fixtures import ScriptedTransport, keeper_payload, reply, response, seeker_payload, synthetic_meter, timed_out, usage
from agents.research import Research, finding_key, fixed_cases
from agents.strategy import baseline as search_baseline, generate, reductions, validate as validate_strategy
from evorph.candidates import baseline
from evorph.contracts import InputError, canonical, parse_json, validate_plan
from evorph.evaluation import plan
from runtime.accounting import Accounting
from runtime.backup import create_backup, restore_backup
from runtime.store import ConflictError, Limits, Store
from service.access import AccessError, authenticate, credential_digest, issue_session, read_submission, submit_challenge
from service.publication import approval_digest, experiment, project_records

PROVIDER = 'synthetic-provider'
MODEL = 'synthetic-research-model'
PRICE = {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000}
ORIGIN = 'https://research.invalid'


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-research-test-')
        self.limits = Limits(queue_total=20, queue_per_submitter=10, max_attempts=2, lease_seconds=180,
                             storage_bytes=128 * 1024 * 1024, min_free_bytes=0, artifact_bytes=16 * 1024 * 1024)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state', self.limits, clock=lambda: 1800000000)
        self.accounts = Accounting(self.store)
        self.accounts.configure(1000000, 5000000)
        self.spec = ChannelSpec(PROVIDER, MODEL, PRICE, 100000, 10000, 100, 30, 'synthetic-byte-meter.v1')
        self.seeker_transport, self.keeper_transport = ScriptedTransport(), ScriptedTransport()
        self.seeker = ModelChannel(self.accounts, self.spec, self.seeker_transport, synthetic_meter)
        self.keeper = ModelChannel(self.accounts, self.spec, self.keeper_transport, synthetic_meter)
        self.research = Research(self.store, self.seeker, self.keeper)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def script(self, target=None, challenge=None, repaired=None, method=None):
        target = target or baseline('ignore-retirement')
        payload = seeker_payload(challenge or plan('prepare', 'confirm', 'legacy-transition'), 'Synthetic candidate explanation.')
        if method is not None:
            payload['strategy'] = method
        child = deepcopy(repaired or baseline())
        child.update(version_id='synthetic-repair', parent_id=target['version_id'])
        self.seeker_transport.script.append(reply(response(PROVIDER, MODEL, payload, usage(100, 100))))
        self.keeper_transport.script.append(reply(response(PROVIDER, MODEL, keeper_payload(child), usage(100, 100))))
        return child

    def freeze(self, key='batch-1', target=None, method=None, submissions=(), reduction_attempts=0, cases=None):
        return self.research.freeze(key, target or baseline('ignore-retirement'), method or search_baseline(),
                                    search_attempts=1, reduction_attempts=reduction_attempts,
                                    evaluation_cases=cases or fixed_cases(), submission_ids=submissions)

    def approvals(self, batch_id):
        return {approval_digest(event) for event in self.research.journal(batch_id)}

    def test_actual_fixed_program_repair_path(self):
        child = self.script()
        batch_id = self.freeze()
        result = self.research.run(batch_id)
        self.assertEqual((result['state'], result['reason']), ('completed', 'retained'))
        self.assertEqual(result['candidate_id'], child['version_id'])
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 1))
        events = self.research.journal(batch_id)
        self.assertIn('finding', [event['kind'] for event in events])
        self.assertTrue(any(event['kind'] == 'verification' and not event['body']['matches_reference'] for event in events))
        comparison = next(event['body'] for event in events if event['kind'] == 'comparison')
        self.assertTrue(comparison['matched'])
        self.assertGreater(comparison['candidate'], comparison['baseline'])
        frozen = next(event for event in events if event['kind'] == 'evaluation_set')
        repair = next(event for event in events if event['kind'] == 'repair')
        self.assertLess(frozen['sequence'], repair['sequence'])
        self.assertTrue(self.store.integrity()['artifacts'] > 0)

    def test_no_finding_does_not_call_keeper(self):
        target = baseline()
        self.script(target=target)
        batch_id = self.freeze(target=target)
        result = self.research.run(batch_id)
        self.assertEqual((result['state'], result['reason']), ('completed', 'no_finding'))
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 0))
        self.assertFalse(any(event['kind'] == 'repair' for event in self.research.journal(batch_id)))

    def test_reject_all_policy_cannot_pass_as_a_repair(self):
        self.script(repaired=baseline('reject-all'))
        batch_id = self.freeze()
        result = self.research.run(batch_id)
        self.assertEqual(result['reason'], 'rejected')
        events = self.research.journal(batch_id)
        self.assertEqual(events[-1]['body']['outcome'], 'rejected')
        self.assertTrue(any(event['kind'] == 'verification' and event['body']['policy_id'] == 'synthetic-repair'
                            and not event['body']['matches_reference'] for event in events))

    def test_completed_batch_reentry_has_no_new_calls_jobs_or_records(self):
        self.script()
        batch_id = self.freeze()
        first = self.research.run(batch_id)
        counts = [self.store.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('model_calls', 'jobs', 'research_journal')]
        self.assertEqual(self.research.run(batch_id), first)
        self.assertEqual(counts, [self.store.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                                  for table in ('model_calls', 'jobs', 'research_journal')])
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 1))

    def test_pending_usage_pauses_batch_without_new_request_keys(self):
        self.seeker_transport.script.append(timed_out())
        batch_id = self.freeze()
        first = self.research.run(batch_id)
        self.assertEqual((first['state'], first['reason']), ('paused', 'seeker_timeout'))
        self.assertEqual(self.research.run(batch_id), first)
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 0))
        self.assertEqual(self.store.connection.execute('SELECT state FROM model_calls').fetchone()[0], 'usage_unknown')
        self.assertGreater(self.accounts.totals()['reserved_micros'], 0)

    def test_batch_and_runtime_conditions_are_immutable(self):
        batch_id = self.freeze()
        self.assertEqual(self.freeze(), batch_id)
        with self.assertRaises(ConflictError):
            self.freeze(target=baseline())
        with self.assertRaises(InputError):
            self.research.freeze('other', baseline(), search_baseline(), search_attempts=1,
                                 reduction_attempts=0, evaluation_cases=[plan('prepare'), plan('confirm')])
        self.seeker.spec.price['output_micros_per_million'] += 1
        with self.assertRaises(ConflictError):
            self.research.run(batch_id)
        self.assertEqual(self.seeker_transport.sends, 0)

    def test_existing_unrelated_queue_entry_is_not_executed(self):
        unrelated = self.store.submit(plan('prepare'), baseline(), 'other-owner', 'other-request')
        self.script()
        self.research.run(self.freeze())
        self.assertEqual(self.store.get_job(unrelated)['state'], 'queued')
        self.assertEqual(self.store.get_job(unrelated)['attempts'], 0)

    def test_missing_crypto_stops_without_manufacturing_a_finding(self):
        self.script()
        batch_id = self.freeze()
        with patch('runtime.worker.run', side_effect=InputError('synthetic-private-environment')):
            result = self.research.run(batch_id)
        self.assertEqual((result['state'], result['reason']), ('failed', 'execution_error'))
        self.assertEqual(self.keeper_transport.sends, 0)
        self.assertFalse(any(event['kind'] == 'finding' for event in self.research.journal(batch_id)))
        self.assertNotIn('synthetic-private-environment', str(result))

    def test_crash_after_model_response_recovers_without_resending(self):
        self.script()
        batch_id = self.freeze()
        original = self.seeker.request
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit(9)
        with patch.object(self.seeker, 'request', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.research.run(batch_id)
        self.assertEqual(self.seeker_transport.sends, 1)
        self.assertEqual(self.research.run(batch_id)['reason'], 'retained')
        self.assertEqual(self.seeker_transport.sends, 1)
        self.assertEqual(self.keeper_transport.sends, 1)

    def test_crash_after_discovery_keeps_history_and_original_newness(self):
        self.script()
        batch_id = self.freeze()
        original = self.research._call
        def interrupted(batch, role, snapshot, observations):
            if role == 'keeper':
                raise SystemExit(9)
            return original(batch, role, snapshot, observations)
        with patch.object(self.research, '_call', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.research.run(batch_id)
        before = self.research.journal(batch_id)
        self.assertEqual(self.research.run(batch_id)['reason'], 'retained')
        after = self.research.journal(batch_id)
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(self.seeker_transport.sends, 1)

    def test_search_strategy_inherits_only_after_equal_attempt_comparison(self):
        method = search_baseline()
        method.update(version_id='synthetic-method-2', parent_id=method['version_id'])
        method['message_order'].remove('legacy-transition')
        method['message_order'].insert(0, 'legacy-transition')
        clock = {**plan('prepare'), 'events': [{'kind': 'advance', 'ticks': 1}]}
        self.script(challenge=clock, method=method)
        batch_id = self.freeze()
        result = self.research.run(batch_id)
        self.assertEqual((result['reason'], result['method_decision']), ('retained', 'retained'))
        comparison = next(event['body'] for event in self.research.journal(batch_id)
                          if event['kind'] == 'comparison' and event['body']['metric'] == 'verified_traces')
        self.assertEqual((comparison['baseline'], comparison['candidate']), (0, 1))
        self.assertTrue(comparison['matched'])
        self.assertEqual(self.store.connection.execute('SELECT state FROM research_methods WHERE version_id=?',
                                                       (method['version_id'],)).fetchone()[0], 'retained')
        self.script(challenge=clock)
        next_batch = self.freeze('batch-2', method=method)
        self.assertEqual(self.research.run(next_batch)['reason'], 'retained')
        self.assertEqual(self.research.batch(next_batch)[1]['method'], method)

    def test_rejected_search_method_cannot_become_an_inherited_method(self):
        method = {**search_baseline(), 'version_id': 'synthetic-method-2', 'parent_id': 'search-baseline-v1'}
        self.script(method=method)
        batch_id = self.freeze()
        self.assertEqual(self.research.run(batch_id)['method_decision'], 'rejected')
        with self.assertRaises(ConflictError):
            self.freeze('batch-2', method=method)

    def test_strategy_language_cannot_change_capabilities_or_budgets(self):
        for key, value in [('message_order', ['private-signing']), ('mutation_order', ['run-code']),
                           ('budget', 1000000), ('reduction_order', 'execute'), ('spec_digest', '0' * 64)]:
            method = {**search_baseline(), key: value}
            with self.assertRaises(InputError):
                validate_strategy(method)
        with self.assertRaises(InputError):
            generate(search_baseline(), 51)
        self.assertEqual(len(generate(search_baseline(), 50)), 50)
        for candidate in generate(search_baseline(), 50):
            validate_plan(candidate)

    def test_reductions_preserve_valid_duplicate_references(self):
        challenge = plan('prepare', 'confirm', 'legacy-transition')
        challenge['events'].append({'kind': 'duplicate', 'event_index': 2})
        for reduced in reductions(challenge, 'reverse'):
            validate_plan(reduced)
            if reduced['events'][-1]['kind'] == 'duplicate':
                ref = reduced['events'][-1]['event_index']
                self.assertEqual(reduced['events'][ref]['message_ref'], 'legacy-transition')

    def test_model_context_excludes_ledger_and_evaluation_case_contents(self):
        hidden_case = {**plan('prepare'), 'events': [{'kind': 'advance', 'ticks': 997}, *plan('prepare')['events']]}
        self.script()
        batch_id = self.freeze(cases=[*fixed_cases(), hidden_case])
        self.research.run(batch_id)
        for transport in (self.seeker_transport, self.keeper_transport):
            raw = transport.sent[0]['raw']
            for segment in parse_json(raw)['segments']:
                self.assertNotIn('"ticks":997', segment['text'])
            self.assertNotIn(b'daily_micros', raw)
            self.assertNotIn(b'price', raw)
            self.assertNotIn(b'resource_policy', raw)
            self.assertNotIn(b'environment', raw)
            self.assertNotIn(b'token_digest', raw)
        keeper_input = parse_json(self.keeper_transport.sent[0]['raw'])
        public = parse_json(next(item['text'] for item in keeper_input['segments'] if item['name'] == 'public_evidence'))
        self.assertIsNotNone(public['observations'][0]['divergence'])

    def test_public_release_requires_exact_event_approval_and_is_read_only(self):
        self.script()
        batch_id = self.freeze()
        self.research.run(batch_id)
        counts = self.store.connection.total_changes
        self.assertEqual(project_records(self.research, batch_id, set()), [])
        approvals = self.approvals(batch_id)
        records = project_records(self.research, batch_id, approvals)
        self.assertTrue(records)
        self.assertEqual(self.store.connection.total_changes, counts)
        payload = canonical(records)
        for field in (b'billed_micros', b'input_tokens', b'synthetic-provider', b'synthetic-research-model',
                      b'body_b64', b'ignore-retirement', b'synthetic-repair', b'environment'):
            self.assertNotIn(field, payload)
        self.assertTrue(all(record['source']['origin'] == 'example' for record in records))
        hypothesis = next(event for event in self.research.journal(batch_id) if event['kind'] == 'hypothesis')
        altered = deepcopy(hypothesis)
        altered['body']['statement'] = 'synthetic-private-changed-text'
        from service.publication import record
        self.assertIsNone(record(self.research, batch_id, altered, approvals))
        self.assertFalse(any(record['source']['origin'] == 'program' for record in records))

    def test_player_authentication_ownership_and_source_survive_reduction(self):
        self.freeze('register-target')
        credentials, session = issue_session('synthetic-player-1', now=100, lifetime_seconds=100)
        challenge = plan('prepare', 'confirm', 'legacy-transition')
        challenge['events'].insert(0, {'kind': 'advance', 'ticks': 1})
        raw = canonical({'request_key': 'submission-1', 'target_version_id': self.research.alias('version', 'ignore-retirement'), 'challenge': challenge})
        submit_args = dict(token=credentials['token'], session=session, now=101, origin=ORIGIN,
                           expected_origin=ORIGIN, csrf=credentials['csrf'])
        submission = submit_challenge(self.research, raw, **submit_args)
        self.assertEqual(submit_challenge(self.research, raw, **submit_args), submission)
        internal_submission = self.store.connection.execute("SELECT internal_id FROM research_aliases WHERE kind='submission' AND public_id=?", (submission,)).fetchone()[0]
        other_credentials, other = issue_session('synthetic-player-2', now=100, lifetime_seconds=100)
        with self.assertRaises(InputError):
            read_submission(self.research, submission, token=other_credentials['token'], session=other, now=101)
        self.script()
        batch_id = self.freeze('player-batch', submissions=[internal_submission], reduction_attempts=2)
        self.assertEqual(self.research.run(batch_id)['reason'], 'retained')
        records = project_records(self.research, batch_id, self.approvals(batch_id))
        contributions = [contribution for record in records for contribution in record['contributions']]
        self.assertEqual({c['contributor_id'] for c in contributions}, {self.research.alias('contributor', 'synthetic-player-1')})
        self.assertEqual({c['kind'] for c in contributions}, {'submission', 'discovery', 'reduction'})
        self.assertEqual({c['submission_id'] for c in contributions}, {submission})
        self.assertNotIn(credentials['token'], canonical(records).decode())
        self.assertNotIn('synthetic-player-1', canonical(records).decode())
        issues = self.store.connection.execute('SELECT original_submission_id FROM research_issues').fetchall()
        self.assertEqual([row[0] for row in issues].count(internal_submission), 1)
        self.assertTrue(all(row[0] in (internal_submission, None) for row in issues))

    def test_session_proof_csrf_expiry_and_payload_owner_are_enforced(self):
        self.freeze('register-target')
        credentials, session = issue_session('synthetic-owner', now=100, lifetime_seconds=10)
        raw = canonical({'request_key': 'submission-1', 'target_version_id': self.research.alias('version', 'ignore-retirement'), 'challenge': plan('prepare')})
        args = dict(token=credentials['token'], session=session, now=101, origin=ORIGIN,
                    expected_origin=ORIGIN, csrf=credentials['csrf'])
        for update in ({'token': 'synthetic-owner'}, {'csrf': credentials['token']}, {'origin': 'null'},
                       {'origin': 'https://other.invalid'}, {'now': 110}, {'session': {**session, 'revoked': True}}):
            with self.assertRaises(AccessError):
                submit_challenge(self.research, raw, **{**args, **update})
        with self.assertRaises(InputError):
            submit_challenge(self.research, canonical({**parse_json(raw), 'owner_id': 'another-owner'}), **args)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0], 0)
        self.assertNotEqual(credential_digest(credentials['token']), credentials['token'])
        self.assertEqual(authenticate(credentials['token'], session, 101), 'synthetic-owner')

    def test_exact_issue_dedup_does_not_merge_other_target_or_trace(self):
        challenge = plan('prepare', 'confirm', 'legacy-transition')
        self.assertEqual(finding_key(challenge, baseline()), finding_key(deepcopy(challenge), baseline()))
        self.assertNotEqual(finding_key(challenge, baseline()), finding_key(challenge, baseline('ignore-retirement')))
        self.assertNotEqual(finding_key(challenge, baseline()), finding_key(plan('legacy-transition'), baseline()))

    def test_backup_restores_journal_methods_and_results_without_calls(self):
        self.script()
        batch_id = self.freeze()
        original = self.research.run(batch_id)
        snapshot, restored_root = self.root / 'backup', self.root / 'restored'
        create_backup(self.store, snapshot)
        restore_backup(snapshot, restored_root, self.limits.storage_bytes)
        restored = Store(restored_root, self.limits, clock=lambda: 1800000000)
        try:
            accounts = Accounting(restored)
            seeker = ModelChannel(accounts, self.spec, ScriptedTransport(), synthetic_meter)
            keeper = ModelChannel(accounts, self.spec, ScriptedTransport(), synthetic_meter)
            research = Research(restored, seeker, keeper)
            self.assertEqual(research.run(batch_id), original)
            self.assertEqual(research.journal(batch_id), self.research.journal(batch_id))
            self.assertEqual((seeker.transport.sends, keeper.transport.sends), (0, 0))
            self.assertEqual(experiment(research, batch_id), experiment(self.research, batch_id))
        finally:
            restored.close()

    def test_authoritative_claim_rejects_modified_input_and_attempt_path(self):
        from runtime.worker import execute_claimed
        job_id = self.store.submit(plan('prepare'), baseline(), 'owner', 'request')
        with self.store.worker_lock():
            job = self.store.claim(job_id)
            changed = deepcopy(job)
            changed['input']['plan'] = plan('confirm')
            with self.assertRaises(ConflictError):
                execute_claimed(self.store, changed)
            changed = {**job, 'run_id': '../synthetic-path'}
            with self.assertRaises(ConflictError):
                execute_claimed(self.store, changed)
        self.assertEqual(list((self.store.root / 'scratch').iterdir()), [])

    def test_saved_unsettled_response_can_be_reconciled_offline_without_resend(self):
        self.script()
        batch_id = self.freeze()
        with patch.object(self.accounts, 'settle', side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                self.research.run(batch_id)
        self.assertEqual(self.store.connection.execute('SELECT state FROM model_calls').fetchone()[0], 'response_saved')
        self.assertEqual(self.research.run(batch_id)['reason'], 'retained')
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 1))
        self.assertEqual(self.accounts.totals()['settled_micros'], 400)

    def test_interruption_event_and_result_survive_or_rollback_together(self):
        batch = self.freeze()
        child = '''
import json, os, sys
from agents.channel import ChannelSpec, ModelChannel
from agents.fixtures import ScriptedTransport, synthetic_meter
from agents.research import Research
from runtime.store import Store, Limits
from runtime.accounting import Accounting
args = json.load(sys.stdin)
store = Store(args['root'], Limits(**args['limits']))
accounts = Accounting(store)
spec = ChannelSpec(**args['spec'])
research = Research(store, ModelChannel(accounts, spec, ScriptedTransport(), synthetic_meter),
                    ModelChannel(accounts, spec, ScriptedTransport(), synthetic_meter))
record = research._record
def interrupted(*args):
    record(*args)
    os._exit(23)
research._record = interrupted
research.finish(args['batch'], 'paused', 'capacity_unavailable')
'''
        result = subprocess.run([sys.executable, '-c', child], input=json.dumps(
            {'root': str(self.store.root), 'limits': asdict(self.limits), 'spec': asdict(self.spec), 'batch': batch}),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIsNone(self.research.batch(batch)[0]['result'])
        self.assertEqual(self.research.journal(batch), [])
        finished = self.research.finish(batch, 'paused', 'capacity_unavailable')
        self.assertEqual(self.research.run(batch), finished)
        before = self.research.journal(batch)
        with self.assertRaises(ConflictError):
            self.research.finish(batch, 'failed', 'another_reason')
        self.assertEqual(self.research.journal(batch), before)
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (0, 0))

    def test_legacy_interruption_is_finalized_before_any_more_model_calls(self):
        self.script()
        batch = self.freeze()
        _, snapshot = self.research.batch(batch)
        self.assertEqual(self.research._call(batch, 'seeker', snapshot, [])['kind'], 'accepted')
        self.research.record(batch, 'stopped', 'experiment', {'state': 'paused', 'reason': 'capacity_unavailable'})
        before = self.research.journal(batch)
        result = self.research.run(batch)
        self.assertEqual((result['state'], result['reason']), ('paused', 'capacity_unavailable'))
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 0))
        self.assertEqual(self.research.journal(batch), before)
        self.assertEqual(self.research.run(batch), result)

    def test_crash_after_committed_interruption_does_not_continue_the_round(self):
        self.script()
        batch = self.freeze()
        original = self.research.finish
        def interrupted(*args):
            original(*args)
            raise SystemExit(9)
        with patch.object(self.research, '_test', side_effect=InputError('Synthetic local error.')), \
             patch.object(self.research, 'finish', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.research.run(batch)
        self.assertEqual(self.research.run(batch)['state'], 'failed')
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 0))

    def test_stale_unconfirmed_cache_uses_settled_evidence_without_resending(self):
        self.script()
        batch_id = self.freeze()
        finished = self.research.run(batch_id)
        _, snapshot = self.research.batch(batch_id)
        row = self.store.connection.execute(
            "SELECT outcome FROM research_calls WHERE batch_id=? AND role='seeker'", (batch_id,)).fetchone()
        cached = parse_json(row['outcome'])
        cached.update(kind='usage_unknown', state='usage_unknown', payload=None)
        self.store.connection.execute(
            "UPDATE research_calls SET outcome=? WHERE batch_id=? AND role='seeker'",
            (canonical(cached), batch_id))
        recovered = self.research._call(batch_id, 'seeker', snapshot, [])
        self.assertEqual((recovered['kind'], recovered['state']), ('accepted', 'settled'))
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 1))
        self.assertEqual(self.research.run(batch_id), finished)
        self.assertEqual((self.seeker_transport.sends, self.keeper_transport.sends), (1, 1))

    def test_mid_batch_configuration_change_cannot_produce_a_matched_decision(self):
        self.script()
        batch_id = self.freeze()
        original = self.seeker_transport.send
        def change(raw, timeout):
            result = original(raw, timeout)
            self.seeker.spec.price['input_micros_per_million'] += 1
            return result
        with patch.object(self.seeker_transport, 'send', side_effect=change):
            with self.assertRaises(ConflictError):
                self.research.run(batch_id)
        self.assertFalse(any(event['kind'] == 'comparison' for event in self.research.journal(batch_id)))
        self.assertEqual(self.keeper_transport.sends, 0)

    def test_wrong_strategy_parent_is_not_adopted(self):
        method = {**search_baseline(), 'version_id': 'synthetic-method-2', 'parent_id': 'other-method'}
        self.script(method=method)
        batch_id = self.freeze()
        self.assertEqual(self.research.run(batch_id)['reason'], 'strategy_parent_mismatch')
        self.assertEqual(self.keeper_transport.sends, 0)
        self.assertIsNone(self.store.connection.execute('SELECT 1 FROM research_methods WHERE version_id=?', (method['version_id'],)).fetchone())

    def test_repeated_evidence_preserves_first_discovery_owner(self):
        self.freeze('register-target')
        challenge = plan('prepare', 'confirm', 'legacy-transition')
        first = self.research.register_submission('first-owner', 'first-request', challenge, 'ignore-retirement')
        second = self.research.register_submission('second-owner', 'second-request', challenge, 'ignore-retirement')
        self.script()
        self.research.run(self.freeze('first-batch', submissions=[first]))
        self.script()
        next_batch = self.freeze('second-batch', submissions=[second])
        self.research.run(next_batch)
        self.assertEqual(self.store.connection.execute('SELECT original_submission_id FROM research_issues').fetchone()[0], first)
        findings = [event for event in self.research.journal(next_batch) if event['kind'] == 'finding']
        self.assertFalse(findings[0]['body']['new_in_archive'])
        self.assertIsNone(findings[0]['body']['contribution_kind'])

    def test_public_identifiers_are_required_at_submission_boundary(self):
        self.freeze('register-target')
        credentials, session = issue_session('owner', now=100, lifetime_seconds=100)
        args = dict(token=credentials['token'], session=session, now=101, origin=ORIGIN,
                    expected_origin=ORIGIN, csrf=credentials['csrf'])
        raw = {'request_key': 'request', 'target_version_id': 'ignore-retirement', 'challenge': plan('prepare')}
        with self.assertRaises(InputError):
            submit_challenge(self.research, canonical(raw), **args)
        raw['target_version_id'] = self.research.alias('version', 'ignore-retirement')
        submission = submit_challenge(self.research, canonical(raw), **args)
        received = read_submission(self.research, submission, token=credentials['token'], session=session, now=101)
        self.assertEqual(received['target_version_id'], raw['target_version_id'])
        self.assertNotIn('ignore-retirement', canonical(received).decode())

    def test_projection_satisfies_existing_frontend_contract_including_unicode(self):
        import json
        import subprocess
        target = baseline()
        text = '\U0001d400' * 1500
        self.seeker_transport.script.append(reply(response(PROVIDER, MODEL, seeker_payload(plan('prepare'), text), usage(100, 100))))
        batch_id = self.freeze(target=target)
        self.research.run(batch_id)
        records = project_records(self.research, batch_id, self.approvals(batch_id))
        utterance = next(record for record in records if record['speaker'] == 'seeker')
        self.assertEqual(utterance['public_text'], text)
        self.assertLessEqual(len(utterance['summary'].encode('utf-16-le')), 4000)
        page = {'schema_version': 'evorph.public-research.v2', 'mode': 'example',
                'experiment': experiment(self.research, batch_id), 'records': records, 'next_cursor': None}
        self.freeze('register-comparison-target')
        submitted = self.research.register_submission('synthetic-owner', 'source-1', plan('prepare', 'confirm', 'legacy-transition'), 'ignore-retirement')
        method = {**search_baseline(), 'version_id': 'synthetic-projection-method', 'parent_id': 'search-baseline-v1'}
        method['message_order'] = ['legacy-transition', 'prepare', 'confirm', 'pq-transition', 'invalid-pq-fallback']
        self.script(method=method)
        repaired_batch = self.freeze('projection-repair', submissions=[submitted])
        self.research.run(repaired_batch)
        repaired_records = project_records(self.research, repaired_batch, self.approvals(repaired_batch))
        self.assertTrue({'contribution', 'hypothesis', 'repair', 'finding', 'comparison', 'verification', 'decision'} <= {record['type'] for record in repaired_records})
        repaired_page = {**page, 'experiment': experiment(self.research, repaired_batch), 'records': repaired_records}
        self.seeker_transport.script.append(timed_out())
        paused_batch = self.freeze('projection-paused')
        self.research.run(paused_batch)
        paused_page = {**page, 'experiment': experiment(self.research, paused_batch),
                       'records': project_records(self.research, paused_batch, self.approvals(paused_batch))}
        self.assertEqual(paused_page['experiment']['state'], 'paused')
        contract = Path(__file__).resolve().parents[2] / 'frontend/dist/research-contract.js'
        program = "const m=await import(process.argv[1]);let raw='';for await(const c of process.stdin)raw+=c;for(const p of JSON.parse(raw)){m.validateRecordPage(p,p.experiment.experiment_id,'example');let rejected=false;try{m.validateRecordPage({...p,mode:'live'},p.experiment.experiment_id,'live')}catch{rejected=true}if(!rejected)throw Error('Example source accepted as live')}console.log('validated');"
        result = subprocess.run(['node', '--input-type=module', '-e', program, contract.as_uri()],
                                input=json.dumps([page, repaired_page, paused_page]), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'validated')

    def test_each_verified_submission_keeps_its_own_discovery_record(self):
        self.freeze('register-target')
        first_plan = plan('prepare', 'confirm', 'legacy-transition')
        second_plan = deepcopy(first_plan)
        second_plan['events'].insert(0, {'kind': 'advance', 'ticks': 1})
        first = self.research.register_submission('owner-one', 'submission', first_plan, 'ignore-retirement')
        second = self.research.register_submission('owner-two', 'submission', second_plan, 'ignore-retirement')
        self.script()
        batch_id = self.freeze('multi-source', submissions=[first, second])
        self.assertEqual(self.research.run(batch_id)['reason'], 'retained')
        credited = [event['body']['submission_id'] for event in self.research.journal(batch_id)
                    if event['kind'] == 'finding' and event['body']['contribution_kind'] == 'discovery']
        self.assertEqual(credited, [first, second])
        owner_pairs = {(row['original_submission_id'], row['owner_id']) for row in self.store.connection.execute(
            'SELECT i.original_submission_id,s.owner_id FROM research_issues i JOIN research_submissions s ON s.submission_id=i.original_submission_id')}
        self.assertEqual(owner_pairs, {(first, 'owner-one'), (second, 'owner-two')})


if __name__ == '__main__':
    unittest.main()
