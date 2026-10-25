from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from evorph import candidates
from evorph.contracts import SPEC, SPEC_DIGEST, InputError, canonical, digest, identifier, integer, parse_json, validate_plan
from evorph.evidence import read_file


class CapacityError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    queue_total: int
    queue_per_submitter: int
    max_attempts: int
    lease_seconds: int
    storage_bytes: int
    min_free_bytes: int
    artifact_bytes: int

    def validate(self):
        for key, value in asdict(self).items():
            integer(value, 0 if key == 'min_free_bytes' else 1, 2**53 - 1)
        if self.queue_per_submitter > self.queue_total or self.artifact_bytes > self.storage_bytes:
            raise InputError('Inconsistent resource configuration.')


SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
  version_id TEXT PRIMARY KEY, parent_id TEXT, content_digest TEXT NOT NULL, body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS specs (spec_digest TEXT PRIMARY KEY, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, request_key TEXT NOT NULL, submitter TEXT NOT NULL,
  input_digest TEXT NOT NULL, input_json BLOB NOT NULL,
  spec_digest TEXT NOT NULL REFERENCES specs, version_id TEXT NOT NULL REFERENCES versions,
  state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','paused')),
  attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until REAL,
  result_run_id TEXT, error_kind TEXT, created_at REAL NOT NULL,
  UNIQUE(submitter,request_key)
);
CREATE TABLE IF NOT EXISTS attempts (
  run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs, attempt INTEGER NOT NULL,
  claim_token TEXT NOT NULL UNIQUE, state TEXT NOT NULL, started_at REAL NOT NULL,
  ended_at REAL, artifact_id TEXT, UNIQUE(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY, size INTEGER NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL REFERENCES attempts, sequence INTEGER NOT NULL, body BLOB NOT NULL,
  PRIMARY KEY(run_id,sequence)
);
CREATE TABLE IF NOT EXISTS links (
  link_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
  body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_policy (
  policy_id INTEGER PRIMARY KEY CHECK(policy_id=1), daily_micros INTEGER NOT NULL,
  monthly_micros INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_calls (
  call_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
  request_json BLOB NOT NULL, role TEXT NOT NULL, model_id TEXT NOT NULL,
  state TEXT NOT NULL, reserved_micros INTEGER NOT NULL, actual_micros INTEGER,
  day TEXT NOT NULL, month TEXT NOT NULL, response_json BLOB, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,created_at,job_id);
'''


class Store:
    def __init__(self, root, limits, clock=time.time):
        limits.validate()
        self.root, self.limits, self.clock = Path(root), limits, clock
        if self.root.is_symlink():
            raise InputError('Expected a regular state directory.')
        self.root.mkdir(mode=0o700, exist_ok=True)
        (self.root / 'objects').mkdir(mode=0o700, exist_ok=True)
        (self.root / 'scratch').mkdir(mode=0o700, exist_ok=True)
        if any((self.root / name).is_symlink() for name in ('objects', 'scratch')):
            raise InputError('Unexpected state subdirectory.')
        for name in ('state.db', 'state.db-wal', 'state.db-shm', 'worker.lock'):
            if (self.root / name).is_symlink():
                raise InputError('Unexpected state file.')
        self.database = self.root / 'state.db'
        self.connection = sqlite3.connect(str(self.database), timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA busy_timeout=5000')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.executescript(SCHEMA)
        self._worker_locked = False
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES('schema','runtime.v1')")
            if self.connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != 'runtime.v1':
                raise InputError('Unsupported runtime schema.')
            config = canonical(asdict(limits)).decode()
            row = self.connection.execute("SELECT value FROM metadata WHERE key='limits'").fetchone()
            if row and row[0] != config:
                raise ConflictError('State resource configuration differs.')
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES('limits',?)", (config,))

    def close(self):
        self.connection.close()

    def configure_limits(self, limits):
        limits.validate()
        with self.transaction():
            self.connection.execute("UPDATE metadata SET value=? WHERE key='limits'", (canonical(asdict(limits)).decode(),))
        self.limits = limits

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise

    @contextmanager
    def worker_lock(self):
        fd = os.open(str(self.root / 'worker.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            self._worker_locked = acquired
            yield acquired
        finally:
            self._worker_locked = False
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def capacity(self, additional=0):
        total = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file() and not p.is_symlink())
        if total + additional > self.limits.storage_bytes or shutil.disk_usage(self.root).free < self.limits.min_free_bytes + additional:
            raise CapacityError('Storage capacity unavailable.')

    def submit(self, challenge, policy, submitter, request_key):
        identifier(submitter)
        identifier(request_key)
        validate_plan(challenge)
        candidates.validate(policy)
        body = canonical({'plan': challenge, 'policy': policy})
        checksum = digest(body)
        with self.transaction():
            row = self.connection.execute('SELECT job_id,input_digest FROM jobs WHERE submitter=? AND request_key=?', (submitter, request_key)).fetchone()
            if row:
                if row['input_digest'] != checksum:
                    raise ConflictError('Request key belongs to a different input.')
                return row['job_id']
            count = self.connection.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','paused')").fetchone()[0]
            own = self.connection.execute("SELECT COUNT(*) FROM jobs WHERE submitter=? AND state IN ('queued','running','paused')", (submitter,)).fetchone()[0]
            if count >= self.limits.queue_total or own >= self.limits.queue_per_submitter:
                raise CapacityError('Research queue is at capacity.')
            self.capacity(len(body) * 4)
            version_body = canonical(policy)
            version = self.connection.execute('SELECT content_digest FROM versions WHERE version_id=?', (policy['version_id'],)).fetchone()
            if version and version[0] != digest(version_body):
                raise ConflictError('Version identifier is immutable.')
            self.connection.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?)', (policy['version_id'], policy['parent_id'], digest(version_body), version_body))
            self.connection.execute('INSERT OR IGNORE INTO specs VALUES(?,?)', (SPEC_DIGEST, canonical(SPEC)))
            job_id = 'job-' + uuid.uuid4().hex
            self.connection.execute('INSERT INTO jobs(job_id,request_key,submitter,input_digest,input_json,spec_digest,version_id,state,created_at) VALUES(?,?,?,?,?,?,?,\'queued\',?)',
                                    (job_id, request_key, submitter, checksum, body, SPEC_DIGEST, policy['version_id'], self.clock()))
            return job_id

    def get_job(self, job_id):
        row = self.connection.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise InputError('Unknown research task.')
        result = dict(row)
        result['input'] = parse_json(result.pop('input_json'), 131072)
        return result

    def recover(self):
        if not self._worker_locked:
            raise ConflictError('Recovery requires the exclusive worker lock.')
        with self.transaction():
            rows = self.connection.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
            for row in rows:
                state = 'failed' if row['attempts'] >= self.limits.max_attempts else 'queued'
                self.connection.execute("UPDATE attempts SET state='interrupted',ended_at=? WHERE claim_token=? AND state='running'", (self.clock(), row['claim_token']))
                self.connection.execute('UPDATE jobs SET state=?,claim_token=NULL,lease_until=NULL,error_kind=? WHERE job_id=?', (state, 'interrupted', row['job_id']))
            self.connection.execute("UPDATE model_calls SET state='usage_unknown' WHERE state='dispatching'")
        for attempt in self.connection.execute("SELECT run_id FROM attempts WHERE state!='running'"):
            folder = self.root / 'scratch' / attempt['run_id']
            if folder.is_symlink():
                raise InputError('Unexpected task scratch path.')
            if folder.is_dir():
                shutil.rmtree(folder)
        return len(rows)

    def claim(self, job_id=None):
        if not self._worker_locked:
            raise ConflictError('Execution requires the exclusive worker lock.')
        if job_id is not None:
            identifier(job_id)
        with self.transaction():
            if self.connection.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            if job_id is None:
                row = self.connection.execute("SELECT job_id,attempts FROM jobs WHERE state='queued' ORDER BY created_at,job_id LIMIT 1").fetchone()
            else:
                row = self.connection.execute("SELECT job_id,attempts FROM jobs WHERE state='queued' AND job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            self.capacity(SPEC['limits']['artifact_bytes'])
            token, run_id = uuid.uuid4().hex, 'run-' + uuid.uuid4().hex
            self.connection.execute("UPDATE jobs SET state='running',attempts=attempts+1,claim_token=?,lease_until=?,error_kind=NULL WHERE job_id=?", (token, self.clock() + self.limits.lease_seconds, row['job_id']))
            self.connection.execute("INSERT INTO attempts(run_id,job_id,attempt,claim_token,state,started_at) VALUES(?,?,?,?,'running',?)", (run_id, row['job_id'], row['attempts'] + 1, token, self.clock()))
        job = self.get_job(row['job_id'])
        job['run_id'] = run_id
        return job

    def write_blob(self, raw):
        with self.transaction():
            return self._write_blob(raw)

    def _write_blob(self, raw):
        if len(raw) > self.limits.artifact_bytes:
            raise CapacityError('Artifact size exceeds configured capacity.')
        checksum = digest(raw)
        destination = self.root / 'objects' / (checksum + '.blob')
        if destination.is_symlink():
            raise InputError('Unexpected artifact path.')
        if destination.exists():
            if digest(read_file(destination.parent, destination.name, self.limits.artifact_bytes)) != checksum:
                raise InputError('Artifact integrity mismatch.')
            return checksum
        self.capacity(len(raw))
        temporary = destination.with_name('pending-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary), str(destination))
            directory = os.open(str(destination.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
        return checksum

    def finish(self, job, packed_evidence, events):
        artifact = self.write_blob(packed_evidence)
        with self.transaction():
            row = self.connection.execute('SELECT state,claim_token,result_run_id FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if row and row['state'] == 'completed' and row['result_run_id'] == job['run_id']:
                saved = self.connection.execute('SELECT artifact_id FROM attempts WHERE run_id=?', (job['run_id'],)).fetchone()[0]
                if saved != artifact:
                    raise ConflictError('Completed result differs.')
                return
            if not row or row['state'] != 'running' or row['claim_token'] != job['claim_token']:
                raise ConflictError('Stale task result.')
            self.connection.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?)', (artifact, len(packed_evidence), 'evidence'))
            for item in events:
                self.connection.execute('INSERT INTO events VALUES(?,?,?)', (job['run_id'], item['sequence'], canonical(item)))
            self.connection.execute("UPDATE attempts SET state='completed',ended_at=?,artifact_id=? WHERE run_id=?", (self.clock(), artifact, job['run_id']))
            self.connection.execute("UPDATE jobs SET state='completed',result_run_id=?,claim_token=NULL,lease_until=NULL WHERE job_id=?", (job['run_id'], job['job_id']))

    def fail(self, job, kind):
        identifier(kind)
        with self.transaction():
            row = self.connection.execute('SELECT state,claim_token FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if not row or row['state'] != 'running' or row['claim_token'] != job['claim_token']:
                raise ConflictError('Stale task failure.')
            self.connection.execute("UPDATE attempts SET state='failed',ended_at=? WHERE run_id=?", (self.clock(), job['run_id']))
            self.connection.execute("UPDATE jobs SET state='failed',error_kind=?,claim_token=NULL,lease_until=NULL WHERE job_id=?", (kind, job['job_id']))

    def add_link(self, link_id, kind, source_id, target_id, payload):
        for value in (link_id, source_id, target_id):
            identifier(value)
        if kind not in ('issue', 'contribution', 'reply', 'parent', 'verification'):
            raise InputError('Unknown archive relationship.')
        body = canonical(payload)
        if len(body) > 65536:
            raise InputError('Archive relationship exceeds size limit.')
        with self.transaction():
            prior = self.connection.execute('SELECT kind,source_id,target_id,body FROM links WHERE link_id=?', (link_id,)).fetchone()
            if prior:
                if tuple(prior) != (kind, source_id, target_id, body):
                    raise ConflictError('Archive relationship is immutable.')
                return
            self.capacity(len(body) * 4)
            self.connection.execute('INSERT INTO links VALUES(?,?,?,?,?)', (link_id, kind, source_id, target_id, body))

    def integrity(self):
        if self.connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise InputError('State database integrity check failed.')
        if self.connection.execute('PRAGMA foreign_key_check').fetchall():
            raise InputError('State reference check failed.')
        referenced = set()
        for row in self.connection.execute('SELECT * FROM artifacts'):
            name = row['artifact_id'] + '.blob'
            raw = read_file(self.root / 'objects', name, self.limits.artifact_bytes)
            if digest(raw) != row['artifact_id'] or len(raw) != row['size']:
                raise InputError('Published artifact integrity mismatch.')
            referenced.add(name)
        missing = self.connection.execute('SELECT run_id FROM attempts WHERE artifact_id IS NOT NULL AND artifact_id NOT IN (SELECT artifact_id FROM artifacts)').fetchall()
        if missing:
            raise InputError('Missing task artifact reference.')
        return {'artifacts': len(referenced), 'unreferenced_files': sorted(p.name for p in (self.root / 'objects').iterdir() if p.name not in referenced)}
