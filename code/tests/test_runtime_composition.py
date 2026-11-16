from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    AdvisorInvocation,
    AdvisorResult,
    ApprovalDecision,
    ApprovalPresentation,
    OneShotAdvisorFlow,
    OneShotPromptGrant,
    RuntimeBridgeCoordinator,
    check_deployment_environment,
    create_session_identity_pin,
    prompt_sha256,
    verify_session_identity,
)


class PromptGate:
    def send_once(self, **_kwargs):
        return {"accepted": True}


class ApprovalChannel:
    def __init__(self):
        self.closed = False

    def open(self):
        return None

    def receive_once(self):
        return ApprovalPresentation("ask_advisor", "call-redacted", False)

    def answer_once(self, _outcome):
        return {"ok": True}

    def close(self):
        self.closed = True


def systemone(p_yes=0.8):
    return {
        "answers": {
            "need_advisor": {
                "probabilities": {"yes": p_yes, "no": 1.0 - p_yes}
            }
        },
        "diagnostics": {
            "need_advisor": {"label_mass": 0.99, "argmax_is_label": True}
        },
    }


class RuntimeCompositionTests(unittest.TestCase):
    def invocation(self, prompt, *, suggestion="Use one reversible check."):
        pin = create_session_identity_pin("synthetic-session")
        channel = ApprovalChannel()
        flow = OneShotAdvisorFlow(
            prompt_gate=PromptGate(),
            approval_channel=channel,
            approval_policy=lambda presentation: ApprovalDecision(
                outcome="allowed-once",
                reason="synthetic_exact_match",
                approval_request_id="approval-redacted",
                call_id=presentation.call_id,
                request_hash="0" * 64,
            ),
            result_reader=lambda _call_id: AdvisorResult(
                status="success", text=suggestion, truncated=False
            ),
        )
        return AdvisorInvocation(
            verified_session=verify_session_identity(pin, "synthetic-session"),
            grant=OneShotPromptGrant(pin.alias, prompt_sha256(prompt)),
            request_id="request-redacted",
            flow=flow,
        )

    def test_coordinator_builds_fresh_bridge_and_content_free_audit(self) -> None:
        audits = []
        coordinator = RuntimeBridgeCoordinator(
            system_one_call=lambda _request: systemone(),
            invocation_factory=self.invocation,
            audit_record=audits.append,
        )
        result = coordinator({"state": {}, "questions": {}}, "Public prompt")
        self.assertTrue(result.gate.triggered)
        self.assertTrue(result.flow and result.flow.advisor_called)
        self.assertEqual(len(audits), 1)
        self.assertFalse(audits[0]["content_persisted"])
        self.assertNotIn("suggestion", audits[0])
        self.assertNotIn("prompt", audits[0])

    def test_prompt_hash_mismatch_fails_before_systemone(self) -> None:
        calls = []

        def bad_invocation(prompt):
            invocation = self.invocation(prompt)
            invocation.grant.expected_prompt_sha256 = "0" * 64
            return invocation

        coordinator = RuntimeBridgeCoordinator(
            system_one_call=lambda request: calls.append(request),
            invocation_factory=bad_invocation,
            audit_record=lambda _record: None,
        )
        with self.assertRaisesRegex(RuntimeError, "prompt_hash_mismatch"):
            coordinator({}, "Public prompt")
        self.assertEqual(calls, [])

    def test_deployment_check_never_returns_values_and_requires_disabled_state(self) -> None:
        env = {
            "ASK_ADVISOR_MODEL": "hidden-model",
            "ASK_ADVISOR_PROXY_BIND_HOST": "hidden-host",
            "ASK_ADVISOR_PROXY_BIND_PORT": "hidden-port",
            "ASK_ADVISOR_PROXY_API_KEY": "hidden-key",
            "ASK_ADVISOR_VLLM_BASE_URL": "hidden-url",
            "ASK_ADVISOR_DSH_BIN": "hidden-bin",
            "ASK_ADVISOR_PIN_DIR": "hidden-dir",
        }
        check = check_deployment_environment(env)
        self.assertTrue(check.ok)
        self.assertTrue(check.activation_disabled)
        self.assertNotIn("hidden", repr(check))
        enabled = check_deployment_environment(
            {**env, "ASK_ADVISOR_ENABLE_RUNTIME": "1"}
        )
        self.assertFalse(enabled.ok)
        self.assertFalse(enabled.activation_disabled)


if __name__ == "__main__":
    unittest.main()
