from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    SessionPinStore,
    create_session_identity_pin,
)


class SessionPinStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "session-pin.json"
        self.real_id = "synthetic-real-id-never-written"
        self.pin = create_session_identity_pin(
            self.real_id,
            alias="dsh-root-0123456789abcdef",
            salt=b"s" * 32,
        )
        self.store = SessionPinStore(integrity_key=b"k" * 32)
        self.addCleanup(self.store.close)

    def test_round_trip_is_private_and_mode_0600(self) -> None:
        self.store.save_new(self.path, self.pin)
        self.assertEqual(self.store.load(self.path), self.pin)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.real_id, self.path.read_text())
        self.assertNotIn("k" * 32, self.path.read_text())

    def test_existing_target_is_never_overwritten(self) -> None:
        self.store.save_new(self.path, self.pin)
        original = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "target_exists"):
            self.store.save_new(self.path, create_session_identity_pin("other-id"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_tampering_is_detected(self) -> None:
        self.store.save_new(self.path, self.pin)
        document = json.loads(self.path.read_text())
        document["record"]["alias"] = "dsh-root-ffffffffffffffff"
        self.path.write_text(json.dumps(document))
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(RuntimeError, "integrity_mismatch"):
            self.store.load(self.path)

    def test_wrong_key_is_rejected(self) -> None:
        self.store.save_new(self.path, self.pin)
        other = SessionPinStore(integrity_key=b"x" * 32)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(RuntimeError, "integrity_mismatch"):
            other.load(self.path)

    def test_unsafe_permissions_are_rejected(self) -> None:
        self.store.save_new(self.path, self.pin)
        os.chmod(self.path, 0o644)
        with self.assertRaisesRegex(RuntimeError, "unsafe_permissions"):
            self.store.load(self.path)

    def test_symlink_is_rejected(self) -> None:
        target = Path(self.temp.name) / "target"
        target.write_text("not-a-pin")
        link = Path(self.temp.name) / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "not_regular_file"):
            self.store.load(link)


if __name__ == "__main__":
    unittest.main()
