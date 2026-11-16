from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    ApprovalRequest,
    OfflineRunner,
    OneShotPromptGate,
    OneShotPromptGrant,
    RestrictedApprovalAnswerer,
    RestrictedApprovalDispatcher,
    canonical_request_hash,
    create_session_identity_pin,
    prompt_sha256,
    verify_session_identity,
)


class OneShotBridgeTests(unittest.TestCase):
    def test_exact_prompt_is_sent_once_without_audit_body(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        def transport(endpoint: str, args: dict[str, Any]):
            calls.append((endpoint, args))
            return {"accepted": True}

        prompt = "Use ask_advisor once for this public synthetic failure."
        pin = create_session_identity_pin("synthetic-session")
        verified = verify_session_identity(pin, "synthetic-session")
        grant = OneShotPromptGrant(pin.alias, prompt_sha256(prompt))
        result = OneShotPromptGate(transport=transport).send_once(
            verified_session=verified,
            grant=grant,
            request_id="request-1",
            prompt=prompt,
        )
        self.assertTrue(result["accepted"])
        self.assertNotIn(prompt, repr(result))
        self.assertEqual(calls[0][0], "session/prompt")
        self.assertEqual(calls[0][1]["mode"], "queue")
        self.assertEqual(
            calls[0][1]["content"],
            [{"type": "text", "text": prompt}],
        )
        self.assertNotIn("text", calls[0][1])
        with self.assertRaisesRegex(RuntimeError, "grant_replayed"):
            OneShotPromptGate(transport=transport).send_once(
                verified_session=verified,
                grant=grant,
                request_id="request-2",
                prompt=prompt,
            )
        self.assertEqual(len(calls), 1)

    def test_hash_alias_and_sensitive_content_fail_closed(self) -> None:
        calls: list[object] = []
        gate = OneShotPromptGate(transport=lambda *args: calls.append(args) or {})
        pin = create_session_identity_pin("synthetic-session")
        prompt = "public synthetic prompt"
        with self.assertRaisesRegex(RuntimeError, "prompt_hash_mismatch"):
            gate.send_once(
                verified_session=verify_session_identity(pin, "synthetic-session"),
                grant=OneShotPromptGrant(pin.alias, prompt_sha256("different")),
                request_id="request-1",
                prompt=prompt,
            )
        with self.assertRaisesRegex(RuntimeError, "session_alias_mismatch"):
            gate.send_once(
                verified_session=verify_session_identity(pin, "synthetic-session"),
                grant=OneShotPromptGrant("other-alias", prompt_sha256(prompt)),
                request_id="request-1",
                prompt=prompt,
            )
        with self.assertRaises(ValueError):
            gate.send_once(
                verified_session=verify_session_identity(pin, "synthetic-session"),
                grant=OneShotPromptGrant(pin.alias, prompt_sha256("Authorization: secret")),
                request_id="request-1",
                prompt="Authorization: secret",
            )
        self.assertEqual(calls, [])

    def test_unknown_acceptance_consumes_grant_without_retry(self) -> None:
        prompt = "public synthetic prompt"
        pin = create_session_identity_pin("synthetic-session")
        grant = OneShotPromptGrant(pin.alias, prompt_sha256(prompt))
        gate = OneShotPromptGate(transport=lambda _endpoint, _args: {"accepted": False})
        with self.assertRaisesRegex(RuntimeError, "not_accepted"):
            gate.send_once(
                verified_session=verify_session_identity(pin, "synthetic-session"),
                grant=grant,
                request_id="request-1",
                prompt=prompt,
            )
        self.assertTrue(grant.consumed)

    def test_approval_dispatcher_rejects_replay(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runner = OfflineRunner(Path(temp.name))
        state = runner.create_run("task", "C", 1)
        event_id = runner.record_observable_event(
            state,
            step_number=3,
            action_type="diagnose_reproduce",
            summary="Synthetic failure.",
            is_new_information=True,
        )
        runner.record_system_one_result(
            state,
            event_id=event_id,
            p_yes=0.8,
            p_no=0.2,
            label_mass=0.99,
            threshold=0.7,
        )
        preview = runner.build_advisor_preview(state, event_id, "Public context.")
        runner.mark_request_started(state, event_id)
        arguments = {
            "question": "What next?",
            "goal": "Resolve synthetic failure.",
            "constraints": "Public data only.",
            "attempts": "One synthetic attempt.",
            "evidence": [],
        }
        request_hash = canonical_request_hash(arguments)
        request = ApprovalRequest(
            run_id=state.run_id,
            trigger_id=preview["trigger_id"],
            event_id=event_id,
            session_alias="root",
            is_root_session=True,
            tool_name="ask_advisor",
            call_id="call-1",
            approval_request_id="approval-1",
            request_hash=request_hash,
            arguments=arguments,
        )
        dispatcher = RestrictedApprovalDispatcher(
            answerer=RestrictedApprovalAnswerer(expected_session_alias="root")
        )
        first = dispatcher.decide_once(
            request,
            state=state,
            expected_trigger_id=preview["trigger_id"],
            expected_request_hash=request_hash,
        )
        second = dispatcher.decide_once(
            request,
            state=state,
            expected_trigger_id=preview["trigger_id"],
            expected_request_hash=request_hash,
        )
        self.assertEqual(first.outcome, "allowed-once")
        self.assertEqual(second.outcome, "rejected")
        self.assertEqual(second.reason, "approval_replay_rejected")


if __name__ == "__main__":
    unittest.main()
