"""Real verification runs against the registry rules.

These cases use upstream leanXMSS and BLS code, so a pass here means the rules
accept a genuinely signed registration and reject genuinely forged ones. When
the pinned dependencies are missing the whole class is skipped and reported as
not run rather than as passing.
"""

import hashlib
import unittest

from crypto_real import (
    DEPENDENCIES_AVAILABLE,
    IMPORT_ERROR,
    XMSS_SIGNATURE_SIZE,
    bls_public_key,
    real_authorizer,
    real_possession_checker,
    sign_registration,
    xmss_public_key,
    xmss_seed,
)
from registry import (
    FAR_FUTURE_EPOCH,
    PQKeyRegistration,
    PQPublicKeys,
    RegistryConfig,
    RegistryState,
    ValidatorEntry,
    process_pq_key_registration,
)

REAL = unittest.skipUnless(DEPENDENCIES_AVAILABLE, f"dependencies unavailable: {IMPORT_ERROR}")

VRF_1 = b"\xc1" * 32
GENESIS_ROOT = b"\x11" * 32
SECRET_0 = int(11).to_bytes(32, "big")
SECRET_1 = int(12).to_bytes(32, "big")
KEYS = {}
BLS_PUBKEYS = {}


def setUpModule():
    if not DEPENDENCIES_AVAILABLE:
        return
    for index in (0, 1):
        seed = xmss_seed(index)
        KEYS[index] = {
            "seed": seed,
            "public": xmss_public_key(seed),
        }
    BLS_PUBKEYS[0] = bls_public_key(SECRET_0)
    BLS_PUBKEYS[1] = bls_public_key(SECRET_1)


def unregistered_state(index=0, bls_pubkey=None, second=None):
    entry = ValidatorEntry(bls_pubkey=bls_pubkey or BLS_PUBKEYS[index])
    others = () if second is None else (second,)
    return RegistryState((entry,) + others)


def message(index=0, generation=0, previous=FAR_FUTURE_EPOCH, vrf=VRF_1):
    return PQKeyRegistration(
        validator_index=index,
        public_keys=PQPublicKeys(
            xmss_public_key=KEYS[generation]["public"], vrf_root=vrf
        ),
        previous_change_epoch=previous,
    )


@REAL
class RealSignatureFormatTest(unittest.TestCase):
    def test_key_and_signature_sizes_match_the_proposal(self):
        # The proposal fixes XMSSPublicKey at 32 bytes and XMSSSignature at 2404.
        proof = sign_registration(message(), KEYS[0]["seed"], SECRET_0)

        self.assertEqual(len(KEYS[0]["public"]), 32)
        self.assertEqual(len(proof.possession_signature), XMSS_SIGNATURE_SIZE)
        self.assertEqual(len(proof.registration_signature), 96)


@REAL
class RealRegistryRunsTest(unittest.TestCase):
    def run_registration(self, signed, state, current_epoch=5, config=None):
        return process_pq_key_registration(
            state,
            signed,
            current_epoch=current_epoch,
            config=config or RegistryConfig(),
            authorizer=real_authorizer,
            possession_checker=real_possession_checker,
        )

    def test_genuine_registration_is_accepted(self):
        signed = sign_registration(message(), KEYS[0]["seed"], SECRET_0)
        outcome = self.run_registration(signed, unregistered_state())

        self.assertTrue(outcome.accepted)
        self.assertEqual(
            outcome.state.validators[0].public_keys.xmss_public_key, KEYS[0]["public"]
        )

    def test_tampered_keys_after_signing_are_rejected_by_bls(self):
        honest = message(generation=0)
        signed = sign_registration(honest, KEYS[0]["seed"], SECRET_0)
        tampered = signed.__class__(
            message=PQKeyRegistration(
                validator_index=honest.validator_index,
                public_keys=PQPublicKeys(
                    xmss_public_key=KEYS[1]["public"], vrf_root=VRF_1
                ),
                previous_change_epoch=honest.previous_change_epoch,
            ),
            possession_signature=signed.possession_signature,
            registration_signature=signed.registration_signature,
        )
        outcome = self.run_registration(tampered, unregistered_state())

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "AUTHORIZATION_INVALID")

    def test_registration_signed_by_another_validator_is_rejected(self):
        signed = sign_registration(message(), KEYS[0]["seed"], SECRET_1)
        outcome = self.run_registration(signed, unregistered_state())

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "AUTHORIZATION_INVALID")

    def test_possession_at_leaf_one_is_rejected_although_the_signature_is_valid(self):
        # The reserved leaf rule is structural; a valid signature elsewhere fails.
        signed = sign_registration(message(), KEYS[0]["seed"], SECRET_0, leaf_index=1)
        outcome = self.run_registration(signed, unregistered_state())

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "POSSESSION_INVALID")

    def test_possession_bound_to_another_validator_is_rejected(self):
        signed = sign_registration(
            message(),
            KEYS[0]["seed"],
            SECRET_0,
            possession_bls_pubkey=BLS_PUBKEYS[1],
        )
        outcome = self.run_registration(signed, unregistered_state())

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "POSSESSION_INVALID")

    def test_uniqueness_is_enforced_independently_of_signature_validity(self):
        config = RegistryConfig(min_change_delay=1)
        holder = ValidatorEntry(
            bls_pubkey=BLS_PUBKEYS[0],
            public_keys=PQPublicKeys(xmss_public_key=KEYS[0]["public"]),
            key_change_epoch=5,
        )
        contender = ValidatorEntry(bls_pubkey=BLS_PUBKEYS[1])
        signed = sign_registration(
            message(index=1, generation=0), KEYS[0]["seed"], SECRET_1
        )
        second = self.run_registration(
            signed, RegistryState((holder, contender)), current_epoch=6, config=config
        )

        # Both proofs verify, so only the occupancy rule can be what rejects it.
        from crypto_real import real_possession_checker

        self.assertTrue(
            real_possession_checker(contender, KEYS[0]["public"], signed.possession_signature)
        )
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "KEY_ALREADY_IN_USE")

    def test_change_after_the_cooldown_needs_a_new_key_and_a_real_proof(self):
        config = RegistryConfig(min_change_delay=2)
        state = RegistryState(
            (
                ValidatorEntry(
                    bls_pubkey=BLS_PUBKEYS[0],
                    public_keys=PQPublicKeys(xmss_public_key=KEYS[0]["public"]),
                    key_change_epoch=0,
                ),
            )
        )
        stale = self.run_registration(
            sign_registration(
                message(previous=0, generation=1), KEYS[1]["seed"], SECRET_0
            ),
            state,
            current_epoch=1,
            config=config,
        )
        allowed = self.run_registration(
            sign_registration(
                message(previous=0, generation=1), KEYS[1]["seed"], SECRET_0
            ),
            state,
            current_epoch=2,
            config=config,
        )

        self.assertEqual(stale.reason, "COOLDOWN_NOT_MET")
        self.assertTrue(allowed.accepted)


if __name__ == "__main__":
    unittest.main()
