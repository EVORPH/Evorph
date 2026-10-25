import argparse
from http.server import HTTPServer
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading

from evorph.contracts import ExecutionError, InputError, canonical, digest, parse_json
from runtime.store import CapacityError, ConflictError, Store
from service.http_api import handler

from .compose import build
from .config import load

SCHEDULE_MISSING = 3
POLL_SECONDS = 0.2


def install_stop_handlers():
    stop = threading.Event()

    def request_stop(*_):
        stop.set()

    for name in ('SIGTERM', 'SIGINT'):
        signal.signal(getattr(signal, name), request_stop)
    return stop


def _shutdown_when_stopped(server, stop):
    stop.wait()
    server.shutdown()


def serve_api(config, stop=None):
    runtime = build(config)
    try:
        api = handler(runtime.service, origin=config.origin, request_timeout=config.request_timeout)
        # One SQLite connection belongs to the thread that opened it, so requests stay serialized.
        server = HTTPServer((config.listen_host, config.listen_port), api)
    except BaseException:
        runtime.close()
        raise
    stop = install_stop_handlers() if stop is None else stop
    threading.Thread(target=_shutdown_when_stopped, args=(server, stop), daemon=True).start()
    print(canonical({'state': 'listening', 'host': server.server_address[0], 'port': server.server_address[1],
                     'mode': runtime.service.mode}).decode(), flush=True)
    try:
        server.serve_forever(poll_interval=POLL_SECONDS)
    finally:
        server.server_close()
        runtime.close()
    return 0


def run_scheduler(config, stop=None):
    runtime = build(config, execute=True)
    try:
        row = runtime.store.connection.execute(
            'SELECT enabled FROM scheduler_policy WHERE policy_id=1').fetchone()
        if row is None:
            print(canonical({'state': 'error',
                             'message': 'No stored schedule; configure one before starting the worker.'}).decode(),
                  file=sys.stderr)
            return SCHEDULE_MISSING
        stop = install_stop_handlers() if stop is None else stop
        print(canonical({'state': 'scheduler_started', 'enabled': bool(row['enabled'])}).decode(), flush=True)
        runtime.scheduler.serve(stop)
        return 0
    finally:
        runtime.close()


def status(config, publication_before=None):
    runtime = build(config)
    try:
        connection = runtime.store.connection
        row = connection.execute('SELECT * FROM scheduler_policy WHERE policy_id=1').fetchone()
        schedule = None if row is None else {
            'enabled': bool(row['enabled']), 'interval_seconds': row['interval_seconds'],
            'check_seconds': row['check_seconds'], 'next_due': row['next_due'],
            'pause_reason': row['pause_reason']}
        allocation = connection.execute(
            'SELECT paused FROM resource_policy WHERE policy_id=1').fetchone()
        calls = {state: count for state, count in connection.execute(
            'SELECT state, COUNT(*) FROM model_calls GROUP BY state')}
        blocking = connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE state IN ('dispatching','response_saved','usage_unknown')"
        ).fetchone()[0]
        pending = connection.execute(
            "SELECT COUNT(*) FROM scheduler_tasks WHERE state='pending'").fetchone()[0]
        mode = connection.execute("SELECT value FROM metadata WHERE key='service_mode'").fetchone()
        print(canonical({'mode': mode[0] if mode else None, 'schedule': schedule, 'pending_tasks': pending,
                         'spending_paused': None if allocation is None else bool(allocation['paused']),
                         'calls_blocking_schedule': blocking, 'model_calls': calls,
                         'publications': runtime.scheduler.publication_status(publication_before)}).decode())
        return 0
    finally:
        runtime.close()


def set_schedule(config, template_id, interval_seconds, check_seconds):
    runtime = build(config)
    try:
        runtime.scheduler.configure(template_id, interval_seconds=interval_seconds, check_seconds=check_seconds)
        print(canonical({'configured': True, 'template_id': template_id, 'enabled': False}).decode())
        return 0
    finally:
        runtime.close()


def set_enabled(config, enabled):
    runtime = build(config)
    try:
        if enabled:
            runtime.scheduler.enable()
        else:
            runtime.scheduler.pause()
        print(canonical({'enabled': enabled}).decode())
        return 0
    finally:
        runtime.close()


def set_interval(config, interval_seconds, enable=False):
    runtime = build(config)
    try:
        result = runtime.scheduler.set_interval(interval_seconds, enable=enable)
        print(canonical({'configured': True, **result}).decode())
        return 0
    finally:
        runtime.close()


def reconcile(config):
    """Settle recorded provider usage from stored evidence; it never resends a generation."""
    runtime = build(config, execute=True)
    try:
        rows = runtime.store.connection.execute(
            "SELECT call_id, role FROM model_calls WHERE state IN ('dispatching','response_saved','usage_unknown') "
            'ORDER BY rowid').fetchall()
        results = []
        for row in rows:
            channel = runtime.research.channels[row['role']]
            entry = getattr(channel, 'reconcile', None)
            if entry is None:
                results.append({'call_id': row['call_id'], 'state': 'unsupported_channel'})
                continue
            try:
                results.append(entry(row['call_id']))
            except (InputError, ConflictError):
                results.append({'call_id': row['call_id'], 'state': 'unresolved'})
        print(canonical({'calls': len(results), 'reconciled': results}).decode())
        return 0
    finally:
        runtime.close()


def replay_evidence(config, pack_path):
    """Recompute one published verdict offline in a throwaway store and compare the
    verdict projection; fixture ceremonies and resource telemetry are excluded by design."""
    from agents.research import recompute_trace, verdict_projection
    pack = parse_json(Path(pack_path).read_bytes(), 16 * 1024 * 1024)
    if type(pack) is not dict or pack.get('schema_version') != 'evidence-pack.v1':
        raise InputError('Unknown evidence pack version.')
    for key in ('candidate', 'challenge', 'trace', 'trace_digest', 'matches_reference', 'pack_sha256'):
        if key not in pack:
            raise InputError('Evidence pack is incomplete.')
    intact = (digest(canonical(pack['trace'])) == pack['trace_digest']
              and pack['pack_sha256'] == digest(canonical({key: value for key, value in pack.items()
                                                           if key != 'pack_sha256'})))
    with tempfile.TemporaryDirectory(prefix='evorph-replay-') as temp:
        store = Store(Path(temp) / 'state', config.limits)
        try:
            trace = recompute_trace(store, pack['challenge'], pack['candidate'], config.openssl)
        finally:
            store.close()
    replayed = intact and verdict_projection(trace) == verdict_projection(pack['trace'])
    print(canonical({'replayed': replayed, 'verdict_digest': digest(canonical(verdict_projection(trace))),
                     'matches_reference': trace['matches_reference']}).decode())
    return 0 if replayed else 1


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', required=True, type=Path)
    parser = argparse.ArgumentParser(description='Operator entry points for the research service.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve-api', parents=[common])
    commands.add_parser('run-scheduler', parents=[common])
    status_command = commands.add_parser('status', parents=[common])
    status_command.add_argument('--publication-before', type=int)
    commands.add_parser('schedule-enable', parents=[common])
    commands.add_parser('schedule-pause', parents=[common])
    schedule = commands.add_parser('schedule', parents=[common])
    schedule.add_argument('--template-id', required=True)
    schedule.add_argument('--interval-seconds', required=True, type=int)
    schedule.add_argument('--check-seconds', required=True, type=int)
    interval = commands.add_parser('schedule-interval', parents=[common])
    interval.add_argument('--interval-seconds', required=True, type=int)
    interval.add_argument('--enable-if-clear', action='store_true')
    commands.add_parser('reconcile', parents=[common])
    replay = commands.add_parser('replay-evidence', parents=[common])
    replay.add_argument('--pack', required=True)
    opts = parser.parse_args(argv)
    try:
        config = load(opts.config)
        if opts.command == 'serve-api':
            return serve_api(config)
        if opts.command == 'run-scheduler':
            return run_scheduler(config)
        if opts.command == 'status':
            return status(config, opts.publication_before)
        if opts.command == 'schedule':
            return set_schedule(config, opts.template_id, opts.interval_seconds, opts.check_seconds)
        if opts.command == 'schedule-interval':
            return set_interval(config, opts.interval_seconds, opts.enable_if_clear)
        if opts.command == 'reconcile':
            return reconcile(config)
        if opts.command == 'replay-evidence':
            return replay_evidence(config, opts.pack)
        if opts.command == 'schedule-enable':
            return set_enabled(config, True)
        return set_enabled(config, False)
    except (InputError, ExecutionError, CapacityError, ConflictError) as exc:
        print(canonical({'state': 'error', 'message': str(exc)}).decode(), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print(canonical({'state': 'error', 'message': 'Local state operation failed.'}).decode(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
