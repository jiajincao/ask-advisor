from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    ApprovalRequest,
    OfflineRunner,
    RestrictedApprovalAnswerer,
    canonical_request_hash,
)


def valid_arguments() -> dict[str, object]:
    return {
        "question": "What is the safest next diagnostic step?",
        "goal": "Resolve the public simulated failure.",
        "constraints": "Do not access credentials or hidden tests.",
        "attempts": "One simulated fix did not change the error.",
        "evidence": [
            {
                "kind": "tool_result",
                "source": "simulated-test-output",
                "start_line": 1,
                "end_line": 3,
            }
        ],
    }


class OfflineBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = OfflineRunner(Path(self.temp.name))
        self.state = self.runner.create_run("bridge-task", "C", 1)
        self.event_id = self.runner.record_observable_event(
            self.state,
            step_number=3,
            action_type="diagnose_reproduce",
            summary="A new simulated failure was observed.",
            is_new_information=True,
        )
        self.runner.record_system_one_result(
            self.state,
            event_id=self.event_id,
            p_yes=0.8,
            p_no=0.2,
            label_mass=0.99,
            threshold=0.7,
        )
        self.preview = self.runner.build_advisor_preview(
            self.state, self.event_id, "Public simulated context only."
        )
        self.runner.mark_request_started(self.state, self.event_id)
        self.arguments = valid_arguments()
        self.request_hash = canonical_request_hash(self.arguments)
        self.answerer = RestrictedApprovalAnswerer(expected_session_alias="root-session")

    def request(self, **overrides: object) -> ApprovalRequest:
        values: dict[str, object] = {
            "run_id": self.state.run_id,
            "trigger_id": self.preview["trigger_id"],
            "event_id": self.event_id,
            "session_alias": "root-session",
            "is_root_session": True,
            "tool_name": "ask_advisor",
            "call_id": "call-1",
            "approval_request_id": "approval-1",
            "request_hash": self.request_hash,
            "arguments": self.arguments,
        }
        values.update(overrides)
        return ApprovalRequest(**values)  # type: ignore[arg-type]

    def decide(self, request: ApprovalRequest):
        return self.answerer.decide(
            request,
            state=self.state,
            expected_trigger_id=self.preview["trigger_id"],
            expected_request_hash=self.request_hash,
            call_limit=3,
        )

    def test_exact_request_is_allowed_once(self) -> None:
        decision = self.decide(self.request())
        self.assertEqual(decision.outcome, "allowed-once")
        self.assertEqual(decision.reason, "all_deterministic_checks_passed")

    def test_other_tool_is_rejected(self) -> None:
        decision = self.decide(self.request(tool_name="shell"))
        self.assertEqual(decision.outcome, "rejected")
        self.assertEqual(decision.reason, "tool_not_allowed")

    def test_non_root_session_is_rejected(self) -> None:
        decision = self.decide(self.request(is_root_session=False))
        self.assertEqual(decision.reason, "root_session_mismatch")

    def test_hash_mismatch_is_rejected(self) -> None:
        decision = self.decide(self.request(request_hash="0" * 64))
        self.assertEqual(decision.reason, "request_hash_mismatch")

    def test_sensitive_arguments_are_rejected(self) -> None:
        arguments = valid_arguments()
        arguments["constraints"] = "Authorization: secret"
        request_hash = canonical_request_hash(arguments)
        decision = self.answerer.decide(
            self.request(arguments=arguments, request_hash=request_hash),
            state=self.state,
            expected_trigger_id=self.preview["trigger_id"],
            expected_request_hash=request_hash,
            call_limit=3,
        )
        self.assertEqual(decision.reason, "sensitive_content_rejected")

    def test_unknown_send_and_retry_are_rejected(self) -> None:
        self.assertEqual(
            self.decide(self.request(unknown_send_state=True)).reason,
            "unknown_send_or_retry_rejected",
        )
        self.assertEqual(
            self.decide(self.request(retry_of="request-old")).reason,
            "unknown_send_or_retry_rejected",
        )

    def test_malformed_evidence_is_rejected(self) -> None:
        arguments = valid_arguments()
        arguments["evidence"] = [{"kind": "hidden_test", "source": "x"}]
        request_hash = canonical_request_hash(arguments)
        decision = self.answerer.decide(
            self.request(arguments=arguments, request_hash=request_hash),
            state=self.state,
            expected_trigger_id=self.preview["trigger_id"],
            expected_request_hash=request_hash,
            call_limit=3,
        )
        self.assertEqual(decision.reason, "invalid_evidence_item")


if __name__ == "__main__":
    unittest.main()
