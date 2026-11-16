from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    HttpJsonResponse,
    ReadonlyDshAdapter,
    ReadonlyDshTransportExecutor,
)


class FakeWebSocket:
    def __init__(self, incoming: Mapping[str, Any]) -> None:
        self.incoming = incoming
        self.sent: list[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        return self.incoming

    def close(self) -> None:
        self.closed = True


class ReadonlyDshTransportExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.http_calls: list[tuple[str, Mapping[str, Any]]] = []
        self.ws_paths: list[str] = []
        self.ws = FakeWebSocket(
            {
                "type": "item",
                "streamId": "stream-1",
                "value": {
                    "type": "snapshot",
                    "cursor": 4,
                    "records": [
                        {
                            "event": {
                                "type": "turn/end",
                                "data": {"private": "never projected"},
                            }
                        }
                    ],
                    "hasMore": True,
                    "projections": {"asOfSeq": 4},
                },
            }
        )

        def http_post(path: str, body: Mapping[str, Any]) -> HttpJsonResponse:
            self.http_calls.append((path, body))
            return HttpJsonResponse(
                status=200,
                body={
                    "rpcId": "rpc-1",
                    "result": {
                        "ok": True,
                        "value": {
                            "items": [
                                {
                                    "sessionId": "opaque-root",
                                    "updatedAt": 1,
                                    "running": False,
                                    "blank": False,
                                }
                            ]
                        },
                    },
                },
            )

        def websocket_connect(path: str) -> FakeWebSocket:
            self.ws_paths.append(path)
            return self.ws

        ids = iter(("rpc-1", "stream-1"))
        self.executor = ReadonlyDshTransportExecutor(
            http_post=http_post,
            websocket_connect=websocket_connect,
            id_factory=lambda: next(ids),
        )
        self.adapter = ReadonlyDshAdapter(
            unary_call=self.executor.unary_call,
            stream_open=self.executor.stream_open,
        )

    def test_http_envelope_and_adapter_integration(self) -> None:
        roots = self.adapter.list_root_sessions()
        self.assertEqual(len(roots), 1)
        self.assertEqual(
            self.http_calls,
            [
                (
                    "/api/session/list",
                    {
                        "type": "client-request",
                        "rpcId": "rpc-1",
                        "method": "session/list",
                        "payload": {"args": {"_request": {}}},
                    },
                )
            ],
        )

    def test_websocket_cancels_and_closes_before_summary_returns(self) -> None:
        root = self.adapter.list_root_sessions()[0]
        summary = self.adapter.follow_once(root)
        self.assertEqual(self.ws_paths, ["/api/remote.mux"])
        self.assertEqual(
            self.ws.sent[0],
            {
                "type": "open",
                "streamId": "stream-1",
                "endpoint": "session/follow",
                "payload": {
                    "args": {
                        "request": {
                            "address": {
                                "kind": "session",
                                "sessionId": "opaque-root",
                            },
                            "maxMessages": 1,
                        }
                    }
                },
            },
        )
        self.assertEqual(
            self.ws.sent[-1],
            {"type": "cancel", "streamId": "stream-1"},
        )
        self.assertTrue(self.ws.closed)
        self.assertEqual(summary.event_type_counts, (("turn/end", 1),))
        self.assertNotIn("private", repr(summary))

    def test_unreviewed_calls_fail_before_transport(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unary_call_not_allowed"):
            self.executor.unary_call("session/create", {})
        with self.assertRaisesRegex(RuntimeError, "stream_open_not_allowed"):
            self.executor.stream_open(
                "session/follow",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": "opaque-root"},
                        "maxMessages": 2,
                    }
                },
            )
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.ws_paths, [])

    def test_rpc_mismatch_is_content_free(self) -> None:
        executor = ReadonlyDshTransportExecutor(
            http_post=lambda _path, _body: HttpJsonResponse(
                200,
                {"rpcId": "wrong", "result": {"ok": False, "secret": "hidden"}},
            ),
            websocket_connect=lambda _path: self.ws,
            id_factory=lambda: "expected",
        )
        with self.assertRaisesRegex(RuntimeError, "rpc_id_mismatch") as caught:
            executor.unary_call("session/list", {"_request": {}})
        self.assertNotIn("hidden", str(caught.exception))

    def test_bad_stream_frame_still_cancels_and_closes(self) -> None:
        ws = FakeWebSocket(
            {"type": "error", "streamId": "stream-x", "secret": "hidden"}
        )
        executor = ReadonlyDshTransportExecutor(
            http_post=lambda _path, _body: HttpJsonResponse(200, {}),
            websocket_connect=lambda _path: ws,
            id_factory=lambda: "stream-x",
        )
        frames = executor.stream_open(
            "session/follow",
            {
                "request": {
                    "address": {"kind": "session", "sessionId": "opaque-root"},
                    "maxMessages": 1,
                }
            },
        )
        with self.assertRaisesRegex(ValueError, "unexpected_stream_frame") as caught:
            next(frames)
        self.assertEqual(
            ws.sent[-1],
            {"type": "cancel", "streamId": "stream-x"},
        )
        self.assertTrue(ws.closed)
        self.assertNotIn("hidden", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
