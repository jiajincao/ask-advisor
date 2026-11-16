from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import OneShotToolCallResolver, canonical_request_hash  # noqa: E402


ARGS = {
    "question": "What is the safest next diagnostic step?",
    "goal": "Resolve a public synthetic failure.",
    "constraints": "Advice only; do not change state.",
    "attempts": "Two synthetic checks produced contradictory observations.",
    "evidence": [],
}


class FakeSocket:
    def __init__(self, frame: Mapping[str, Any]) -> None:
        self.frame = frame
        self.sent: list[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        return self.frame

    def close(self) -> None:
        self.closed = True


def snapshot(arguments: Mapping[str, Any], *, call_id: str = "call-1", name: str = "ask_advisor"):
    return {
        "type": "item",
        "streamId": "stream-1",
        "value": {
            "type": "snapshot",
            "records": [
                {
                    "event": {
                        "type": "tool/call",
                        "data": {
                            "turn": 1,
                            "step": 1,
                            "callId": call_id,
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                }
            ],
        },
    }


class ToolCallResolverTests(unittest.TestCase):
    def test_exact_call_returns_only_expected_hash_and_closes(self) -> None:
        socket = FakeSocket(snapshot(ARGS))
        resolver = OneShotToolCallResolver(
            websocket_connect=lambda _path: socket,
            session_id="agent-opaque",
            expected_arguments=ARGS,
            id_factory=lambda: "stream-1",
        )
        result = resolver.resolve_once("call-1")
        self.assertEqual(result, canonical_request_hash(ARGS))
        self.assertEqual(socket.sent[-1], {"type": "cancel", "streamId": "stream-1"})
        self.assertTrue(socket.closed)
        self.assertNotIn("question", result)
        with self.assertRaisesRegex(RuntimeError, "resolver_replayed"):
            resolver.resolve_once("call-1")

    def test_mismatch_other_tool_and_duplicate_fail_closed(self) -> None:
        cases = [
            snapshot({**ARGS, "question": "changed"}),
            snapshot(ARGS, name="other_tool"),
            {
                **snapshot(ARGS),
                "value": {
                    "type": "snapshot",
                    "records": snapshot(ARGS)["value"]["records"] * 2,
                },
            },
        ]
        for frame in cases:
            socket = FakeSocket(frame)
            resolver = OneShotToolCallResolver(
                websocket_connect=lambda _path, socket=socket: socket,
                session_id="agent-opaque",
                expected_arguments=ARGS,
                id_factory=lambda: "stream-1",
            )
            with self.assertRaises(RuntimeError):
                resolver.resolve_once("call-1")
            self.assertTrue(socket.closed)

    def test_follow_request_is_bounded_and_has_no_assistant_stream(self) -> None:
        socket = FakeSocket(snapshot(ARGS))
        resolver = OneShotToolCallResolver(
            websocket_connect=lambda _path: socket,
            session_id="agent-opaque",
            expected_arguments=ARGS,
            id_factory=lambda: "stream-1",
        )
        resolver.resolve_once("call-1")
        request = socket.sent[0]["payload"]["args"]["request"]
        self.assertEqual(request["maxMessages"], 50)
        self.assertNotIn("assistantStream", request)


if __name__ == "__main__":
    unittest.main()

