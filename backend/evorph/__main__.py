import argparse
from pathlib import Path
import sys

from . import candidates, evidence
from .contracts import SPEC, InputError, ExecutionError, canonical, parse_json
from .evaluation import calibrate, plan
from .fixtures import build
from .legacy import replay_probe
from .runner import run
from .vectors import verify


def write_json(path, data):
    with path.open('xb') as output:
        output.write(canonical(data) + b'\n')


def main():
    parser = argparse.ArgumentParser(description='Bounded retired-key migration experiments.')
    parser.add_argument('--openssl')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('spec')
    execute = commands.add_parser('run')
    execute.add_argument('--plan', type=Path)
    execute.add_argument('--policy', type=Path)
    execute.add_argument('--calibration', choices=['ignore-retirement', 'silent-fallback', 'reject-all'])
    execute.add_argument('--output', type=Path, required=True)
    replay = commands.add_parser('replay')
    replay.add_argument('directory', type=Path)
    probe = commands.add_parser('replay-probe')
    probe.add_argument('directory', type=Path)
    check = commands.add_parser('check')
    check.add_argument('--vectors', type=Path, default=Path(__file__).resolve().parents[1] / 'vectors')
    check.add_argument('--legacy', type=Path)
    check.add_argument('--output', type=Path, required=True)
    opts = parser.parse_args()
    try:
        if opts.command == 'spec':
            result = SPEC
        elif opts.command == 'replay':
            result = evidence.replay(opts.directory, opts.openssl)
        elif opts.command == 'replay-probe':
            result = replay_probe(opts.directory, opts.openssl)
        else:
            if opts.output.exists() or not opts.output.parent.is_dir():
                raise InputError('Choose a new output directory under an existing parent.')
            bundle, environment = build(opts.openssl)
            challenge = plan('prepare', 'confirm', 'legacy-transition', 'pq-transition', 'invalid-pq-fallback')
            policy = candidates.baseline(getattr(opts, 'calibration', None))
            if opts.command == 'run':
                if opts.plan:
                    challenge = parse_json(evidence.read_file(opts.plan.parent, opts.plan.name, 65536))
                if opts.policy:
                    policy = parse_json(evidence.read_file(opts.policy.parent, opts.policy.name, 65536))
                trace = run(challenge, policy, bundle, opts.openssl)
                evidence.save(opts.output, challenge, policy, bundle, trace)
                result = {'written': True, 'events': len(trace['events']), 'matches_reference': trace['matches_reference'],
                          'metrics': trace['metrics'], 'model_calls': 0}
            else:
                opts.output.mkdir()
                write_json(opts.output / 'spec.json', SPEC)
                calibration = calibrate(bundle, opts.openssl)
                vector_result = verify(opts.vectors, opts.openssl)
                write_json(opts.output / 'calibration.json', calibration)
                write_json(opts.output / 'vectors.json', vector_result)
                trace = run(challenge, policy, bundle, opts.openssl)
                evidence.save(opts.output / 'baseline', challenge, policy, bundle, trace)
                replay_result = evidence.replay(opts.output / 'baseline', opts.openssl)
                write_json(opts.output / 'replay.json', replay_result)
                legacy = replay_probe(opts.legacy, opts.openssl) if opts.legacy else None
                if legacy:
                    write_json(opts.output / 'legacy-replay.json', legacy)
                result = {'calibration_passed': calibration['passed'],
                          'official_vectors': {'passed': vector_result['passed'], 'applicable': vector_result['applicable'],
                                               'excluded_cases': sum(x['cases'] for x in vector_result['excluded'])},
                          'evidence_replayed': replay_result['replayed'], 'legacy_probe_replayed': legacy,
                          'environment': environment, 'trace_metrics': trace['metrics'],
                          'model_calls': 0, 'linux_target_verified': False,
                          'limits_note': 'Wall-clock and output limits enforced. Target Linux aggregate memory and CPU enforcement require target-host verification.'}
                write_json(opts.output / 'summary.json', result)
                if not calibration['passed'] or vector_result['passed'] != vector_result['applicable']:
                    print(canonical(result).decode())
                    return 1
        print(canonical(result).decode())
        return 0
    except (InputError, ExecutionError, OSError) as exc:
        message = str(exc) if isinstance(exc, (InputError, ExecutionError)) else 'Local file operation failed.'
        print(canonical({'status': 'error', 'message': message}).decode(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
