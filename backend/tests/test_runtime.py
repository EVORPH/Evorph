from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evorph.candidates import baseline
from evorph.contracts import ExecutionError, InputError, canonical
from evorph.evaluation import plan
from runtime.accounting import Accounting
from runtime.backup import create_backup, restore_backup
from runtime.store import CapacityError, ConflictError, Limits, Store
from runtime.worker import public_task_status, replay_task, run_once


def synthetic_limits():
    return Limits(queue_total=4, queue_per_submitter=2, max_attempts=2, lease_seconds=180,
                  storage_bytes=64 * 1024 * 1024, min_free_bytes=0, artifact_bytes=16 * 1024 * 1024)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-runtime-test-')
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.limits = synthetic_limits()
        self.store = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        self.accounts = Accounting(self.store)
        self.challenge = plan('prepare', 'confirm', 'pq-transition', 'legacy-transition')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, request='request-1', submitter='participant-1'):
        return self.store.submit(self.challenge, baseline(), submitter, request)

    def reserve(self, key='call-request-1'):
        return self.accounts.reserve(key, 'seeker', 'synthetic/model',
                                     {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000},
                                     10, 10, 'synthetic-context')

    def test_no_implicit_resource_or_spending_configuration(self):
        with self.assertRaises(TypeError):
            Limits()
        with self.assertRaises(CapacityError):
            self.reserve()
        with self.assertRaises(InputError):
            replace(self.limits, queue_total=0).validate()

    def test_submission_idempotency_persists_after_reopen(self):
        first = self.submit()
        self.assertEqual(self.submit(), first)
        self.store.close()
        self.store = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        self.assertEqual(self.submit(), first)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_conflicting_request_and_version_do_not_replace_history(self):
        self.submit()
        with self.assertRaises(ConflictError):
            self.store.submit(plan('prepare'), baseline(), 'participant-1', 'request-1')
        candidate = baseline()
        candidate['rules']['operation']['effect'] = 'none'
        with self.assertRaises(ConflictError):
            self.store.submit(self.challenge, candidate, 'participant-2', 'request-2')

    def test_queue_limits_are_enforced_without_losing_existing_tasks(self):
        first = self.submit()
        self.submit('request-2')
        with self.assertRaises(CapacityError):
            self.submit('request-3')
        self.assertEqual(self.submit(), first)
        self.submit('request-1', 'participant-2')
        self.submit('request-2', 'participant-2')
        with self.assertRaises(CapacityError):
            self.submit('request-1', 'participant-3')

    def test_disk_low_watermark_rejects_new_work(self):
        self.store.limits = replace(self.limits, min_free_bytes=2**52)
        with self.assertRaises(CapacityError):
            self.submit()
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_exclusive_worker_prevents_parallel_claim_even_after_lease(self):
        self.submit()
        other = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        try:
            with self.store.worker_lock() as held:
                self.assertTrue(held)
                self.assertIsNotNone(self.store.claim())
                self.now += 1000
                self.assertEqual(run_once(other)['state'], 'worker_busy')
                with self.assertRaises(ConflictError):
                    other.recover()
                self.assertIsNone(self.store.claim())
        finally:
            other.close()

    def test_interrupted_tasks_have_bounded_distinct_attempts(self):
        job_id = self.submit()
        with self.store.worker_lock():
            first = self.store.claim()
        with self.store.worker_lock():
            self.assertEqual(self.store.recover(), 1)
            second = self.store.claim()
            self.assertNotEqual(first['run_id'], second['run_id'])
            with self.assertRaises(ConflictError):
                self.store.finish(first, b'synthetic-stale-evidence', [])
        with self.store.worker_lock():
            self.assertEqual(self.store.recover(), 1)
            self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get_job(job_id)['state'], 'failed')
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM attempts WHERE state='interrupted'").fetchone()[0], 2)

    def test_actual_process_exit_releases_lock_and_recovers_committed_task(self):
        job_id = self.submit()
        program = 'import os,sys;from runtime.store import Store;from test_runtime import synthetic_limits;s=Store(sys.argv[1],synthetic_limits());lock=s.worker_lock();lock.__enter__();s.claim();os._exit(9)'
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-c', program, str(self.store.root)], env=env, timeout=10)
        self.assertEqual(result.returncode, 9)
        with self.store.worker_lock() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.get_job(job_id)['state'], 'queued')

    def test_real_local_job_preserves_events_and_public_evidence(self):
        job_id = self.submit()
        result = run_once(self.store)
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['matches_reference'])
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 4)
        self.assertTrue(replay_task(self.store, job_id)['replayed'])
        self.assertEqual(run_once(self.store)['state'], 'idle')
        public = public_task_status(self.store, job_id)
        self.assertEqual(set(public), {'job_id', 'state', 'run_id'})
        self.assertNotIn('submitter', public)
        self.assertEqual(self.store.integrity()['unreferenced_files'], [])

    def test_resource_configuration_can_be_adjusted_privately(self):
        updated = replace(self.limits, queue_total=6, queue_per_submitter=3)
        self.store.configure_limits(updated)
        self.store.close()
        self.store = Store(self.root / 'state', updated, clock=lambda: self.now)
        self.assertEqual(self.store.limits.queue_total, 6)

    def test_interrupted_attempt_scratch_is_cleaned_without_touching_other_files(self):
        self.submit()
        with self.store.worker_lock():
            job = self.store.claim()
        scratch = self.store.root / 'scratch' / job['run_id']
        scratch.mkdir()
        (scratch / 'temporary.key').write_bytes(b'synthetic-ephemeral-material')
        other = self.store.root / 'scratch' / 'unrelated'
        other.mkdir()
        with self.store.worker_lock():
            self.store.recover()
        self.assertFalse(scratch.exists())
        self.assertTrue(other.exists())

    def test_execution_failure_does_not_become_a_successful_check(self):
        job_id = self.submit()
        with patch('runtime.worker.build', side_effect=ExecutionError('synthetic failure')):
            result = run_once(self.store)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_kind'], 'execution_error')
        self.assertEqual(self.store.get_job(job_id)['result_run_id'], None)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)

    def test_archive_relationships_are_immutable_and_idempotent(self):
        args = ('link-1', 'contribution', 'submission-1', 'issue-1', {'kind': 'discovery'})
        self.store.add_link(*args)
        self.store.add_link(*args)
        with self.assertRaises(ConflictError):
            self.store.add_link('link-1', 'contribution', 'submission-1', 'issue-1', {'kind': 'reduction'})
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM links').fetchone()[0], 1)

    def test_reserve_rounds_up_without_floating_point(self):
        self.assertEqual(Accounting.estimate({'input_micros_per_million': 1, 'output_micros_per_million': 1}, 1, 1), 1)
        with self.assertRaises(InputError):
            Accounting.estimate({'input_micros_per_million': 1.5, 'output_micros_per_million': 1}, 1, 1)

    def test_request_reservation_and_dispatch_are_not_duplicated(self):
        self.accounts.configure(100, 200)
        call_id = self.reserve()
        self.assertEqual(self.reserve(), call_id)
        self.assertEqual(self.accounts.totals()['reserved_micros'], 20)
        self.accounts.dispatch(call_id)
        with self.assertRaises(ConflictError):
            self.accounts.dispatch(call_id)
        with self.assertRaises(ConflictError):
            self.accounts.cancel_before_send(call_id)

    def test_unconfirmed_usage_survives_recovery_and_day_rollover(self):
        self.accounts.configure(30, 100)
        call_id = self.reserve()
        self.accounts.dispatch(call_id)
        with self.store.worker_lock():
            self.store.recover()
        self.assertEqual(self.accounts.get(call_id)['state'], 'usage_unknown')
        self.now += 40 * 86400
        with self.assertRaises(CapacityError):
            self.reserve('another-request')
        self.assertEqual(self.accounts.totals()['reserved_micros'], 20)

    def test_response_and_settlement_are_durable_and_idempotent(self):
        self.accounts.configure(100, 200)
        call_id = self.reserve()
        self.accounts.dispatch(call_id)
        with self.assertRaises(ConflictError):
            self.accounts.settle(call_id, 10)
        response = {'kind': 'synthetic_test', 'usage': {'billed_micros': 10}}
        self.accounts.record_response(call_id, response)
        self.accounts.record_response(call_id, response)
        self.accounts.settle(call_id, 10)
        self.accounts.settle(call_id, 10)
        self.assertEqual(self.accounts.totals(), {'settled_micros': 10, 'reserved_micros': 0})
        with self.assertRaises(ConflictError):
            self.accounts.settle(call_id, 11)
        with self.assertRaises(ConflictError):
            self.accounts.record_response(call_id, {'changed': True})

    def test_daily_and_monthly_allocations_are_separate(self):
        self.accounts.configure(30, 30)
        call_id = self.reserve()
        with self.assertRaises(CapacityError):
            self.reserve('call-request-2')
        self.accounts.dispatch(call_id)
        self.accounts.record_response(call_id, {'kind': 'synthetic_test', 'billed_micros': 20})
        self.accounts.settle(call_id, 20)
        self.now += 86400
        with self.assertRaises(CapacityError):
            self.reserve('call-request-2')

    def test_confirmed_unsent_request_can_release_reservation(self):
        self.accounts.configure(30, 100)
        call_id = self.reserve()
        self.accounts.cancel_before_send(call_id)
        self.accounts.cancel_before_send(call_id)
        self.assertEqual(self.accounts.totals()['reserved_micros'], 0)
        self.reserve('another-request')

    def test_actual_overrun_is_recorded_not_discarded(self):
        self.accounts.configure(100, 200)
        call_id = self.reserve()
        self.accounts.dispatch(call_id)
        self.accounts.record_response(call_id, {'kind': 'synthetic_test', 'billed_micros': 25})
        self.accounts.settle(call_id, 25)
        self.assertEqual(self.accounts.totals()['settled_micros'], 25)
        with self.assertRaises(CapacityError):
            self.reserve('another-request')

    def test_backup_restores_artifacts_relationships_and_unknown_usage(self):
        job_id = self.submit()
        self.assertEqual(run_once(self.store)['state'], 'completed')
        self.store.add_link('link-1', 'contribution', 'submission-1', 'issue-1', {'kind': 'discovery'})
        self.accounts.configure(30, 100)
        call_id = self.reserve()
        self.accounts.dispatch(call_id)
        snapshot = self.root / 'snapshot'
        self.assertTrue(create_backup(self.store, snapshot)['verified'])
        destination = self.root / 'restored'
        self.assertTrue(restore_backup(snapshot, destination, self.limits.storage_bytes)['restored'])
        restored = Store(destination, self.limits, clock=lambda: self.now)
        try:
            self.assertTrue(replay_task(restored, job_id)['replayed'])
            with restored.worker_lock():
                restored.recover()
            self.assertEqual(Accounting(restored).get(call_id)['state'], 'usage_unknown')
            self.assertEqual(Accounting(restored).totals()['reserved_micros'], 20)
            self.assertEqual(restored.connection.execute('SELECT COUNT(*) FROM links').fetchone()[0], 1)
            self.assertEqual(run_once(restored)['state'], 'idle')
        finally:
            restored.close()

    def test_corrupt_backup_and_existing_destination_are_rejected(self):
        snapshot = self.root / 'snapshot'
        create_backup(self.store, snapshot)
        with self.assertRaises(InputError):
            restore_backup(snapshot, self.store.root, self.limits.storage_bytes)
        config = snapshot / 'config.json'
        config.write_bytes(b'{}')
        with self.assertRaises(InputError):
            restore_backup(snapshot, self.root / 'restored', self.limits.storage_bytes)
        self.assertFalse((self.root / 'restored').exists())
        self.assertTrue(self.store.database.exists())

    def test_missing_live_evidence_blocks_worker_without_erasing_task(self):
        job_id = self.submit()
        run_once(self.store)
        artifact = next((self.store.root / 'objects').iterdir())
        artifact.write_bytes(b'broken')
        with self.assertRaises(InputError):
            run_once(self.store)
        self.assertEqual(self.store.get_job(job_id)['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
