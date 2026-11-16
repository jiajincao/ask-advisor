from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import OfflineDshEventAdapter, SimulatedDshEvent  # noqa: E402


class OfflineDshEventAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = OfflineDshEventAdapter(
            log_path=Path(self.temp.name) / "dsh-events.jsonl",
            run_id="run-1",
            trigger_id="trigger-1",
            event_id="event-1",
            session_request_id="session-request-1",
            dsh_session_alias="root-session",
        )

    @staticmethod
    def event(seq: int, event_type: str, data: dict[str, object]) -> SimulatedDshEvent:
        return SimulatedDshEvent(seq, event_type, "root-session", data)

    def complete_chain(self) -> None:
        self.adapter.ingest(
            self.event(1, "session/prompt.accepted", {"request_id": "session-request-1"})
        )
        self.adapter.ingest(
            self.event(2, "tool/call", {"tool_name": "ask_advisor", "call_id": "call-1"})
        )
        self.adapter.ingest(
            self.event(
                3,
                "approval/asked",
                {
                    "tool_name": "ask_advisor",
                    "call_id": "call-1",
                    "approval_request_id": "approval-1",
                },
            )
        )
        self.adapter.ingest(
            self.event(
                4,
                "approval/decided",
                {"approval_request_id": "approval-1", "outcome": "allowed-once"},
            )
        )
        self.adapter.ingest(
            self.event(
                5,
                "superadvisor/result",
                {"request_id": "advisor-1", "status": "success", "truncated": False},
            )
        )

    def test_complete_chain_is_correlated(self) -> None:
        self.complete_chain()
        snapshot = self.adapter.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["tool_call_id"], "call-1")
        self.assertEqual(snapshot["approval_request_id"], "approval-1")
        self.assertEqual(snapshot["superadvisor_request_id"], "advisor-1")

    def test_replayed_sequence_is_rejected(self) -> None:
        event = self.event(1, "session/prompt.accepted", {"request_id": "session-request-1"})
        self.adapter.ingest(event)
        with self.assertRaisesRegex(ValueError, "non_monotonic_seq"):
            self.adapter.ingest(event)

    def test_out_of_order_tool_call_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool_call_out_of_order"):
            self.adapter.ingest(
                self.event(1, "tool/call", {"tool_name": "ask_advisor", "call_id": "call-1"})
            )

    def test_other_tool_is_rejected(self) -> None:
        self.adapter.ingest(
            self.event(1, "session/prompt.accepted", {"request_id": "session-request-1"})
        )
        with self.assertRaisesRegex(ValueError, "tool_not_allowed"):
            self.adapter.ingest(
                self.event(2, "tool/call", {"tool_name": "shell", "call_id": "call-1"})
            )

    def test_mismatched_approval_id_is_rejected(self) -> None:
        self.adapter.ingest(
            self.event(1, "session/prompt.accepted", {"request_id": "session-request-1"})
        )
        self.adapter.ingest(
            self.event(2, "tool/call", {"tool_name": "ask_advisor", "call_id": "call-1"})
        )
        self.adapter.ingest(
            self.event(
                3,
                "approval/asked",
                {
                    "tool_name": "ask_advisor",
                    "call_id": "call-1",
                    "approval_request_id": "approval-1",
                },
            )
        )
        with self.assertRaisesRegex(ValueError, "approval_request_id_mismatch"):
            self.adapter.ingest(
                self.event(
                    4,
                    "approval/decided",
                    {"approval_request_id": "approval-other", "outcome": "allowed-once"},
                )
            )

    def test_success_requires_allowed_once(self) -> None:
        self.adapter.ingest(
            self.event(1, "session/prompt.accepted", {"request_id": "session-request-1"})
        )
        self.adapter.ingest(
            self.event(2, "tool/call", {"tool_name": "ask_advisor", "call_id": "call-1"})
        )
        self.adapter.ingest(
            self.event(
                3,
                "approval/asked",
                {
                    "tool_name": "ask_advisor",
                    "call_id": "call-1",
                    "approval_request_id": "approval-1",
                },
            )
        )
        self.adapter.ingest(
            self.event(
                4,
                "approval/decided",
                {"approval_request_id": "approval-1", "outcome": "rejected"},
            )
        )
        with self.assertRaisesRegex(ValueError, "success_without_allowed_once"):
            self.adapter.ingest(
                self.event(
                    5,
                    "superadvisor/result",
                    {"request_id": "advisor-1", "status": "success", "truncated": False},
                )
            )

    def test_sensitive_event_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sensitive content"):
            self.adapter.ingest(
                self.event(
                    1,
                    "session/prompt.accepted",
                    {"request_id": "session-request-1", "note": "Authorization: secret"},
                )
            )

    def test_transport_is_disabled(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "intentionally disabled"):
            self.adapter.connect()


if __name__ == "__main__":
    unittest.main()
