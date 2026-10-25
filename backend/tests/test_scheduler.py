from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.channel import ChannelSpec, ModelChannel
from agents.fixtures import ScriptedTransport, keeper_payload, reply, response, seeker_payload, synthetic_meter, timed_out, usage
from agents.research import Research, fixed_cases
from agents.scheduler import Scheduler
from agents.strategy import baseline as method
from evorph.candidates import baseline
from evorph.contracts import InputError, canonical, parse_json
from evorph.evaluation import plan
from runtime.accounting import Accounting
from runtime.backup import create_backup, restore_backup
from runtime.store import ConflictError, Limits, Store


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evorph-scheduler-test-')
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.limits = Limits(20, 10, 2, 180, 128 * 1024 * 1024, 0, 16 * 1024 * 1024)
        self.store = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        self.accounts = Accounting(self.store)
        self.accounts.configure(1000000, 5000000)
        self.spec = ChannelSpec('synthetic-provider', 'synthetic/model',
                                {'input_micros_per_million': 1000000, 'output_micros_per_million': 1000000},
                                100000, 10000, 100, 10, 'synthetic-byte-meter.v1')
        self.transport = ScriptedTransport([reply(response('synthetic-provider', 'synthetic/model',
            seeker_payload(plan('prepare'), 'Synthetic scheduled candidate.'), usage(10, 10))) for _ in range(5)])
        self.research = self.make_research(self.store, self.accounts)
        self.template = self.research.freeze('template', baseline(), method(), search_attempts=1,
                                             reduction_attempts=0, evaluation_cases=fixed_cases())
        self.scheduler = Scheduler(self.research)
        self.scheduler.configure(self.template, interval_seconds=60, check_seconds=5)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Offline scheduling test.'))
        self.network.start()

    def make_research(self, store, accounts):
        return Research(store, ModelChannel(accounts, self.spec, self.transport, synthetic_meter),
                        ModelChannel(accounts, self.spec, ScriptedTransport(), synthetic_meter))

    def tearDown(self):
        self.network.stop()
        self.store.close()
        self.temp.cleanup()

    def submit(self, key):
        return self.research.register_submission('synthetic-player', key, plan('prepare'), 'baseline-v1')

    def test_seeker_strategy_from_online_shape_is_compared_and_gates_inheritance(self):
        proposed = {**method(), 'version_id': 'search-proposed-1', 'parent_id': 'search-baseline-v1'}
        payload = {**seeker_payload(plan('prepare', 'confirm', 'legacy-transition'), 'Synthetic scheduled candidate.'),
                   'strategy': proposed}
        child = {**baseline(), 'version_id': 'synthetic-repair', 'parent_id': 'ignore-retirement'}
        scripted = lambda body: ScriptedTransport([reply(response(
            'synthetic-provider', 'synthetic/model', body, usage(10, 10)))])
        research = Research(self.store,
                            ModelChannel(self.accounts, self.spec, scripted(payload), synthetic_meter),
                            ModelChannel(self.accounts, self.spec, scripted(keeper_payload(child)), synthetic_meter))
        batch_id = research.freeze('strategy-round', baseline('ignore-retirement'), method(),
                                   search_attempts=2, reduction_attempts=0, evaluation_cases=fixed_cases())
        research.run(batch_id)
        decisions = [record['body'] for record in research.journal(batch_id)
                     if record['kind'] == 'decision' and record['body'].get('reason') == 'fixed_search_comparison']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['candidate_id'], 'search-proposed-1')
        row = self.store.connection.execute('SELECT state,body FROM research_methods WHERE version_id=?',
                                            ('search-proposed-1',)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(parse_json(row['body']), proposed)
        with self.store.transaction():
            self.store.connection.execute("UPDATE research_methods SET state='proposed' WHERE version_id=?",
                                          ('search-proposed-1',))
        with self.assertRaises(ConflictError):
            research.freeze('gate-proposed', baseline('ignore-retirement'), proposed,
                            search_attempts=1, reduction_attempts=0, evaluation_cases=fixed_cases())
        with self.store.transaction():
            self.store.connection.execute("UPDATE research_methods SET state='retained' WHERE version_id=?",
                                          ('search-proposed-1',))
        research.freeze('gate-retained', baseline('ignore-retirement'), proposed,
                        search_attempts=1, reduction_attempts=0, evaluation_cases=fixed_cases())

    def test_retained_round_advances_the_schedule_template_and_the_next_round_inherits_it(self):
        child = {**baseline(), 'version_id': 'synthetic-repair', 'parent_id': 'ignore-retirement'}
        scripted = lambda payload: ScriptedTransport([reply(response(
            'synthetic-provider', 'synthetic/model', payload, usage(10, 10))) for _ in range(4)])
        research = Research(self.store,
                            ModelChannel(self.accounts, self.spec, scripted(seeker_payload(
                                plan('prepare', 'confirm', 'legacy-transition'), 'Synthetic scheduled candidate.')),
                                synthetic_meter),
                            ModelChannel(self.accounts, self.spec, scripted(keeper_payload(child)), synthetic_meter))
        template = research.freeze('inherit-template', baseline('ignore-retirement'), method(),
                                   search_attempts=1, reduction_attempts=0, evaluation_cases=fixed_cases())
        scheduler = Scheduler(research)
        scheduler.configure(template, interval_seconds=1, check_seconds=1)
        scheduler.enable()
        first = scheduler.tick()
        self.assertEqual(first['state'], 'completed')
        self.assertEqual(first['source'], 'background')
        grown = [record['body']['cases'] for record in research.journal(first['batch_id'])
                 if record['kind'] == 'evaluation_set']
        self.assertTrue(grown and len(grown[-1]) > len(fixed_cases()))
        policy = self.store.connection.execute('SELECT template_id FROM scheduler_policy WHERE policy_id=1').fetchone()
        self.assertNotEqual(policy['template_id'], template)
        _, inherited = research.batch(policy['template_id'])
        self.assertEqual(inherited['target']['version_id'], 'synthetic-repair')
        self.assertEqual(inherited['evaluation_cases'], grown[-1])
        self.now += 1
        second = scheduler.tick()
        self.assertEqual(second['state'], 'completed')
        _, snapshot = research.batch(second['batch_id'])
        self.assertEqual(snapshot['target']['version_id'], 'synthetic-repair')
        self.assertEqual(snapshot['evaluation_cases'], grown[-1])

    def test_disabled_until_configured_and_enabled(self):
        self.assertEqual(self.scheduler.tick(), {'state': 'paused'})
        self.assertEqual(self.transport.sends, 0)
        with self.assertRaises(InputError):
            self.scheduler.configure(self.template, interval_seconds=0, check_seconds=1)
        with self.assertRaises(InputError):
            self.scheduler.configure(self.template, interval_seconds=10, check_seconds=20)

    def test_player_fifo_priority_and_no_repeated_submission(self):
        first, second = self.submit('first'), self.submit('second')
        self.scheduler.enable()
        one = self.scheduler.tick()
        self.now += 60
        two = self.scheduler.tick()
        self.assertEqual((one['state'], one['source'], two['source']), ('completed', 'player', 'player'))
        rows = self.store.connection.execute('SELECT source_id,batch_id FROM scheduler_tasks ORDER BY rowid').fetchall()
        self.assertEqual([row['source_id'] for row in rows], [first, second])
        self.assertEqual(self.scheduler.tick()['state'], 'waiting')
        self.assertEqual(self.transport.sends, 2)
        self.assertEqual(self.research.batch(rows[0]['batch_id'])[1]['submissions'][0]['submission_id'], first)

    def test_background_interval_does_not_catch_up_missed_periods(self):
        self.scheduler.enable()
        first = self.scheduler.tick()
        self.assertEqual((first['state'], first['source']), ('completed', 'background'))
        self.now += 59
        self.assertEqual(self.scheduler.tick()['state'], 'waiting')
        self.now += 86400
        second = self.scheduler.tick()
        self.assertEqual(second['source'], 'background')
        self.assertEqual(self.scheduler.tick()['state'], 'waiting')
        self.assertEqual(self.transport.sends, 2)

    def test_queued_submission_shares_the_interval_and_runs_first_when_due(self):
        self.scheduler.enable()
        self.scheduler.tick()
        submitted = self.submit('arrived-later')
        # A queued submission keeps its priority but does not buy an extra paid round.
        self.assertEqual(self.scheduler.tick()['state'], 'waiting')
        self.assertEqual(self.transport.sends, 1)
        self.now += 60
        self.assertEqual(self.scheduler.tick()['source'], 'player')
        self.assertEqual(self.store.connection.execute(
            'SELECT COUNT(*) FROM scheduler_tasks WHERE source_id=?', (submitted,)).fetchone()[0], 1)

    def test_interval_change_applies_immediately_and_survives_the_running_round(self):
        self.scheduler.enable()
        self.assertEqual(self.scheduler.set_interval(21600)['interval_seconds'], 21600)
        policy = self.store.connection.execute('SELECT * FROM scheduler_policy WHERE policy_id=1').fetchone()
        self.assertEqual(policy['next_due'], self.now + 21600)
        self.assertTrue(policy['enabled'])
        self.assertIsNone(policy['pause_reason'])
        self.now += 21600
        original = self.research.run

        def rescheduled_mid_round(batch_id):
            self.now += 300
            self.scheduler.set_interval(300)
            return original(batch_id)

        with patch.object(self.research, 'run', side_effect=rescheduled_mid_round):
            self.assertEqual(self.scheduler.tick()['state'], 'completed')
        stored = self.store.connection.execute('SELECT * FROM scheduler_policy WHERE policy_id=1').fetchone()
        # The round claimed under the old rhythm must not overwrite the newer deadline.
        self.assertEqual(stored['next_due'], self.now + 300)
        self.assertEqual(stored['interval_revision'], policy['interval_revision'] + 1)
        self.assertEqual(self.scheduler.tick()['state'], 'waiting')

    def test_interval_change_does_not_clear_a_recorded_fault(self):
        self.scheduler.enable()
        self.transport.script = [timed_out()]
        self.submit('first')
        self.assertEqual(self.scheduler.tick()['state'], 'paused')
        result = self.scheduler.set_interval(900, enable=True)
        self.assertEqual(result['pause_reason'], 'seeker_timeout')
        self.assertFalse(result['enabled'])
        self.assertEqual(self.store.connection.execute(
            'SELECT enabled,pause_reason FROM scheduler_policy').fetchone()['pause_reason'], 'seeker_timeout')
        with self.assertRaises(ConflictError):
            self.scheduler.enable()

    def test_settled_fault_requires_enable_and_starts_a_new_round_without_resending(self):
        channel = self.research.channels['seeker']
        original = channel.classify
        interrupted = [True]

        def uncertain(call_id, response, role, parent, measured, bound, request):
            if interrupted[0]:
                interrupted[0] = False
                return channel.unconfirmed(call_id, 'usage_unknown', 'synthetic_interruption', measured, bound)
            return original(call_id, response, role, parent, measured, bound, request)

        self.scheduler.enable()
        with patch.object(channel, 'classify', side_effect=uncertain):
            first = self.scheduler.tick()
            self.assertEqual(first['state'], 'paused')
            with self.assertRaises(ConflictError):
                self.scheduler.enable()
            row = self.store.connection.execute('SELECT * FROM model_calls').fetchone()
            self.store.connection.execute("UPDATE model_calls SET state='response_saved' WHERE call_id=?",
                                          (row['call_id'],))
            self.accounts.settle(row['call_id'], 20)
            self.assertEqual(self.accounts.get(row['call_id'])['state'], 'settled')
            self.assertEqual(self.transport.sends, 1)
            self.scheduler.enable()
            self.assertEqual(self.scheduler.tick()['state'], 'waiting')
            self.assertEqual(self.transport.sends, 1)
            self.now += 60
            second = self.scheduler.tick()
        self.assertEqual(second['state'], 'completed')
        self.assertNotEqual(second['batch_id'], first['batch_id'])
        self.assertEqual(self.transport.sends, 2)
        self.assertEqual(self.research.batch(first['batch_id'])[0]['state'], 'paused')

    def test_stop_lets_the_claimed_round_finish_and_blocks_the_next_one(self):
        self.scheduler.enable()
        original = self.research.run

        def pause_after_claim(batch_id):
            self.scheduler.pause()
            return original(batch_id)

        with patch.object(self.research, 'run', side_effect=pause_after_claim):
            self.assertEqual(self.scheduler.tick()['state'], 'completed')
        self.assertEqual(self.scheduler.tick()['state'], 'paused')
        self.assertEqual(self.transport.sends, 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM scheduler_tasks WHERE state='pending'").fetchone()[0], 0)
        resumed = self.scheduler.set_interval(60, enable=True)
        self.assertTrue(resumed['enabled'])
        self.assertIsNone(resumed['pause_reason'])
        self.now += 60
        self.assertEqual(self.scheduler.tick()['state'], 'completed')
        self.assertEqual(self.transport.sends, 2)

    def test_recovered_claim_keeps_its_original_interval_revision(self):
        other = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        peer = Scheduler(self.make_research(other, Accounting(other)))
        self.scheduler.enable()

        def interrupted(_):
            peer.set_interval(300)
            raise SystemExit(9)

        try:
            with patch.object(self.research, 'run', side_effect=interrupted):
                with self.assertRaises(SystemExit):
                    self.scheduler.tick()
            claimed = self.store.connection.execute(
                "SELECT claim_interval_revision FROM scheduler_tasks WHERE state='pending'").fetchone()[0]
            current = self.store.connection.execute(
                'SELECT interval_revision,next_due FROM scheduler_policy').fetchone()
            self.assertEqual(claimed, current['interval_revision'] - 1)
            self.now += 300
            self.assertEqual(self.scheduler.tick()['state'], 'completed')
            stored = self.store.connection.execute('SELECT next_due FROM scheduler_policy').fetchone()[0]
            self.assertEqual(stored, current['next_due'])
            self.assertEqual(self.transport.sends, 1)
        finally:
            other.close()

    def test_scheduler_exception_finishes_batch_and_retries_publication(self):
        broken = method()
        broken.update(version_id=baseline()['version_id'], parent_id=broken['version_id'])
        payload = seeker_payload(plan('prepare'))
        payload['strategy'] = broken
        self.transport.script = [reply(response('synthetic-provider', 'synthetic/model', payload, usage(10, 10)))]

        def unavailable(batch_id):
            raise ConflictError('Synthetic publication fault.')

        scheduler = self.with_publisher(unavailable)
        outcome = scheduler.tick()
        row, _ = self.research.batch(outcome['batch_id'])
        self.assertEqual((outcome['state'], row['state']), ('failed', 'failed'))
        self.assertEqual(row['result'], canonical(
            {'batch_id': outcome['batch_id'], 'state': 'failed', 'reason': 'scheduler_execution_error'}))
        stopped = [event for event in self.research.journal(outcome['batch_id']) if event['kind'] == 'experiment']
        self.assertEqual([event['body']['state'] for event in stopped], ['failed'])
        self.assertEqual(self.store.connection.execute(
            'SELECT COUNT(*) FROM scheduler_publications').fetchone()[0], 1)
        released = []
        scheduler.publisher = released.append
        self.assertEqual(scheduler.catch_up(), 1)
        self.assertEqual(released, [outcome['batch_id']])
        self.assertEqual(self.transport.sends, 1)

    def test_paused_schedule_retries_publication_without_restarting_research(self):
        attempts = []
        allow = [False]

        def release(batch_id):
            attempts.append(batch_id)
            if not allow[0]:
                raise ConflictError('Projection unavailable.')

        scheduler = self.with_publisher(release)
        self.transport.script = [timed_out()]
        self.assertEqual(scheduler.tick()['state'], 'paused')
        self.assertEqual(self.store.connection.execute(
            'SELECT COUNT(*) FROM scheduler_publications').fetchone()[0], 1)
        deferred = scheduler.publication_status()['items']
        self.assertEqual(deferred[0]['state'], 'deferred')
        self.assertEqual(deferred[0]['reason_categories'], ['projection_conflict'])
        self.assertIsNone(deferred[0]['withheld_count'])
        allow[0] = True
        self.assertEqual(scheduler.tick()['state'], 'paused')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.store.connection.execute(
            'SELECT COUNT(*) FROM scheduler_publications').fetchone()[0], 0)
        self.assertEqual(scheduler.publication_status()['items'][0]['state'], 'published')
        self.assertEqual(self.transport.sends, 1)

    def with_publisher(self, publisher):
        scheduler = Scheduler(self.research, publisher=publisher)
        scheduler.configure(self.template, interval_seconds=60, check_seconds=5)
        scheduler.enable()
        return scheduler

    def test_finished_round_is_handed_to_the_publisher_once(self):
        released = []
        scheduler = self.with_publisher(released.append)
        result = scheduler.tick()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(released, [result['batch_id']])
        self.assertEqual(scheduler.tick()['state'], 'waiting')
        self.assertEqual(released, [result['batch_id']])

    def test_publication_failure_does_not_change_the_round_result(self):
        def broken(batch_id):
            raise ConflictError('Projection unavailable.')
        scheduler = self.with_publisher(broken)
        self.assertEqual(scheduler.tick()['state'], 'completed')
        self.assertEqual(self.transport.sends, 1)
        self.assertEqual(scheduler.tick()['state'], 'waiting')

    def test_deferred_publication_does_not_hold_up_other_batches(self):
        attempts = []
        blocked = []

        def first_stays_unavailable(batch_id):
            attempts.append(batch_id)
            if not blocked:
                blocked.append(batch_id)
            if batch_id == blocked[0]:
                raise ConflictError('Projection unavailable.')

        scheduler = self.with_publisher(first_stays_unavailable)
        first = scheduler.tick()
        self.now += 60
        second = scheduler.tick()
        self.assertEqual((first['state'], second['state']), ('completed', 'completed'))
        self.assertEqual(attempts, [first['batch_id'], first['batch_id'], second['batch_id']])
        queued = self.store.connection.execute('SELECT batch_id FROM scheduler_publications').fetchall()
        self.assertEqual([row['batch_id'] for row in queued], [first['batch_id']])
        self.assertEqual(self.transport.sends, 2)

    def finished_task(self, key, finalize=True):
        batch = self.research.freeze(key, baseline(), method(), search_attempts=1,
                                     reduction_attempts=0, evaluation_cases=fixed_cases())
        result = {'batch_id': batch, 'state': 'failed', 'reason': 'scheduler_execution_error'}
        if finalize:
            self.research.finish(batch, result['state'], result['reason'])
        with self.store.transaction():
            self.store.connection.execute("INSERT INTO scheduler_tasks VALUES(?,NULL,?,?,'failed',?,1)",
                                          (key, self.template, batch, canonical(result)))
            self.store.connection.execute('INSERT INTO scheduler_publications VALUES(?)', (batch,))
        return batch

    def test_blocked_frequency_is_saved_without_enabling_or_clearing_faults(self):
        other = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        try:
            peer = Scheduler(self.make_research(other, Accounting(other)))
            self.accounts.pause()
            saved = peer.set_interval(300, enable=True)
            self.assertEqual((saved['interval_seconds'], saved['enabled'], saved['blocked_reason']),
                             (300, False, 'resource_blocked'))
            self.assertIsNone(saved['pause_reason'])
            policy = self.store.connection.execute('SELECT * FROM scheduler_policy').fetchone()
            self.assertEqual(policy['next_due'], self.now + 300)
            self.assertEqual(policy['interval_revision'], 2)
            self.assertTrue(self.store.connection.execute('SELECT paused FROM resource_policy').fetchone()[0])
            self.accounts.configure(1000000, 5000000)
            call = self.accounts.reserve('in-flight', 'seeker', self.spec.model_id, self.spec.price, 10, 10, 'context')
            self.accounts.dispatch(call)
            saved = peer.set_interval(900, enable=True)
            self.assertEqual((saved['interval_seconds'], saved['enabled'], saved['blocked_reason']),
                             (900, False, 'resource_blocked'))
            self.assertEqual(self.accounts.get(call)['state'], 'dispatching')
            self.assertEqual(self.transport.sends, 0)
        finally:
            other.close()

    def test_retry_rotation_survives_restart_and_reaches_later_batches(self):
        batches = [self.finished_task('rotation-' + str(i)) for i in range(11)]
        attempted = []
        def publish(batch):
            attempted.append(batch)
            if batch != batches[-1]:
                raise ConflictError('Synthetic unavailable projection.')
        self.scheduler.publisher = publish
        self.assertEqual(self.scheduler.catch_up(), 0)
        reopened = Scheduler(self.research, publisher=publish)
        self.assertEqual(reopened.catch_up(), 1)
        self.assertIn(batches[-1], attempted)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM scheduler_publications').fetchone()[0], 10)
        self.assertEqual(self.transport.sends, 0)

    def test_legacy_terminal_task_repairs_batch_before_publishing(self):
        batch = self.finished_task('legacy-split', finalize=False)
        self.store.connection.execute('DELETE FROM scheduler_publications WHERE batch_id=?', (batch,))
        self.store.connection.execute("UPDATE metadata SET value='scheduler.v3' WHERE key='scheduler_schema'")
        observed = []
        def publish(batch_id):
            observed.append(self.research.batch(batch_id)[0]['state'])
        upgraded = Scheduler(self.research, publisher=publish)
        with self.store.worker_lock():
            self.assertEqual(upgraded.catch_up(), 0)
        self.assertIsNone(self.research.batch(batch)[0]['result'])
        self.assertEqual(upgraded.catch_up(), 1)
        self.assertEqual(observed, ['failed'])
        before = self.research.journal(batch)
        self.assertEqual(upgraded.catch_up(), 0)
        self.assertEqual(self.research.journal(batch), before)
        self.assertEqual(self.transport.sends, 0)

    def test_publication_summary_covers_all_pages_and_pending_work(self):
        batches = [self.finished_task('page-' + str(i)) for i in range(51)]
        for batch in batches[1:]:
            self.store.connection.execute('DELETE FROM scheduler_publications WHERE batch_id=?', (batch,))
            self.store.connection.execute('INSERT INTO scheduler_publication_reports VALUES(?,?,?,?)',
                                          (batch, 'published', 0, canonical([])))
        first = self.scheduler.publication_status()
        self.assertEqual(first['summary']['total'], 51)
        self.assertEqual(first['summary']['deferred'], 1)
        self.assertEqual(len(first['items']), 50)
        self.assertIsNotNone(first['next_before'])
        second = self.scheduler.publication_status(first['next_before'])
        self.assertEqual(second['summary'], first['summary'])
        self.assertEqual(second['items'][0]['experiment_id'], self.research.alias('experiment', batches[0]))
        self.assertEqual(second['items'][0]['state'], 'deferred')
        self.assertIsNone(second['next_before'])
        for bad in (0, -1, True, '1', 2**53):
            with self.assertRaises(InputError):
                self.scheduler.publication_status(bad)

    def test_console_frequency_tiers_are_accepted_and_bounded(self):
        for seconds in (180, 300, 900, 1800, 3600, 7200, 14400, 21600):
            self.assertEqual(self.scheduler.set_interval(seconds)['interval_seconds'], seconds)
            self.assertEqual(self.store.connection.execute(
                'SELECT check_seconds FROM scheduler_policy WHERE policy_id=1').fetchone()[0], 5)
        for invalid in (0, -1, 2**31):
            with self.assertRaises(InputError):
                self.scheduler.set_interval(invalid)

    def test_timeout_pauses_without_resending_or_running_next_task(self):
        self.transport.script = [timed_out()]
        self.submit('first')
        self.submit('second')
        self.scheduler.enable()
        self.assertEqual(self.scheduler.tick()['state'], 'paused')
        self.assertEqual(self.scheduler.tick()['state'], 'paused')
        self.assertEqual(self.transport.sends, 1)
        with self.assertRaises(ConflictError):
            self.scheduler.enable()
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM scheduler_tasks').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT state FROM model_calls').fetchone()[0], 'usage_unknown')

    def test_crash_between_batch_creation_and_binding_reuses_same_batch(self):
        self.scheduler.enable()
        freeze = self.research.freeze
        def interrupted(*args, **kwargs):
            freeze(*args, **kwargs)
            raise SystemExit(9)
        with patch.object(self.research, 'freeze', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.scheduler.tick()
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM research_batches').fetchone()[0], 2)
        with self.assertRaises(ConflictError):
            self.scheduler.configure(self.template, interval_seconds=30, check_seconds=5)
        self.assertEqual(self.scheduler.tick()['state'], 'completed')
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM research_batches').fetchone()[0], 2)
        self.assertEqual(self.transport.sends, 1)

    def test_crash_after_research_completion_does_not_run_again(self):
        self.scheduler.enable()
        with patch.object(self.scheduler, '_finish', side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                self.scheduler.tick()
        self.assertEqual(self.transport.sends, 1)
        self.store.close()
        self.store = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        self.accounts = Accounting(self.store)
        self.research = self.make_research(self.store, self.accounts)
        self.scheduler = Scheduler(self.research)
        self.assertEqual(self.scheduler.tick()['state'], 'completed')
        self.assertEqual(self.transport.sends, 1)
        self.assertEqual(self.scheduler.tick()['state'], 'waiting')

    def test_scheduler_lock_and_existing_worker_lock_prevent_overlap(self):
        other = Store(self.root / 'state', self.limits, clock=lambda: self.now)
        try:
            peer = Scheduler(self.make_research(other, Accounting(other)))
            self.scheduler.enable()
            with self.scheduler._lock():
                self.assertEqual(peer.tick()['state'], 'scheduler_busy')
            with other.worker_lock():
                first = self.scheduler.tick()
                self.assertEqual(first['state'], 'worker_busy')
            self.assertEqual(self.scheduler.tick()['task_id'], first['task_id'])
            self.assertEqual(self.transport.sends, 1)
        finally:
            other.close()

    def test_paused_accounting_and_unknown_usage_block_new_schedule(self):
        self.scheduler.enable()
        self.accounts.pause()
        self.assertEqual(self.scheduler.tick()['state'], 'resource_paused')
        self.accounts.configure(1000000, 5000000)
        call = self.accounts.reserve('existing-unknown', 'seeker', 'synthetic/model', self.spec.price, 10, 10, 'context')
        self.accounts.dispatch(call)
        self.accounts.mark_unknown(call)
        self.assertEqual(self.scheduler.tick()['state'], 'resource_paused')
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM scheduler_tasks').fetchone()[0], 0)

    def test_runner_loop_obeys_stop_event_without_sleeping_in_test(self):
        self.scheduler.enable()
        scheduler = self.scheduler
        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True
                return True
        with patch.object(scheduler, 'tick', wraps=scheduler.tick) as tick:
            scheduler.serve(Stop())
            self.assertEqual(tick.call_count, 2)
        self.assertEqual(self.transport.sends, 1)

    def test_backup_preserves_completion_and_deadline(self):
        self.scheduler.enable()
        self.scheduler.tick()
        backup = self.root / 'backup'
        restored_path = self.root / 'restored'
        create_backup(self.store, backup)
        restore_backup(backup, restored_path, self.limits.storage_bytes)
        restored = Store(restored_path, self.limits, clock=lambda: self.now)
        try:
            scheduler = Scheduler(self.make_research(restored, Accounting(restored)))
            self.assertEqual(scheduler.tick()['state'], 'waiting')
            self.assertEqual(self.transport.sends, 1)
            self.assertEqual(restored.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            restored.close()

    def test_pause_and_changed_template_configuration_do_not_start_new_calls(self):
        self.scheduler.enable()
        self.scheduler.pause()
        self.assertEqual(self.scheduler.tick()['state'], 'paused')
        self.scheduler.enable()
        self.research.channels['seeker'].spec.price['input_micros_per_million'] += 1
        self.assertEqual(self.scheduler.tick()['state'], 'failed')
        self.assertEqual(self.transport.sends, 0)
        self.assertEqual(self.scheduler.tick()['state'], 'paused')


if __name__ == '__main__':
    unittest.main()
