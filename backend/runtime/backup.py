from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid

from evorph.contracts import InputError, canonical, integer, parse_json
from evorph.evidence import read_file
from .store import ConflictError


def file_hash(path):
    value, size = hashlib.sha256(), 0
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise InputError('Backup requires regular files.')
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            value.update(chunk)
            size += len(chunk)
    return {'sha256': value.hexdigest(), 'size': size}


def copy_file(source, destination, limit):
    fd = os.open(str(source), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as input_file, destination.open('xb') as output:
        if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
            raise InputError('Backup requires regular files.')
        size = 0
        while True:
            chunk = input_file.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise InputError('Backup file exceeds expected size.')
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())


def create_backup(store, destination):
    destination = Path(destination)
    if destination.exists() or not destination.parent.is_dir() or store.root.resolve() in destination.resolve().parents:
        raise InputError('Choose a new backup directory outside the running state.')
    destination.mkdir(mode=0o700)
    (destination / 'objects').mkdir(mode=0o700)
    try:
        with store.transaction():
            store.integrity()
            source = sqlite3.connect(str(store.database), timeout=5)
            target = sqlite3.connect(str(destination / 'state.db'))
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            for row in store.connection.execute('SELECT artifact_id,size FROM artifacts'):
                name = row['artifact_id'] + '.blob'
                copy_file(store.root / 'objects' / name, destination / 'objects' / name, row['size'])
                copied = file_hash(destination / 'objects' / name)
                if copied != {'sha256': row['artifact_id'], 'size': row['size']}:
                    raise InputError('Backup artifact differs from its reference.')
            (destination / 'config.json').write_bytes(canonical(asdict(store.limits)))
        # Manifest names are slash-separated on every platform so a backup also
        # restores under an OS whose path separator differs from the writer's.
        files = {path.relative_to(destination).as_posix(): file_hash(path)
                 for path in destination.rglob('*') if path.is_file()}
        with (destination / 'manifest.json').open('xb') as output:
            output.write(canonical({'schema_version': 'backup.v1', 'files': files}))
            output.flush()
            os.fsync(output.fileno())
        return {'files': len(files), 'verified': True}
    except BaseException:
        shutil.rmtree(destination)
        raise


def restore_backup(source, destination, max_bytes):
    integer(max_bytes, 1, 2**53 - 1)
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or (source / 'objects').is_symlink() or not source.is_dir():
        raise InputError('Unexpected backup directory.')
    if destination.exists() or not destination.parent.is_dir():
        raise InputError('Restore requires a new state directory.')
    manifest = parse_json(read_file(source, 'manifest.json', 1048576), 1048576)
    if type(manifest) is not dict or set(manifest) != {'schema_version', 'files'} or manifest['schema_version'] != 'backup.v1':
        raise InputError('Unknown backup format.')
    files = manifest['files']
    if type(files) is not dict or not 2 <= len(files) <= 10000 or not {'state.db', 'config.json'} <= set(files):
        raise InputError('Invalid backup manifest.')
    total = 0
    for name, expected in files.items():
        if name not in ('state.db', 'config.json') and not re.fullmatch(r'objects/[a-f0-9]{64}\.blob', name):
            raise InputError('Invalid backup filename.')
        if type(expected) is not dict or set(expected) != {'sha256', 'size'} or type(expected['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', expected['sha256']):
            raise InputError('Invalid backup file metadata.')
        integer(expected['size'], 0, max_bytes)
        total += expected['size']
    if total > max_bytes:
        raise InputError('Backup exceeds restore capacity.')
    temporary = destination.with_name('restore-' + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    (temporary / 'objects').mkdir(mode=0o700)
    try:
        for name, expected in files.items():
            copy_file(source / name, temporary / name, expected['size'])
            if file_hash(temporary / name) != expected:
                raise InputError('Backup integrity mismatch.')
        db = sqlite3.connect(str(temporary / 'state.db'))
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise InputError('Restored database is inconsistent.')
            config = canonical(parse_json((temporary / 'config.json').read_bytes())).decode()
            if db.execute("SELECT value FROM metadata WHERE key='limits'").fetchone()[0] != config:
                raise InputError('Restored resource configuration differs.')
            for artifact_id, size in db.execute('SELECT artifact_id,size FROM artifacts'):
                if files.get('objects/' + artifact_id + '.blob') != {'sha256': artifact_id, 'size': size}:
                    raise InputError('Restored artifact reference is incomplete.')
        finally:
            db.close()
        (temporary / 'config.json').unlink()
        os.rename(str(temporary), str(destination))
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return {'restored': True, 'files': len(files), 'tasks_started': 0, 'model_calls': 0}
