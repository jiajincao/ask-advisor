from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    AdvisorResult,
    ApprovalDecision,
    ApprovalPresentation,
    OneShotAdvisorFlow,
    OneShotFlowResult,
    OneShotPromptGrant,
    create_session_identity_pin,
    prompt_sha256,
    verify_session_identity,
)


class FakePromptGate:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.calls = 0

    def send_once(self, **_kwargs):
        self.calls += 1
        self.events.append("prompt")
        if self.fail:
            raise RuntimeError("synthetic prompt failure")
        return {"accepted": True}


class FakeApprovalChannel:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.answers: list[str] = []

    def open(self) -> None:
        self.events.append("approval-open")

    def receive_once(self) -> ApprovalPresentation:
        self.events.append("approval-receive")
        return ApprovalPresentation("ask_advisor", "call-1", False)

    def answer_once(self, outcome: str):
        self.answers.append(outcome)
        self.events.append(f"approval-{outcome}")
        return {"answered": True}

    def close(self) -> None:
        self.events.append("approval-close")


def allowed(call_id: str = "call-1") -> ApprovalDecision:
    return ApprovalDecision(
        outcome="allowed-once",
        reason="test",
        approval_request_id="event-alias",
        call_id=call_id,
        request_hash="hash",
    )


class OneShotFlowTests(unittest.TestCase):
    def inputs(self):
        prompt = "Public synthetic task requesting one Advisor suggestion."
        pin = create_session_identity_pin("synthetic-session")
        return {
            "verified_session": verify_session_identity(pin, "synthetic-session"),
            "grant": OneShotPromptGrant(pin.alias, prompt_sha256(prompt)),
            "request_id": "request-1",
            "prompt": prompt,
        }

    def test_happy_path_has_fixed_order_and_suggestion_boundary(self) -> None:
        events: list[str] = []
        gate = FakePromptGate(events)
        channel = FakeApprovalChannel(events)

        def read_result(call_id: str) -> AdvisorResult:
            self.assertEqual(call_id, "call-1")
            events.append("advisor-result")
            return AdvisorResult("success", "Try the smaller diagnostic step.", False)

        flow = OneShotAdvisorFlow(
            prompt_gate=gate,
            approval_channel=channel,
            approval_policy=lambda _request: allowed(),
            result_reader=read_result,
        )
        result = flow.run_once(**self.inputs())
        self.assertEqual(
            result,
            OneShotFlowResult(
                status="success",
                advisor_called=True,
                suggestion=(
                    "[EXTERNAL_ADVISOR_SUGGESTION]\n"
                    "Try the smaller diagnostic step."
                ),
                truncated=False,
            ),
        )
        self.assertEqual(
            events,
            [
                "approval-open",
                "prompt",
                "approval-receive",
                "approval-allowed-once",
                "advisor-result",
                "approval-close",
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "flow_replayed"):
            flow.run_once(**self.inputs())
        self.assertEqual(gate.calls, 1)

    def test_policy_rejection_stops_before_result(self) -> None:
        events: list[str] = []
        channel = FakeApprovalChannel(events)
        reads: list[str] = []
        flow = OneShotAdvisorFlow(
            prompt_gate=FakePromptGate(events),
            approval_channel=channel,
            approval_policy=lambda request: ApprovalDecision(
                "rejected", "policy", "event-alias", request.call_id, "hash"
            ),
            result_reader=lambda call_id: reads.append(call_id),
        )
        result = flow.run_once(**self.inputs())
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.advisor_called)
        self.assertEqual(channel.answers, ["rejected"])
        self.assertEqual(reads, [])

    def test_call_mismatch_is_rejected_and_never_reads_result(self) -> None:
        events: list[str] = []
        channel = FakeApprovalChannel(events)
        reads: list[str] = []
        flow = OneShotAdvisorFlow(
            prompt_gate=FakePromptGate(events),
            approval_channel=channel,
            approval_policy=lambda _request: allowed("other-call"),
            result_reader=lambda call_id: reads.append(call_id),
        )
        with self.assertRaisesRegex(RuntimeError, "approval_correlation_mismatch"):
            flow.run_once(**self.inputs())
        self.assertEqual(channel.answers, ["rejected"])
        self.assertEqual(reads, [])

    def test_prompt_failure_attempts_rejection_and_does_not_retry(self) -> None:
        events: list[str] = []
        gate = FakePromptGate(events, fail=True)
        channel = FakeApprovalChannel(events)
        flow = OneShotAdvisorFlow(
            prompt_gate=gate,
            approval_channel=channel,
            approval_policy=lambda _request: allowed(),
            result_reader=lambda _call: AdvisorResult("success", "unused", False),
        )
        with self.assertRaisesRegex(RuntimeError, "synthetic prompt failure"):
            flow.run_once(**self.inputs())
        self.assertEqual(gate.calls, 1)
        self.assertEqual(channel.answers, ["rejected"])

    def test_sensitive_or_failed_advisor_result_is_not_returned(self) -> None:
        for advisor in (
            AdvisorResult("unknown", "No stable result.", False),
            AdvisorResult("success", "Authorization: secret", False),
        ):
            events: list[str] = []
            flow = OneShotAdvisorFlow(
                prompt_gate=FakePromptGate(events),
                approval_channel=FakeApprovalChannel(events),
                approval_policy=lambda _request: allowed(),
                result_reader=lambda _call, advisor=advisor: advisor,
            )
            with self.assertRaises((RuntimeError, ValueError)):
                flow.run_once(**self.inputs())


if __name__ == "__main__":
    unittest.main()

