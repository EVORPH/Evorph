"""Bounded exhaustive sweep of registration sequences against a written oracle.

The oracle restates the proposal's state rules from the text in
``research/eip8450-proposal-a8a412e.md`` and is deliberately not a copy of the
model's validation code. Proofs are always valid in this sweep, so only the
state rules differ between the two. A divergence is a candidate defect in either
side; it is not a finding about Ethereum.

Run: python3 sweep.py
"""

from __future__ import annotations

import itertools
import json

from registry import (
    FAR_FUTURE_EPOCH,
    ZERO_XMSS_PUBLIC_KEY,
    PQKeyRegistration,
    PQPublicKeys,
    RegistryConfig,
    RegistryState,
    SignedPQKeyRegistration,
    ValidatorEntry,
    process_pq_key_registration,
)

DELAY = 4
VALIDATOR_COUNT = 2
MAX_SEQUENCE = 3
EPOCHS = range(0, 2 * DELAY + 1)
STUB_PROOF = b"\x00\x00\x00\x00" + b"\xb0" * 48 + b"\xa1" * 32 + b"\x01" + b"\x11" * 50

# Actions: which validator, which new key, and whether it replays a stale epoch.
ACTION_ALPHABET = [
    ("first", 0, 0),
    ("first", 1, 1),
    ("change", 0, 1),
    ("change", 1, 0),
    ("stale_replay", 0, 2),
    ("stale_replay", 1, 2),
    ("poach", 1, 0),
]


def valid_proof(bls_pubkey: bytes, xmss_pubkey: bytes) -> bytes:
    return (
        int(0).to_bytes(4, "little")
        + bls_pubkey
        + xmss_pubkey
        + b"\x01"
        + b"\x11" * (len(STUB_PROOF) - 85)
    )


def resolve_poach_key(owner_key):
    """One shared definition of the poaching target for both sides.

    An owner without a registered key has either the unset value or the all-zero
    entry, which are treated alike so the action means the same thing in the
    model and in the oracle.
    """
    if owner_key in (None, ZERO_XMSS_PUBLIC_KEY):
        return bytes([0xA0]) * 32
    return owner_key


def build_signed(
    state: RegistryState, action: str, index: int, generation: int
) -> SignedPQKeyRegistration:
    stored = state.validators[index]
    xmss = bytes([0xA0 + generation]) * 32
    if action == "stale_replay":
        previous = 2
    elif action == "first":
        previous = FAR_FUTURE_EPOCH
    else:
        previous = stored.key_change_epoch
    if action == "poach":
        previous = stored.key_change_epoch
        xmss = resolve_poach_key(state.validators[0].public_keys.xmss_public_key)
    message = PQKeyRegistration(
        validator_index=index,
        public_keys=PQPublicKeys(xmss_public_key=xmss, vrf_root=bytes([0xC0]) * 32),
        previous_change_epoch=previous,
    )
    return SignedPQKeyRegistration(
        message=message,
        possession_signature=valid_proof(stored.bls_pubkey, xmss),
        registration_signature=stored.bls_pubkey + b"\x22" * 48,
    )


def oracle_accepts(actions, epoch):
    """Apply the proposal's state rules independently and return (accepted, rule)."""
    stored = {i: {"change": FAR_FUTURE_EPOCH, "xmss": None} for i in range(VALIDATOR_COUNT)}
    for position, (action, index, generation) in enumerate(actions):
        xmss = bytes([0xA0 + generation]) * 32
        if action == "poach":
            xmss = resolve_poach_key(stored[0]["xmss"])
        entry = stored[index]
        registered = entry["change"] != FAR_FUTURE_EPOCH
        previous = (
            2
            if action == "stale_replay"
            else (FAR_FUTURE_EPOCH if action == "first" else entry["change"])
        )
        if previous != entry["change"]:
            return False, "PREVIOUS_EPOCH_MISMATCH", position
        if registered and epoch < entry["change"] + DELAY:
            return False, "COOLDOWN_NOT_MET", position
        if any(other["change"] != FAR_FUTURE_EPOCH and other["xmss"] == xmss for other in stored.values()):
            return False, "KEY_ALREADY_IN_USE", position
        entry["change"] = epoch
        entry["xmss"] = xmss
    return True, None, len(actions)


def initial_state() -> RegistryState:
    return RegistryState(
        tuple(ValidatorEntry(bls_pubkey=bytes([0xB0 + i]) * 48) for i in range(VALIDATOR_COUNT))
    )


def run_model(actions, epoch):
    state = initial_state()
    for position, action in enumerate(actions):
        signed = build_signed(state, *action)
        outcome = process_pq_key_registration(
            state, signed, current_epoch=epoch, config=RegistryConfig(min_change_delay=DELAY)
        )
        if not outcome.accepted:
            return False, outcome.reason, position
        state = outcome.state
    return True, None, len(actions)


def main() -> dict:
    sequences = []
    for length in range(1, MAX_SEQUENCE + 1):
        sequences.extend(itertools.product(ACTION_ALPHABET, repeat=length))

    divergences = []
    verdict_counts = {}
    for epoch in EPOCHS:
        for actions in sequences:
            model = run_model(actions, epoch)
            oracle = oracle_accepts(actions, epoch)
            verdict_counts[(model[0], model[1])] = verdict_counts.get((model[0], model[1]), 0) + 1
            if (model[0], model[1]) != (oracle[0], oracle[1]):
                divergences.append(
                    {
                        "epoch": epoch,
                        "actions": [list(a) for a in actions],
                        "model": [model[0], model[1], model[2]],
                        "oracle": [oracle[0], oracle[1], oracle[2]],
                    }
                )

    return {
        "sequences_per_epoch": len(sequences),
        "epochs": len(list(EPOCHS)),
        "comparisons": len(sequences) * len(list(EPOCHS)),
        "divergences": len(divergences),
        "divergence_examples": divergences[:5],
        "model_verdict_tally": {
            f"{accepted}|{reason or 'ACCEPTED'}": count
            for (accepted, reason), count in sorted(
                verdict_counts.items(), key=lambda item: -item[1]
            )
        },
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
