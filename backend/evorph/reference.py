from copy import deepcopy

from .contracts import DUAL, LEGACY, PQ, SPEC


def step(state, body, signatures, new_key_digest):
    result = deepcopy(state)
    kind = body['kind']

    def reject(reason):
        return {'accepted': False, 'reason': reason, 'state': result}

    if body['domain'] != SPEC['domain'] or body['account'] != SPEC['account']:
        return reject('scope')
    token = kind + ':' + body['nonce']
    if token in state['used']:
        return reject('replay')
    if kind == 'operation':
        key = 'pq' if state['phase'] == 'PQ_ACTIVE' else 'legacy'
        algorithm = PQ if key == 'pq' else LEGACY
        epoch = 1 if key == 'pq' else 0
        if body['algorithm'] != algorithm or body['key_id'] != key or body['epoch'] != epoch:
            return reject('current_authority')
        if signatures.get(key) != 'valid':
            return reject('signature')
        result['label'] = body['value']
    else:
        expected_phase = 'LEGACY' if kind == 'prepare' else 'PREPARED'
        if state['phase'] != expected_phase:
            return reject('phase')
        if body['algorithm'] != DUAL or body['key_id'] != 'legacy+pq' or body['epoch'] != 0:
            return reject('migration_origin')
        if body['to_epoch'] != 1 or body['new_key_sha256'] != new_key_digest:
            return reject('migration_target')
        if signatures.get('legacy') != 'valid' or signatures.get('pq') != 'valid':
            return reject('dual_proof')
        pending = {'migration_id': body['migration_id'], 'from_epoch': body['epoch'],
                   'to_epoch': body['to_epoch'], 'new_key_sha256': body['new_key_sha256']}
        if kind == 'prepare':
            result['phase'] = 'PREPARED'
            result['pending'] = pending
        else:
            if state['pending'] != pending:
                return reject('migration_transcript')
            result.update(phase='PQ_ACTIVE', epoch=1, active_key='pq', pending=None)
    result['used'].append(token)
    return {'accepted': True, 'reason': 'authorized', 'state': result}
