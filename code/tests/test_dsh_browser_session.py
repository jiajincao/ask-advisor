from __future__ import annotations

import http.cookiejar
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    BrowserAuthenticatedDshSession,
    create_session_identity_pin,
    make_browser_readonly_client,
    verify_session_identity,
)


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body, self.status, self.closed = body, status, False

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, jar: http.cookiejar.CookieJar) -> None:
        self.requests: list[object] = []
        self.responses: list[FakeResponse] = []
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0, name="synthetic_session", value="synthetic_secret",
                port="43123", port_specified=True, domain="127.0.0.1",
                domain_specified=False, domain_initial_dot=False, path="/",
                path_specified=True, secure=False, expires=None, discard=True,
                comment=None, comment_url=None, rest={},
            )
        )

    def open(self, request: object, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        if isinstance(request, str):
            response = FakeResponse(b"authenticated")
        else:
            request_body = json.loads(request.data)
            response = FakeResponse(json.dumps({
                "rpcId": request_body["rpcId"],
                "result": {"ok": True, "value": {"items": [
                    {
                        "sessionId": "opaque-root",
                        "updatedAt": 1,
                        "running": False,
                        "blank": False,
                    }
                ]}},
            }).encode())
        self.responses.append(response)
        return response


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[Mapping[str, Any]] = []

    def send_json(self, frame: Mapping[str, Any]) -> None:
        self.sent.append(frame)

    def receive_json(self) -> Mapping[str, Any]:
        return {
            "type": "item",
            "streamId": self.sent[0]["streamId"],
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


class BrowserAuthenticatedDshSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.openers: list[FakeOpener] = []
        self.dials: list[tuple[str, Mapping[str, str], FakeSocket]] = []

        def opener_factory(jar: http.cookiejar.CookieJar) -> FakeOpener:
            opener = FakeOpener(jar)
            self.openers.append(opener)
            return opener

        def dialer(url: str, headers: Mapping[str, str]) -> FakeSocket:
            socket = FakeSocket()
            self.dials.append((url, headers, socket))
            return socket

        self.session = BrowserAuthenticatedDshSession(
            bootstrap_url="http://127.0.0.1:43123/?token=synthetic_secret",
            websocket_dialer=dialer,
            opener_factory=opener_factory,
        )

    def tearDown(self) -> None:
        self.session.close()

    def test_bootstrap_secret_is_redacted_and_not_retained(self) -> None:
        shown = repr(self.session)
        self.assertNotIn("synthetic_secret", shown)
        self.assertNotIn("43123", shown)
        self.assertIn("<redacted>", shown)
        self.assertFalse(hasattr(self.session, "bootstrap_url"))

    def test_post_json_uses_relative_api_path(self) -> None:
        response = self.session.post_json(
            "/api/session/list", {"rpcId": "rpc-synthetic"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["rpcId"], "rpc-synthetic")
        request = self.openers[0].requests[-1]
        self.assertEqual(request.full_url, "http://127.0.0.1:43123/api/session/list")
        self.assertTrue(self.openers[0].responses[-1].closed)

    def test_websocket_receives_cookie_only_inside_dialer(self) -> None:
        socket = self.session.connect_json("/api/remote.mux")
        url, headers, _ = self.dials[0]
        self.assertEqual(url, "ws://127.0.0.1:43123/api/remote.mux")
        self.assertIn("Cookie", headers)
        self.assertNotIn("synthetic_secret", repr(self.session))
        self.session.close()
        self.assertTrue(socket.closed)

    def test_non_loopback_and_credential_authorities_are_rejected(self) -> None:
        for url in (
            "http://example.invalid:43123/?token=x",
            "http://user:pass@127.0.0.1:43123/?token=x",
            "http://127.0.0.1/?token=x",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "non_loopback_authority"):
                    BrowserAuthenticatedDshSession(
                        bootstrap_url=url,
                        websocket_dialer=lambda _url, _headers: FakeSocket(),
                    )

    def test_close_is_idempotent_and_clears_session(self) -> None:
        self.session.close()
        self.session.close()
        self.assertIn("closed", repr(self.session))
        with self.assertRaisesRegex(RuntimeError, "session_closed"):
            self.session.post_json("/api/session/list", {})

    def test_invalid_paths_are_rejected_before_io(self) -> None:
        before = len(self.openers[0].requests)
        for path in ("/not-api", "/api/../secret", "/api/x?secret=y"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "invalid_relative_api_path"):
                    self.session.post_json(path, {})
        self.assertEqual(len(self.openers[0].requests), before)

    def test_bootstrap_failure_is_redacted(self) -> None:
        class FailingOpener:
            def open(self, _request: object, timeout: float | None = None) -> object:
                raise RuntimeError("sensitive-bootstrap-failure")

        with self.assertRaisesRegex(RuntimeError, "bootstrap_request_failed") as caught:
            BrowserAuthenticatedDshSession(
                bootstrap_url="http://127.0.0.1:43123/?token=synthetic_secret",
                websocket_dialer=lambda _url, _headers: FakeSocket(),
                opener_factory=lambda _jar: FailingOpener(),
            )
        self.assertNotIn("sensitive-bootstrap-failure", str(caught.exception))
        self.assertNotIn("synthetic_secret", str(caught.exception))

    def test_missing_cookie_fails_closed(self) -> None:
        class CookielessOpener:
            def open(self, _request: object, timeout: float | None = None) -> FakeResponse:
                return FakeResponse(b"no-cookie")

        with self.assertRaisesRegex(RuntimeError, "cookie_not_issued"):
            BrowserAuthenticatedDshSession(
                bootstrap_url="http://127.0.0.1:43123/?token=synthetic_secret",
                websocket_dialer=lambda _url, _headers: FakeSocket(),
                opener_factory=lambda _jar: CookielessOpener(),
            )

    def test_http_failure_is_redacted(self) -> None:
        class FailsAfterBootstrap(FakeOpener):
            def open(self, request: object, timeout: float | None = None) -> FakeResponse:
                if isinstance(request, str):
                    return super().open(request, timeout)
                raise RuntimeError("sensitive-http-failure")

        openers: list[FailsAfterBootstrap] = []

        def factory(jar: http.cookiejar.CookieJar) -> FailsAfterBootstrap:
            opener = FailsAfterBootstrap(jar)
            openers.append(opener)
            return opener

        session = BrowserAuthenticatedDshSession(
            bootstrap_url="http://127.0.0.1:43123/?token=synthetic_secret",
            websocket_dialer=lambda _url, _headers: FakeSocket(),
            opener_factory=factory,
        )
        self.addCleanup(session.close)
        with self.assertRaisesRegex(RuntimeError, "http_request_failed") as caught:
            session.post_json("/api/session/list", {})
        self.assertNotIn("sensitive-http-failure", str(caught.exception))

    def test_websocket_failure_is_redacted(self) -> None:
        def failing_dialer(_url: str, _headers: Mapping[str, str]) -> FakeSocket:
            raise RuntimeError("sensitive-websocket-failure")

        session = BrowserAuthenticatedDshSession(
            bootstrap_url="http://127.0.0.1:43123/?token=synthetic_secret",
            websocket_dialer=failing_dialer,
            opener_factory=lambda jar: FakeOpener(jar),
        )
        self.addCleanup(session.close)
        with self.assertRaisesRegex(RuntimeError, "websocket_connect_failed") as caught:
            session.connect_json("/api/remote.mux")
        self.assertNotIn("sensitive-websocket-failure", str(caught.exception))

    def test_full_stack_is_lazy_and_content_free(self) -> None:
        provider_calls: list[str] = []

        def provider() -> str:
            provider_calls.append("called")
            return "http://127.0.0.1:43123/?token=synthetic_secret"

        client = make_browser_readonly_client(
            bootstrap_url_provider=provider,
            websocket_dialer=lambda url, headers: self._record_dial(url, headers),
            opener_factory=lambda jar: self._record_opener(jar),
        )
        self.assertEqual(provider_calls, [])
        self.assertNotIn("synthetic_secret", repr(client))
        with client:
            roots = client.list_root_sessions()
            selected = client.select_latest_nonblank_root(roots)
            pin = create_session_identity_pin(selected.opaque_id)
            verified = verify_session_identity(pin, selected.opaque_id)
            summary = client.follow_verified_once(verified)
        self.assertEqual(provider_calls, ["called"])
        self.assertEqual(summary.event_type_counts, (("turn/end", 1),))
        self.assertNotIn("data", repr(summary))
        self.assertTrue(self.dials[-1][2].closed)

    def _record_opener(self, jar: http.cookiejar.CookieJar) -> FakeOpener:
        opener = FakeOpener(jar)
        self.openers.append(opener)
        return opener

    def _record_dial(self, url: str, headers: Mapping[str, str]) -> FakeSocket:
        socket = FakeSocket()
        self.dials.append((url, headers, socket))
        return socket


if __name__ == "__main__":
    unittest.main()
