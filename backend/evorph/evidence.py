import os
from pathlib import Path

from .contracts import SPEC, SPEC_DIGEST, InputError, canonical, digest, parse_json
from .runner import run

REQUIRED = {'spec.json', 'plan.json', 'policy.json', 'fixtures.json', 'trace.json'}


def source_files():
    return {('source-' + file.name + '.txt'): file.read_bytes()
            for file in sorted(Path(__file__).parent.glob('*.py'))}


def save(directory, plan, policy, fixtures, trace):
    directory = Path(directory)
    if directory.exists():
        raise InputError('Evidence output must be a new directory.')
    directory.mkdir()
    documents = {'spec.json': SPEC, 'plan.json': plan, 'policy.json': policy,
                 'fixtures.json': fixtures, 'trace.json': trace}
    files = {name: canonical(value) + b'\n' for name, value in documents.items()}
    files.update(source_files())
    if sum(map(len, files.values())) > SPEC['limits']['artifact_bytes']:
        raise InputError('Evidence size limit exceeded.')
    for name, raw in files.items():
        with (directory / name).open('xb') as output:
            output.write(raw)
    manifest = {name: digest(raw) for name, raw in files.items()}
    with (directory / 'manifest.json').open('xb') as output:
        output.write(canonical(manifest) + b'\n')
    return {'files': len(files), 'manifest_digest': digest(canonical(manifest))}


def read_file(directory, name, limit):
    if type(name) is not str or Path(name).name != name or name in ('.', '..'):
        raise InputError('Invalid evidence filename.')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    import stat
    try:
        fd = os.open(str(Path(directory) / name), flags)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise InputError('Expected a regular evidence file.')
            raw = source.read(limit + 1)
    except OSError as exc:
        raise InputError('Evidence file unavailable.') from exc
    if len(raw) > limit:
        raise InputError('Evidence file exceeds limit.')
    return raw


def load(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise InputError('Expected an evidence directory.')
    manifest = parse_json(read_file(directory, 'manifest.json', 65536))
    allowed = REQUIRED | set(source_files())
    if type(manifest) is not dict or set(manifest) != allowed:
        raise InputError('Unexpected evidence manifest.')
    if set(p.name for p in directory.iterdir()) != allowed | {'manifest.json'}:
        raise InputError('Unexpected evidence directory contents.')
    files, total = {}, 0
    for name, expected in manifest.items():
        raw = read_file(directory, name, SPEC['limits']['artifact_bytes'])
        total += len(raw)
        if total > SPEC['limits']['artifact_bytes'] or digest(raw) != expected:
            raise InputError('Evidence integrity mismatch.')
        files[name] = raw
    for name, current in source_files().items():
        if files[name] != current:
            raise InputError('Replay implementation differs from the archived version.')
    documents = {name[:-5]: parse_json(files[name], SPEC['limits']['artifact_bytes']) for name in REQUIRED}
    if documents['spec'] != SPEC or digest(canonical(documents['spec'])) != SPEC_DIGEST:
        raise InputError('Evidence specification mismatch.')
    return documents


def replay(directory, binary=None):
    data = load(directory)
    recorded = data['trace']
    actual = run(data['plan'], data['policy'], data['fixtures'], binary,
                 diagnostics=recorded['fixture_scope'] == 'diagnostic')
    fields = ['schema_version', 'spec_digest', 'plan_digest', 'candidate_digest', 'fixture_digest',
              'fixture_scope', 'events', 'matches_reference', 'model_calls']
    if any(actual.get(key) != recorded.get(key) for key in fields):
        raise InputError('Replayed observations differ from the recorded trace.')
    return {'replayed': True, 'events': len(actual['events']), 'matches_reference': actual['matches_reference'],
            'private_keys_required': False, 'environment': actual['environment']}
