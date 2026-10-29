"""Real leanXMSS and BLS path for the EIP-8450 registry rules.

Dependencies are the ones the pinned consensus-specs pull request names:
``py-lean-pq`` 0.7.0 for leanXMSS and ``eth-consensus-specs`` for SSZ, signing
roots and BLS. The containers, domain types and the possession leaf index come
from the proposal text at commit a8a412e4a9355605bbe04fd01e8b2a03f399fb47.
Nothing in this module invents a cryptographic construction.
"""

from __future__ import annotations

import hashlib

from registry import PQKeyRegistration, PQPublicKeys, SignedPQKeyRegistration

DEPENDENCY_NAME = "py-lean-pq==0.7.0, eth-consensus-specs==1.7.0a13"

try:
    import py_lean_pq
    from eth_consensus_specs.heze import spec as SPEC
    from eth_consensus_specs.utils import bls as BLS
    from remerkleable.basic import uint64
    from remerkleable.byte_arrays import Bytes32, Bytes48
    from remerkleable.complex import Container

    DEPENDENCIES_AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - environment dependent
    DEPENDENCIES_AVAILABLE = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

XMSS_SIGNATURE_SIZE = 2404
GENESIS_VALIDATORS_ROOT = b"\x11" * 32
DOMAIN_PQ_KEY_REGISTRATION = bytes.fromhex("12000000")
DOMAIN_PQ_KEY_POSSESSION = bytes.fromhex("13000000")


if DEPENDENCIES_AVAILABLE:

    class SszPQPublicKeys(Container):
        xmss_public_key: Bytes32
        vrf_root: Bytes32

    class SszPQKeyRegistration(Container):
        validator_index: uint64
        public_keys: SszPQPublicKeys
        previous_change_epoch: uint64

    class SszPQKeyPossession(Container):
        validator_pubkey: Bytes48
        xmss_public_key: Bytes32


def xmss_seed(validator_index: int, generation: int = 0) -> bytes:
    """The deterministic seed layout used by the pinned consensus tests."""
    return hashlib.sha256(
        b"xmss"
        + int(validator_index).to_bytes(8, "little")
        + int(generation).to_bytes(8, "little")
    ).digest()


def xmss_public_key(seed: bytes) -> bytes:
    return bytes(py_lean_pq.XmssSecretKey.from_seed(seed).public_key)


def xmss_sign(seed: bytes, message: bytes, leaf_index: int) -> bytes:
    secret = py_lean_pq.XmssSecretKey.from_seed(seed)
    return bytes(secret.sign(message, int(leaf_index)))


def xmss_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify without raising, as the proposal requires of ``xmss.Verify``."""
    try:
        py_lean_pq.xmss_verify(bytes(public_key), bytes(message), bytes(signature))
    except (py_lean_pq.LeanPqError, ValueError):
        return False
    return True


def possession_signing_root(
    bls_pubkey: bytes, xmss_pubkey: bytes, genesis_validators_root: bytes = GENESIS_VALIDATORS_ROOT
) -> bytes:
    """The message a proof of possession signs, bound to both keys."""
    possession = SszPQKeyPossession(
        validator_pubkey=Bytes48(bls_pubkey), xmss_public_key=Bytes32(xmss_pubkey)
    )
    domain = SPEC.compute_domain(
        SPEC.DomainType(DOMAIN_PQ_KEY_POSSESSION),
        genesis_validators_root=SPEC.Root(genesis_validators_root),
    )
    return bytes(SPEC.compute_signing_root(possession, domain))


def registration_signing_root(
    message: PQKeyRegistration, genesis_validators_root: bytes = GENESIS_VALIDATORS_ROOT
) -> bytes:
    container = SszPQKeyRegistration(
        validator_index=uint64(message.validator_index),
        public_keys=SszPQPublicKeys(
            xmss_public_key=Bytes32(message.public_keys.xmss_public_key),
            vrf_root=Bytes32(message.public_keys.vrf_root),
        ),
        previous_change_epoch=uint64(message.previous_change_epoch),
    )
    domain = SPEC.compute_domain(
        SPEC.DomainType(DOMAIN_PQ_KEY_REGISTRATION),
        genesis_validators_root=SPEC.Root(genesis_validators_root),
    )
    return bytes(SPEC.compute_signing_root(container, domain))


def real_authorizer(validator, message: PQKeyRegistration, registration_signature: bytes) -> bool:
    if not BLS.bls_active:
        raise RuntimeError("BLS is inactive; refusing to treat a stub as verification")
    root = registration_signing_root(message)
    try:
        return bool(BLS.Verify(validator.bls_pubkey, root, registration_signature))
    except Exception:
        return False


def real_possession_checker(
    validator, xmss_public_key: bytes, possession_signature: bytes
) -> bool:
    root = possession_signing_root(validator.bls_pubkey, xmss_public_key)
    return xmss_verify(xmss_public_key, root, possession_signature)


def sign_registration(
    message: PQKeyRegistration,
    xmss_seed_bytes: bytes,
    bls_secret_key: bytes,
    leaf_index: int = 0,
    possession_bls_pubkey: bytes | None = None,
) -> SignedPQKeyRegistration:
    """Build a genuinely signed registration for the given state message."""
    xmss_pubkey = message.public_keys.xmss_public_key
    root = possession_signing_root(
        possession_bls_pubkey if possession_bls_pubkey is not None else BLS.SkToPk(bls_secret_key),
        xmss_pubkey,
    )
    return SignedPQKeyRegistration(
        message=message,
        possession_signature=xmss_sign(xmss_seed_bytes, root, leaf_index),
        registration_signature=bytes(BLS.Sign(bls_secret_key, registration_signing_root(message))),
    )


def bls_public_key(secret_key: bytes) -> bytes:
    return bytes(BLS.SkToPk(secret_key))
