import platform
import resource
import tempfile
import time
from copy import deepcopy

from . import candidates, reference
from .contracts import (LEGACY, PQ, PUBLIC_MESSAGES, SPEC, SPEC_DIGEST, InputError,
                        ExecutionError, canonical, digest, exact, identifier,
                        initial_state, message, unb64, validate_plan)
from .crypto import Crypto
from .fixtures import mutated


def validate_bundle(bundle):
    exact(bundle, {'schema_version', 'spec_digest', 'keys', 'messages'})
    if bundle['schema_version'] != 'fixtures.v1' or bundle['spec_digest'] != SPEC_DIGEST:
        raise InputError('Unknown fixture version.')
    if len(canonical(bundle)) > SPEC['limits']['bundle_bytes']:
        raise InputError('Fixture bundle exceeds limit.')
    exact(bundle['keys'], {'legacy', 'pq'})
    for key in bundle['keys'].values():
        unb64(key, 8192)
    if type(bundle['messages']) is not dict or not 1 <= len(bundle['messages']) <= 64:
        raise InputError('Invalid fixture count.')
    for name, envelope in bundle['messages'].items():
        identifier(name)
        exact(envelope, {'message_b64', 'signatures'})
        unb64(envelope['message_b64'], SPEC['limits']['message_bytes'])
        if type(envelope['signatures']) is not dict or not set(envelope['signatures']) <= {'legacy', 'pq'}:
            raise InputError('Unknown signer.')
        for signature in envelope['signatures'].values():
            unb64(signature, 8192)
    return bundle


def run(plan, policy, bundle, binary=None, diagnostics=False, task_seconds=None):
    validate_bundle(bundle)
    allowed = set(bundle['messages']) if diagnostics else PUBLIC_MESSAGES
    validate_plan(plan, allowed)
    candidates.validate(policy)
    if any(event.get('message_ref') not in bundle['messages'] for event in plan['events']
           if 'message_ref' in event):
        raise InputError('Missing fixture reference.')
    start = time.monotonic()
    timeout = SPEC['limits']['task_seconds'] if task_seconds is None else min(task_seconds, SPEC['limits']['task_seconds'])
    deadline = start + timeout
    expected, actual, ticks = initial_state(), initial_state(), 0
    events, delivered = [], {}
    new_key_digest = digest(unb64(bundle['keys']['pq'], 8192))
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with tempfile.TemporaryDirectory(prefix='evorph-run-') as temporary:
        crypto = Crypto(temporary, binary, deadline)
        environment = crypto.capabilities()
        for key, algorithm in [('legacy', LEGACY), ('pq', PQ)]:
            crypto.register_public(key, unb64(bundle['keys'][key], 8192), algorithm)
        for index, event in enumerate(plan['events']):
            if time.monotonic() >= deadline:
                raise ExecutionError('Task deadline exceeded.')
            before_reference, before_actual = deepcopy(expected), deepcopy(actual)
            entry = {'sequence': index + 1, 'logical_time': ticks, 'action': event,
                     'reference_before': before_reference, 'candidate_before': before_actual,
                     'historical_evidence': 'unknown'}
            if event['kind'] == 'advance':
                ticks += event['ticks']
                entry.update(signature_verdicts={}, reference={'accepted': True, 'reason': 'clock', 'state': deepcopy(expected)},
                             candidate={'accepted': True, 'reason': 'clock', 'state': deepcopy(actual)}, message_digest=None)
            else:
                if event['kind'] == 'duplicate':
                    envelope = delivered[event['event_index']]
                else:
                    envelope = bundle['messages'][event['message_ref']]
                    if event['kind'] == 'mutate':
                        envelope = mutated(envelope, event['mutation'])
                delivered[index] = envelope
                raw = unb64(envelope['message_b64'], SPEC['limits']['message_bytes'])
                entry['message_digest'] = digest(raw)
                try:
                    body = message(raw)
                except InputError:
                    entry.update(signature_verdicts={'legacy': 'not_run', 'pq': 'not_run'},
                                 reference={'accepted': False, 'reason': 'message_format', 'state': deepcopy(expected)},
                                 candidate={'accepted': False, 'reason': 'message_format', 'state': deepcopy(actual)})
                else:
                    facts = {key: crypto.verify(key, raw, unb64(value, 8192))
                             for key, value in envelope['signatures'].items()}
                    entry.update(signature_verdicts=facts,
                                 reference=reference.step(expected, body, facts, new_key_digest),
                                 candidate=candidates.step(actual, body, facts, new_key_digest, policy))
                    expected, actual = entry['reference']['state'], entry['candidate']['state']
            entry['matches_reference'] = (entry['reference']['accepted'] == entry['candidate']['accepted']
                                          and entry['reference']['state'] == entry['candidate']['state'])
            events.append(entry)
    units = 1024 * 1024 if platform.system() == 'Darwin' else 1024
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    report = {'schema_version': 'trace.v1', 'spec_digest': SPEC_DIGEST,
              'plan_digest': digest(canonical(plan)), 'candidate_digest': digest(canonical(policy)),
              'fixture_digest': digest(canonical(bundle)), 'fixture_scope': 'diagnostic' if diagnostics else 'public-challenge',
              'events': events, 'matches_reference': all(item['matches_reference'] for item in events),
              'environment': environment,
              'metrics': {'wall_seconds': round(time.monotonic() - start, 6),
                          'child_cpu_seconds': round(cpu_after.ru_utime + cpu_after.ru_stime - cpu_before.ru_utime - cpu_before.ru_stime, 6),
                          'parent_peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / units,
                          'child_highwater_rss_mib': cpu_after.ru_maxrss / units,
                          'memory_scope': 'Process-lifetime high-water marks, not simultaneous task-group memory.',
                          'platform': platform.system(), 'linux_target_verified': False},
              'model_calls': 0}
    if len(canonical(report)) > SPEC['limits']['artifact_bytes']:
        raise ExecutionError('Trace exceeds artifact limit.')
    return report
