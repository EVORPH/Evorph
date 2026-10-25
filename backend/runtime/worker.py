from pathlib import Path
import tempfile

from evorph import evidence
from evorph.contracts import ExecutionError, InputError, SPEC, b64, canonical, parse_json, unb64
from evorph.fixtures import build
from evorph.runner import run
from .store import CapacityError, ConflictError


def pack(directory):
    return canonical({file.name: b64(evidence.read_file(directory, file.name, SPEC['limits']['artifact_bytes']))
                      for file in sorted(Path(directory).iterdir())})


def unpack(raw, directory):
    files = parse_json(raw, 16777216)
    if type(files) is not dict or len(files) > 100:
        raise InputError('Invalid archived evidence.')
    directory = Path(directory)
    directory.mkdir()
    total = 0
    for name, value in files.items():
        if type(name) is not str or Path(name).name != name or name in ('.', '..'):
            raise InputError('Invalid evidence name.')
        data = unb64(value, SPEC['limits']['artifact_bytes'])
        total += len(data)
        if total > SPEC['limits']['artifact_bytes']:
            raise InputError('Archived evidence exceeds size limit.')
        with (directory / name).open('xb') as output:
            output.write(data)
    return directory


def run_once(store, binary=None):
    with store.worker_lock() as acquired:
        if not acquired:
            return {'state': 'worker_busy'}
        store.integrity()
        store.recover()
        job = store.claim()
        if job is None:
            return {'state': 'idle'}
        return execute_claimed(store, job, binary)


def execute_claimed(store, job, binary=None, bundle=None):
    if not store._worker_locked:
        raise ConflictError('Execution requires the exclusive worker lock.')
    current = store.get_job(job['job_id'])
    attempt = store.connection.execute("SELECT run_id FROM attempts WHERE job_id=? AND claim_token=? AND state='running'",
                                       (job['job_id'], job['claim_token'])).fetchone()
    if (current['state'] != 'running' or current['claim_token'] != job['claim_token'] or not attempt
            or attempt['run_id'] != job['run_id'] or canonical(current['input']) != canonical(job['input'])):
        raise ConflictError('Stale task execution.')
    scratch = store.root / 'scratch' / job['run_id']
    scratch.mkdir(mode=0o700)
    previous_temp = tempfile.tempdir
    try:
        # Core temporary keys stay inside the recoverable attempt directory.
        tempfile.tempdir = str(scratch.resolve())
        if bundle is None:
            bundle, _ = build(binary)
        trace = run(job['input']['plan'], job['input']['policy'], bundle, binary)
        with tempfile.TemporaryDirectory(prefix='evorph-archive-') as temp:
            folder = Path(temp) / 'evidence'
            evidence.save(folder, job['input']['plan'], job['input']['policy'], bundle, trace)
            packed = pack(folder)
        store.finish(job, packed, trace['events'])
    except (ExecutionError, InputError, CapacityError, OSError) as exc:
        kind = 'storage_unavailable' if isinstance(exc, (CapacityError, OSError)) else 'execution_error'
        store.fail(job, kind)
        return {'job_id': job['job_id'], 'state': 'failed', 'error_kind': kind}
    finally:
        tempfile.tempdir = previous_temp
        import shutil
        shutil.rmtree(scratch)
    return {'job_id': job['job_id'], 'state': 'completed', 'run_id': job['run_id'],
            'matches_reference': trace['matches_reference'], 'events': len(trace['events']), 'model_calls': 0}


def replay_task(store, job_id, binary=None):
    job = store.get_job(job_id)
    if job['state'] != 'completed':
        raise InputError('No completed evidence for this task.')
    row = store.connection.execute('SELECT artifact_id FROM attempts WHERE run_id=?', (job['result_run_id'],)).fetchone()
    raw = evidence.read_file(store.root / 'objects', row[0] + '.blob', store.limits.artifact_bytes)
    from evorph.contracts import digest
    if digest(raw) != row[0]:
        raise InputError('Task artifact integrity mismatch.')
    with tempfile.TemporaryDirectory(prefix='evorph-replay-') as temp:
        directory = unpack(raw, Path(temp) / 'evidence')
        return evidence.replay(directory, binary)


def public_task_status(store, job_id):
    job = store.get_job(job_id)
    return {'job_id': job_id, 'state': job['state'], 'run_id': job['result_run_id']}
