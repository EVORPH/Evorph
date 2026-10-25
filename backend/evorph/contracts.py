import base64
import hashlib
import json
import re

LEGACY = 'ECDSA-P256-SHA256'
PQ = 'ML-DSA-44'
DUAL = 'ECDSA-P256-SHA256+ML-DSA-44'
CONTEXT = b'EVORPH-S1-v1'
SPEC = {
    'version': 'retired-key.v1',
    'domain': 'EVORPH/retired-key/v1',
    'account': 'experiment-account',
    'algorithms': [LEGACY, PQ],
    'context_hex': CONTEXT.hex(),
    'phases': ['LEGACY', 'PREPARED', 'PQ_ACTIVE'],
    'prepare': 'In the prepare step both registered keys must approve the single pending migration while the legacy algorithm stays active.',
    'confirm': 'Both keys confirm that same migration; atomically activate epoch 1 and retire legacy.',
    'operation': 'Only the currently active key and epoch may set the migration label; the verifier rejects any signature fallback.',
    'nonce': 'Each message kind uses its own namespace for the one-time value. The value may be consumed only after the corresponding effect succeeds; any duplicate is rejected by the verifier.',
    'history': 'A valid signature by itself does not prove the signing time or that the signer still holds current authority.',
    'limits': {'events': 32, 'input_bytes': 65536, 'message_bytes': 4096,
               'process_seconds': 10, 'task_seconds': 60, 'process_output_bytes': 262144,
               'bundle_bytes': 4194304, 'artifact_bytes': 8388608,
               'ast_nodes': 128, 'ast_depth': 12},
}
PUBLIC_MESSAGES = {'prepare', 'confirm', 'legacy-transition', 'pq-transition', 'invalid-pq-fallback'}
MUTATIONS = {'signature', 'body', 'domain', 'account', 'epoch', 'algorithm', 'key', 'nonce', 'new-key'}


class InputError(ValueError):
    pass


class ExecutionError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(value).hexdigest()


SPEC_DIGEST = digest(canonical(SPEC))


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise InputError('Unexpected object fields.')


def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,119}', value):
        raise InputError('Invalid identifier.')
    return value


def integer(value, low=0, high=2**31 - 1):
    if type(value) is not int or not low <= value <= high:
        raise InputError('Invalid integer.')


def b64(value):
    return base64.b64encode(value).decode('ascii')


def unb64(value, limit):
    if type(value) is not str or len(value) > 4 * ((limit + 2) // 3):
        raise InputError('Invalid binary length.')
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise InputError('Invalid binary encoding.') from exc
    if len(result) > limit or b64(result) != value:
        raise InputError('Invalid binary encoding.')
    return result


def parse_json(raw, limit=65536):
    if len(raw) > limit:
        raise InputError('Input exceeds size limit.')

    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise InputError('Duplicate object key.')
            obj[key] = value
        return obj

    def constant(_):
        raise InputError('Non-finite JSON number.')

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InputError('Invalid JSON data.') from exc


def message(raw):
    body = parse_json(raw, SPEC['limits']['message_bytes'])
    if type(body) is not dict:
        raise InputError('Invalid message.')
    kind = body.get('kind')
    common = {'domain', 'account', 'kind', 'algorithm', 'key_id', 'epoch', 'nonce'}
    if kind == 'operation':
        exact(body, common | {'action', 'value'})
        if body['action'] != 'set_label' or type(body['value']) is not str or len(body['value']) > 120:
            raise InputError('Invalid operation.')
    elif kind in ('prepare', 'confirm'):
        exact(body, common | {'migration_id', 'to_epoch', 'new_key_sha256'})
        identifier(body['migration_id'])
        integer(body['to_epoch'])
        if type(body['new_key_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', body['new_key_sha256']):
            raise InputError('Invalid key digest.')
    else:
        raise InputError('Unknown message kind.')
    for key in ('domain', 'account', 'algorithm', 'key_id', 'nonce'):
        if type(body[key]) is not str or not body[key] or len(body[key]) > 120:
            raise InputError('Invalid message field.')
    integer(body['epoch'])
    if canonical(body) != raw:
        raise InputError('Message encoding is not canonical.')
    return body


def validate_plan(plan, allowed=PUBLIC_MESSAGES):
    exact(plan, {'schema_version', 'spec_digest', 'events'})
    if plan['schema_version'] != 'challenge.v1' or plan['spec_digest'] != SPEC_DIGEST:
        raise InputError('Unknown challenge version.')
    events = plan['events']
    if type(events) is not list or not 1 <= len(events) <= SPEC['limits']['events']:
        raise InputError('Invalid event count.')
    if len(canonical(plan)) > SPEC['limits']['input_bytes']:
        raise InputError('Challenge exceeds size limit.')
    delivered = set()
    for index, event in enumerate(events):
        if type(event) is not dict:
            raise InputError('Invalid event.')
        kind = event.get('kind')
        if kind in ('deliver', 'mutate'):
            exact(event, {'kind', 'message_ref'} | ({'mutation'} if kind == 'mutate' else set()))
            if type(event['message_ref']) is not str or event['message_ref'] not in allowed:
                raise InputError('Message reference outside fixture capabilities.')
            if kind == 'mutate' and (type(event['mutation']) is not str or event['mutation'] not in MUTATIONS):
                raise InputError('Unknown mutation.')
            delivered.add(index)
        elif kind == 'duplicate':
            exact(event, {'kind', 'event_index'})
            integer(event['event_index'], 0, index - 1)
            if event['event_index'] not in delivered:
                raise InputError('Duplicate requires a delivered message.')
            delivered.add(index)
        elif kind == 'advance':
            exact(event, {'kind', 'ticks'})
            integer(event['ticks'], 1, 1000)
        else:
            raise InputError('Unknown event kind.')
    return plan


def initial_state():
    return {'phase': 'LEGACY', 'epoch': 0, 'active_key': 'legacy', 'pending': None,
            'used': [], 'label': ''}
