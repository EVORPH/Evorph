from copy import deepcopy

from .contracts import DUAL, LEGACY, PQ, SPEC, SPEC_DIGEST, InputError, canonical, exact, identifier

FACTS = {'scope', 'fresh', 'phase', 'generation', 'registered_key', 'authority',
         'signature', 'legacy_signature', 'target', 'transcript'}
EFFECTS = {'prepare', 'activate', 'apply', 'none'}


def validate(candidate):
    exact(candidate, {'schema_version', 'spec_digest', 'version_id', 'parent_id', 'rules'})
    if candidate['schema_version'] != 'policy.v1' or candidate['spec_digest'] != SPEC_DIGEST:
        raise InputError('Unknown policy version.')
    identifier(candidate['version_id'])
    if candidate['parent_id'] is not None:
        identifier(candidate['parent_id'])
    if candidate['parent_id'] == candidate['version_id'] or len(canonical(candidate)) > 65536:
        raise InputError('Invalid policy identity or size.')
    exact(candidate['rules'], {'prepare', 'confirm', 'operation'})
    count = 0

    def inspect(node, depth):
        nonlocal count
        count += 1
        if count > SPEC['limits']['ast_nodes'] or depth > SPEC['limits']['ast_depth']:
            raise InputError('Policy expression exceeds limits.')
        if type(node) is not dict or len(node) != 1:
            raise InputError('Invalid policy expression.')
        operator, value = next(iter(node.items()))
        if operator == 'fact':
            if type(value) is not str or value not in FACTS:
                raise InputError('Unknown policy fact.')
        elif operator == 'constant':
            if type(value) is not bool:
                raise InputError('Invalid policy constant.')
        elif operator in ('all', 'any'):
            if type(value) is not list or not value:
                raise InputError('Invalid policy operands.')
            for operand in value:
                inspect(operand, depth + 1)
        elif operator == 'not':
            inspect(value, depth + 1)
        else:
            raise InputError('Unknown policy operator.')

    for rule in candidate['rules'].values():
        exact(rule, {'when', 'effect'})
        if type(rule['effect']) is not str or rule['effect'] not in EFFECTS:
            raise InputError('Unknown policy effect.')
        inspect(rule['when'], 0)
    return candidate


def baseline(fault=None):
    names = ['scope', 'fresh', 'phase', 'generation', 'registered_key', 'authority',
             'signature', 'target', 'transcript']
    rules = {kind: {'when': {'all': [{'fact': name} for name in names]}, 'effect': effect}
             for kind, effect in [('prepare', 'prepare'), ('confirm', 'activate'), ('operation', 'apply')]}
    if fault == 'ignore-retirement':
        rules['operation']['when']['all'] = [item for item in rules['operation']['when']['all']
                                             if item['fact'] not in ('authority', 'generation')]
    elif fault == 'silent-fallback':
        nodes = rules['operation']['when']['all']
        nodes[names.index('signature')] = {'any': [{'fact': 'signature'}, {'fact': 'legacy_signature'}]}
    elif fault == 'reject-all':
        for rule in rules.values():
            rule['when'] = {'constant': False}
    elif fault is not None:
        raise InputError('Unknown calibration policy.')
    return validate({'schema_version': 'policy.v1', 'spec_digest': SPEC_DIGEST,
                     'version_id': fault or 'baseline-v1', 'parent_id': None, 'rules': rules})


def step(state, body, signatures, new_key_digest, policy):
    kind = body['kind']
    migration = kind != 'operation'
    key_metadata = {'legacy': (LEGACY, 0), 'pq': (PQ, 1)}
    selected = key_metadata.get(body['key_id'])
    proposed = None if not migration else {
        'migration_id': body['migration_id'], 'from_epoch': body['epoch'],
        'to_epoch': body['to_epoch'], 'new_key_sha256': body['new_key_sha256']}
    facts = {
        'scope': body['domain'] == SPEC['domain'] and body['account'] == SPEC['account'],
        'fresh': kind + ':' + body['nonce'] not in state['used'],
        'phase': kind == 'operation' or state['phase'] == {'prepare': 'LEGACY', 'confirm': 'PREPARED'}.get(kind),
        'generation': body['epoch'] == state['epoch'],
        'registered_key': (body['algorithm'] == DUAL and body['key_id'] == 'legacy+pq') if migration else bool(selected and selected[0] == body['algorithm']),
        'authority': migration or body['key_id'] == state['active_key'],
        'signature': all(signatures.get(key) == 'valid' for key in ('legacy', 'pq')) if migration else signatures.get(body['key_id']) == 'valid',
        'legacy_signature': signatures.get('legacy') == 'valid',
        'target': not migration or body['to_epoch'] == 1 and body['new_key_sha256'] == new_key_digest,
        'transcript': kind != 'confirm' or state['pending'] == proposed,
    }

    def evaluate(node):
        operator, value = next(iter(node.items()))
        if operator == 'fact':
            return facts[value]
        if operator == 'constant':
            return value
        if operator == 'not':
            return not evaluate(value)
        if operator == 'all':
            return all(evaluate(item) for item in value)
        return any(evaluate(item) for item in value)

    result = deepcopy(state)
    rule = policy['rules'][kind]
    accepted = evaluate(rule['when'])
    if accepted:
        effect = rule['effect']
        if effect == 'prepare' and migration:
            result.update(phase='PREPARED', pending=proposed)
        elif effect == 'activate' and migration:
            result.update(phase='PQ_ACTIVE', epoch=body['to_epoch'], active_key='pq', pending=None)
        elif effect == 'apply' and not migration:
            result['label'] = body['value']
        elif effect != 'none':
            return {'accepted': False, 'reason': 'effect_shape', 'state': result}
        result['used'].append(kind + ':' + body['nonce'])
    return {'accepted': accepted, 'reason': 'rule_true' if accepted else 'rule_false', 'state': result}
