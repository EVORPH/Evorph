import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .contracts import CONTEXT, LEGACY, PQ, SPEC, ExecutionError, InputError


def run_process(command, cwd, timeout=10, output_limit=262144):
    started = time.monotonic()
    output = [bytearray(), bytearray()]
    try:
        process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env={**os.environ, 'LC_ALL': 'C'})
    except OSError as exc:
        raise ExecutionError('Process unavailable.') from exc
    selector = selectors.DefaultSelector()
    for index, stream in enumerate((process.stdout, process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, index)
    try:
        while selector.get_map() or process.poll() is None:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ExecutionError('Process deadline exceeded.')
            for key, _ in selector.select(min(remaining, 0.05)):
                data = os.read(key.fd, 16384)
                if not data:
                    selector.unregister(key.fileobj)
                else:
                    output[key.data].extend(data)
                    if sum(map(len, output)) > output_limit:
                        raise ExecutionError('Process output limit exceeded.')
        return process.returncode, bytes(output[0]), bytes(output[1])
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        selector.close()
        process.stdout.close()
        process.stderr.close()


class Crypto:
    def __init__(self, scratch, binary=None, deadline=None):
        selected = binary or os.environ.get('EVORPH_OPENSSL') or shutil.which('openssl')
        if not selected:
            raise ExecutionError('OpenSSL is unavailable.')
        self.binary = str(Path(selected).resolve())
        self.scratch = Path(scratch)
        self.deadline = deadline
        self.keys = {}

    def run(self, args, verify=False):
        limit = SPEC['limits']['process_seconds']
        if self.deadline is not None:
            limit = min(limit, self.deadline - time.monotonic())
        if limit <= 0:
            raise ExecutionError('Task deadline exceeded.')
        code, out, err = run_process([self.binary] + args, self.scratch, limit,
                                     SPEC['limits']['process_output_bytes'])
        if verify:
            if code == 0:
                return 'valid'
            if code == 1 and b'Signature Verification Failure' in out:
                return 'invalid'
            raise ExecutionError('Signature verifier execution failed.')
        if code != 0:
            raise ExecutionError('Cryptographic operation failed.')
        return out

    def capabilities(self):
        version = self.run(['version']).decode('ascii').strip()
        algorithms = self.run(['list', '-signature-algorithms'])
        if b'ML-DSA-44' not in algorithms or b'ECDSA' not in algorithms:
            raise ExecutionError('Required signature algorithms unavailable.')
        return {'openssl': version, 'algorithms': [LEGACY, PQ]}

    def keygen(self, name, algorithm):
        if name not in ('legacy', 'pq') or algorithm not in (LEGACY, PQ):
            raise InputError('Unknown key registration.')
        private = self.scratch / (name + '.key')
        public = self.scratch / (name + '.pub')
        args = ['genpkey', '-algorithm', 'EC' if algorithm == LEGACY else PQ]
        if algorithm == LEGACY:
            args += ['-pkeyopt', 'ec_paramgen_curve:P-256']
        self.run(args + ['-out', str(private)])
        self.run(['pkey', '-in', str(private), '-pubout', '-out', str(public)])
        self.keys[name] = (private, public, algorithm)
        return public.read_bytes()

    def register_public(self, name, raw, algorithm):
        if name not in ('legacy', 'pq') or len(raw) > 8192 or algorithm not in (LEGACY, PQ):
            raise InputError('Invalid public key registration.')
        if not raw.startswith(b'-----BEGIN PUBLIC KEY-----') or b'PRIVATE' in raw:
            raise InputError('Expected a public key.')
        path = self.scratch / (name + '.pub')
        path.write_bytes(raw)
        info = self.run(['pkey', '-pubin', '-in', str(path), '-text', '-noout'])
        if algorithm == LEGACY and b'ASN1 OID: prime256v1' not in info:
            raise InputError('Unexpected classical key type.')
        if algorithm == PQ and b'ML-DSA-44' not in info:
            raise InputError('Unexpected post-quantum key type.')
        self.keys[name] = (None, path, algorithm)

    @staticmethod
    def options(algorithm, context):
        if algorithm == LEGACY:
            return ['-rawin', '-digest', 'sha256']
        if len(context) > 255:
            raise InputError('Context exceeds algorithm limit.')
        return ['-rawin', '-pkeyopt', 'hexcontext-string:' + context.hex()]

    def sign(self, name, raw, context=CONTEXT):
        private, _, algorithm = self.keys[name]
        if private is None:
            raise InputError('Signing material unavailable.')
        msg, sig = self.scratch / 'message.bin', self.scratch / 'signature.bin'
        msg.write_bytes(raw)
        self.run(['pkeyutl', '-sign', '-inkey', str(private), '-in', str(msg), '-out', str(sig)]
                 + self.options(algorithm, context))
        return sig.read_bytes()

    def verify(self, name, raw, signature, context=CONTEXT):
        if name not in self.keys or len(raw) > 1048576 or len(signature) > 8192:
            raise InputError('Invalid signature input.')
        _, public, algorithm = self.keys[name]
        msg, sig = self.scratch / 'message.bin', self.scratch / 'signature.bin'
        msg.write_bytes(raw)
        sig.write_bytes(signature)
        return self.run(['pkeyutl', '-verify', '-pubin', '-inkey', str(public),
                         '-in', str(msg), '-sigfile', str(sig)] + self.options(algorithm, context), verify=True)
