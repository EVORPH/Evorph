from contextlib import contextmanager
import fcntl
import os
import sqlite3
import sys
import uuid

from evorph.contracts import InputError, canonical, integer, parse_json
from runtime.store import CapacityError, ConflictError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS scheduler_policy (
 policy_id INTEGER PRIMARY KEY CHECK(policy_id=1), template_id TEXT NOT NULL REFERENCES research_batches,
 interval_seconds INTEGER NOT NULL, check_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
 next_due REAL NOT NULL, pause_reason TEXT, interval_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scheduler_tasks (
 task_id TEXT PRIMARY KEY, source_id TEXT UNIQUE REFERENCES research_submissions,
 template_id TEXT NOT NULL REFERENCES research_batches, batch_id TEXT UNIQUE REFERENCES research_batches,
 state TEXT NOT NULL CHECK(state IN ('pending','completed','paused','failed')), result BLOB,
 claim_interval_revision INTEGER
);
CREATE TABLE IF NOT EXISTS scheduler_publications (
 batch_id TEXT PRIMARY KEY REFERENCES research_batches
);
CREATE TABLE IF NOT EXISTS scheduler_publication_reports (
 batch_id TEXT PRIMARY KEY REFERENCES research_batches,
 state TEXT NOT NULL CHECK(state IN ('published','partial','deferred')),
 withheld_count INTEGER,
 reason_categories BLOB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduler_single_pending ON scheduler_tasks(state) WHERE state='pending';
'''

BLOCKING_STATES = ('dispatching', 'response_saved', 'usage_unknown')
TERMINAL_STATES = ('completed', 'paused', 'failed')
# The control surface changes the frequency out of band, so the worker never sleeps
# longer than this between looks at the stored policy.
CONTROL_POLL_SECONDS = 5
CATCH_UP_LIMIT = 10


class Scheduler:
    def __init__(self, research, publisher=None):
        self.research, self.store = research, research.store
        self.publisher = publisher
        row = self.store.connection.execute("SELECT value FROM metadata WHERE key='scheduler_schema'").fetchone()
        if row and row[0] not in ('scheduler.v1', 'scheduler.v2', 'scheduler.v3', 'scheduler.v4', 'scheduler.v5'):
            raise InputError('Unsupported scheduler schema.')
        before, after = '', ''
        if row and row[0] == 'scheduler.v1':
            before += 'ALTER TABLE scheduler_policy ADD COLUMN interval_revision INTEGER NOT NULL DEFAULT 0;\n'
        if row and row[0] in ('scheduler.v1', 'scheduler.v2'):
            before += ('ALTER TABLE scheduler_tasks ADD COLUMN claim_interval_revision INTEGER;\n'
                       'UPDATE scheduler_tasks SET claim_interval_revision='
                       '(SELECT interval_revision FROM scheduler_policy WHERE policy_id=1) '
                       "WHERE state='pending';\n")
            after = ("INSERT INTO scheduler_publications(batch_id) SELECT batch_id FROM scheduler_tasks "
                     "WHERE batch_id IS NOT NULL AND state IN ('completed','paused','failed');\n")
        if row and row[0] != 'scheduler.v5':
            after += ("INSERT OR IGNORE INTO scheduler_publications(batch_id) "
                      'SELECT t.batch_id FROM scheduler_tasks t JOIN research_batches b ON b.batch_id=t.batch_id '
                      "WHERE t.state IN ('paused','failed') AND b.result IS NULL;\n")
        try:
            self.store.connection.executescript('BEGIN IMMEDIATE;\n' + before + SCHEMA + after +
                "INSERT INTO metadata VALUES('scheduler_schema','scheduler.v5') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value;\nCOMMIT;")
        except sqlite3.Error:
            self.store.connection.rollback()
            raise

    @contextmanager
    def _lock(self):
        fd = os.open(str(self.store.root / 'scheduler.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _blocked(self):
        """Unsettled provider usage or a paused ledger stops new paid work."""
        row = self.store.connection.execute('SELECT paused FROM resource_policy WHERE policy_id=1').fetchone()
        blocking = self.store.connection.execute(
            'SELECT COUNT(*) FROM model_calls WHERE state IN (?,?,?)', BLOCKING_STATES).fetchone()[0]
        return bool(blocking) or row is None or bool(row[0])

    def configure(self, template_id, *, interval_seconds, check_seconds):
        integer(interval_seconds, 1, 2**31 - 1)
        integer(check_seconds, 1, interval_seconds)
        _, template = self.research.batch(template_id)
        if template['submissions'] or template['configuration'] != self.research.configuration():
            raise InputError('Scheduling requires a current submission-free batch template.')
        with self._lock() as held:
            if not held:
                raise ConflictError('Scheduler is working.')
            with self.store.transaction():
                if self.store.connection.execute("SELECT 1 FROM scheduler_tasks WHERE state='pending'").fetchone():
                    raise ConflictError('Finish the pending task before changing its schedule.')
                self.store.connection.execute(
                    'INSERT INTO scheduler_policy(policy_id,template_id,interval_seconds,check_seconds,'
                    'enabled,next_due,pause_reason,interval_revision) VALUES(1,?,?,?,0,?,NULL,1) '
                    'ON CONFLICT(policy_id) DO UPDATE SET template_id=excluded.template_id,'
                    'interval_seconds=excluded.interval_seconds,check_seconds=excluded.check_seconds,'
                    'enabled=0,next_due=excluded.next_due,pause_reason=NULL,'
                    'interval_revision=scheduler_policy.interval_revision+1',
                    (template_id, interval_seconds, check_seconds, self.store.clock()))

    def set_interval(self, interval_seconds, *, enable=False):
        """Change the rhythm without clearing a fault; resume only a normally paused schedule."""
        integer(interval_seconds, 1, 2**31 - 1)
        if type(enable) is not bool:
            raise InputError('Schedule resume must be explicit.')
        now = self.store.clock()
        with self.store.transaction():
            row = self.store.connection.execute('SELECT * FROM scheduler_policy WHERE policy_id=1').fetchone()
            if row is None:
                raise InputError('Configure the scheduler before changing its frequency.')
            due = now + interval_seconds
            blocked_reason = None
            if enable and not row['enabled']:
                if row['pause_reason'] is not None:
                    blocked_reason = 'recorded_fault'
                elif self._blocked():
                    blocked_reason = 'resource_blocked'
            resume = enable and not row['enabled'] and blocked_reason is None
            self.store.connection.execute(
                'UPDATE scheduler_policy SET interval_seconds=?, check_seconds=?, next_due=?, enabled=?, '
                'interval_revision=interval_revision+1 WHERE policy_id=1',
                (interval_seconds, min(row['check_seconds'], interval_seconds), due,
                 int(bool(row['enabled']) or resume)))
            return {'interval_seconds': interval_seconds, 'next_due': due,
                    'enabled': bool(row['enabled']) or resume,
                    'pause_reason': row['pause_reason'], 'interval_revision': row['interval_revision'] + 1,
                    'blocked_reason': blocked_reason}

    def enable(self):
        with self.store.transaction():
            row = self.store.connection.execute('SELECT * FROM scheduler_policy WHERE policy_id=1').fetchone()
            if not row:
                raise InputError('Configure the scheduler before enabling it.')
            if self._blocked():
                raise ConflictError('Resolve the recorded provider usage and spending state first.')
            self.store.connection.execute('UPDATE scheduler_policy SET enabled=1,pause_reason=NULL WHERE policy_id=1')

    def pause(self):
        with self.store.transaction():
            self.store.connection.execute('UPDATE scheduler_policy SET enabled=0 WHERE policy_id=1')

    def _finish(self, task, result, revision):
        if result['state'] == 'worker_busy':
            return {'state': 'worker_busy', 'task_id': task['task_id']}
        if result['state'] not in TERMINAL_STATES:
            raise InputError('Unexpected research completion state.')
        if task['batch_id'] is not None and result['state'] != 'completed':
            result = self.research.finish(task['batch_id'], result['state'], result['reason'])
        with self.store.transaction():
            self.store.connection.execute('UPDATE scheduler_tasks SET state=?,result=? WHERE task_id=?',
                                          (result['state'], canonical(result), task['task_id']))
            if task['batch_id'] is not None:
                self.store.connection.execute('INSERT OR IGNORE INTO scheduler_publications(batch_id) VALUES(?)',
                                              (task['batch_id'],))
            interval = self.store.connection.execute(
                'SELECT interval_seconds FROM scheduler_policy WHERE policy_id=1').fetchone()[0]
            # A frequency change made while this round was running owns the next deadline,
            # so finishing the round must not overwrite it.
            self.store.connection.execute(
                'UPDATE scheduler_policy SET next_due=? WHERE policy_id=1 AND interval_revision=?',
                (self.store.clock() + interval, revision))
            if result['state'] != 'completed':
                self.store.connection.execute('UPDATE scheduler_policy SET enabled=0,pause_reason=? WHERE policy_id=1',
                                              (result['reason'],))
        return {'state': result['state'], 'task_id': task['task_id'], 'batch_id': task['batch_id'],
                'source': 'player' if task['source_id'] else 'background'}

    def _release(self, batch_id, state):
        """Project a finished round; a projection failure never re-runs research or re-bills."""
        if self.publisher is None or batch_id is None or state not in TERMINAL_STATES:
            return False
        try:
            batch, _ = self.research.batch(batch_id)
            if batch['result'] is None:
                task = self.store.connection.execute(
                    'SELECT state,result FROM scheduler_tasks WHERE batch_id=?', (batch_id,)).fetchone()
                if task is None or task['state'] not in ('paused', 'failed') or task['result'] is None:
                    raise InputError('A terminal task result is required for publication recovery.')
                result = parse_json(task['result'])
                if result.get('state') != task['state'] or not isinstance(result.get('reason'), str):
                    raise InputError('The terminal task result is inconsistent.')
                with self.store.worker_lock() as held:
                    if not held:
                        raise ConflictError('Research is working.')
                    self.research.finish(batch_id, result['state'], result['reason'])
                batch, _ = self.research.batch(batch_id)
            if batch['state'] != state:
                raise ConflictError('Publication and research completion states differ.')
            report = self.publisher(batch_id)
            if report is None:
                report = {'state': 'published', 'withheld_count': 0, 'reason_categories': []}
            if (type(report) is not dict or set(report) != {'state', 'withheld_count', 'reason_categories'}
                    or report['state'] not in ('published', 'partial')):
                raise InputError('Invalid publication report.')
            integer(report['withheld_count'], 0, 1000000)
            if (type(report['reason_categories']) is not list
                    or any(type(reason) is not str or len(reason) > 80 for reason in report['reason_categories'])):
                raise InputError('Invalid publication reasons.')
            with self.store.transaction():
                self.store.connection.execute(
                    'INSERT INTO scheduler_publication_reports(batch_id,state,withheld_count,reason_categories) '
                    'VALUES(?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET state=excluded.state,'
                    'withheld_count=excluded.withheld_count,reason_categories=excluded.reason_categories',
                    (batch_id, report['state'], report['withheld_count'], canonical(report['reason_categories'])))
                self.store.connection.execute('DELETE FROM scheduler_publications WHERE batch_id=?', (batch_id,))
            return True
        except (InputError, ConflictError, CapacityError, OSError, sqlite3.Error) as exc:
            reason = {InputError: 'projection_input', ConflictError: 'projection_conflict',
                      CapacityError: 'storage_unavailable', OSError: 'io_unavailable',
                      sqlite3.Error: 'database_unavailable'}.get(type(exc), 'projection_unavailable')
            try:
                with self.store.transaction():
                    self.store.connection.execute(
                        'INSERT INTO scheduler_publication_reports(batch_id,state,withheld_count,reason_categories) '
                        "VALUES(?,'deferred',NULL,?) ON CONFLICT(batch_id) DO UPDATE SET state='deferred',"
                        'withheld_count=NULL,reason_categories=excluded.reason_categories',
                        (batch_id, canonical([reason])))
            except sqlite3.Error:
                pass
            print(canonical({'state': 'publication_deferred', 'reason': type(exc).__name__}).decode(),
                  file=sys.stderr, flush=True)
            return False

    def publication_status(self, before=None):
        if before is not None:
            integer(before, 1, 2**53 - 1)
        source = (
            'SELECT t.rowid AS position,a.public_id,p.batch_id AS pending,r.withheld_count,r.reason_categories,'
            "CASE WHEN p.batch_id IS NOT NULL THEN 'deferred' ELSE COALESCE(r.state,'unreported') END AS state "
            "FROM scheduler_tasks t JOIN research_aliases a ON a.kind='experiment' AND a.internal_id=t.batch_id "
            'LEFT JOIN scheduler_publications p ON p.batch_id=t.batch_id '
            'LEFT JOIN scheduler_publication_reports r ON r.batch_id=t.batch_id '
            "WHERE t.state IN ('completed','paused','failed')")
        summary = dict.fromkeys(('published', 'partial', 'deferred', 'unreported'), 0)
        for row in self.store.connection.execute(
                'WITH reports AS (' + source + ') SELECT state,COUNT(*) AS count FROM reports GROUP BY state'):
            summary[row['state']] = row['count']
        summary['total'] = sum(summary.values())
        rows = self.store.connection.execute(
            'WITH reports AS (' + source + ') SELECT * FROM reports '
            'WHERE (? IS NULL OR position<?) ORDER BY position DESC LIMIT 51', (before, before)).fetchall()
        result = []
        for row in rows[:50]:
            reasons = (parse_json(row['reason_categories'], 4096) if row['reason_categories'] is not None
                       else ['projection_pending'] if row['pending'] else [])
            result.append({'experiment_id': row['public_id'], 'state': row['state'],
                           'withheld_count': row['withheld_count'], 'reason_categories': reasons})
        return {'items': result, 'summary': summary, 'next_before': rows[49]['position'] if len(rows) > 50 else None}

    def catch_up(self):
        """Re-project finished batches whose publication never landed; read-only about spending."""
        if self.publisher is None:
            return 0
        try:
            rows = self.store.connection.execute(
                'SELECT p.batch_id,t.state FROM scheduler_publications p '
                'JOIN scheduler_tasks t ON t.batch_id=p.batch_id '
                "WHERE t.state IN ('completed','paused','failed') ORDER BY p.rowid LIMIT ?",
                (CATCH_UP_LIMIT,)).fetchall()
        except sqlite3.Error:
            return 0
        released = 0
        for row in rows:
            if self._release(row['batch_id'], row['state']):
                released += 1
            else:
                try:
                    with self.store.transaction():
                        self.store.connection.execute(
                            'UPDATE scheduler_publications SET rowid='
                            '(SELECT MAX(rowid)+1 FROM scheduler_publications) WHERE batch_id=?', (row['batch_id'],))
                except sqlite3.Error:
                    pass
        return released

    def tick(self):
        with self._lock() as held:
            if not held:
                return {'state': 'scheduler_busy'}
            self.catch_up()
            policy = self.store.connection.execute('SELECT * FROM scheduler_policy WHERE policy_id=1').fetchone()
            task = self.store.connection.execute("SELECT * FROM scheduler_tasks WHERE state='pending'").fetchone()
            if task is None and (not policy or not policy['enabled']):
                return {'state': 'paused'}
            revision = policy['interval_revision']
            if task is not None and task['claim_interval_revision'] is not None:
                revision = task['claim_interval_revision']
            if task is None:
                if self._blocked():
                    return {'state': 'resource_paused'}
                # One rhythm for every paid round: a queued player submission is served first
                # when the deadline arrives, but it does not get its own extra call.
                if self.store.clock() < policy['next_due']:
                    return {'state': 'waiting', 'next_due': policy['next_due']}
                _, template = self.research.batch(policy['template_id'])
                source = self.store.connection.execute(
                    'SELECT s.submission_id FROM research_submissions s WHERE s.target_id=? '
                    'AND NOT EXISTS(SELECT 1 FROM scheduler_tasks t WHERE t.source_id=s.submission_id) '
                    "AND NOT EXISTS(SELECT 1 FROM research_journal j WHERE j.kind='contribution' AND json_extract(j.body,'$.submission_id')=s.submission_id) "
                    'ORDER BY s.rowid LIMIT 1', (template['target']['version_id'],)).fetchone()
                task_id = 'schedule-' + uuid.uuid4().hex
                with self.store.transaction():
                    # The control process holds its own connection, so the claim re-checks
                    # everything that can have changed since this tick started.
                    current = self.store.connection.execute(
                        'SELECT enabled,next_due,interval_revision FROM scheduler_policy WHERE policy_id=1').fetchone()
                    if current is None or not current['enabled']:
                        return {'state': 'paused'}
                    if self._blocked():
                        return {'state': 'resource_paused'}
                    if self.store.clock() < current['next_due']:
                        return {'state': 'waiting', 'next_due': current['next_due']}
                    if self.store.connection.execute(
                            "SELECT 1 FROM scheduler_tasks WHERE state='pending'").fetchone():
                        return {'state': 'scheduler_busy'}
                    revision = current['interval_revision']
                    self.store.capacity(4096)
                    self.store.connection.execute(
                        'INSERT INTO scheduler_tasks(task_id,source_id,template_id,batch_id,state,result,'
                        "claim_interval_revision) VALUES(?,?,?,NULL,'pending',NULL,?)",
                        (task_id, source[0] if source else None, policy['template_id'], revision))
                task = self.store.connection.execute('SELECT * FROM scheduler_tasks WHERE task_id=?', (task_id,)).fetchone()
            task = dict(task)
            try:
                if task['batch_id'] is None:
                    _, template = self.research.batch(task['template_id'])
                    if template['configuration'] != self.research.configuration():
                        raise ConflictError('Scheduling template configuration changed.')
                    batch_id = self.research.freeze(task['task_id'], template['target'], template['method'],
                        search_attempts=template['search_attempts'], reduction_attempts=template['reduction_attempts'],
                        evaluation_cases=template['evaluation_cases'], submission_ids=[task['source_id']] if task['source_id'] else [])
                    with self.store.transaction():
                        self.store.connection.execute('UPDATE scheduler_tasks SET batch_id=? WHERE task_id=?', (batch_id, task['task_id']))
                    task['batch_id'] = batch_id
                # Research owns the existing worker lock and response recovery.
                result = self.research.run(task['batch_id'])
            except CapacityError:
                result = {'state': 'paused', 'reason': 'capacity_unavailable'}
            except (InputError, ConflictError, OSError):
                result = {'state': 'failed', 'reason': 'scheduler_execution_error'}
            outcome = self._finish(task, result, revision)
            if outcome['state'] != 'worker_busy':
                self._release(task['batch_id'], result['state'])
            return outcome

    def serve(self, stop):
        self.catch_up()
        while not stop.is_set():
            result = self.tick()
            if result['state'] == 'completed':
                continue
            row = self.store.connection.execute('SELECT check_seconds FROM scheduler_policy WHERE policy_id=1').fetchone()
            if row is None:
                raise InputError('Configure the scheduler before starting it.')
            if stop.wait(min(row[0], CONTROL_POLL_SECONDS)):
                break
