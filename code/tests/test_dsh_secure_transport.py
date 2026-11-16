from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    HttpJsonResponse,
    SecureReadonlyDshClient,
    create_session_identity_pin,
    verify_session_identity,
)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        stream_id = self.sent[0]["streamId"]
        return {
            "type": "item",
            "streamId": stream_id,
            "value": {
                "type": "snapshot",
                "cursor": 1,
                "records": [{"event": {"type": "turn/end", "data": {}}}],
                "hasMore": True,
                "projections": {},
            },
        }

    def close(self) -> None:
        self.closed = True


class FakeAuthenticatedSession:
    def __init__(self, *, fail_http: bool = False) -> None:
        self.fail_http = fail_http
        self.closed = False
        self.http_paths: list[str] = []
        self.websocket_paths: list[str] = []
        self.socket = FakeSocket()

    def post_json(self, path: str, body: dict[str, Any]) -> HttpJsonResponse:
        self.http_paths.append(path)
        if self.fail_http:
            raise RuntimeError("simulated transport failure")
        return HttpJsonResponse(
            200,
            {
                "rpcId": body["rpcId"],
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

    def connect_json(self, path: str) -> FakeSocket:
        self.websocket_paths.append(path)
        return self.socket

    def close(self) -> None:
        self.closed = True


class SecureReadonlyDshClientTests(unittest.TestCase):
    def test_session_is_lazy_and_closed_after_success(self) -> None:
        created: list[FakeAuthenticatedSession] = []

        def factory() -> FakeAuthenticatedSession:
            session = FakeAuthenticatedSession()
            created.append(session)
            return session

        client = SecureReadonlyDshClient(session_factory=factory)
        self.assertEqual(created, [])
        with client:
            roots = client.list_root_sessions()
            selected = client.select_latest_nonblank_root(roots)
            pin = create_session_identity_pin(selected.opaque_id)
            verified = verify_session_identity(pin, selected.opaque_id)
            summary = client.follow_verified_once(verified)
            self.assertEqual(summary.event_type_counts, (("turn/end", 1),))
            self.assertFalse(created[0].closed)
        self.assertTrue(created[0].closed)
        self.assertTrue(created[0].socket.closed)
        self.assertEqual(
            created[0].http_paths,
            ["/api/session/list", "/api/session/list"],
        )
        self.assertEqual(created[0].websocket_paths, ["/api/remote.mux"])

    def test_session_closes_when_transport_raises(self) -> None:
        session = FakeAuthenticatedSession(fail_http=True)
        client = SecureReadonlyDshClient(session_factory=lambda: session)
        with self.assertRaisesRegex(RuntimeError, "simulated transport failure"):
            with client:
                client.list_root_sessions()
        self.assertTrue(session.closed)

    def test_operations_require_an_open_scope(self) -> None:
        client = SecureReadonlyDshClient(
            session_factory=lambda: FakeAuthenticatedSession()
        )
        with self.assertRaisesRegex(RuntimeError, "client_not_open"):
            client.list_root_sessions()

    def test_client_cannot_be_entered_twice(self) -> None:
        client = SecureReadonlyDshClient(
            session_factory=lambda: FakeAuthenticatedSession()
        )
        with client:
            with self.assertRaisesRegex(RuntimeError, "client_already_open"):
                client.__enter__()

    def test_all_write_capabilities_remain_disabled(self) -> None:
        client = SecureReadonlyDshClient(
            session_factory=lambda: FakeAuthenticatedSession()
        )
        for method in (
            client.create_session,
            client.send_prompt,
            client.answer_approval,
            client.call_advisor,
        ):
            with self.assertRaisesRegex(RuntimeError, "intentionally disabled"):
                method()

    def test_raw_follow_requires_verified_identity(self) -> None:
        session = FakeAuthenticatedSession()
        client = SecureReadonlyDshClient(session_factory=lambda: session)
        with client:
            root = client.list_root_sessions()[0]
            with self.assertRaisesRegex(RuntimeError, "verified_identity_required"):
                client.follow_once(root)

    def test_verified_reference_is_one_use(self) -> None:
        session = FakeAuthenticatedSession()
        client = SecureReadonlyDshClient(session_factory=lambda: session)
        pin = create_session_identity_pin("opaque-root")
        verified = verify_session_identity(pin, "opaque-root")
        with client:
            client.follow_verified_once(verified)
            with self.assertRaisesRegex(RuntimeError, "replayed_reference"):
                client.follow_verified_once(verified)

    def test_verified_missing_root_fails_closed(self) -> None:
        session = FakeAuthenticatedSession()
        client = SecureReadonlyDshClient(session_factory=lambda: session)
        pin = create_session_identity_pin("no-longer-present")
        verified = verify_session_identity(pin, "no-longer-present")
        with client:
            with self.assertRaisesRegex(RuntimeError, "pinned_root_unavailable"):
                client.follow_verified_once(verified)


if __name__ == "__main__":
    unittest.main()
