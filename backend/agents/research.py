from dataclasses import asdict
import uuid

from evorph import candidates
from evorph.contracts import (MUTATIONS, PUBLIC_MESSAGES, SPEC, SPEC_DIGEST, ExecutionError,
                             InputError, canonical, digest, exact, identifier, integer,
                             parse_json, unb64, validate_plan)
from evorph.evidence import read_file
from evorph.fixtures import build
from runtime.store import CapacityError, ConflictError
from runtime.worker import execute_claimed
from .channel import MAX_RESPONSE_BYTES
from .fixtures import ScriptedTransport
from .openrouter import OpenRouterTransport
from . import strategy as strategies

MODES = ('example', 'live')
REVIEWED_TRANSPORTS = (ScriptedTransport, OpenRouterTransport)


def verification_scope():
    return ('The verifier only accepts these {} message kinds for confirmation or transition steps: {}; '
            'and only these {} mutation targets: {}. Any other kind is rejected as invalid.').format(
                len(PUBLIC_MESSAGES), ', '.join(sorted(PUBLIC_MESSAGES)),
                len(MUTATIONS), ', '.join(sorted(MUTATIONS)))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS research_batches (
 batch_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, snapshot BLOB NOT NULL,
 snapshot_digest TEXT NOT NULL, state TEXT NOT NULL, bundle BLOB, bundle_digest TEXT, result BLOB
);
CREATE TABLE IF NOT EXISTS research_calls (
 batch_id TEXT NOT NULL REFERENCES research_batches, role TEXT NOT NULL,
 input_json BLOB NOT NULL, call_id TEXT, outcome BLOB, PRIMARY KEY(batch_id,role)
);
CREATE TABLE IF NOT EXISTS research_journal (
 batch_id TEXT NOT NULL REFERENCES research_batches, step_key TEXT NOT NULL,
 record_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL, kind TEXT NOT NULL, body BLOB NOT NULL,
 PRIMARY KEY(batch_id,step_key), UNIQUE(batch_id,sequence)
);
CREATE TABLE IF NOT EXISTS research_methods (
 version_id TEXT PRIMARY KEY, parent_id TEXT, body BLOB NOT NULL, body_digest TEXT NOT NULL,
 state TEXT NOT NULL, source_batch TEXT REFERENCES research_batches
);
CREATE TABLE IF NOT EXISTS research_submissions (
 submission_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_key TEXT NOT NULL,
 target_id TEXT NOT NULL, challenge BLOB NOT NULL, UNIQUE(owner_id,request_key)
);
CREATE TABLE IF NOT EXISTS research_issues (
 issue_id TEXT PRIMARY KEY, issue_key TEXT NOT NULL UNIQUE, batch_id TEXT NOT NULL REFERENCES research_batches,
 record_id TEXT NOT NULL REFERENCES research_journal(record_id), original_submission_id TEXT REFERENCES research_submissions
);
CREATE TABLE IF NOT EXISTS research_aliases (
 kind TEXT NOT NULL, internal_id TEXT NOT NULL, public_id TEXT NOT NULL UNIQUE, PRIMARY KEY(kind,internal_id)
);
'''


class ResearchStopped(RuntimeError):
    def __init__(self, reason, state='paused'):
        super().__init__(reason)
        self.reason, self.state = reason, state


def fixed_cases():
    def plan(*names):
        return {'schema_version': 'challenge.v1', 'spec_digest': SPEC_DIGEST,
                'events': [{'kind': 'deliver', 'message_ref': name} for name in names]}
    return [plan('legacy-transition', 'prepare', 'confirm', 'pq-transition'),
            plan('prepare', 'confirm', 'pq-transition', 'legacy-transition', 'invalid-pq-fallback')]


def finding_key(challenge, policy):
    return digest(canonical({'spec': SPEC_DIGEST, 'target': digest(canonical(policy)), 'challenge': challenge}))


def divergence(trace):
    for event in trace['events']:
        if not event['matches_reference']:
            return {'action': event['action'],
                    'reference_accepted': event['reference']['accepted'],
                    'candidate_accepted': event['candidate']['accepted'],
                    'reference_phase': event['reference']['state']['phase'],
                    'candidate_phase': event['candidate']['state']['phase'],
                    'reference_epoch': event['reference']['state']['epoch'],
                    'candidate_epoch': event['candidate']['state']['epoch'],
                    'reference_key': event['reference']['state']['active_key'],
                    'candidate_key': event['candidate']['state']['active_key']}
    return None


class Research:
    def __init__(self, store, seeker, keeper, binary=None, mode='example'):
        if seeker.accounting.store is not store or keeper.accounting.store is not store:
            raise InputError('Research channels must share the execution ledger.')
        if mode not in MODES:
            raise InputError('Unknown research source mode.')
        # Only the two reviewed transports may reach this integration, both roles have to
        # agree, and a live archive may only be produced by live channels.
        for channel in (seeker, keeper):
            if type(channel.transport) not in REVIEWED_TRANSPORTS:
                raise InputError('This research integration accepts only the reviewed transports.')
        live = type(seeker.transport) is OpenRouterTransport
        if live is not (type(keeper.transport) is OpenRouterTransport):
            raise InputError('Research roles must use the same reviewed transport.')
        if live is not (mode == 'live'):
            raise InputError('Research source mode does not match its channels.')
        self.store, self.channels, self.binary = store, {'seeker': seeker, 'keeper': keeper}, binary
        self.mode = mode
        row = store.connection.execute("SELECT value FROM metadata WHERE key='research_schema'").fetchone()
        if row and row[0] != 'research.v1':
            raise InputError('Unsupported research schema.')
        store.connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA +
                                       "INSERT OR IGNORE INTO metadata VALUES('research_schema','research.v1');\nCOMMIT;")

    def alias(self, kind, internal_id, create=False):
        row = self.store.connection.execute('SELECT public_id FROM research_aliases WHERE kind=? AND internal_id=?',
                                            (kind, internal_id)).fetchone()
        if row:
            return row[0]
        if not create:
            raise InputError('Public reference is unavailable.')
        public_id = kind + '-' + uuid.uuid4().hex
        self.store.connection.execute('INSERT INTO research_aliases VALUES(?,?,?)', (kind, internal_id, public_id))
        return public_id

    def register_submission(self, owner_id, request_key, challenge, target_id):
        for value in (owner_id, request_key, target_id):
            identifier(value)
        validate_plan(challenge)
        raw = canonical(challenge)
        with self.store.transaction():
            if not self.store.connection.execute('SELECT 1 FROM versions WHERE version_id=?', (target_id,)).fetchone():
                raise InputError('Submission target is unavailable.')
            prior = self.store.connection.execute('SELECT * FROM research_submissions WHERE owner_id=? AND request_key=?',
                                                   (owner_id, request_key)).fetchone()
            if prior:
                if prior['challenge'] != raw or prior['target_id'] != target_id:
                    raise ConflictError('Submission key belongs to different input.')
                return prior['submission_id']
            self.store.capacity(len(raw) * 4)
            submission_id = 'submission-' + uuid.uuid4().hex
            self.store.connection.execute('INSERT INTO research_submissions VALUES(?,?,?,?,?)',
                                           (submission_id, owner_id, request_key, target_id, raw))
            for kind, value in [('submission', submission_id), ('contributor', owner_id)]:
                self.alias(kind, value, True)
            return submission_id

    def submission(self, submission_id, owner_id):
        row = self.store.connection.execute('SELECT * FROM research_submissions WHERE submission_id=? AND owner_id=?',
                                            (submission_id, owner_id)).fetchone()
        if row is None:
            raise InputError('Submission is unavailable.')
        return {'submission_id': row['submission_id'], 'target_id': row['target_id'],
                'challenge': parse_json(row['challenge'])}

    def configuration(self):
        return {'channels': {role: asdict(channel.spec) for role, channel in self.channels.items()},
                'execution_limits': asdict(self.store.limits), 'binary': self.binary}

    def freeze(self, request_key, target, method, *, search_attempts, reduction_attempts,
               evaluation_cases, submission_ids=()):
        identifier(request_key)
        candidates.validate(target)
        strategies.validate(method)
        integer(search_attempts, 1, len(PUBLIC_MESSAGES) * (1 + len(MUTATIONS)))
        integer(reduction_attempts, 0, SPEC['limits']['events'])
        if type(evaluation_cases) is not list or not 2 <= len(evaluation_cases) <= 50:
            raise InputError('Evaluation cases exceed the public comparison contract.')
        for case in evaluation_cases:
            validate_plan(case)
        checksums = {digest(canonical(case)) for case in evaluation_cases}
        if len(checksums) != len(evaluation_cases) or any(digest(canonical(case)) not in checksums for case in fixed_cases()):
            raise InputError('Evaluation must retain the fixed normal and retirement cases.')
        if len(submission_ids) > 100 or len(set(submission_ids)) != len(submission_ids):
            raise InputError('Invalid submission selection.')
        submitted = []
        for submission_id in submission_ids:
            identifier(submission_id)
            row = self.store.connection.execute('SELECT * FROM research_submissions WHERE submission_id=?', (submission_id,)).fetchone()
            if not row or row['target_id'] != target['version_id']:
                raise InputError('Submission does not target this frozen version.')
            submitted.append({'submission_id': row['submission_id'], 'owner_id': row['owner_id'],
                              'challenge': parse_json(row['challenge'])})
        snapshot = {'spec_digest': SPEC_DIGEST, 'target': target, 'method': method,
                    'search_attempts': search_attempts, 'reduction_attempts': reduction_attempts,
                    'evaluation_cases': evaluation_cases, 'test_set_version': 'tests-' + digest(canonical(evaluation_cases)),
                    'test_exposure': 'public_calibration', 'submissions': submitted,
                    'configuration': self.configuration(), 'mode': self.mode}
        raw = canonical(snapshot)
        if len(raw) > SPEC['limits']['artifact_bytes']:
            raise InputError('Frozen batch exceeds size limit.')
        with self.store.transaction():
            prior = self.store.connection.execute('SELECT batch_id,snapshot_digest FROM research_batches WHERE request_key=?', (request_key,)).fetchone()
            if prior:
                if prior['snapshot_digest'] != digest(raw):
                    raise ConflictError('Batch key belongs to different frozen input.')
                return prior['batch_id']
            current = self.store.connection.execute('SELECT content_digest FROM versions WHERE version_id=?', (target['version_id'],)).fetchone()
            if current and current[0] != digest(canonical(target)):
                raise ConflictError('Target version is immutable.')
            if (target['version_id'] == method['version_id']
                    or self.store.connection.execute('SELECT 1 FROM research_methods WHERE version_id=?', (target['version_id'],)).fetchone()
                    or self.store.connection.execute('SELECT 1 FROM versions WHERE version_id=?', (method['version_id'],)).fetchone()):
                raise ConflictError('Version identifier is already assigned to another artifact kind.')
            if target['parent_id'] is not None and not self.store.connection.execute('SELECT 1 FROM versions WHERE version_id=?', (target['parent_id'],)).fetchone():
                raise InputError('Target parent version is unavailable.')
            self.store.capacity(len(raw) * 4)
            self.store.connection.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?)',
                                          (target['version_id'], target['parent_id'], digest(canonical(target)), canonical(target)))
            previous = self.store.connection.execute('SELECT * FROM research_methods WHERE version_id=?', (method['version_id'],)).fetchone()
            if previous:
                if previous['body'] != canonical(method) or previous['state'] not in ('seed', 'retained'):
                    raise ConflictError('Search method is not an accepted immutable version.')
            else:
                if method != strategies.baseline():
                    raise InputError('Initial research requires the fixed baseline method.')
                self.store.connection.execute('INSERT INTO research_methods VALUES(?,?,?,?,?,NULL)',
                                              (method['version_id'], None, canonical(method), digest(canonical(method)), 'seed'))
            batch_id = 'batch-' + uuid.uuid4().hex
            self.store.connection.execute("INSERT INTO research_batches VALUES(?,?,?,?,'queued',NULL,NULL,NULL)",
                                          (batch_id, request_key, raw, digest(raw)))
            for kind, value in [('experiment', batch_id), ('version', target['version_id']), ('version', method['version_id']),
                                ('profile', batch_id), ('model', batch_id), ('tests', snapshot['test_set_version'])]:
                self.alias(kind, value, True)
            return batch_id

    def batch(self, batch_id):
        row = self.store.connection.execute('SELECT * FROM research_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not row or digest(row['snapshot']) != row['snapshot_digest']:
            raise InputError('Frozen research batch is unavailable or inconsistent.')
        return dict(row), parse_json(row['snapshot'], SPEC['limits']['artifact_bytes'])

    def journal(self, batch_id):
        return [{'batch_id': batch_id, 'record_id': row['record_id'], 'sequence': row['sequence'], 'kind': row['kind'],
                 'body': parse_json(row['body'], SPEC['limits']['artifact_bytes'])}
                for row in self.store.connection.execute('SELECT * FROM research_journal WHERE batch_id=? ORDER BY sequence', (batch_id,))]

    def record(self, batch_id, step_key, kind, body):
        with self.store.transaction():
            return self._record(batch_id, step_key, kind, body)

    def _record(self, batch_id, step_key, kind, body):
        raw = canonical(body)
        if len(raw) > SPEC['limits']['artifact_bytes']:
            raise InputError('Research record exceeds size limit.')
        prior = self.store.connection.execute('SELECT record_id,kind,body FROM research_journal WHERE batch_id=? AND step_key=?', (batch_id, step_key)).fetchone()
        if prior:
            if prior['kind'] != kind or prior['body'] != raw:
                raise ConflictError('Research record is immutable.')
            return prior['record_id']
        self.store.capacity(len(raw) * 4)
        record_id = 'record-' + uuid.uuid4().hex
        sequence = self.store.connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM research_journal WHERE batch_id=?', (batch_id,)).fetchone()[0]
        self.store.connection.execute('INSERT INTO research_journal VALUES(?,?,?,?,?,?)', (batch_id, step_key, record_id, sequence, kind, raw))
        self.alias('record', record_id, True)
        if kind in ('finding', 'contribution'):
            self.alias('contribution', record_id, True)
        if kind == 'comparison':
            self.alias('tests', body['test_set'], True)
        return record_id

    @staticmethod
    def _settled(channel, cached):
        call_id = cached.get('call_id')
        if type(call_id) is not str or not call_id:
            return False
        try:
            return channel.accounting.get(call_id)['state'] == 'settled'
        except InputError:
            return False

    def _call(self, batch_id, role, snapshot, public_evidence):
        if snapshot['configuration'] != self.configuration():
            raise ConflictError('Frozen execution configuration changed.')
        target = snapshot['target']
        segments = [{'name': 'task_rules', 'text': canonical({'role': role, 'spec': SPEC,
                     'verification_scope': verification_scope(),
                     'output': {'body_schema': 'challenge.v1' if role == 'seeker' else 'policy.v1',
                                'statement': 'Intentionally public statement, separate from private reasoning.',
                                'optional_strategy_schema': 'search-strategy.v1' if role == 'seeker' else None}}).decode()},
                    {'name': 'public_evidence', 'text': canonical({'target': target, 'observations': public_evidence}).decode()},
                    {'name': 'approved_history', 'text': canonical({'accepted_search_method': snapshot['method']}).decode()}]
        raw = canonical(segments)
        channel = self.channels[role]
        with self.store.transaction():
            prior = self.store.connection.execute('SELECT * FROM research_calls WHERE batch_id=? AND role=?', (batch_id, role)).fetchone()
            if prior:
                if prior['input_json'] != raw:
                    raise ConflictError('Frozen model input changed.')
                if prior['outcome'] is not None:
                    cached = parse_json(prior['outcome'], SPEC['limits']['artifact_bytes'])
                    # A cached unconfirmed outcome is not final: reconciliation may have settled
                    # the same call afterwards, and the stored provider evidence is enough to
                    # classify it again without sending anything.
                    if cached.get('state') == 'settled' or not self._settled(channel, cached):
                        return cached
            else:
                self.store.capacity(len(raw) * 4)
                self.store.connection.execute('INSERT INTO research_calls VALUES(?,?,?,NULL,NULL)', (batch_id, role, raw))
        parent = target['version_id'] if role == 'keeper' else None
        result = channel.request(role, batch_id, segments, expected_parent=parent)
        if result.kind == 'duplicate' and result.state in ('settled', 'response_saved'):
            row = channel.accounting.get(result.call_id)
            saved = parse_json(row['response_json'], 1048576)
            if saved.get('kind') == 'channel-response.v1' and saved.get('complete') is True:
                response = unb64(saved['body_b64'], MAX_RESPONSE_BYTES)
                if digest(response) != saved['body_digest']:
                    raise InputError('Stored model response is inconsistent.')
                result = channel.classify(result.call_id, response, role, parent, result.input_measurement,
                                          result.input_bound, parse_json(row['request_json']))
        outcome = {'kind': result.kind, 'call_id': result.call_id, 'state': result.state, 'payload': result.payload}
        body = canonical(outcome)
        with self.store.transaction():
            self.store.capacity(len(body) * 4)
            self.store.connection.execute('UPDATE research_calls SET call_id=?,outcome=? WHERE batch_id=? AND role=?',
                                          (result.call_id or None, body, batch_id, role))
        return outcome

    def _test(self, batch_id, key, challenge, policy, bundle):
        if self.batch(batch_id)[1]['configuration'] != self.configuration():
            raise ConflictError('Frozen execution configuration changed.')
        job_id = self.store.submit(challenge, policy, batch_id, 'test-' + digest(canonical({'batch': batch_id, 'key': key})))
        job = self.store.get_job(job_id)
        if job['state'] == 'queued':
            claimed = self.store.claim(job_id)
            if claimed is None:
                raise ResearchStopped('execution_unavailable')
            execute_claimed(self.store, claimed, self.binary, bundle)
            job = self.store.get_job(job_id)
        if job['state'] != 'completed':
            raise ResearchStopped('execution_error', 'failed')
        if self.batch(batch_id)[1]['configuration'] != self.configuration():
            raise ConflictError('Frozen execution configuration changed.')
        attempt = self.store.connection.execute('SELECT artifact_id FROM attempts WHERE run_id=?', (job['result_run_id'],)).fetchone()
        packed = read_file(self.store.root / 'objects', attempt['artifact_id'] + '.blob', self.store.limits.artifact_bytes)
        if digest(packed) != attempt['artifact_id']:
            raise InputError('Research artifact is inconsistent.')
        files = parse_json(packed, self.store.limits.artifact_bytes)
        trace = parse_json(unb64(files['trace.json'], SPEC['limits']['artifact_bytes']), SPEC['limits']['artifact_bytes'])
        if (trace['plan_digest'] != digest(canonical(challenge)) or trace['candidate_digest'] != digest(canonical(policy))
                or trace['fixture_digest'] != digest(canonical(bundle)) or trace['spec_digest'] != SPEC_DIGEST):
            raise InputError('Research trace is not bound to this test.')
        with self.store.transaction():
            self.alias('test', job_id, True)
            self.alias('evidence', attempt['artifact_id'], True)
            self.alias('version', policy['version_id'], True)
        body = {'job_id': job_id, 'run_id': job['result_run_id'], 'artifact_id': attempt['artifact_id'],
                'policy_id': policy['version_id'], 'parent_id': policy['parent_id'],
                'matches_reference': trace['matches_reference'], 'divergence': divergence(trace)}
        record_id = self.record(batch_id, 'verify-' + key, 'verification', body)
        return {'record_id': record_id, 'trace': trace, 'body': body, 'challenge': challenge}

    def _discovery(self, batch_id, result, target, submission=None):
        key = finding_key(result['challenge'], target)
        with self.store.transaction():
            prior = self.store.connection.execute('SELECT * FROM research_issues WHERE issue_key=?', (key,)).fetchone()
            if prior:
                return prior['issue_id'], prior['batch_id'] == batch_id and prior['record_id'] == result['record_id']
            issue_id = 'issue-' + uuid.uuid4().hex
            self.store.connection.execute('INSERT INTO research_issues VALUES(?,?,?,?,?)',
                                          (issue_id, key, batch_id, result['record_id'], submission))
            self.alias('issue', issue_id, True)
        return issue_id, True

    def run(self, batch_id):
        with self.store.worker_lock() as held:
            if not held:
                return {'state': 'worker_busy'}
            row, snapshot = self.batch(batch_id)
            if row['result'] is not None:
                return parse_json(row['result'])
            stopped = self.store.connection.execute(
                "SELECT body FROM research_journal WHERE batch_id=? AND step_key='stopped' AND kind='experiment'",
                (batch_id,)).fetchone()
            if stopped is not None:
                interruption = parse_json(stopped['body'])
                return self.finish(batch_id, interruption['state'], interruption['reason'])
            if snapshot['configuration'] != self.configuration():
                raise ConflictError('Frozen execution configuration changed.')
            self.store.integrity()
            self.store.recover()
            with self.store.transaction():
                self.store.connection.execute("UPDATE research_batches SET state='running' WHERE batch_id=?", (batch_id,))
            try:
                if row['bundle'] is None:
                    bundle, _ = build(self.binary)
                    raw = canonical(bundle)
                    with self.store.transaction():
                        self.store.capacity(len(raw) * 4)
                        self.store.connection.execute('UPDATE research_batches SET bundle=?,bundle_digest=? WHERE batch_id=?', (raw, digest(raw), batch_id))
                else:
                    if digest(row['bundle']) != row['bundle_digest']:
                        raise InputError('Frozen fixtures are inconsistent.')
                    bundle = parse_json(row['bundle'], SPEC['limits']['bundle_bytes'])
                result = self._run(batch_id, snapshot, bundle)
            except ResearchStopped as exc:
                result = {'batch_id': batch_id, 'state': exc.state, 'reason': exc.reason}
            except CapacityError:
                result = {'batch_id': batch_id, 'state': 'paused', 'reason': 'capacity_unavailable'}
            except (ExecutionError, InputError, OSError):
                result = {'batch_id': batch_id, 'state': 'failed', 'reason': 'execution_error'}
            if result['state'] != 'completed':
                return self.finish(batch_id, result['state'], result['reason'])
            with self.store.transaction():
                self.store.connection.execute('UPDATE research_batches SET state=?,result=? WHERE batch_id=?',
                                              (result['state'], canonical(result), batch_id))
            return result

    def finish(self, batch_id, state, reason):
        """Record an interrupted round without replacing an existing terminal result."""
        if state not in ('paused', 'failed') or type(reason) is not str or not reason:
            raise InputError('Invalid research interruption.')
        with self.store.transaction():
            row, _ = self.batch(batch_id)
            if row['result'] is not None:
                existing = parse_json(row['result'])
                if existing.get('state') != state or existing.get('reason') != reason:
                    raise ConflictError('Research already has a different terminal result.')
                return existing
            result = {'batch_id': batch_id, 'state': state, 'reason': reason}
            self._record(batch_id, 'stopped', 'experiment', {'state': state, 'reason': reason})
            self.store.connection.execute('UPDATE research_batches SET state=?,result=? WHERE batch_id=?',
                                          (state, canonical(result), batch_id))
        return result

    def _run(self, batch_id, snapshot, bundle):
        target, method = snapshot['target'], snapshot['method']
        sources = []
        for index, submission in enumerate(snapshot['submissions']):
            origin = self.record(batch_id, 'submission-' + str(index), 'contribution', submission)
            checked = self._test(batch_id, 'submission-' + str(index), submission['challenge'], target, bundle)
            sources.append((checked, submission['submission_id'], origin))
        seeker = self._call(batch_id, 'seeker', snapshot, [])
        if seeker['kind'] != 'accepted':
            raise ResearchStopped('seeker_' + seeker['kind'])
        proposal = seeker['payload']
        proposed_method = proposal.get('strategy')
        if proposed_method is not None:
            if proposed_method['parent_id'] != method['version_id']:
                raise ResearchStopped('strategy_parent_mismatch', 'failed')
            with self.store.transaction():
                prior = self.store.connection.execute('SELECT body,source_batch FROM research_methods WHERE version_id=?', (proposed_method['version_id'],)).fetchone()
                if prior and (prior['body'] != canonical(proposed_method) or prior['source_batch'] != batch_id):
                    raise ConflictError('Search method version is immutable.')
                if self.store.connection.execute('SELECT 1 FROM versions WHERE version_id=?', (proposed_method['version_id'],)).fetchone():
                    raise ConflictError('Version identifier is already assigned to another artifact kind.')
                self.store.connection.execute('INSERT OR IGNORE INTO research_methods VALUES(?,?,?,?,?,?)',
                                              (proposed_method['version_id'], method['version_id'], canonical(proposed_method),
                                               digest(canonical(proposed_method)), 'proposed', batch_id))
                self.alias('version', proposed_method['version_id'], True)
        hypothesis = self.record(batch_id, 'seeker', 'hypothesis',
                                 {'call_id': seeker['call_id'], 'statement': proposal['statement'], 'challenge': proposal['body']})
        checked = self._test(batch_id, 'seeker', proposal['body'], target, bundle)
        sources.append((checked, None, hypothesis))
        baseline_search = [self._test(batch_id, 'search-parent-' + str(index), challenge, target, bundle)
                           for index, challenge in enumerate(strategies.generate(method, snapshot['search_attempts']))]
        sources.extend((result, None, hypothesis) for result in baseline_search)
        method_decision = 'unchanged'
        if proposed_method is not None:
            proposed_search = [self._test(batch_id, 'search-child-' + str(index), challenge, target, bundle)
                               for index, challenge in enumerate(strategies.generate(proposed_method, snapshot['search_attempts']))]
            sources.extend((result, None, hypothesis) for result in proposed_search)
            def score(results):
                return len({finding_key(result['challenge'], target) for result in results if not result['trace']['matches_reference']})
            before, after = score(baseline_search), score(proposed_search)
            matched = self._matched(baseline_search + proposed_search)
            improved = matched and after > before
            method_decision = 'retained' if improved else 'rejected'
            comparison = self.record(batch_id, 'strategy-comparison', 'comparison',
                                     {'baseline_id': method['version_id'], 'candidate_id': proposed_method['version_id'],
                                      'test_set': 'search-suite-' + digest(canonical({'target': target, 'generator': 'search.v1'})),
                                      'metric': 'verified_traces',
                                      'baseline': before, 'candidate': after, 'direction': 'higher', 'matched': matched,
                                      'conclusion': 'incomparable' if not matched else 'improved' if after > before else 'regressed' if after < before else 'unchanged',
                                      'supporting': [result['record_id'] for result in baseline_search + proposed_search]})
            self.record(batch_id, 'strategy-decision', 'decision', {'outcome': method_decision, 'candidate_id': proposed_method['version_id'],
                         'parent_id': method['version_id'], 'supporting': [comparison], 'reason': 'fixed_search_comparison'})
            with self.store.transaction():
                self.store.connection.execute('UPDATE research_methods SET state=? WHERE version_id=? AND source_batch=?',
                                              (method_decision, proposed_method['version_id'], batch_id))
        failures = [(result, submission, origin) for result, submission, origin in sources if not result['trace']['matches_reference']]
        if not failures:
            self.record(batch_id, 'no-finding', 'decision', {'outcome': 'deferred', 'candidate_id': None, 'parent_id': None,
                         'supporting': [checked['record_id']], 'reason': 'no_verified_discrepancy'})
            return {'batch_id': batch_id, 'state': 'completed', 'reason': 'no_finding', 'method_decision': method_decision}
        original, submission_id, origin = failures[0]
        issue_id, finding = None, None
        for index, (observed, submitted, source_record) in enumerate(failures):
            observed_issue, is_new = self._discovery(batch_id, observed, target, submitted)
            finding_record = self.record(batch_id, 'finding-' + str(index), 'finding',
                                         {'issue_id': observed_issue, 'verification_id': observed['record_id'],
                                          'origin_record_id': source_record, 'submission_id': submitted,
                                          'new_in_archive': is_new,
                                          'contribution_kind': 'discovery' if is_new and submitted is not None else None})
            if index == 0:
                issue_id, finding = observed_issue, finding_record
        reduced = original
        for index, challenge in enumerate(strategies.reductions(original['challenge'], method['reduction_order'])):
            if index >= snapshot['reduction_attempts']:
                break
            attempt = self._test(batch_id, 'reduction-' + str(index), challenge, target, bundle)
            if (attempt['body']['divergence'] == original['body']['divergence']
                    and len(challenge['events']) < len(reduced['challenge']['events'])):
                reduced = attempt
        if reduced['record_id'] != original['record_id']:
            self.record(batch_id, 'reduction', 'finding', {'issue_id': issue_id, 'verification_id': reduced['record_id'],
                        'origin_record_id': origin, 'submission_id': submission_id, 'new_in_archive': False,
                        'contribution_kind': 'reduction' if submission_id is not None else None})
        cases = list(snapshot['evaluation_cases'])
        known = {digest(canonical(case)) for case in cases}
        for result, _, _ in failures:
            checksum = digest(canonical(result['challenge']))
            if checksum not in known:
                cases.append(result['challenge'])
                known.add(checksum)
        # Both arms share the public contract's limit of 100 verification references.
        if len(cases) > 50:
            raise ResearchStopped('evaluation_capacity', 'failed')
        self.record(batch_id, 'evaluation-set', 'evaluation_set',
                    {'cases': cases, 'digest': digest(canonical(cases))})
        keeper = self._call(batch_id, 'keeper', snapshot,
                            [{'challenge': reduced['challenge'], 'divergence': reduced['body']['divergence']}])
        if keeper['kind'] != 'accepted':
            raise ResearchStopped('keeper_' + keeper['kind'])
        child = keeper['payload']['body']
        with self.store.transaction():
            if self.store.connection.execute('SELECT 1 FROM research_methods WHERE version_id=?', (child['version_id'],)).fetchone():
                raise ConflictError('Version identifier is already assigned to another artifact kind.')
            self.alias('version', child['version_id'], True)
        repair = self.record(batch_id, 'keeper', 'repair', {'call_id': keeper['call_id'], 'statement': keeper['payload']['statement'],
                             'candidate_id': child['version_id'], 'parent_id': target['version_id'], 'reply_to': finding})
        old = [self._test(batch_id, 'evaluation-parent-' + str(index), challenge, target, bundle) for index, challenge in enumerate(cases)]
        new = [self._test(batch_id, 'evaluation-child-' + str(index), challenge, child, bundle) for index, challenge in enumerate(cases)]
        before = sum(result['trace']['matches_reference'] for result in old)
        after = sum(result['trace']['matches_reference'] for result in new)
        matched = self._matched(old + new)
        retained = matched and after == len(cases) and after > before
        comparison = self.record(batch_id, 'policy-comparison', 'comparison',
                                 {'baseline_id': target['version_id'], 'candidate_id': child['version_id'],
                                  'test_set': 'tests-' + digest(canonical(cases)), 'metric': 'matching_cases',
                                  'baseline': before, 'candidate': after, 'direction': 'higher', 'matched': matched,
                                  'conclusion': 'incomparable' if not matched else 'improved' if after > before else 'regressed' if after < before else 'unchanged',
                                  'supporting': [result['record_id'] for result in old + new]})
        outcome = 'retained' if retained else 'rejected'
        self.record(batch_id, 'policy-decision', 'decision', {'outcome': outcome, 'candidate_id': child['version_id'],
                    'parent_id': target['version_id'], 'supporting': [repair, comparison], 'reason': 'normal_and_retirement_cases'})
        return {'batch_id': batch_id, 'state': 'completed', 'reason': outcome, 'candidate_id': child['version_id'],
                'issue_id': issue_id, 'method_decision': method_decision}

    @staticmethod
    def _matched(results):
        identities = {digest(canonical({'environment': result['trace']['environment'],
                                       'fixtures': result['trace']['fixture_digest'], 'spec': result['trace']['spec_digest']}))
                      for result in results}
        return bool(results) and len(identities) == 1


def recompute_trace(store, challenge, policy, binary):
    """Re-run the fixed judging for one recorded test inside the caller's own store, so a
    third party can reproduce a published verdict offline: no network, no model call and
    no spending, only the deterministic kernel and the local signature binary."""
    candidates.validate(policy)
    validate_plan(challenge)
    bundle, _ = build(binary)
    with store.worker_lock() as held:
        if not held:
            raise ConflictError('Replay requires the exclusive worker lock.')
        job_id = store.submit(challenge, policy, 'evidence-replay',
                              'replay-' + digest(canonical({'challenge': challenge, 'policy': policy})))
        claimed = store.claim(job_id)
        if claimed is None:
            raise ConflictError('Replay requires the exclusive worker lock.')
        execute_claimed(store, claimed, binary, bundle)
    job = store.get_job(job_id)
    if job['state'] != 'completed':
        raise ExecutionError('Replay execution failed.')
    attempt = store.connection.execute('SELECT artifact_id FROM attempts WHERE run_id=?', (job['result_run_id'],)).fetchone()
    packed = read_file(store.root / 'objects', attempt['artifact_id'] + '.blob', store.limits.artifact_bytes)
    if digest(packed) != attempt['artifact_id']:
        raise InputError('Research artifact is inconsistent.')
    files = parse_json(packed, store.limits.artifact_bytes)
    return parse_json(unb64(files['trace.json'], SPEC['limits']['artifact_bytes']), SPEC['limits']['artifact_bytes'])


# Fixture ceremonies are regenerated per run by design, and cpu/rss telemetry is not a
# verdict, so replay compares exactly the fields a judgment depends on. Ceremony-bound
# values (one run's message digests and generated key digests) are pruned recursively.
VERDICT_KEYS = ('schema_version', 'spec_digest', 'plan_digest', 'candidate_digest', 'events', 'matches_reference')
CEREMONY_BOUND_KEYS = ('message_digest', 'new_key_sha256')


def _prune_ceremony(value):
    if type(value) is dict:
        return {key: _prune_ceremony(item) for key, item in value.items() if key not in CEREMONY_BOUND_KEYS}
    if type(value) is list:
        return [_prune_ceremony(item) for item in value]
    return value


def verdict_projection(trace):
    return _prune_ceremony({key: trace[key] for key in VERDICT_KEYS})
