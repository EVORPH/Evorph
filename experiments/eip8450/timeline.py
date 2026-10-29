"""Generate the chapter 2 timeline data from the registry model itself.

Nothing here is hand-written as a result: every row is produced by running the
event through ``registry.process_pq_key_registration`` and recording the state
before, the request, the verdict and the state after. The output is the payload
the page renders, so the page cannot drift from the model.

Run: python3 timeline.py
"""

from __future__ import annotations

import datetime
import json

from oracle import DELAY, SENTINEL, bls_pubkey, resolve_poach_key, valid_proof, xmss_public_key
from registry import (
    PQKeyRegistration,
    PQPublicKeys,
    RegistryConfig,
    RegistryState,
    SignedPQKeyRegistration,
    ValidatorEntry,
    process_pq_key_registration,
)

CONFIG = RegistryConfig(min_change_delay=DELAY)
VRF = bytes([0xC0]) * 32

SCENARIO = [
    ("register", 0, 0, 0, "validator 0 registers its first post-quantum key"),
    ("register", 1, 1, 1, "validator 1 registers a different key"),
    ("stale", 0, 2, 1, "an old registration is replayed after it already applied"),
    ("change", 0, 3, 2, "validator 0 tries to change keys before the delay"),
    ("change", 0, 2, 4, "validator 0 changes keys exactly at the delay boundary"),
    ("vrf_only", 0, 4, 8, "validator 0 keeps its key and changes only the VRF root"),
    ("poach", 1, None, 8, "validator 1 tries to take validator 0's current key"),
    ("release", 1, 0, 8, "validator 1 registers the key validator 0 released"),
    ("exited", 2, 5, 8, "a validator that has initiated an exit tries to register"),
]


def build(state: RegistryState, kind: str, index: int, generation, epoch: int):
    stored = state.validators[index]
    new_key = xmss_public_key(generation) if generation is not None else None
    if kind == "poach":
        new_key = resolve_poach_key(state.validators[0].public_keys.xmss_public_key)
    elif kind == "vrf_only":
        new_key = stored.public_keys.xmss_public_key
    previous = {
        "register": SENTINEL,
        "stale": 2,
    }.get(kind, stored.key_change_epoch)
    message = PQKeyRegistration(
        validator_index=index,
        public_keys=PQPublicKeys(xmss_public_key=new_key, vrf_root=bytes([epoch]) * 32 if kind == "vrf_only" else VRF),
        previous_change_epoch=previous,
    )
    return SignedPQKeyRegistration(
        message=message,
        possession_signature=valid_proof(stored.bls_pubkey, new_key),
        registration_signature=stored.bls_pubkey + b"\x22" * 48,
    )


def snapshot(state: RegistryState, index: int) -> dict:
    entry = state.validators[index]
    return {
        "registered": entry.key_change_epoch != SENTINEL,
        "last_change_epoch": None if entry.key_change_epoch == SENTINEL else entry.key_change_epoch,
        "xmss_public_key": entry.public_keys.xmss_public_key.hex()[:16],
        "exited": entry.exit_epoch != SENTINEL,
    }


def run():
    # Validator 2 exists only to be in an exited state.
    state = RegistryState(
        (
            ValidatorEntry(bls_pubkey=bls_pubkey(0)),
            ValidatorEntry(bls_pubkey=bls_pubkey(1)),
            ValidatorEntry(bls_pubkey=bls_pubkey(2), exit_epoch=7),
        )
    )
    rows = []
    for kind, index, generation, epoch, label in SCENARIO:
        signed = build(state, kind, index, generation, epoch)
        before = snapshot(state, index)
        outcome = process_pq_key_registration(state, signed, current_epoch=epoch, config=CONFIG)
        rows.append(
            {
                "epoch": epoch,
                "validator_index": index,
                "event": kind,
                "label": label,
                "request": {
                    "previous_change_epoch": None
                    if signed.message.previous_change_epoch == SENTINEL
                    else signed.message.previous_change_epoch,
                    "xmss_public_key": signed.message.public_keys.xmss_public_key.hex()[:16],
                    "vrf_root": signed.message.public_keys.vrf_root.hex()[:16],
                    "possession_leaf": int.from_bytes(signed.possession_signature[:4], "little"),
                },
                "accepted": outcome.accepted,
                "reason": outcome.reason,
                "before": before,
                "after": snapshot(outcome.state, index) if outcome.accepted else before,
                "state_changed": outcome.accepted,
            }
        )
        if outcome.accepted:
            state = outcome.state
    return rows


def main() -> None:
    rows = run()
    payload = {
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
        "minimum_change_delay_epochs": DELAY,
        "spec_commit": "a8a412e4a9355605bbe04fd01e8b2a03f399fb47",
        "evidence_tier": "rule model, synthetic proofs",
        "rows": rows,
        "totals": {
            "events": len(rows),
            "accepted": sum(row["accepted"] for row in rows),
            "rejected": sum(not row["accepted"] for row in rows),
        },
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
