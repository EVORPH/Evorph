import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from evorph import candidates, evidence, reference
from evorph.contracts import (SPEC, SPEC_DIGEST, ExecutionError, InputError, b64,
                              canonical, digest, initial_state, message, parse_json,
                              unb64, validate_plan)
from evorph.crypto import run_process
from evorph.evaluation import calibrate, plan
from evorph.fixtures import build
from evorph.legacy import replay_probe
from evorph.runner import run
from evorph.vectors import verify

ROOT = Path(__file__).resolve().parents[2]


class KernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle, cls.environment = build(diagnostics=True)
        cls.policy = candidates.baseline()

    def execute(self, *names, policy=None):
        return run(plan(*names), policy or self.policy, self.bundle, diagnostics=True)

    def test_normal_cutover_preserves_new_key_and_retires_old(self):
        trace = self.execute('legacy-transition', 'prepare', 'confirm', 'legacy-transition', 'pq-transition')
        self.assertTrue(trace['matches_reference'])
        self.assertEqual([e['reference']['accepted'] for e in trace['events']], [True, True, True, False, True])
        late = trace['events'][3]
        self.assertEqual(late['signature_verdicts']['legacy'], 'valid')
        self.assertEqual(late['historical_evidence'], 'unknown')
        self.assertEqual(trace['events'][-1]['reference']['state']['phase'], 'PQ_ACTIVE')

    def test_prepared_phase_keeps_legacy_authority(self):
        trace = self.execute('prepare', 'legacy-transition', 'pq-transition')
        self.assertEqual([e['reference']['accepted'] for e in trace['events']], [True, True, False])
        self.assertTrue(trace['matches_reference'])

    def test_confirm_without_prepare_and_repeated_ceremony(self):
        trace = self.execute('confirm', 'prepare', 'prepare', 'confirm', 'confirm', 'prepare')
        self.assertEqual([e['reference']['accepted'] for e in trace['events']], [False, True, False, True, False, False])
        self.assertTrue(trace['matches_reference'])

    def test_invalid_dual_authorization(self):
        for stage in ('prepare', 'confirm'):
            for signer in ('legacy', 'pq'):
                with self.subTest(stage=stage, signer=signer):
                    steps = ([] if stage == 'prepare' else ['prepare']) + ['diag-' + stage + '-' + signer + '-only']
                    result = self.execute(*steps)
                    self.assertFalse(result['events'][-1]['reference']['accepted'])
                    self.assertTrue(result['matches_reference'])

    def test_ceremony_bound_to_origin_and_target(self):
        for kind in ('prepare', 'confirm'):
            for field in ('epoch', 'to_epoch', 'new_key_sha256', 'algorithm', 'key_id'):
                with self.subTest(kind=kind, field=field):
                    steps = ([] if kind == 'prepare' else ['prepare']) + ['diag-' + kind + '-' + field]
                    result = self.execute(*steps)
                    self.assertFalse(result['events'][-1]['reference']['accepted'])
                    self.assertTrue(result['matches_reference'])

    def test_confirmation_must_match_prepared_migration(self):
        for names in [('prepare', 'diag-confirm-migration_id'), ('diag-prepare-migration_id', 'confirm')]:
            result = self.execute(*names)
            self.assertFalse(result['events'][-1]['reference']['accepted'])
            self.assertTrue(result['matches_reference'])

    def test_signed_wrong_operation_bindings(self):
        for field in ('domain', 'account', 'epoch', 'algorithm', 'key_id'):
            with self.subTest(field=field):
                result = self.execute('prepare', 'confirm', 'diag-operation-' + field)
                event = result['events'][-1]
                self.assertEqual(event['signature_verdicts']['pq'], 'valid')
                self.assertFalse(event['reference']['accepted'])
                self.assertEqual(event['reference_before'], event['reference']['state'])
                self.assertTrue(result['matches_reference'])

    def test_wrong_context_and_fallback(self):
        result = self.execute('prepare', 'confirm', 'diag-wrong-context', 'invalid-pq-fallback', 'pq-transition')
        self.assertTrue(result['matches_reference'])
        self.assertEqual(result['events'][2]['signature_verdicts']['pq'], 'invalid')
        self.assertEqual(result['events'][3]['signature_verdicts'], {'pq': 'invalid', 'legacy': 'valid'})
        self.assertFalse(result['events'][3]['reference']['accepted'])
        self.assertTrue(result['events'][4]['reference']['accepted'])

    def test_three_calibration_faults_cannot_pass(self):
        for fault in ('ignore-retirement', 'silent-fallback', 'reject-all'):
            result = self.execute('prepare', 'confirm', 'legacy-transition', 'pq-transition', 'invalid-pq-fallback',
                                  policy=candidates.baseline(fault))
            self.assertFalse(result['matches_reference'], fault)

    def test_rejection_does_not_consume_nonce(self):
        p = plan('prepare', 'confirm')
        p['events'] += [{'kind': 'mutate', 'message_ref': 'pq-transition', 'mutation': 'body'},
                        {'kind': 'deliver', 'message_ref': 'pq-transition'},
                        {'kind': 'duplicate', 'event_index': 3}]
        trace = run(p, self.policy, self.bundle)
        self.assertEqual([e['reference']['accepted'] for e in trace['events']], [True, True, False, True, False])
        self.assertTrue(trace['matches_reference'])

    def test_reference_is_independent_of_candidate(self):
        with patch('evorph.candidates.step', side_effect=lambda state, *args: {'accepted': False, 'state': state, 'reason': 'test'}):
            trace = self.execute('prepare')
        self.assertTrue(trace['events'][0]['reference']['accepted'])
        self.assertFalse(trace['matches_reference'])

    def test_finite_limits_and_capabilities(self):
        invalid = [dict(plan('prepare'), shell='anything'), plan(*(['prepare'] * 33)),
                   plan('diag-operation-domain'), plan('../../key'),
                   dict(plan('prepare'), events=[{'kind': 'sign', 'key': 'pq'}]),
                   dict(plan('prepare'), events=[{'kind': 'duplicate', 'event_index': 0}])]
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(InputError):
                    validate_plan(item)
        with self.assertRaises(InputError):
            validate_plan(dict(plan('prepare'), events=[{'kind': 'advance', 'ticks': True}]))

    def test_policy_boundaries(self):
        for value in ({'eval': 'print(1)'}, {'fact': 'private_key'}, {'all': []}):
            policy = deepcopy(self.policy)
            policy['rules']['operation']['when'] = value
            with self.assertRaises(InputError):
                candidates.validate(policy)
        policy = deepcopy(self.policy)
        node = {'constant': True}
        for _ in range(14):
            node = {'not': node}
        policy['rules']['operation']['when'] = node
        with self.assertRaises(InputError):
            candidates.validate(policy)

    def test_json_encoding_boundaries(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'x' * 65537):
            with self.assertRaises(InputError):
                parse_json(raw)
        raw = unb64(self.bundle['messages']['pq-transition']['message_b64'], 4096)
        with self.assertRaises(InputError):
            message(b' ' + raw)
        body = parse_json(raw)
        body['epoch'] = 1.0
        with self.assertRaises(InputError):
            message(canonical(body))

    def test_task_deadline_is_not_a_signature_rejection(self):
        with self.assertRaises(ExecutionError):
            run(plan('prepare'), self.policy, self.bundle, task_seconds=0)

    def test_files_are_public_only_and_temporary_keys_removed(self):
        with tempfile.TemporaryDirectory(prefix='evorph-test-') as temp:
            with patch('tempfile.tempdir', temp):
                bundle, _ = build()
            self.assertEqual(list(Path(temp).iterdir()), [])
            for raw in bundle['keys'].values():
                self.assertNotIn(b'PRIVATE KEY', unb64(raw, 8192))

    def test_replay_requires_no_private_keys_and_does_not_write(self):
        challenge = plan('prepare', 'confirm', 'pq-transition', 'legacy-transition')
        trace = run(challenge, self.policy, self.bundle)
        with tempfile.TemporaryDirectory(prefix='evorph-evidence-') as temp:
            out = Path(temp) / 'path with spaces'
            evidence.save(out, challenge, self.policy, self.bundle, trace)
            before = {p.name: (digest(p.read_bytes()), p.stat().st_mtime_ns) for p in out.iterdir()}
            self.assertTrue(evidence.replay(out)['replayed'])
            self.assertEqual(before, {p.name: (digest(p.read_bytes()), p.stat().st_mtime_ns) for p in out.iterdir()})
            with self.assertRaises(InputError):
                evidence.save(out, challenge, self.policy, self.bundle, trace)

    def test_tampering_is_detected_even_with_rehashed_manifest(self):
        challenge = plan('prepare', 'confirm', 'pq-transition')
        trace = run(challenge, self.policy, self.bundle)
        with tempfile.TemporaryDirectory(prefix='evorph-evidence-') as temp:
            out = Path(temp) / 'record'
            evidence.save(out, challenge, self.policy, self.bundle, trace)
            file = out / 'fixtures.json'
            data = json.loads(file.read_bytes())
            sig = bytearray(unb64(data['messages']['pq-transition']['signatures']['pq'], 8192))
            sig[0] ^= 1
            data['messages']['pq-transition']['signatures']['pq'] = b64(sig)
            file.write_bytes(canonical(data))
            with self.assertRaises(InputError):
                evidence.replay(out)
            manifest = json.loads((out / 'manifest.json').read_bytes())
            manifest[file.name] = digest(file.read_bytes())
            (out / 'manifest.json').write_bytes(canonical(manifest))
            with self.assertRaises(InputError):
                evidence.replay(out)

    def test_evidence_paths_cannot_follow_symlinks(self):
        with tempfile.TemporaryDirectory(prefix='evorph-evidence-') as temp:
            source = Path(temp) / 'source'
            source.write_bytes(b'public')
            (Path(temp) / 'linked').symlink_to(source)
            with self.assertRaises(InputError):
                evidence.read_file(temp, 'linked', 20)
            with self.assertRaises(InputError):
                evidence.read_file(temp, '../source', 20)

    def test_fixed_policy_baseline_partitions(self):
        report = calibrate(self.bundle)
        self.assertTrue(report['passed'])
        self.assertEqual([p['cases'] for p in report['partitions'].values()], [3, 7, 9])
        self.assertTrue(report['final_report_consumed'])

    def test_matched_official_vectors(self):
        result = verify(ROOT / 'backend/vectors')
        self.assertEqual(result['passed'], 15)
        self.assertEqual(result['applicable'], 15)
        self.assertEqual(sum(group['cases'] for group in result['excluded']), 165)
        self.assertEqual(sum(item['expected'] for item in result['results']), 3)

    def test_original_probe_replay(self):
        report = replay_probe(ROOT / 'frontend/dist/data')
        self.assertEqual(report['checks'], 13)
        self.assertEqual(report['signature_verdicts'], 16)


class ProcessTests(unittest.TestCase):
    def test_output_limit(self):
        with tempfile.TemporaryDirectory(prefix='evorph-process-') as temp:
            with self.assertRaisesRegex(ExecutionError, 'output limit'):
                run_process([sys.executable, '-c', 'import os;os.write(1,b"x"*20000)'], temp, output_limit=1024)

    def test_timeout_reaps_process_group(self):
        script = 'import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);pathlib.Path("child.pid").write_text(str(p.pid));time.sleep(30)'
        with tempfile.TemporaryDirectory(prefix='evorph-process-') as temp:
            with self.assertRaisesRegex(ExecutionError, 'deadline'):
                run_process([sys.executable, '-c', script], temp, timeout=0.5)
            pid = (Path(temp) / 'child.pid').read_text()
            result = subprocess.run(['ps', '-o', 'stat=', '-p', pid], capture_output=True, text=True, timeout=3)
            self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'))

    def test_missing_process_is_an_execution_error(self):
        with tempfile.TemporaryDirectory(prefix='evorph-process-') as temp:
            with self.assertRaises(ExecutionError):
                run_process([str(Path(temp) / 'absent')], temp)


if __name__ == '__main__':
    unittest.main()
