"""Offline state model for EIP-8450 post-quantum key registration.

Scope is the registry state machine only: acceptance conditions, ordering and
rejection reasons. Proof checking is an injected callback, so the same rules run
against synthetic test doubles and against real leanXMSS and BLS code.

Reference: EIP-8450 draft, commit
a8a412e4a9355605bbe04fd01e8b2a03f399fb47, ``process_pq_key_registration``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

FAR_FUTURE_EPOCH = 2**64 - 1
PQ_POSSESSION_XMSS_INDEX = 0
XMSS_SIGNATURE_SIZE = 2404
PRESET_MAIN_DELAY = 2**8
PRESET_MINIMAL_DELAY = 2**3
MIN_DELAY_FLOOR = 1
ZERO_XMSS_PUBLIC_KEY = b"\x00" * 32
ZERO_VRF_ROOT = b"\x00" * 32

REASON_INDEX_OUT_OF_RANGE = "INDEX_OUT_OF_RANGE"
REASON_EXIT_INITIATED = "EXIT_INITIATED"
REASON_PREVIOUS_EPOCH_MISMATCH = "PREVIOUS_EPOCH_MISMATCH"
REASON_COOLDOWN_NOT_MET = "COOLDOWN_NOT_MET"
REASON_AUTHORIZATION_INVALID = "AUTHORIZATION_INVALID"
REASON_KEY_ALREADY_IN_USE = "KEY_ALREADY_IN_USE"
REASON_POSSESSION_INVALID = "POSSESSION_INVALID"


def xmss_signature_index(signature: bytes) -> int:
    """A signature carries its leaf index in its first four bytes, little-endian."""
    return int.from_bytes(signature[0:4], "little")


@dataclass(frozen=True)
class RegistryConfig:
    min_change_delay: int = PRESET_MAIN_DELAY

    def __post_init__(self) -> None:
        if self.min_change_delay < MIN_DELAY_FLOOR:
            raise ValueError("min_change_delay must be a positive integer")


@dataclass(frozen=True)
class PQPublicKeys:
    xmss_public_key: bytes = ZERO_XMSS_PUBLIC_KEY
    vrf_root: bytes = ZERO_VRF_ROOT


@dataclass(frozen=True)
class ValidatorEntry:
    bls_pubkey: bytes
    exit_epoch: int = FAR_FUTURE_EPOCH
    public_keys: PQPublicKeys = PQPublicKeys()
    key_change_epoch: int = FAR_FUTURE_EPOCH
    # Metadata only. The proposal allows registration once a validator has an
    # index, which can be before activation, so no rule reads this field.
    activation_epoch: int = FAR_FUTURE_EPOCH

    @property
    def has_initiated_exit(self) -> bool:
        return self.exit_epoch != FAR_FUTURE_EPOCH


@dataclass(frozen=True)
class RegistryState:
    validators: tuple[ValidatorEntry, ...] = ()

    def is_pq_registered(self, index: int) -> bool:
        return self.validators[index].key_change_epoch != FAR_FUTURE_EPOCH

    def is_xmss_public_key_in_use(self, xmss_public_key: bytes) -> bool:
        """Match registered entries only, so an unset zero entry never collides."""
        return any(
            entry.public_keys.xmss_public_key == xmss_public_key
            for entry in self.validators
            if entry.key_change_epoch != FAR_FUTURE_EPOCH
        )


@dataclass(frozen=True)
class PQKeyRegistration:
    validator_index: int
    public_keys: PQPublicKeys
    previous_change_epoch: int


@dataclass(frozen=True)
class SignedPQKeyRegistration:
    """A registration as it appears in a block: message plus both proofs.

    ``possession_signature`` is a leanXMSS signature at leaf 0 of the new key
    over the possession of that key for the validator's BLS public key.
    ``registration_signature`` is the BLS signature that authorizes the message.
    """

    message: PQKeyRegistration
    possession_signature: bytes
    registration_signature: bytes


@dataclass(frozen=True)
class Outcome:
    accepted: bool
    reason: str | None = None
    state: RegistryState | None = None


def synthetic_authorizer(
    validator: ValidatorEntry, message: PQKeyRegistration, registration_signature: bytes
) -> bool:
    """Test double for BLS verification of the registration message.

    The stub carries the claiming BLS public key, which is the only thing a
    synthetic path can compare. It proves nothing about signatures.
    """
    return registration_signature[:48] == validator.bls_pubkey


def synthetic_possession_checker(
    validator: ValidatorEntry, xmss_public_key: bytes, possession_signature: bytes
) -> bool:
    """Test double for leanXMSS verification of the possession binding."""
    if len(possession_signature) < 85:
        return False
    claimed_bls = possession_signature[4:52]
    claimed_xmss = possession_signature[52:84]
    marked_valid = possession_signature[84:85] == b"\x01"
    return (
        marked_valid
        and claimed_bls == validator.bls_pubkey
        and claimed_xmss == xmss_public_key
    )


def _validate(
    state: RegistryState,
    signed: SignedPQKeyRegistration,
    current_epoch: int,
    config: RegistryConfig,
    authorizer,
    possession_checker,
) -> str | None:
    """Return the first failing rule, evaluated in specification order."""
    message = signed.message
    if message.validator_index >= len(state.validators) or message.validator_index < 0:
        return REASON_INDEX_OUT_OF_RANGE

    index = message.validator_index
    validator = state.validators[index]
    if validator.has_initiated_exit:
        return REASON_EXIT_INITIATED
    if message.previous_change_epoch != validator.key_change_epoch:
        return REASON_PREVIOUS_EPOCH_MISMATCH

    # A first registration is not rate limited; the sentinel is never advanced.
    if state.is_pq_registered(index) and current_epoch < (
        validator.key_change_epoch + config.min_change_delay
    ):
        return REASON_COOLDOWN_NOT_MET
    if not authorizer(validator, message, signed.registration_signature):
        return REASON_AUTHORIZATION_INVALID
    if state.is_xmss_public_key_in_use(message.public_keys.xmss_public_key):
        return REASON_KEY_ALREADY_IN_USE
    if xmss_signature_index(signed.possession_signature) != PQ_POSSESSION_XMSS_INDEX:
        return REASON_POSSESSION_INVALID
    if not possession_checker(validator, message.public_keys.xmss_public_key, signed.possession_signature):
        return REASON_POSSESSION_INVALID
    return None


def process_pq_key_registration(
    state: RegistryState,
    signed: SignedPQKeyRegistration,
    current_epoch: int,
    config: RegistryConfig | None = None,
    authorizer=synthetic_authorizer,
    possession_checker=synthetic_possession_checker,
) -> Outcome:
    """Apply one registration to a new state, or return a rejection.

    Entries are frozen and rebuilt on success, so a rejection cannot leave a
    partial write in the state it was given.
    """
    config = config or RegistryConfig()
    reason = _validate(state, signed, current_epoch, config, authorizer, possession_checker)
    if reason is not None:
        return Outcome(accepted=False, reason=reason, state=None)

    entries = list(state.validators)
    entries[signed.message.validator_index] = replace(
        entries[signed.message.validator_index],
        public_keys=signed.message.public_keys,
        key_change_epoch=current_epoch,
    )
    return Outcome(accepted=True, reason=None, state=RegistryState(tuple(entries)))


def apply_block(
    state: RegistryState,
    registrations: tuple[SignedPQKeyRegistration, ...],
    current_epoch: int,
    config: RegistryConfig | None = None,
    authorizer=synthetic_authorizer,
    possession_checker=synthetic_possession_checker,
) -> Outcome:
    """Process registrations in order against the evolving state.

    A failing registration invalidates the whole block, so no state is returned.
    """
    working = state
    for position, signed in enumerate(registrations):
        outcome = process_pq_key_registration(
            working, signed, current_epoch, config, authorizer, possession_checker
        )
        if not outcome.accepted:
            return Outcome(
                accepted=False, reason=f"{outcome.reason}@{position}", state=None
            )
        working = outcome.state
    return Outcome(accepted=True, reason=None, state=working)
