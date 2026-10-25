import tempfile
import time

from .contracts import LEGACY, PQ, InputError, canonical, digest, parse_json, unb64
from .crypto import Crypto
from .evidence import read_file


HASHES = {
    'report.json': '47163b125f1700394099dd52d6637858af8b8ec1a011a7889007e0377827e96a',
    'pq-public.pem': '56a6fc1a06bee83cec86ed113b889643abbe9c967150b6ec7a0807618d94840a',
    'probe-source.py.txt': '50136b307d03b288ac421d2409f4b045a01f80a50dbf173611b78dc07cbfb618',
    'legacy-public.pem': '1e65d5537c647be9e3292f9093fc16552391b3e62a6be1dafe8d795396fdd2e1',
    'fixtures.json': '457e4e9a036a096f7b6c9adfd64c7c296a9adc46ce3bb7bd7d96c4cb72d24a6d',
}


def replay_probe(directory, binary=None):
    files = {name: read_file(directory, name, 1048576) for name in HASHES}
    if any(digest(files[name]) != expected for name, expected in HASHES.items()):
        raise InputError('Legacy probe snapshot differs from its pinned version.')
    report = parse_json(files['report.json'], 1048576)
    fixtures = parse_json(files['fixtures.json'], 1048576)
    phase, used, count, checks = 'LEGACY', set(), 0, []
    domain, account = 'EVORPH/retired-key/probe-v1', 'experiment-account'
    with tempfile.TemporaryDirectory(prefix='evorph-legacy-') as temporary:
        crypto = Crypto(temporary, binary, time.monotonic() + 60)
        for key, algorithm in [('legacy', LEGACY), ('pq', PQ)]:
            crypto.register_public(key, files[key + '-public.pem'], algorithm)
        for event in report['transitions']:
            body = event['body']
            sigs = {key: crypto.verify(key, canonical(body), unb64(value, 8192))
                    for key, value in event['signatures_b64'].items()}
            count += len(sigs)
            kind = body['action']
            before, after = ('LEGACY', 'PREPARED') if kind == 'prepare' else ('PREPARED', 'PQ_ACTIVE')
            accepted = (phase == before and body['domain'] == domain and body['account'] == account
                        and body['from_epoch'] == 0 and body['to_epoch'] == 1 and body['new_algorithm'] == PQ
                        and body['new_key_sha256'] == digest(files['pq-public.pem'])
                        and sigs == {'legacy': 'valid', 'pq': 'valid'})
            if not accepted or event['before'] != phase or event['after'] != after:
                raise InputError('Legacy migration replay disagrees.')
            phase = after
            checks.append({'case': kind + '-authorized', 'accepted': accepted})
        observations = {item['id']: item for item in report['cases']}
        for item in fixtures:
            body = item['body']
            raw = unb64(item['message_b64'], 4096)
            if raw != canonical(body) or item['phase'] != phase or item['used_nonces_before'] != sorted(used):
                raise InputError('Legacy fixture sequence differs.')
            primary = crypto.verify(item['signer'], raw, unb64(item['signature_b64'], 8192), item['verify_context'].encode('ascii'))
            count += 1
            fallback = None
            if item['fallback_signature_b64'] is not None and primary == 'invalid':
                fallback = crypto.verify('legacy', raw, unb64(item['fallback_signature_b64'], 8192))
                count += 1
            accepted = (primary == 'valid' and item['signer'] == 'pq' and body['domain'] == domain
                        and body['account'] == account and body['algorithm'] == PQ and body['key_id'] == 'pq'
                        and type(body['epoch']) is int and body['epoch'] == 1
                        and body['action'] == 'set_label' and body['nonce'] not in used)
            recorded = observations[item['id']]
            if (accepted != recorded['actual_accept'] or accepted != recorded['expected_accept']
                    or (primary == 'valid') != item['primary_verification']['valid']
                    or (fallback is not None and (fallback == 'valid') != item['fallback_verification']['valid'])):
                raise InputError('Legacy signature or authority replay disagrees.')
            if recorded['synthetic_bug_accept'] is not None:
                buggy = primary == 'valid' if item['id'] == 'retired-key-delayed-request' else primary == 'valid' or fallback == 'valid'
                if buggy != recorded['synthetic_bug_accept']:
                    raise InputError('Legacy calibration replay disagrees.')
            if accepted:
                used.add(body['nonce'])
            checks.append({'case': item['id'], 'accepted': accepted})
    return {'replayed': True, 'checks': len(checks), 'signature_verdicts': count,
            'source_hashes': HASHES, 'private_keys_required': False,
            'scope': 'Pinned probe-v1 ceremony and operation replay; not a second cryptographic implementation.'}
