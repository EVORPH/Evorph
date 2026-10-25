import base64
import tempfile
import time
from pathlib import Path

from .contracts import PQ, ExecutionError, InputError, digest, parse_json
from .crypto import Crypto
from .evidence import read_file

COMMIT = '975de31eb83d87039ec88934fdc47d8c312b892d'
HASHES = {
    'prompt.json': 'e2cba4589389756fa0bea1a7e6837138bf0a81f9d14234c9ee8f6d33caa1654e',
    'expectedResults.json': 'e1d84ef1b2f35196278ab0b0ed6a46ec62cc03d2dfa92c564199e1999bfb8ea6',
    'registration.json': '55878cee2c7a8cf275f4138231f1b5f98880ff81a99face609207415053fb859',
    'UPSTREAM_NOTICE.txt': 'd5a569884ee83bd1c4737042d0a2cc7d68c6950690f75a73ef14f505a9aa3555',
}


def public_pem(raw):
    if len(raw) != 1312:
        raise InputError('Unexpected ML-DSA-44 public key size.')

    def tlv(tag, body):
        length = len(body)
        size = bytes([length]) if length < 128 else (b'\x82' + length.to_bytes(2, 'big'))
        return bytes([tag]) + size + body

    oid = bytes.fromhex('608648016503040311')
    der = tlv(0x30, tlv(0x30, tlv(0x06, oid)) + tlv(0x03, b'\x00' + raw))
    encoded = base64.b64encode(der)
    return b'-----BEGIN PUBLIC KEY-----\n' + b'\n'.join(encoded[i:i + 64] for i in range(0, len(encoded), 64)) + b'\n-----END PUBLIC KEY-----\n'


def verify(directory, binary=None):
    directory = Path(directory)
    files = {name: read_file(directory, name, 4194304) for name in HASHES}
    if any(digest(files[name]) != expected for name, expected in HASHES.items()):
        raise InputError('Official vector snapshot integrity mismatch.')
    prompt = parse_json(files['prompt.json'], 4194304)
    expected = parse_json(files['expectedResults.json'], 4194304)
    answers = {(group['tgId'], item['tcId']): item['testPassed']
               for group in expected['testGroups'] for item in group['tests']}
    results, excluded = [], []
    with tempfile.TemporaryDirectory(prefix='evorph-vectors-') as temporary:
        crypto = Crypto(temporary, binary, time.monotonic() + 60)
        environment = crypto.capabilities()
        for group in prompt['testGroups']:
            applicable = (group['parameterSet'] == PQ and group['signatureInterface'] == 'external'
                          and group.get('preHash') == 'pure')
            if not applicable:
                excluded.append({'group': group['tgId'], 'cases': len(group['tests']),
                                 'reason': 'Different parameter set, prehash or internal interface.'})
                continue
            for item in group['tests']:
                crypto.register_public('pq', public_pem(bytes.fromhex(item['pk'])), PQ)
                try:
                    status = crypto.verify('pq', bytes.fromhex(item['message']),
                                           bytes.fromhex(item['signature']), bytes.fromhex(item['context']))
                except ExecutionError:
                    status = 'error'
                answer = answers[(group['tgId'], item['tcId'])]
                results.append({'group': group['tgId'], 'case': item['tcId'], 'expected': answer,
                                'observed': status, 'matches': status == ('valid' if answer else 'invalid')})
    if not results:
        raise InputError('No matching official vectors.')
    return {'source': 'usnistgov/ACVP-Server', 'commit': COMMIT, 'source_hashes': HASHES,
            'parameter_set': PQ, 'interface': 'external/pure', 'results': results,
            'passed': sum(item['matches'] for item in results), 'applicable': len(results),
            'excluded': excluded, 'environment': environment, 'certification': False}
