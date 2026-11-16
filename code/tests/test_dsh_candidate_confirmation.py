from __future__ import annotations

import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    RootSessionCandidate,
    SessionCandidateConfirmation,
    verify_session_identity,
)


def root(
    opaque_id: str,
    *,
    updated: int,
    last_prompt: int | None,
    blank: bool = False,
    running: bool = False,
) -> RootSessionCandidate:
    return RootSessionCandidate(
        opaque_id=opaque_id,
        running=running,
        blank=blank,
        updated_at=updated,
        last_prompt_at=last_prompt,
    )


class CandidateConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = (
            "synthetic-latest-id",
            "synthetic-fallback-id",
            "synthetic-blank-id",
            "synthetic-running-id",
        )
        self.confirmation = SessionCandidateConfirmation(
            (
                root(self.ids[1], updated=30, last_prompt=None),
                root(self.ids[0], updated=20, last_prompt=40),
                root(self.ids[2], updated=50, last_prompt=50, blank=True),
                root(self.ids[3], updated=60, last_prompt=60, running=True),
            )
        )

    def tearDown(self) -> None:
        self.confirmation.close()

    def test_summaries_contain_no_ids_or_timestamps(self) -> None:
        summaries = self.confirmation.summaries()
        encoded = json.dumps([asdict(item) for item in summaries], sort_keys=True)
        for opaque_id in self.ids:
            self.assertNotIn(opaque_id, encoded)
        self.assertNotIn("updated_at", encoded.replace("updated_at_fallback", ""))
        self.assertNotIn("last_prompt_at\"", encoded.replace("last_prompt_at\"", ""))
        self.assertNotIn(self.ids[0], repr(self.confirmation))

    def test_recommendation_is_latest_eligible_nonblank_root(self) -> None:
        summaries = self.confirmation.summaries()
        recommended = [item for item in summaries if item.recommended]
        self.assertEqual(len(recommended), 1)
        self.assertEqual(recommended[0].recency_rank, 3)
        self.assertEqual(recommended[0].recency_source, "last_prompt_at")

    def test_confirmation_creates_pin_without_persisting_id(self) -> None:
        recommended = next(
            item for item in self.confirmation.summaries() if item.recommended
        )
        pin = self.confirmation.freeze_confirmed(
            recommended.anonymous_index,
            alias="dsh-root-0123456789abcdef",
            salt=b"s" * 32,
        )
        encoded = json.dumps(asdict(pin), sort_keys=True)
        self.assertNotIn(self.ids[0], encoded)
        verified = verify_session_identity(pin, self.ids[0])
        self.assertEqual(verified.take_opaque_id(), self.ids[0])
        with self.assertRaisesRegex(RuntimeError, "already_consumed"):
            self.confirmation.summaries()

    def test_blank_and_running_roots_cannot_be_frozen(self) -> None:
        summaries = self.confirmation.summaries()
        blank = next(item for item in summaries if item.blank)
        running = next(item for item in summaries if item.running)
        with self.assertRaisesRegex(RuntimeError, "blank_root"):
            self.confirmation.freeze_confirmed(blank.anonymous_index)
        with self.assertRaisesRegex(RuntimeError, "running_root"):
            self.confirmation.freeze_confirmed(running.anonymous_index)

    def test_invalid_index_is_rejected_without_consuming(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_index"):
            self.confirmation.freeze_confirmed(999)
        self.assertEqual(len(self.confirmation.summaries()), 4)

    def test_duplicate_real_ids_are_rejected(self) -> None:
        duplicate = root("same-sensitive-id", updated=1, last_prompt=1)
        with self.assertRaisesRegex(ValueError, "duplicate_root"):
            SessionCandidateConfirmation((duplicate, duplicate))


if __name__ == "__main__":
    unittest.main()
