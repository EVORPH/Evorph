import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import secrets
import sqlite3
import uuid

from evorph.contracts import SPEC, InputError, canonical, exact, identifier, integer, parse_json
from runtime.store import ConflictError
from . import publication
from .access import credential_digest, issue_session

PRIVATE_MARKERS = ('micros', 'token_digest', 'csrf', 'api_key', 'bearer', 'authorization')
RELEASABLE = ('hypothesis', 'repair', 'verification', 'comparison', 'decision', 'contribution', 'finding', 'experiment')
TEXT_LIMITS = {'title': 240, 'summary': 2000, 'public_text': 50000}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS service_identities (
  owner_id TEXT PRIMARY KEY, recovery_digest TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS service_sessions (
  token_digest TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES service_identities,
  body BLOB NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS service_rate (
  key TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(key,window)
);
CREATE TABLE IF NOT EXISTS service_approvals (
  approval_digest TEXT PRIMARY KEY, batch_id TEXT NOT NULL, record_id TEXT NOT NULL,
  reviewer TEXT NOT NULL, approved_at REAL NOT NULL, UNIQUE(batch_id,record_id)
);
CREATE TABLE IF NOT EXISTS service_experiments (
  public_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE, body BLOB NOT NULL, published_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS service_records (
  public_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES service_experiments(batch_id),
  sequence INTEGER NOT NULL, body BLOB NOT NULL, published_at REAL NOT NULL, UNIQUE(batch_id,sequence)
);
'''


class RateLimited(RuntimeError):
    """Configured request allowance for this window is exhausted."""


class ReviewError(InputError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ServiceLimits:
    """Operator-supplied allowances; there are no built-in defaults."""
    session_seconds: int
    identity_per_window: int
    write_per_window: int
    read_per_window: int
    window_seconds: int
    page_limit: int

    def validate(self):
        for value in asdict(self).values():
            integer(value, 1, 2**31 - 1)
        if self.page_limit > 100:
            raise InputError('Public protocol allows at most 100 records per page.')
        return self


def cursor(payload, key):
    encoded = base64.urlsafe_b64encode(canonical(payload)).rstrip(b'=')
    signature = hmac.new(key, encoded, hashlib.sha256).hexdigest()
    return encoded.decode() + '.' + signature


def decode(value, keys, key):
    if value is None:
        return None
    if type(value) is not str or len(value) > 1000:
        raise InputError('Invalid pagination cursor.')
    try:
        encoded, signature = value.rsplit('.', 1)
        raw = encoded.encode('ascii')
        if not hmac.compare_digest(hmac.new(key, raw, hashlib.sha256).hexdigest(), signature):
            raise ValueError()
        decoded = base64.b64decode(raw + b'=' * (-len(raw) % 4), altchars=b'-_', validate=True)
        payload = parse_json(decoded)
    except (ValueError, TypeError, UnicodeError):
        raise InputError('Invalid pagination cursor.') from None
    exact(payload, keys)
    return payload


def review_event(event):
    """Mechanical release gate; semantic review of free text is recorded per reviewer."""
    kind, body = event['kind'], event['body']
    if kind not in RELEASABLE:
        raise ReviewError('unsupported_kind', 'This record kind has no approved public projection.')
    if kind in ('hypothesis', 'repair'):
        text = body['statement']
        if type(text) is not str or not text.strip() or len(text) > TEXT_LIMITS['public_text']:
            raise ReviewError('invalid_statement', 'Public statement is not releasable.')
        lowered = text.lower()
        if any(marker in lowered for marker in PRIVATE_MARKERS):
            raise ReviewError('private_marker', 'Public statement contains a private marker.')
    return publication.approval_digest(event)


class PublicService:
    def __init__(self, store, research, limits, *, now, mode='example'):
        limits.validate()
        if research.store is not store or mode not in ('example', 'live'):
            raise InputError('Service configuration is inconsistent.')
        self.store, self.research, self.limits, self.now, self.mode = store, research, limits, now, mode
        row = store.connection.execute("SELECT value FROM metadata WHERE key='service_schema'").fetchone()
        if row and row[0] not in ('service.v1', 'service.v2'):
            raise InputError('Unsupported service schema.')
        before, after = '', ''
        if row and row[0] == 'service.v1':
            before = 'ALTER TABLE service_records RENAME TO service_records_v1;\n'
            after = 'INSERT INTO service_records SELECT * FROM service_records_v1;\nDROP TABLE service_records_v1;\n'
        try:
            store.connection.executescript("BEGIN IMMEDIATE;\n" + before + SCHEMA + after +
                "INSERT INTO metadata VALUES('service_schema','service.v2') ON CONFLICT(key) DO UPDATE SET value=excluded.value;\nCOMMIT;")
        except sqlite3.Error:
            store.connection.rollback()
            raise InputError('Service migration could not preserve archive integrity.') from None
        with store.transaction():
            prior = store.connection.execute("SELECT value FROM metadata WHERE key='service_mode'").fetchone()
            if prior and prior[0] != mode:
                raise ConflictError('Public archive source mode is immutable.')
            store.connection.execute("INSERT OR IGNORE INTO metadata VALUES('service_mode',?)", (mode,))
            store.connection.execute("INSERT OR IGNORE INTO metadata VALUES('service_cursor_key',?)", (secrets.token_hex(32),))
            self._cursor_key = bytes.fromhex(store.connection.execute("SELECT value FROM metadata WHERE key='service_cursor_key'").fetchone()[0])

    def _capacity(self, raw):
        self.store.capacity(len(raw) * 4)

    def charge(self, key, allowance):
        integer(allowance, 1, 2**31 - 1)
        window = int(self.now() // self.limits.window_seconds)
        if type(key) is not str or not key or len(key) > 512:
            raise InputError('Invalid request source.')
        key = hashlib.sha256(key.encode()).hexdigest()
        with self.store.transaction():
            self.store.connection.execute('DELETE FROM service_rate WHERE window<?', (window,))
            count = self.store.connection.execute('SELECT count FROM service_rate WHERE key=? AND window=?', (key, window)).fetchone()
            used = 0 if count is None else count[0]
            if used >= allowance:
                raise RateLimited('Request allowance is exhausted for this window.')
            self.store.connection.execute('INSERT INTO service_rate VALUES(?,?,?) ON CONFLICT(key,window) DO UPDATE SET count=count+1',
                                          (key, window, used + 1))

    def create_identity(self, client_key):
        self.charge('identity:' + client_key, self.limits.identity_per_window)
        owner_id = 'owner-' + uuid.uuid4().hex
        credentials, session = issue_session(owner_id, now=int(self.now()), lifetime_seconds=self.limits.session_seconds)
        recovery = secrets.token_urlsafe(32)
        body = canonical(session)
        with self.store.transaction():
            self._capacity(body)
            self.store.connection.execute('INSERT INTO service_identities VALUES(?,?,?,0)',
                                          (owner_id, credential_digest(recovery), self.now()))
            self.store.connection.execute('INSERT INTO service_sessions VALUES(?,?,?,?,0)',
                                          (session['token_digest'], owner_id, body, session['expires_at']))
            contributor_id = self.research.alias('contributor', owner_id, True)
        return {'credentials': credentials, 'recovery': recovery, 'contributor_id': contributor_id}

    def session(self, token):
        row = self.store.connection.execute('SELECT s.body FROM service_sessions s JOIN service_identities i ON i.owner_id=s.owner_id '
                                            'WHERE s.token_digest=? AND s.revoked=0 AND i.revoked=0 AND s.expires_at>?',
                                            (credential_digest(token), self.now())).fetchone()
        return None if row is None else parse_json(row['body'], 4096)

    def recover_identity(self, recovery, client_key):
        self.charge('recovery:' + client_key, self.limits.identity_per_window)
        checksum = credential_digest(recovery)
        with self.store.transaction():
            identity = self.store.connection.execute('SELECT owner_id,revoked FROM service_identities WHERE recovery_digest=?', (checksum,)).fetchone()
            if identity is None or identity['revoked']:
                raise InputError('Recovery credentials are unavailable.')
            self.store.connection.execute('UPDATE service_sessions SET revoked=1 WHERE owner_id=?', (identity['owner_id'],))
            issued = issue_session(identity['owner_id'], now=int(self.now()), lifetime_seconds=self.limits.session_seconds)
            replacement = secrets.token_urlsafe(32)
            body = canonical(issued[1])
            self._capacity(body)
            self.store.connection.execute('INSERT INTO service_sessions VALUES(?,?,?,?,0)',
                                          (issued[1]['token_digest'], identity['owner_id'], body, issued[1]['expires_at']))
            self.store.connection.execute('UPDATE service_identities SET recovery_digest=? WHERE owner_id=?',
                                          (credential_digest(replacement), identity['owner_id']))
        return {'credentials': issued[0], 'recovery': replacement,
                'contributor_id': self.research.alias('contributor', identity['owner_id'])}

    def approve(self, batch_id, reviewer, reviewed):
        identifier(reviewer)
        if type(reviewed) is not dict or not reviewed:
            raise InputError('Explicit reviewed record digests are required.')
        approved = []
        with self.store.transaction():
            events = {event['record_id']: event for event in self.research.journal(batch_id)}
            if not set(reviewed) <= set(events):
                raise InputError('Reviewed records do not belong to this batch.')
            for record_id, expected in reviewed.items():
                event = events[record_id]
                checksum = review_event(event)
                if expected != checksum:
                    raise ConflictError('Reviewed record content changed.')
                prior = self.store.connection.execute('SELECT approval_digest FROM service_approvals WHERE batch_id=? AND record_id=?',
                                                      (batch_id, event['record_id'])).fetchone()
                if prior:
                    if prior[0] != checksum:
                        raise ConflictError('Approved record content changed.')
                    continue
                body = canonical({'batch_id': batch_id, 'record_id': event['record_id'], 'reviewer': reviewer})
                self._capacity(body)
                self.store.connection.execute('INSERT INTO service_approvals VALUES(?,?,?,?,?)',
                                              (checksum, batch_id, event['record_id'], reviewer, self.now()))
                approved.append(event['record_id'])
        return approved

    def approvals(self, batch_id):
        return {row[0] for row in self.store.connection.execute('SELECT approval_digest FROM service_approvals WHERE batch_id=?', (batch_id,))}

    def publish(self, batch_id):
        """Projects only approved whole records into the public archive; idempotent."""
        if self.research.batch(batch_id)[1]['mode'] != self.mode:
            raise InputError('Research source does not match this public archive.')
        published = 0
        with self.store.transaction():
            approved = self.approvals(batch_id)
            prior = self.store.connection.execute('SELECT public_id FROM service_experiments WHERE batch_id=?', (batch_id,)).fetchone()
            if prior is None:
                body = canonical(publication.experiment(self.research, batch_id))
                self._capacity(body)
                public_id = self.research.alias('experiment', batch_id)
                self.store.connection.execute('INSERT INTO service_experiments VALUES(?,?,?,?)',
                                              (public_id, batch_id, body, self.now()))
            for event in self.research.journal(batch_id):
                record = publication.record(self.research, batch_id, event, approved)
                if record is None:
                    continue
                existing = self.store.connection.execute('SELECT sequence,body,batch_id FROM service_records WHERE public_id=?',
                                                         (record['record_id'],)).fetchone()
                if existing:
                    record['sequence'] = existing['sequence']
                    if existing['batch_id'] != batch_id or existing['body'] != canonical(record):
                        raise ConflictError('Published record is immutable.')
                    continue
                # Publication order keeps late-reviewed records visible to incremental readers.
                record['sequence'] = self.store.connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM service_records WHERE batch_id=?', (batch_id,)).fetchone()[0]
                body = canonical(record)
                self._capacity(body)
                self.store.connection.execute('INSERT INTO service_records VALUES(?,?,?,?,?)',
                                              (record['record_id'], batch_id, record['sequence'], body, self.now()))
                published += 1
        return {'experiment_id': self.research.alias('experiment', batch_id), 'published': published}

    def challenge_config(self, experiment_id):
        from evorph.contracts import SPEC_DIGEST
        identifier(experiment_id)
        row = self.store.connection.execute('SELECT batch_id FROM service_experiments WHERE public_id=?', (experiment_id,)).fetchone()
        if row is None:
            raise InputError('Published experiment is unavailable.')
        snapshot = self.research.batch(row['batch_id'])[1]
        scenarios = []
        for scenario, messages in [('delayed-old-request', ['prepare', 'confirm', 'legacy-transition']),
                                   ('invalid-pq-fallback', ['prepare', 'confirm', 'invalid-pq-fallback']),
                                   ('replayed-request', ['prepare', 'confirm', 'pq-transition'])]:
            events = [{'kind': 'deliver', 'message_ref': name} for name in messages]
            if scenario == 'replayed-request':
                events.append({'kind': 'duplicate', 'event_index': 2})
            scenarios.append({'scenario_id': scenario, 'challenge': {'schema_version': 'challenge.v1', 'spec_digest': SPEC_DIGEST, 'events': events}})
        return {'mode': self.mode, 'experiment_id': experiment_id,
                'target_version_id': self.research.alias('version', snapshot['target']['version_id']), 'scenarios': scenarios}

    def submission_state(self, public_id):
        row = self.store.connection.execute("SELECT b.state FROM research_journal j JOIN research_batches b ON b.batch_id=j.batch_id "
            "JOIN research_aliases a ON a.kind='submission' AND a.public_id=? AND a.internal_id=json_extract(j.body,'$.submission_id') "
            "WHERE j.kind='contribution' ORDER BY j.rowid DESC LIMIT 1", (public_id,)).fetchone()
        return 'queued' if row is None else row['state']

    def refresh_experiment(self, batch_id):
        body = canonical(publication.experiment(self.research, batch_id))
        with self.store.transaction():
            self.store.connection.execute('UPDATE service_experiments SET body=? WHERE batch_id=?', (body, batch_id))

    def list_experiments(self, raw_cursor=None, limit=None):
        limit = self.limits.page_limit if limit is None else limit
        integer(limit, 1, self.limits.page_limit)
        position = decode(raw_cursor, {'kind', 'sequence'}, self._cursor_key)
        floor = 0
        if position is not None:
            if position['kind'] != 'experiments':
                raise InputError('Cursor does not belong to this query.')
            integer(position['sequence'], 0, 2**53 - 1)
            floor = position['sequence']
        rows = self.store.connection.execute('SELECT rowid AS archive_sequence,body FROM service_experiments WHERE rowid>? ORDER BY rowid LIMIT ?',
                                             (floor, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = cursor({'kind': 'experiments', 'sequence': page[-1]['archive_sequence']}, self._cursor_key) if len(rows) > limit else None
        return {'schema_version': publication.SCHEMA_VERSION, 'mode': self.mode,
                'experiments': [parse_json(row['body'], 65536) for row in page], 'next_cursor': next_cursor}

    def list_records(self, experiment_id, raw_cursor=None, after_sequence=None, limit=None):
        limit = self.limits.page_limit if limit is None else limit
        integer(limit, 1, self.limits.page_limit)
        identifier(experiment_id)
        row = self.store.connection.execute('SELECT * FROM service_experiments WHERE public_id=?', (experiment_id,)).fetchone()
        if row is None:
            raise InputError('Unknown public experiment.')
        position = decode(raw_cursor, {'kind', 'experiment_id', 'sequence'}, self._cursor_key)
        if position is not None and after_sequence is not None:
            raise InputError('Cursor and incremental position cannot be combined.')
        floor = 0
        if position is not None:
            if position['kind'] != 'records' or position['experiment_id'] != experiment_id:
                raise InputError('Cursor does not belong to this experiment.')
            integer(position['sequence'], 0, 2**53 - 1)
            floor = position['sequence']
        if after_sequence is not None:
            integer(after_sequence, 0, 2**53 - 1)
            floor = after_sequence
        rows = self.store.connection.execute('SELECT body FROM service_records WHERE batch_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                                             (row['batch_id'], floor, limit + 1)).fetchall()
        records = [parse_json(item['body'], 1048576) for item in rows[:limit]]
        next_cursor = cursor({'kind': 'records', 'experiment_id': experiment_id, 'sequence': records[-1]['sequence']}, self._cursor_key) if len(rows) > limit else None
        return {'schema_version': publication.SCHEMA_VERSION, 'mode': self.mode,
                'experiment': parse_json(row['body'], 65536), 'records': records, 'next_cursor': next_cursor}
