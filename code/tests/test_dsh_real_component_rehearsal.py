from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    AdvisorResult,
    ApprovalDecision,
    HttpJsonResponse,
    OneShotAdvisorFlow,
    OneShotApprovalChannel,
    OneShotPromptGate,
    OneShotPromptGrant,
    OneShotPromptTransportExecutor,
    create_session_identity_pin,
    prompt_sha256,
    verify_session_identity,
)


class ScriptedSocket:
    def __init__(self, frames: list[Mapping[str, Any]]) -> None:
        self._frames = iter(frames)
        self.sent: list[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        return next(self._frames)

    def close(self) -> None:
        self.closed = True


class RealComponentRehearsalTests(unittest.TestCase):
    def test_real_components_complete_one_synthetic_flow(self) -> None:
        agent_id = "synthetic-agent"
        prompt = "Public synthetic task. Request one Advisor suggestion only."
        http_calls: list[tuple[str, Mapping[str, Any]]] = []

        def http_post(path: str, body: Mapping[str, Any]) -> HttpJsonResponse:
            http_calls.append((path, body))
            if path == "/api/session/prompt":
                return HttpJsonResponse(
                    200,
                    {
                        "rpcId": body["rpcId"],
                        "result": {"ok": True, "value": {"accepted": True}},
                    },
                )
            if path == "/api/$events/result":
                return HttpJsonResponse(
                    200,
                    {"rpcId": body["rpcId"], "result": {"ok": True}},
                )
            raise AssertionError("unexpected path")

        socket = ScriptedSocket(
            [
                {
                    "type": "item",
                    "streamId": "stream-rpc",
                    "value": {
                        "type": "ready",
                        "clientId": "client-opaque",
                        "host": {"home": "/redacted"},
                    },
                },
                {
                    "type": "item",
                    "streamId": "stream-rpc",
                    "value": {
                        "type": "waterfall",
                        "event": "approval/request",
                        "eventId": "event-opaque",
                        "agentId": agent_id,
                        "request": {
                            "toolName": "ask_advisor",
                            "callId": "call-opaque",
                        },
                    },
                },
            ]
        )
        prompt_transport = OneShotPromptTransportExecutor(
            http_post=http_post,
            id_factory=lambda: "prompt-rpc",
        )
        prompt_gate = OneShotPromptGate(transport=prompt_transport)
        approval_ids = iter(("stream-rpc", "approval-rpc"))
        approval_channel = OneShotApprovalChannel(
            http_post=http_post,
            websocket_connect=lambda path: socket
            if path == "/api/remote.mux"
            else None,
            expected_agent_id=agent_id,
            id_factory=lambda: next(approval_ids),
        )
        flow = OneShotAdvisorFlow(
            prompt_gate=prompt_gate,
            approval_channel=approval_channel,
            approval_policy=lambda request: ApprovalDecision(
                outcome="allowed-once",
                reason="synthetic exact match",
                approval_request_id="redacted-event-alias",
                call_id=request.call_id,
                request_hash="synthetic-hash",
            ),
            result_reader=lambda call_id: AdvisorResult(
                "success",
                "Inspect the smallest failing operation before changing state."
                if call_id == "call-opaque"
                else "",
                False,
            ),
        )
        pin = create_session_identity_pin(agent_id)
        result = flow.run_once(
            verified_session=verify_session_identity(pin, agent_id),
            grant=OneShotPromptGrant(pin.alias, prompt_sha256(prompt)),
            request_id="session-request",
            prompt=prompt,
        )

        self.assertEqual(result.status, "success")
        self.assertTrue(result.advisor_called)
        self.assertTrue(result.suggestion.startswith("[EXTERNAL_ADVISOR_SUGGESTION]"))
        self.assertEqual(
            [path for path, _body in http_calls],
            ["/api/session/prompt", "/api/$events/result"],
        )
        self.assertEqual(set(http_calls[0][1]["payload"]["args"]), {"request"})
        prompt_args = http_calls[0][1]["payload"]["args"]["request"]
        self.assertEqual(
            prompt_args["content"],
            [{"type": "text", "text": prompt}],
        )
        self.assertEqual(
            http_calls[1][1]["payload"]["args"]["outcome"],
            {"kind": "result", "value": "allowed-once"},
        )
        self.assertTrue(socket.closed)

    def test_prompt_executor_rejects_unreviewed_shape_before_http(self) -> None:
        calls: list[object] = []
        executor = OneShotPromptTransportExecutor(
            http_post=lambda *args: calls.append(args) or HttpJsonResponse(200, {}),
            id_factory=lambda: "rpc",
        )
        with self.assertRaisesRegex(RuntimeError, "request_not_allowed"):
            executor(
                "session/prompt",
                {
                    "sessionId": "agent",
                    "requestId": "request",
                    "mode": "steer",
                    "content": [{"type": "text", "text": "no"}],
                },
            )
        self.assertEqual(calls, [])

    def test_unknown_prompt_acceptance_is_not_retried(self) -> None:
        calls: list[object] = []
        executor = OneShotPromptTransportExecutor(
            http_post=lambda *args: calls.append(args)
            or HttpJsonResponse(503, {"secret": "hidden"}),
            id_factory=lambda: "rpc",
        )
        args = {
            "sessionId": "agent",
            "requestId": "request",
            "mode": "queue",
            "content": [{"type": "text", "text": "public synthetic prompt"}],
        }
        with self.assertRaisesRegex(RuntimeError, "http_status_not_ok"):
            executor("session/prompt", args)
        with self.assertRaisesRegex(RuntimeError, "transport_replayed"):
            executor("session/prompt", args)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

