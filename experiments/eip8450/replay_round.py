"""Replay a recorded model round offline, with no model and no network.

The sequences in ``round_replay.json`` were proposed by one model call and are
reproduced here verbatim so that anyone can re-check what was reported. Each
sequence runs through the shipped rules model and through the separately written
statement of the same rules, at every recorded epoch, and the two verdicts are
compared on acceptance, rejection reason and first failing position. Full state
is not compared; that is the job of the rule tests.

Run: python3 replay_round.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from oracle import (
    ACTIONS,
    DELAY,
    SENTINEL,
    bls_pubkey,
    oracle_verdict,
    resolve_poach_key,
    valid_proof,
    xmss_public_key,
)
from registry import (
    FAR_FUTURE_EPOCH,
    PQKeyRegistration,
    PQPublicKeys,
    RegistryConfig,
    RegistryState,
    SignedPQKeyRegistration,
    ValidatorEntry,
    process_pq_key_registration,
)

HERE = Path(__file__).resolve().parent


def build_signed(state: RegistryState, name: str, generation: int) -> SignedPQKeyRegistration:
    """Fixture construction only: it builds a message, it decides nothing."""
    action, index = ACTIONS[name]
    stored = state.validators[index]
    new_key = xmss_public_key(generation)
    if action == "register":
        previous = FAR_FUTURE_EPOCH
    elif action == "stale_replay":
        previous = 2
    else:
        previous = stored.key_change_epoch
    if action == "poach":
        new_key = resolve_poach_key(state.validators[0].public_keys.xmss_public_key)
    message = PQKeyRegistration(
        validator_index=index,
        public_keys=PQPublicKeys(xmss_public_key=new_key, vrf_root=bytes([0xC0]) * 32),
        previous_change_epoch=previous,
    )
    leaf = 1 if action == "register_wrong_leaf" else 0
    return SignedPQKeyRegistration(
        message=message,
        possession_signature=valid_proof(stored.bls_pubkey, new_key, leaf),
        registration_signature=stored.bls_pubkey + b"\x22" * 48,
    )


def model_verdict(steps, config):
    state = RegistryState(tuple(ValidatorEntry(bls_pubkey=bls_pubkey(i)) for i in (0, 1)))
    for position, (name, generation, epoch) in enumerate(steps):
        index = ACTIONS[name][1]
        if ACTIONS[name][0] == "register_after_exit":
            entries = list(state.validators)
            entries[index] = ValidatorEntry(
                bls_pubkey=entries[index].bls_pubkey,
                exit_epoch=epoch,
                public_keys=entries[index].public_keys,
                key_change_epoch=entries[index].key_change_epoch,
            )
            state = RegistryState(tuple(entries))
        signed = build_signed(state, name, generation)
        outcome = process_pq_key_registration(
            state, signed, current_epoch=epoch, config=config
        )
        if not outcome.accepted:
            return {"accepted": False, "reason": outcome.reason, "position": position}
        state = outcome.state
    return {"accepted": True, "reason": None, "position": len(steps)}


def main() -> int:
    record = json.loads((HERE / "round_replay.json").read_text())
    config = RegistryConfig(min_change_delay=DELAY)
    comparisons = agreements = 0
    failures = []

    for sequence in record["sequences"]:
        steps = [
            (step["action"], step.get("generation", 0), step.get("epoch"))
            for step in sequence["steps"]
        ]
        for epoch in record["epochs"]:
            filled = [(name, gen, step_epoch if step_epoch is not None else epoch)
                      for name, gen, step_epoch in steps]
            expected = oracle_verdict(filled, epoch)
            actual = model_verdict(filled, config)
            comparisons += 1
            pair = (actual["accepted"], actual["reason"], actual["position"])
            want = (expected["accepted"], expected["reason"], expected["position"])
            if pair == want:
                agreements += 1
            else:
                failures.append({"sequence": sequence["label"], "epoch": epoch,
                                 "model": pair, "written_statement": want})

    summary = {
        "recorded_at_utc": record["recorded_at_utc"],
        "code_version": record["code_version"],
        "sequences": len(record["sequences"]),
        "comparisons": comparisons,
        "agreements": agreements,
        "disagreements": len(failures),
        "recorded_in_the_file": record["recorded"],
        "replay_matches_record": (
            comparisons == record["recorded"]["comparisons"]
            and agreements == record["recorded"]["agreements"]
        ),
    }
    print(json.dumps(summary, indent=2))
    for failure in failures[:5]:
        print("disagreement:", failure)
    return 0 if not failures and summary["replay_matches_record"] else 1


if __name__ == "__main__":
    sys.exit(main())
