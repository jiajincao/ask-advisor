from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import OneShotAdvisorResultReader  # noqa: E402


def tool_result(call_id: str = "call-1", status: str = "ok", text: str = "Try one diagnostic."):
    value = {"status": status, "text": text, "request_id": "remote-id", "truncated": False}
    return {
        "event": {
            "type": "tool/result",
            "data": {
                "message": {
                    "source": {"callId": call_id},
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": call_id,
                            "content": [{"type": "text", "text": json.dumps(value)}],
                        }
                    ],
                }
            },
        }
    }


class FakeSocket:
    def __init__(self, values: list[Mapping[str, Any]]) -> None:
        self.values = iter(values)
        self.sent: list[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        return next(self.values)

    def close(self) -> None:
        self.closed = True


class AdvisorResultReaderTests(unittest.TestCase):
    def test_reads_correlated_live_result_and_normalizes_ok(self) -> None:
        socket = FakeSocket(
            [
                {"type": "item", "streamId": "stream-1", "value": {"type": "snapshot", "records": []}},
                {"type": "item", "streamId": "stream-1", "value": tool_result()},
            ]
        )
        reader = OneShotAdvisorResultReader(
            websocket_connect=lambda _path: socket,
            session_id="session",
            id_factory=lambda: "stream-1",
        )
        result = reader("call-1")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.text, "Try one diagnostic.")
        self.assertTrue(socket.closed)
        with self.assertRaisesRegex(RuntimeError, "reader_replayed"):
            reader("call-1")

    def test_snapshot_result_is_accepted_and_stream_cancelled(self) -> None:
        socket = FakeSocket(
            [{"type": "item", "streamId": "stream-1", "value": {"type": "snapshot", "records": [tool_result()]}}]
        )
        reader = OneShotAdvisorResultReader(
            websocket_connect=lambda _path: socket,
            session_id="session",
            id_factory=lambda: "stream-1",
        )
        reader("call-1")
        self.assertEqual(socket.sent[-1], {"type": "cancel", "streamId": "stream-1"})

    def test_wrong_call_is_ignored_then_matching_result_is_used(self) -> None:
        socket = FakeSocket(
            [
                {"type": "item", "streamId": "stream-1", "value": {"type": "snapshot", "records": [tool_result("other")] }},
                {"type": "item", "streamId": "stream-1", "value": tool_result("call-1")},
            ]
        )
        reader = OneShotAdvisorResultReader(
            websocket_connect=lambda _path: socket,
            session_id="session",
            id_factory=lambda: "stream-1",
        )
        self.assertEqual(reader("call-1").status, "success")

    def test_sensitive_or_invalid_result_fails_closed(self) -> None:
        for record in (
            tool_result(text="Authorization: secret"),
            {"event": {"type": "tool/result", "data": {"message": {"source": {"callId": "call-1"}, "content": []}}}},
        ):
            socket = FakeSocket(
                [{"type": "item", "streamId": "stream-1", "value": {"type": "snapshot", "records": [record]}}]
            )
            reader = OneShotAdvisorResultReader(
                websocket_connect=lambda _path, socket=socket: socket,
                session_id="session",
                id_factory=lambda: "stream-1",
            )
            with self.assertRaises((RuntimeError, ValueError)):
                reader("call-1")
            self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()

