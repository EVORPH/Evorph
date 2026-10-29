"""Written oracle for the registry rules, independent of the model.

Every condition below is transcribed from the proposal text at commit
a8a412e4a9355605bbe04fd01e8b2a03f399fb47 (process_pq_key_registration) and the
case list in its Test Cases section. This module imports nothing from
``registry`` on purpose: the loop compares two independent statements of the
same rules, so agreement is evidence and disagreement is a lead.
"""

from __future__ import annotations

DELAY = 4
VALIDATOR_COUNT = 2
FAR_FUTURE = (2, 64)  # marker for the never-registered sentinel
SENTINEL = 2**64 - 1

# Named actions keep proposed sequences readable for a model.
ACTIONS = {
    "first0": ("register", 0),
    "first1": ("register", 1),
    "change0": ("change", 0),
    "change1": ("change", 1),
    "poach1": ("poach", 1),
    "stale0": ("stale_replay", 0),
    "stale1": ("stale_replay", 1),
    "exited0": ("register_after_exit", 0),
    "leaf1_0": ("register_wrong_leaf", 0),
}


def bls_pubkey(index: int) -> bytes:
    return bytes([0xB0 + index]) * 48


def xmss_public_key(generation: int) -> bytes:
    return bytes([0xA0 + generation]) * 32


def valid_proof(claimed_bls: bytes, claimed_xmss: bytes, leaf: int = 0) -> bytes:
    return (
        int(leaf).to_bytes(4, "little")
        + claimed_bls
        + claimed_xmss
        + b"\x01"
        + b"\x11" * 50
    )


def resolve_poach_key(owner_key):
    """The key the poaching action grabs: the owner's current one, or a fresh one."""
    if owner_key is None or owner_key == b"\x00" * 32:
        return xmss_public_key(0)
    return owner_key


def _state_stub():
    return {
        i: {"change": SENTINEL, "xmss": None, "exit": SENTINEL}
        for i in range(VALIDATOR_COUNT)
    }


def oracle_verdict(sequence, epoch):
    """Return the first rule the proposal breaks, or acceptance.

    ``sequence`` is a list of ``(action_name, generation)`` pairs. Proofs are
    assumed valid unless the action names a stale previous epoch or a wrong
    leaf, so this judges state rules and the two named proof defects only.
    """
    state = _state_stub()
    for position, step in enumerate(sequence):
        name, generation = step[0], step[1]
        step_epoch = step[2] if len(step) > 2 else epoch
        action, index = ACTIONS[name]
        entry = state[index]
        if action in ("register", "register_wrong_leaf"):
            new_key = xmss_public_key(generation)
            previous = SENTINEL
        elif action == "change":
            new_key = xmss_public_key(generation)
            previous = entry["change"]
        elif action == "poach":
            new_key = resolve_poach_key(state[0]["xmss"])
            previous = entry["change"]
        elif action == "stale_replay":
            new_key = xmss_public_key(generation)
            previous = 2
        elif action == "register_after_exit":
            new_key = xmss_public_key(generation)
            previous = entry["change"]
            entry["exit"] = step_epoch  # exit already initiated when this is processed
        else:  # pragma: no cover - guard for unknown action names
            raise ValueError(f"unknown action {name}")

        if entry["exit"] != SENTINEL:
            return {"accepted": False, "reason": "EXIT_INITIATED", "position": position}
        if previous != entry["change"]:
            return {"accepted": False, "reason": "PREVIOUS_EPOCH_MISMATCH", "position": position}
        registered = entry["change"] != SENTINEL
        if registered and step_epoch < entry["change"] + DELAY:
            return {"accepted": False, "reason": "COOLDOWN_NOT_MET", "position": position}
        if any(
            other["change"] != SENTINEL and other["xmss"] == new_key
            for other in state.values()
        ):
            return {"accepted": False, "reason": "KEY_ALREADY_IN_USE", "position": position}

        if action == "register_wrong_leaf":
            return {"accepted": False, "reason": "POSSESSION_INVALID", "position": position}

        entry["change"] = step_epoch
        entry["xmss"] = new_key

    return {"accepted": True, "reason": None, "position": len(sequence)}


def describe_rules() -> str:
    """The fixed rule text handed to both roles, so neither can renegotiate it."""
    return (
        "Rules (pinned EIP-8450 draft):\n"
        "1. The validator index must exist.\n"
        "2. A validator that has initiated an exit cannot register.\n"
        "3. previous_change_epoch must equal the stored change epoch; a first "
        "registration names the never-registered sentinel.\n"
        "4. Only an already-registered validator is rate limited: the current epoch "
        "must be at least stored epoch plus the minimum change delay.\n"
        "5. The validator's signing key authorizes the registration.\n"
        "6. The new key must not be the current key of any validator, including the "
        "registering one; an unregistered validator holds no key.\n"
        "7. The possession proof must be signed at leaf 0 and bind the signing key "
        "identity and the new key together.\n"
        "A VRF root may repeat. A key released by a change becomes available again.\n"
    )
