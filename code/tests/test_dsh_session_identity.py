from __future__ import annotations

import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    SessionIdentityPin,
    create_session_identity_pin,
    verify_session_identity,
)


class SessionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.real_id = "synthetic-real-session-id-never-persist"
        self.pin = create_session_identity_pin(
            self.real_id,
            alias="dsh-root-0123456789abcdef",
            salt=b"s" * 32,
        )

    def test_pin_contains_only_alias_salt_and_fingerprint(self) -> None:
        encoded = json.dumps(asdict(self.pin), sort_keys=True)
        self.assertNotIn(self.real_id, encoded)
        self.assertEqual(self.pin.alias, "dsh-root-0123456789abcdef")
        self.assertEqual(len(self.pin.salt_hex), 64)
        self.assertEqual(len(self.pin.fingerprint_sha256), 64)

    def test_matching_id_yields_one_use_redacted_reference(self) -> None:
        verified = verify_session_identity(self.pin, self.real_id)
        self.assertNotIn(self.real_id, repr(verified))
        self.assertEqual(verified.take_opaque_id(), self.real_id)
        self.assertNotIn(self.real_id, repr(verified))
        with self.assertRaisesRegex(RuntimeError, "replayed_reference"):
            verified.take_opaque_id()

    def test_mismatch_is_fail_closed_and_content_free(self) -> None:
        wrong = "different-sensitive-session-id"
        with self.assertRaisesRegex(RuntimeError, "fingerprint_mismatch") as caught:
            verify_session_identity(self.pin, wrong)
        self.assertNotIn(wrong, str(caught.exception))
        self.assertNotIn(self.real_id, str(caught.exception))

    def test_tampered_fingerprint_is_rejected(self) -> None:
        tampered = SessionIdentityPin(
            alias=self.pin.alias,
            salt_hex=self.pin.salt_hex,
            fingerprint_sha256="0" * 64,
        )
        with self.assertRaisesRegex(RuntimeError, "fingerprint_mismatch"):
            verify_session_identity(tampered, self.real_id)

    def test_audit_fields_exclude_salt_and_real_id(self) -> None:
        fields = self.pin.audit_fields()
        encoded = json.dumps(fields, sort_keys=True)
        self.assertNotIn(self.real_id, encoded)
        self.assertNotIn(self.pin.salt_hex, encoded)
        self.assertEqual(set(fields), {"alias", "fingerprint_sha256", "version"})

    def test_random_defaults_do_not_repeat(self) -> None:
        first = create_session_identity_pin(self.real_id)
        second = create_session_identity_pin(self.real_id)
        self.assertNotEqual(first.alias, second.alias)
        self.assertNotEqual(first.salt_hex, second.salt_hex)
        self.assertNotEqual(first.fingerprint_sha256, second.fingerprint_sha256)


if __name__ == "__main__":
    unittest.main()
