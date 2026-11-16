from __future__ import annotations

import http.cookiejar
import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .dsh_transport import HttpJsonResponse, WebSocketJsonConnection
from .dsh_secure_transport import SecureReadonlyDshClient


class HttpOpener(Protocol):
    def open(self, request: object, timeout: float | None = None) -> object: ...


OpenerFactory = Callable[[http.cookiejar.CookieJar], HttpOpener]
WebSocketDialer = Callable[[str, Mapping[str, str]], WebSocketJsonConnection]
BootstrapUrlProvider = Callable[[], str]


def _default_opener_factory(jar: http.cookiejar.CookieJar) -> HttpOpener:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


class BrowserAuthenticatedDshSession:
    """Credential-owning loopback DSH browser session."""

    _ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def __init__(
        self,
        *,
        bootstrap_url: str,
        websocket_dialer: WebSocketDialer,
        opener_factory: OpenerFactory = _default_opener_factory,
        timeout: float = 10.0,
    ) -> None:
        if not callable(websocket_dialer) or not callable(opener_factory):
            raise TypeError("DSH browser session requires callable transports")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("DSH browser session rejected: invalid_timeout")
        parsed = self._validated_loopback_url(bootstrap_url)
        self._base_url: str | None = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        )
        self._timeout = float(timeout)
        self._jar = http.cookiejar.CookieJar()
        self._opener: HttpOpener | None = opener_factory(self._jar)
        self._websocket_dialer: WebSocketDialer | None = websocket_dialer
        self._connections: list[WebSocketJsonConnection] = []
        self._closed = False
        try:
            response = self._opener.open(bootstrap_url, timeout=self._timeout)
            self._consume_and_close(response)
            if not tuple(self._jar):
                raise RuntimeError("DSH browser authentication failed: cookie_not_issued")
        except BaseException as exc:
            self.close()
            if isinstance(exc, RuntimeError) and str(exc).startswith("DSH "):
                raise
            raise RuntimeError(
                "DSH browser authentication failed: bootstrap_request_failed"
            ) from exc

    @classmethod
    def _validated_loopback_url(cls, value: Any) -> urllib.parse.SplitResult:
        if not isinstance(value, str) or not value:
            raise ValueError("DSH browser session rejected: invalid_bootstrap_url")
        parsed = urllib.parse.urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("DSH browser session rejected: non_loopback_authority") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in cls._ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port is None
        ):
            raise ValueError("DSH browser session rejected: non_loopback_authority")
        return parsed

    @staticmethod
    def _consume_and_close(response: object) -> bytes:
        try:
            read = getattr(response, "read", None)
            if not callable(read):
                raise RuntimeError("DSH browser transport failed: invalid_http_response")
            data = read()
            if not isinstance(data, bytes):
                raise RuntimeError("DSH browser transport failed: invalid_http_body")
            return data
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _validated_api_path(path: Any) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/api/")
            or "?" in path
            or "#" in path
            or ".." in path
        ):
            raise ValueError("DSH browser session rejected: invalid_relative_api_path")
        return path

    def _require_open(self) -> str:
        if self._closed or self._base_url is None:
            raise RuntimeError("DSH browser session rejected: session_closed")
        return self._base_url

    def post_json(self, path: str, body: dict[str, Any]) -> HttpJsonResponse:
        base_url = self._require_open()
        relative_path = self._validated_api_path(path)
        try:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("DSH browser session rejected: invalid_json_body") from exc
        request = urllib.request.Request(
            base_url + relative_path,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            if self._opener is None:
                raise RuntimeError("DSH browser session rejected: session_closed")
            response = self._opener.open(request, timeout=self._timeout)
            status = getattr(response, "status", None)
            raw = self._consume_and_close(response)
            decoded = json.loads(raw)
        except BaseException as exc:
            if isinstance(exc, (ValueError, RuntimeError)) and str(exc).startswith("DSH "):
                raise
            raise RuntimeError("DSH browser transport failed: http_request_failed") from exc
        if not isinstance(status, int) or not isinstance(decoded, Mapping):
            raise RuntimeError("DSH browser transport failed: invalid_http_response")
        return HttpJsonResponse(status=status, body=decoded)

    def connect_json(self, path: str) -> WebSocketJsonConnection:
        base_url = self._require_open()
        relative_path = self._validated_api_path(path)
        parsed = urllib.parse.urlsplit(base_url)
        ws_url = urllib.parse.urlunsplit(
            ("wss" if parsed.scheme == "https" else "ws", parsed.netloc, relative_path, "", "")
        )
        cookie_request = urllib.request.Request(base_url + relative_path)
        self._jar.add_cookie_header(cookie_request)
        cookie = cookie_request.get_header("Cookie")
        if not isinstance(cookie, str) or not cookie:
            raise RuntimeError("DSH browser authentication failed: cookie_unavailable")
        try:
            if self._websocket_dialer is None:
                raise RuntimeError("DSH browser session rejected: session_closed")
            connection = self._websocket_dialer(ws_url, {"Cookie": cookie})
        except BaseException as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("DSH "):
                raise
            raise RuntimeError("DSH browser transport failed: websocket_connect_failed") from exc
        self._connections.append(connection)
        return connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for connection in reversed(self._connections):
            try:
                connection.close()
            except BaseException:
                pass
        self._connections.clear()
        self._jar.clear()
        self._base_url = None
        self._opener = None
        self._websocket_dialer = None

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"{type(self).__name__}(state={state!r}, authority='<redacted>')"


def make_browser_readonly_client(
    *,
    bootstrap_url_provider: BootstrapUrlProvider,
    websocket_dialer: WebSocketDialer,
    opener_factory: OpenerFactory = _default_opener_factory,
    timeout: float = 10.0,
) -> SecureReadonlyDshClient:
    """Compose the full read-only stack without retaining a bootstrap URL."""

    if not callable(bootstrap_url_provider):
        raise TypeError("bootstrap_url_provider must be callable")

    def create_session() -> BrowserAuthenticatedDshSession:
        bootstrap_url = bootstrap_url_provider()
        return BrowserAuthenticatedDshSession(
            bootstrap_url=bootstrap_url,
            websocket_dialer=websocket_dialer,
            opener_factory=opener_factory,
            timeout=timeout,
        )

    return SecureReadonlyDshClient(session_factory=create_session)
