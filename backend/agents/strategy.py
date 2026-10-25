from copy import deepcopy

from evorph.contracts import (MUTATIONS, PUBLIC_MESSAGES, SPEC_DIGEST, InputError, exact,
                             identifier, integer, validate_plan)


def validate(strategy):
    exact(strategy, {'schema_version', 'spec_digest', 'version_id', 'parent_id',
                     'message_order', 'mutation_order', 'reduction_order'})
    if strategy['schema_version'] != 'search-strategy.v1' or strategy['spec_digest'] != SPEC_DIGEST:
        raise InputError('Unknown search strategy version.')
    identifier(strategy['version_id'])
    if strategy['parent_id'] is not None:
        identifier(strategy['parent_id'])
    if strategy['version_id'] == strategy['parent_id']:
        raise InputError('Invalid strategy parent.')
    for key, allowed in [('message_order', PUBLIC_MESSAGES), ('mutation_order', MUTATIONS)]:
        values = strategy[key]
        if (type(values) is not list or len(values) != len(allowed)
                or any(type(value) is not str for value in values) or set(values) != allowed):
            raise InputError('Strategy order must preserve the fixed capabilities.')
    if strategy['reduction_order'] not in ('forward', 'reverse'):
        raise InputError('Unknown reduction order.')
    return strategy


def baseline():
    return validate({'schema_version': 'search-strategy.v1', 'spec_digest': SPEC_DIGEST,
                     'version_id': 'search-baseline-v1', 'parent_id': None,
                     'message_order': ['prepare', 'confirm', 'pq-transition', 'legacy-transition', 'invalid-pq-fallback'],
                     'mutation_order': sorted(MUTATIONS), 'reduction_order': 'forward'})


def generate(strategy, attempts):
    validate(strategy)
    integer(attempts, 1, len(PUBLIC_MESSAGES) * (1 + len(MUTATIONS)))
    events = [{'kind': 'deliver', 'message_ref': name} for name in ('prepare', 'confirm')]
    generated = []
    for mutation in [None] + strategy['mutation_order']:
        for name in strategy['message_order']:
            event = {'kind': 'deliver' if mutation is None else 'mutate', 'message_ref': name}
            if mutation is not None:
                event['mutation'] = mutation
            generated.append(validate_plan({'schema_version': 'challenge.v1', 'spec_digest': SPEC_DIGEST,
                                            'events': deepcopy(events) + [event]}))
            if len(generated) == attempts:
                return generated
    return generated


def reductions(challenge, order):
    validate_plan(challenge)
    if order not in ('forward', 'reverse'):
        raise InputError('Unknown reduction order.')
    indices = range(len(challenge['events']))
    if order == 'reverse':
        indices = reversed(indices)
    for index in indices:
        candidate = deepcopy(challenge)
        candidate['events'].pop(index)
        if any(event.get('kind') == 'duplicate' and event['event_index'] == index for event in candidate['events']):
            continue
        for event in candidate['events']:
            if event['kind'] == 'duplicate' and event['event_index'] > index:
                event['event_index'] -= 1
        try:
            yield validate_plan(candidate)
        except InputError:
            continue
