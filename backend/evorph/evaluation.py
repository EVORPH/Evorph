import itertools
import random

from .candidates import baseline
from .contracts import SPEC_DIGEST
from .runner import run


def plan(*names):
    return {'schema_version': 'challenge.v1', 'spec_digest': SPEC_DIGEST,
            'events': [{'kind': 'deliver', 'message_ref': name} for name in names]}


def scenarios(seed=1729):
    development = [plan('prepare', 'confirm', 'legacy-transition', 'pq-transition', 'invalid-pq-fallback'),
                   plan('legacy-transition', 'prepare', 'confirm', 'pq-transition'),
                   plan('confirm', 'prepare', 'prepare', 'confirm', 'confirm', 'pq-transition', 'pq-transition')]
    selection = [plan(*order) for order in itertools.permutations(('prepare', 'confirm', 'pq-transition'))]
    selection.append(plan('prepare', 'legacy-transition', 'confirm', 'legacy-transition', 'pq-transition'))
    final = []
    rng = random.Random(seed)
    for mutation in ('signature', 'body', 'domain', 'account', 'epoch', 'algorithm', 'key', 'nonce', 'new-key'):
        challenge = plan('prepare', 'confirm', 'pq-transition')
        target = rng.choice(('prepare', 'confirm', 'pq-transition'))
        challenge['events'].insert(rng.randrange(4), {'kind': 'mutate', 'message_ref': target, 'mutation': mutation})
        challenge['events'].insert(0, {'kind': 'advance', 'ticks': rng.randrange(1, 20)})
        challenge['events'].append({'kind': 'duplicate', 'event_index': 1})
        final.append(challenge)
    return {'development': development, 'selection_validation': selection, 'final_report': final}


def calibrate(bundle, binary=None, seed=1729):
    partitions = scenarios(seed)
    reports = {}
    for name, challenges in partitions.items():
        checks = []
        for challenge in challenges:
            result = run(challenge, baseline(), bundle, binary)
            checks.append({'plan_digest': result['plan_digest'], 'matches_reference': result['matches_reference'],
                           'events': len(result['events'])})
        reports[name] = {'cases': len(checks), 'passed': sum(item['matches_reference'] for item in checks), 'results': checks}
    fault_challenge = partitions['development'][0]
    faults = {name: not run(fault_challenge, baseline(name), bundle, binary)['matches_reference']
              for name in ('ignore-retirement', 'silent-fallback', 'reject-all')}
    return {'kind': 'fixed_policy_calibration', 'seed': seed, 'partitions': reports,
            'synthetic_faults_detected': faults, 'model_calls': 0,
            'final_report_consumed': True,
            'evaluation_note': 'Calibration only. These published cases are consumed; create a fresh final set before model selection.',
            'passed': all(part['cases'] == part['passed'] for part in reports.values()) and all(faults.values())}
