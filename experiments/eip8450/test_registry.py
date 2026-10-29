"""Focused state-rule tests for the offline EIP-8450 registry model.

Expected results are taken from the proposal text at commit
a8a412e4a9355605bbe04fd01e8b2a03f399fb47, not from this implementation.
Cryptographic checks are synthetic test doubles, so a pass here shows that the
state rules match the specification wording; it shows nothing about signatures.
"""

import json
import unittest
from pathlib import Path
from copy import deepcopy

from registry import (
    FAR_FUTURE_EPOCH,
    PRESET_MAIN_DELAY,
    PRESET_MINIMAL_DELAY,
    PQKeyRegistration,
    PQPublicKeys,
    RegistryConfig,
    RegistryState,
    SignedPQKeyRegistration,
    ValidatorEntry,
    XMSS_SIGNATURE_SIZE,
    apply_block,
    process_pq_key_registration,
)

BLS_0 = b"\xb0" * 48
BLS_1 = b"\xb1" * 48
KEY_A = b"\xa1" * 32
KEY_B = b"\xb2" * 32
KEY_ZERO = b"\x00" * 32
VRF_1 = b"\xc1" * 32
VRF_2 = b"\xc2" * 32


def keys(xmss=KEY_A, vrf=VRF_1) -> PQPublicKeys:
    return PQPublicKeys(xmss_public_key=xmss, vrf_root=vrf)


def validator(bls=BLS_0, **overrides) -> ValidatorEntry:
    base = {
        "bls_pubkey": bls,
        "exit_epoch": FAR_FUTURE_EPOCH,
        "public_keys": PQPublicKeys(),
        "key_change_epoch": FAR_FUTURE_EPOCH,
    }
    base.update(overrides)
    return ValidatorEntry(**base)


def state_of(*entries: ValidatorEntry) -> RegistryState:
    return RegistryState(tuple(entries))


def stub_possession(leaf=0, claimed_bls=None, claimed_xmss=KEY_A, valid=True) -> bytes:
    """A byte layout of our own that carries the fields a real proof binds."""
    claimed_bls = claimed_bls if claimed_bls is not None else BLS_0
    return (
        int(leaf).to_bytes(4, "little")
        + claimed_bls
        + claimed_xmss
        + (b"\x01" if valid else b"\x00")
        + b"\x11" * (XMSS_SIGNATURE_SIZE - 85)
    )


def stub_registration(signer: bytes = BLS_0) -> bytes:
    return signer + b"\x22" * (96 - 48)


def registration(
    index=0,
    pk=None,
    previous=FAR_FUTURE_EPOCH,
    signer=None,
    leaf=0,
    bound_bls=None,
    bound_xmss=None,
    valid=True,
):
    pk = pk or keys()
    # Each test validator signs for its own index unless a case asks otherwise.
    message_signed_by = signer if signer is not None else (BLS_0 if index == 0 else BLS_1)
    claimed_bls = bound_bls if bound_bls is not None else message_signed_by
    claimed_xmss = bound_xmss if bound_xmss is not None else pk.xmss_public_key
    return SignedPQKeyRegistration(
        message=PQKeyRegistration(
            validator_index=index, public_keys=pk, previous_change_epoch=previous
        ),
        possession_signature=stub_possession(
            leaf=leaf, claimed_bls=claimed_bls, claimed_xmss=claimed_xmss, valid=valid
        ),
        registration_signature=stub_registration(message_signed_by),
    )


class PresetTest(unittest.TestCase):
    def test_delay_presets_match_the_proposal(self):
        self.assertEqual(PRESET_MAIN_DELAY, 256)
        self.assertEqual(PRESET_MINIMAL_DELAY, 8)

    def test_delay_must_be_positive(self):
        with self.assertRaises(ValueError):
            RegistryConfig(min_change_delay=0)


class FirstRegistrationTest(unittest.TestCase):
    def test_accepted_and_written(self):
        state = state_of(validator())
        outcome = process_pq_key_registration(state, registration(), current_epoch=10)

        self.assertTrue(outcome.accepted)
        stored = outcome.state.validators[0]
        self.assertEqual(stored.public_keys, keys())
        self.assertEqual(stored.key_change_epoch, 10)

    def test_index_out_of_range_is_rejected(self):
        state = state_of(validator())
        outcome = process_pq_key_registration(state, registration(index=1), current_epoch=10)

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "INDEX_OUT_OF_RANGE")

    def test_zero_key_of_an_unregistered_validator_is_not_in_use(self):
        # Proposal: an unset all-zero entry does not count as holding the zero key.
        state = state_of(validator(bls=BLS_0), validator(bls=BLS_1))
        outcome = process_pq_key_registration(
            state, registration(index=1, pk=keys(xmss=KEY_ZERO)), current_epoch=5
        )

        self.assertTrue(outcome.accepted)


class ChangeTest(unittest.TestCase):
    def setUp(self):
        self.config = RegistryConfig(min_change_delay=PRESET_MAIN_DELAY)
        self.state = state_of(validator(public_keys=keys(), key_change_epoch=100))

    def test_change_exactly_at_the_boundary_is_accepted(self):
        # assert get_current_epoch(state) >= last_change_epoch + MIN_DELAY
        outcome = process_pq_key_registration(
            self.state,
            registration(pk=keys(xmss=KEY_B), previous=100),
            current_epoch=100 + PRESET_MAIN_DELAY,
            config=self.config,
        )

        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.state.validators[0].public_keys.xmss_public_key, KEY_B)

    def test_change_one_epoch_before_the_boundary_is_rejected(self):
        outcome = process_pq_key_registration(
            self.state,
            registration(pk=keys(xmss=KEY_B), previous=100),
            current_epoch=100 + PRESET_MAIN_DELAY - 1,
            config=self.config,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "COOLDOWN_NOT_MET")

    def test_minimal_preset_moves_the_same_boundary(self):
        config = RegistryConfig(min_change_delay=PRESET_MINIMAL_DELAY)
        state = state_of(validator(public_keys=keys(), key_change_epoch=10))

        too_early = process_pq_key_registration(
            state,
            registration(pk=keys(xmss=KEY_B), previous=10),
            current_epoch=10 + PRESET_MINIMAL_DELAY - 1,
            config=config,
        )
        on_time = process_pq_key_registration(
            state,
            registration(pk=keys(xmss=KEY_B), previous=10),
            current_epoch=10 + PRESET_MINIMAL_DELAY,
            config=config,
        )

        self.assertEqual(too_early.reason, "COOLDOWN_NOT_MET")
        self.assertTrue(on_time.accepted)

    def test_change_that_keeps_the_vrf_root_is_accepted(self):
        # Proposal test case: a change that keeps the VRF root is valid.
        outcome = process_pq_key_registration(
            self.state,
            registration(pk=keys(xmss=KEY_B, vrf=VRF_1), previous=100),
            current_epoch=100 + PRESET_MAIN_DELAY,
            config=self.config,
        )

        self.assertTrue(outcome.accepted)

    def test_change_of_vrf_root_only_is_rejected(self):
        # Every registration carries a new leanXMSS key.
        outcome = process_pq_key_registration(
            self.state,
            registration(pk=keys(xmss=KEY_A, vrf=VRF_2), previous=100),
            current_epoch=100 + PRESET_MAIN_DELAY,
            config=self.config,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "KEY_ALREADY_IN_USE")

    def test_first_registration_skips_the_cooldown(self):
        # A never-registered validator holds the sentinel and is not rate limited.
        state = state_of(validator())
        outcome = process_pq_key_registration(
            state, registration(), current_epoch=0, config=self.config
        )

        self.assertTrue(outcome.accepted)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.config = RegistryConfig(min_change_delay=PRESET_MINIMAL_DELAY)
        self.state = state_of(validator(public_keys=keys(), key_change_epoch=10))

    def test_resubmitting_an_applied_registration_fails(self):
        applied = process_pq_key_registration(
            self.state,
            registration(pk=keys(xmss=KEY_B), previous=10),
            current_epoch=18,
            config=self.config,
        )
        replay = process_pq_key_registration(
            applied.state,
            registration(pk=keys(xmss=KEY_B), previous=10),
            current_epoch=30,
            config=self.config,
        )

        self.assertTrue(applied.accepted)
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.reason, "PREVIOUS_EPOCH_MISMATCH")

    def test_stale_previous_change_epoch_fails(self):
        outcome = process_pq_key_registration(
            self.state,
            registration(pk=keys(xmss=KEY_B), previous=4),
            current_epoch=30,
            config=self.config,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "PREVIOUS_EPOCH_MISMATCH")


class ExitTest(unittest.TestCase):
    def test_registration_for_a_validator_that_initiated_exit_is_rejected(self):
        state = state_of(validator(exit_epoch=50))
        outcome = process_pq_key_registration(state, registration(), current_epoch=60)

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "EXIT_INITIATED")

    def test_cooldown_check_runs_before_the_exit_of_a_later_request(self):
        # Ordering: exit is checked before the cooldown, so an exited validator
        # that is also too early reports the exit.
        state = state_of(
            validator(exit_epoch=40, public_keys=keys(), key_change_epoch=100)
        )
        outcome = process_pq_key_registration(
            state,
            registration(pk=keys(xmss=KEY_B), previous=100),
            current_epoch=101,
            config=RegistryConfig(min_change_delay=PRESET_MAIN_DELAY),
        )

        self.assertEqual(outcome.reason, "EXIT_INITIATED")


class OwnershipTest(unittest.TestCase):
    def test_two_validators_contending_the_same_key_in_one_block(self):
        # Proposal test case: the second registration of the same key is invalid
        # and the block is discarded, so no state changes.
        state = state_of(validator(bls=BLS_0), validator(bls=BLS_1))
        before = deepcopy(state)
        outcome = apply_block(
            state,
            (
                registration(index=0, pk=keys()),
                registration(index=1, pk=keys(), signer=BLS_1),
            ),
            current_epoch=5,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "KEY_ALREADY_IN_USE@1")
        self.assertIsNone(outcome.state)
        self.assertEqual(state, before)

    def test_registering_the_key_the_validator_already_holds_is_rejected(self):
        # The uniqueness scan includes the registering validator itself.
        state = state_of(validator(public_keys=keys(), key_change_epoch=0))
        outcome = process_pq_key_registration(
            state,
            registration(pk=keys(), previous=0),
            current_epoch=1000,
            config=RegistryConfig(min_change_delay=PRESET_MAIN_DELAY),
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "KEY_ALREADY_IN_USE")

    def test_a_key_freed_by_a_change_can_be_registered_by_another_validator(self):
        config = RegistryConfig(min_change_delay=PRESET_MAIN_DELAY)
        state = state_of(
            validator(bls=BLS_0, public_keys=keys(), key_change_epoch=0),
            validator(bls=BLS_1),
        )
        changed = process_pq_key_registration(
            state,
            registration(index=0, pk=keys(xmss=KEY_B), previous=0),
            current_epoch=PRESET_MAIN_DELAY,
            config=config,
        )
        reclaimed = process_pq_key_registration(
            changed.state,
            registration(index=1, pk=keys(), signer=BLS_1),
            current_epoch=PRESET_MAIN_DELAY,
            config=config,
        )

        self.assertTrue(changed.accepted)
        self.assertTrue(reclaimed.accepted)
        self.assertEqual(
            reclaimed.state.validators[1].public_keys.xmss_public_key, KEY_A
        )


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.state = state_of(validator(bls=BLS_0), validator(bls=BLS_1))

    def test_registration_signed_by_another_validators_key_is_rejected(self):
        outcome = process_pq_key_registration(
            self.state, registration(index=0, signer=BLS_1), current_epoch=5
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "AUTHORIZATION_INVALID")

    def test_possession_bound_to_another_validator_is_rejected(self):
        # A copied key cannot be proven for a different validator.
        outcome = process_pq_key_registration(
            self.state,
            registration(index=0, bound_bls=BLS_1),
            current_epoch=5,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "POSSESSION_INVALID")

    def test_possession_at_a_nonzero_leaf_index_is_rejected(self):
        outcome = process_pq_key_registration(
            self.state, registration(index=0, leaf=1), current_epoch=5
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "POSSESSION_INVALID")

    def test_invalid_possession_is_rejected(self):
        outcome = process_pq_key_registration(
            self.state, registration(index=0, valid=False), current_epoch=5
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "POSSESSION_INVALID")

    def test_cooldown_is_evaluated_before_authorization(self):
        # Rule order follows the proposal, so the reported reason is the first
        # assertion that fails.
        state = state_of(validator(public_keys=keys(), key_change_epoch=100))
        outcome = process_pq_key_registration(
            state,
            registration(pk=keys(xmss=KEY_B), previous=100, signer=BLS_1),
            current_epoch=101,
            config=RegistryConfig(min_change_delay=PRESET_MAIN_DELAY),
        )

        self.assertEqual(outcome.reason, "COOLDOWN_NOT_MET")


class NoPartialWriteTest(unittest.TestCase):
    """Every rule must reject without touching the state it was given."""

    def cases(self):
        registered = state_of(
            validator(bls=BLS_0, public_keys=keys(), key_change_epoch=0),
            validator(bls=BLS_1),
        )
        exited = state_of(validator(bls=BLS_0, exit_epoch=3), validator(bls=BLS_1))
        config = RegistryConfig(min_change_delay=PRESET_MAIN_DELAY)

        return [
            (
                "INDEX_OUT_OF_RANGE",
                registered,
                registration(index=9),
                5,
                config,
            ),
            ("EXIT_INITIATED", exited, registration(), 5, config),
            (
                "PREVIOUS_EPOCH_MISMATCH",
                registered,
                registration(pk=keys(xmss=KEY_B)),
                500,
                config,
            ),
            (
                "COOLDOWN_NOT_MET",
                registered,
                registration(pk=keys(xmss=KEY_B), previous=0),
                PRESET_MAIN_DELAY - 1,
                config,
            ),
            (
                "AUTHORIZATION_INVALID",
                registered,
                registration(pk=keys(xmss=KEY_B), previous=0, signer=BLS_1),
                PRESET_MAIN_DELAY,
                config,
            ),
            (
                "KEY_ALREADY_IN_USE",
                registered,
                registration(index=1, pk=keys()),
                PRESET_MAIN_DELAY,
                config,
            ),
            (
                "POSSESSION_INVALID",
                registered,
                registration(index=1, pk=keys(xmss=KEY_B), leaf=7),
                PRESET_MAIN_DELAY,
                config,
            ),
        ]

    def test_every_rejection_leaves_the_input_state_untouched(self):
        for reason, state, signed, epoch, config in self.cases():
            before = deepcopy(state)
            outcome = process_pq_key_registration(
                state, signed, current_epoch=epoch, config=config
            )

            self.assertFalse(outcome.accepted, msg=reason)
            self.assertEqual(outcome.reason, reason)
            self.assertIsNone(outcome.state, msg=reason)
            self.assertEqual(state, before, msg=reason)



class UpstreamParityTest(unittest.TestCase):
    """Cases taken from the pinned consensus-specs test file, commit
    c031b137b7f6620679fdb1e9bd7d9a22470bc0e2.
    """

    GENESIS_EPOCH = 0

    def test_registration_before_activation_is_accepted(self):
        # Registration needs an index, not activation.
        state = state_of(validator(bls=BLS_0, activation_epoch=999))
        outcome = process_pq_key_registration(state, registration(), current_epoch=5)

        self.assertTrue(outcome.accepted)

    def test_registration_for_the_last_validator_is_accepted(self):
        state = state_of(validator(bls=BLS_0), validator(bls=BLS_1))
        outcome = process_pq_key_registration(
            state, registration(index=1), current_epoch=5
        )

        self.assertTrue(outcome.accepted)

    def test_duplicate_vrf_root_across_validators_is_accepted(self):
        # VRF roots are not required to be unique.
        state = state_of(validator(bls=BLS_0), validator(bls=BLS_1))
        first = process_pq_key_registration(state, registration(index=0), current_epoch=5)
        second = process_pq_key_registration(
            first.state,
            registration(index=1, pk=keys(xmss=KEY_B, vrf=VRF_1)),
            current_epoch=5,
        )

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual(
            second.state.validators[1].public_keys.vrf_root,
            second.state.validators[0].public_keys.vrf_root,
        )

    def test_unregistered_previous_epoch_at_genesis_is_rejected(self):
        state = state_of(validator(bls=BLS_0))
        outcome = process_pq_key_registration(
            state,
            registration(previous=self.GENESIS_EPOCH),
            current_epoch=self.GENESIS_EPOCH,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "PREVIOUS_EPOCH_MISMATCH")

    def test_replay_of_a_genesis_epoch_registration_is_rejected(self):
        # Storing epoch 0 differs from the sentinel the replay names.
        config = RegistryConfig(min_change_delay=PRESET_MAIN_DELAY)
        state = state_of(validator(bls=BLS_0))
        signed = registration()
        applied = process_pq_key_registration(
            state, signed, current_epoch=self.GENESIS_EPOCH, config=config
        )
        replay = process_pq_key_registration(
            applied.state,
            signed,
            current_epoch=self.GENESIS_EPOCH + PRESET_MAIN_DELAY,
            config=config,
        )

        self.assertEqual(applied.state.validators[0].key_change_epoch, self.GENESIS_EPOCH)
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.reason, "PREVIOUS_EPOCH_MISMATCH")

    def test_change_in_the_same_epoch_is_rejected(self):
        state = state_of(validator(bls=BLS_0, public_keys=keys(), key_change_epoch=100))
        outcome = process_pq_key_registration(
            state,
            registration(pk=keys(xmss=KEY_B), previous=100),
            current_epoch=100,
            config=RegistryConfig(min_change_delay=PRESET_MAIN_DELAY),
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "COOLDOWN_NOT_MET")

    def test_same_key_with_a_different_vrf_root_is_still_rejected(self):
        # Uniqueness covers the leanXMSS key only, not the pair.
        state = state_of(
            validator(bls=BLS_0, public_keys=keys(), key_change_epoch=0),
            validator(bls=BLS_1),
        )
        outcome = process_pq_key_registration(
            state,
            registration(index=1, pk=keys(xmss=KEY_A, vrf=VRF_2)),
            current_epoch=PRESET_MAIN_DELAY,
            config=RegistryConfig(min_change_delay=PRESET_MAIN_DELAY),
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "KEY_ALREADY_IN_USE")

    def test_possession_signed_by_a_different_key_is_rejected(self):
        state = state_of(validator(bls=BLS_0), validator(bls=BLS_1))
        outcome = process_pq_key_registration(
            state,
            registration(pk=keys(xmss=KEY_B), bound_xmss=KEY_A),
            current_epoch=5,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "POSSESSION_INVALID")


class WrongLeafReachesThePossessionRuleTest(unittest.TestCase):
    """A proof signed at the wrong leaf must be stopped by that rule, not earlier."""

    def test_a_delayed_wrong_leaf_registration_is_refused_for_its_proof(self):
        config = RegistryConfig(min_change_delay=PRESET_MAIN_DELAY)
        first = process_pq_key_registration(
            state_of(validator()), registration(), current_epoch=0, config=config
        )
        later = process_pq_key_registration(
            first.state,
            registration(pk=keys(xmss=KEY_B), previous=0, leaf=1),
            current_epoch=PRESET_MAIN_DELAY,
            config=config,
        )

        self.assertTrue(first.accepted)
        self.assertEqual(later.reason, "POSSESSION_INVALID")
        self.assertIsNone(later.state)

    def test_the_written_statement_reaches_the_same_rule_at_the_same_step(self):
        from oracle import DELAY, oracle_verdict

        verdict = oracle_verdict([("first0", 0, 0), ("leaf1_0", 1, DELAY)], 0)

        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["reason"], "POSSESSION_INVALID")
        self.assertEqual(verdict["position"], 1)

    def test_the_replay_of_the_recorded_round_still_agrees(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "replay_round.py"], capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent)
        )
        summary = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(summary["disagreements"], 0)
        self.assertTrue(summary["replay_matches_record"])


if __name__ == "__main__":
    unittest.main()
