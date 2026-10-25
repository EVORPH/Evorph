import tempfile
import time
from copy import deepcopy

from .contracts import CONTEXT, DUAL, LEGACY, PQ, SPEC, SPEC_DIGEST, b64, canonical, digest, message, unb64
from .crypto import Crypto


def operation(key='pq', nonce='new-1', **changes):
    body = {'domain': SPEC['domain'], 'account': SPEC['account'], 'kind': 'operation',
            'algorithm': PQ if key == 'pq' else LEGACY, 'key_id': key,
            'epoch': 1 if key == 'pq' else 0, 'nonce': nonce,
            'action': 'set_label', 'value': 'experimental'}
    body.update(changes)
    return body


def build(binary=None, diagnostics=False):
    deadline = time.monotonic() + SPEC['limits']['task_seconds']
    with tempfile.TemporaryDirectory(prefix='evorph-fixture-') as temporary:
        crypto = Crypto(temporary, binary, deadline)
        environment = crypto.capabilities()
        keys = {key: crypto.keygen(key, algorithm)
                for key, algorithm in [('legacy', LEGACY), ('pq', PQ)]}
        messages = {}

        def add(name, body, signers, context=CONTEXT):
            raw = canonical(body)
            messages[name] = {'message_b64': b64(raw),
                              'signatures': {key: b64(crypto.sign(key, raw, context)) for key in signers}}

        ceremony = {'domain': SPEC['domain'], 'account': SPEC['account'],
                    'kind': 'prepare', 'algorithm': DUAL, 'key_id': 'legacy+pq',
                    'epoch': 0, 'nonce': 'prepare-1', 'migration_id': 'migration-1',
                    'to_epoch': 1, 'new_key_sha256': digest(keys['pq'])}
        add('legacy-transition', operation('legacy', 'old-1'), ['legacy'])
        add('prepare', ceremony, ['legacy', 'pq'])
        confirm = dict(ceremony, kind='confirm', nonce='confirm-1')
        add('confirm', confirm, ['legacy', 'pq'])
        add('pq-transition', operation(), ['pq'])
        add('invalid-pq-fallback', operation(nonce='fallback-1'), ['pq', 'legacy'])
        damaged = bytearray(unb64(messages['invalid-pq-fallback']['signatures']['pq'], 8192))
        damaged[0] ^= 1
        messages['invalid-pq-fallback']['signatures']['pq'] = b64(damaged)
        if diagnostics:
            for field, value in [('domain', 'other-domain'), ('account', 'other-account'),
                                 ('epoch', 0), ('algorithm', LEGACY), ('key_id', 'legacy')]:
                add('diag-operation-' + field, operation(nonce='negative-' + field, **{field: value}), ['pq'])
            add('diag-wrong-context', operation(nonce='wrong-context'), ['pq'], b'OTHER-CONTEXT')
            for kind, source in [('prepare', ceremony), ('confirm', confirm)]:
                for field, value in [('migration_id', 'other-migration'), ('to_epoch', 2),
                                     ('new_key_sha256', '0' * 64), ('epoch', 1),
                                     ('algorithm', PQ), ('key_id', 'pq')]:
                    add('diag-' + kind + '-' + field, dict(source, **{field: value}), ['legacy', 'pq'])
                add('diag-' + kind + '-legacy-only', source, ['legacy'])
                add('diag-' + kind + '-pq-only', source, ['pq'])
    bundle = {'schema_version': 'fixtures.v1', 'spec_digest': SPEC_DIGEST,
              'keys': {key: b64(value) for key, value in keys.items()}, 'messages': messages}
    return bundle, environment


def mutated(envelope, mutation):
    result = deepcopy(envelope)
    if mutation == 'signature':
        for key, value in result['signatures'].items():
            raw = bytearray(unb64(value, 8192))
            if raw:
                raw[0] ^= 1
            result['signatures'][key] = b64(raw)
    else:
        body = message(unb64(result['message_b64'], SPEC['limits']['message_bytes']))
        if mutation == 'body':
            body['nonce'] += '-changed'
        else:
            field = {'domain': 'domain', 'account': 'account', 'epoch': 'epoch',
                     'algorithm': 'algorithm', 'key': 'key_id', 'nonce': 'nonce',
                     'new-key': 'new_key_sha256'}[mutation]
            if field in body:
                body[field] = body[field] + 1 if field == 'epoch' else ('f' * 64 if field == 'new_key_sha256' else 'altered')
            else:
                body['nonce'] += '-changed'
        result['message_b64'] = b64(canonical(body))
    return result
