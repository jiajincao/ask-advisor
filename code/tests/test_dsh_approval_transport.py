from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    HttpJsonResponse,
    OneShotApprovalChannel,
)


class FakeWebSocket:
    def __init__(self, incoming: list[Mapping[str, Any]]) -> None:
        self.incoming = iter(incoming)
        self.sent: list[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        return next(self.incoming)

    def close(self) -> None:
        self.closed = True


class ApprovalTransportTests(unittest.TestCase):
    def make_socket(self, request: Mapping[str, Any] | None = None) -> FakeWebSocket:
        return FakeWebSocket(
            [
                {
                    "type": "item",
                    "streamId": "stream-1",
                    "value": {
                        "type": "ready",
                        "clientId": "client-opaque",
                        "host": {"home": "/redacted"},
                    },
                },
                {
                    "type": "item",
                    "streamId": "stream-1",
                    "value": {
                        "type": "waterfall",
                        "event": "approval/request",
                        "eventId": "event-opaque",
                        "agentId": "agent-opaque",
                        "request": request
                        or {"toolName": "ask_advisor", "callId": "call-1"},
                    },
                },
            ]
        )

    def test_exact_approval_is_answered_once_and_ids_are_not_returned(self) -> None:
        ws = self.make_socket()
        posts: list[tuple[str, Mapping[str, Any]]] = []

        def post(path: str, body: Mapping[str, Any]) -> HttpJsonResponse:
            posts.append((path, body))
            return HttpJsonResponse(200, {"rpcId": "rpc-1", "result": {"ok": True}})

        ids = iter(("stream-1", "rpc-1"))
        channel = OneShotApprovalChannel(
            http_post=post,
            websocket_connect=lambda _path: ws,
            expected_agent_id="agent-opaque",
            id_factory=lambda: next(ids),
        )
        channel.open()
        presentation = channel.receive_once()
        result = channel.answer_once("allowed-once")
        self.assertEqual(presentation.tool_name, "ask_advisor")
        self.assertEqual(presentation.call_id, "call-1")
        self.assertEqual(result, {"answered": True, "outcome": "allowed-once"})
        self.assertEqual(posts[0][0], "/api/$events/result")
        self.assertEqual(
            posts[0][1]["payload"]["args"]["outcome"],
            {"kind": "result", "value": "allowed-once"},
        )
        self.assertNotIn("opaque", repr(channel))
        self.assertTrue(ws.closed)
        with self.assertRaisesRegex(RuntimeError, "answer_replayed"):
            channel.answer_once("rejected")
        self.assertEqual(len(posts), 1)

    def test_open_uses_exact_events_stream_protocol(self) -> None:
        ws = self.make_socket()
        channel = OneShotApprovalChannel(
            http_post=lambda _path, _body: HttpJsonResponse(200, {}),
            websocket_connect=lambda path: ws if path == "/api/remote.mux" else None,
            expected_agent_id="agent-opaque",
            id_factory=lambda: "stream-1",
        )
        channel.open()
        self.assertEqual(
            ws.sent[0],
            {
                "type": "open",
                "streamId": "stream-1",
                "endpoint": "$events",
                "payload": {"args": {}},
            },
        )
        channel.close()
        self.assertEqual(ws.sent[-1], {"type": "cancel", "streamId": "stream-1"})

    def test_emit_before_approval_is_ignored(self) -> None:
        ws = self.make_socket()
        frames = list(ws.incoming)
        ws = FakeWebSocket(
            [
                frames[0],
                {
                    "type": "item",
                    "streamId": "stream-1",
                    "value": {"type": "emit", "event": "api-session/status", "args": []},
                },
                frames[1],
            ]
        )
        channel = OneShotApprovalChannel(
            http_post=lambda _path, _body: HttpJsonResponse(200, {}),
            websocket_connect=lambda _path: ws,
            expected_agent_id="agent-opaque",
            id_factory=lambda: "stream-1",
        )
        channel.open()
        self.assertEqual(channel.receive_once().call_id, "call-1")
        channel.close()

    def test_wrong_agent_or_tool_fails_closed_without_http(self) -> None:
        for request, agent in (
            ({"toolName": "other", "callId": "call-1"}, "agent-opaque"),
            ({"toolName": "ask_advisor", "callId": "call-1"}, "other-agent"),
        ):
            ws = self.make_socket(request)
            posts: list[object] = []
            channel = OneShotApprovalChannel(
                http_post=lambda *args: posts.append(args) or HttpJsonResponse(200, {}),
                websocket_connect=lambda _path, ws=ws: ws,
                expected_agent_id=agent,
                id_factory=lambda: "stream-1",
            )
            channel.open()
            with self.assertRaisesRegex(RuntimeError, "approval_not_allowed|receive_failed"):
                channel.receive_once()
            channel.close()
            self.assertEqual(posts, [])

    def test_unknown_http_result_consumes_answer_and_closes(self) -> None:
        ws = self.make_socket()
        channel = OneShotApprovalChannel(
            http_post=lambda _path, _body: HttpJsonResponse(503, {"secret": "hidden"}),
            websocket_connect=lambda _path: ws,
            expected_agent_id="agent-opaque",
            id_factory=iter(("stream-1", "rpc-1")).__next__,
        )
        channel.open()
        channel.receive_once()
        with self.assertRaisesRegex(RuntimeError, "http_status_not_ok") as caught:
            channel.answer_once("allowed-once")
        self.assertNotIn("hidden", str(caught.exception))
        self.assertTrue(ws.closed)
        with self.assertRaisesRegex(RuntimeError, "answer_replayed"):
            channel.answer_once("allowed-once")


if __name__ == "__main__":
    unittest.main()

