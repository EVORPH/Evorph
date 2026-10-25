import argparse
from pathlib import Path
import sqlite3
import sys

from evorph.candidates import baseline
from evorph.contracts import ExecutionError, InputError, canonical, exact, parse_json
from evorph.evaluation import plan
from evorph.evidence import read_file
from .accounting import Accounting
from .backup import create_backup, restore_backup
from .store import CapacityError, ConflictError, Limits, Store
from .worker import public_task_status, replay_task, run_once


def read_json(path):
    return parse_json(read_file(path.parent, path.name, 131072), 131072)


def main():
    parser = argparse.ArgumentParser(description='Persistent local research execution.')
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--openssl')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    commands.add_parser('configure')
    enqueue = commands.add_parser('enqueue')
    enqueue.add_argument('--request-key', required=True)
    enqueue.add_argument('--submitter', required=True)
    enqueue.add_argument('--plan', type=Path)
    enqueue.add_argument('--policy', type=Path)
    commands.add_parser('work-once')
    status = commands.add_parser('status')
    status.add_argument('job_id')
    replay = commands.add_parser('replay')
    replay.add_argument('job_id')
    backup = commands.add_parser('backup')
    backup.add_argument('--output', type=Path, required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--source', type=Path, required=True)
    commands.add_parser('check')
    opts = parser.parse_args()
    store = None
    try:
        config = read_json(opts.config)
        exact(config, {'limits', 'spending'})
        exact(config['limits'], set(Limits.__dataclass_fields__))
        limits = Limits(**config['limits'])
        limits.validate()
        if config['spending'] is not None:
            exact(config['spending'], {'daily_micros', 'monthly_micros'})
        if opts.command == 'restore':
            result = restore_backup(opts.source, opts.state, limits.storage_bytes)
        else:
            if opts.command == 'init' and (opts.state / 'state.db').exists():
                raise InputError('State already initialized; use explicit configuration update.')
            if opts.command not in ('init',) and not (opts.state / 'state.db').is_file():
                raise InputError('Initialize the private runtime state first.')
            opening_limits = limits
            if opts.command == 'configure':
                with sqlite3.connect(str(opts.state / 'state.db')) as connection:
                    saved = connection.execute("SELECT value FROM metadata WHERE key='limits'").fetchone()
                    opening_limits = Limits(**parse_json(saved[0]))
            store = Store(opts.state, opening_limits)
            if opts.command in ('init', 'configure'):
                if opts.command == 'configure':
                    store.configure_limits(limits)
                if config['spending'] is not None:
                    Accounting(store).configure(**config['spending'])
                else:
                    Accounting(store).pause()
                result = {'configured': True, 'model_transport_connected': False}
            elif opts.command == 'enqueue':
                challenge = read_json(opts.plan) if opts.plan else plan('prepare', 'confirm', 'legacy-transition', 'pq-transition')
                policy = read_json(opts.policy) if opts.policy else baseline()
                result = public_task_status(store, store.submit(challenge, policy, opts.submitter, opts.request_key))
            elif opts.command == 'work-once':
                result = run_once(store, opts.openssl)
            elif opts.command == 'status':
                result = public_task_status(store, opts.job_id)
            elif opts.command == 'replay':
                result = replay_task(store, opts.job_id, opts.openssl)
            elif opts.command == 'backup':
                result = create_backup(store, opts.output)
            else:
                result = store.integrity()
        print(canonical(result).decode())
        return 0
    except (InputError, ExecutionError, CapacityError, ConflictError, OSError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, (InputError, ExecutionError, CapacityError, ConflictError)) else 'Local state operation failed.'
        print(canonical({'state': 'error', 'message': message}).decode(), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
