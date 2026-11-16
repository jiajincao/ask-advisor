from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .dsh_browser_session import WebSocketDialer
from .dsh_transport import WebSocketJsonConnection


class CredentialTextSource(Protocol):
    def readline(self, limit: int = -1) -> str: ...

    def close(self) -> None: ...


class RawWebSocket(Protocol):
    def send(self, data: str) -> object: ...

    def recv(self) -> str | bytes: ...

    def close(self) -> object: ...


RawWebSocketDialer = Callable[[str, Mapping[str, str]], RawWebSocket]


class OneShotBootstrapUrlProvider:
    """Read one credential-bearing bootstrap URL and immediately close input."""

    def __init__(self, *, source: CredentialTextSource, max_chars: int = 4096) -> None:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
            raise ValueError("DSH bootstrap provider rejected: invalid_size_limit")
        self._source: CredentialTextSource | None = source
        self._max_chars = max_chars
        self._used = False

    def __call__(self) -> str:
        if self._used:
            raise RuntimeError("DSH bootstrap provider rejected: already_consumed")
        self._used = True
        source = self._source
        self._source = None
        if source is None:
            raise RuntimeError("DSH bootstrap provider rejected: source_unavailable")
        try:
            value = source.readline(self._max_chars + 1)
        except BaseException as exc:
            raise RuntimeError("DSH bootstrap provider failed: source_read_failed") from exc
        finally:
            try:
                source.close()
            except BaseException:
                pass
        if not isinstance(value, str):
            raise RuntimeError("DSH bootstrap provider rejected: invalid_source_value")
        value = value.strip()
        if not value or len(value) > self._max_chars:
            raise RuntimeError("DSH bootstrap provider rejected: invalid_source_value")
        return value

    def close(self) -> None:
        source = self._source
        self._source = None
        self._used = True
        if source is not None:
            try:
                source.close()
            except BaseException:
                pass

    def __repr__(self) -> str:
        state = "consumed" if self._used else "pending"
        return f"{type(self).__name__}(state={state!r}, value='<redacted>')"


class JsonWebSocketConnection:
    """Content-free JSON framing wrapper over an injected raw WebSocket."""

    def __init__(self, raw_socket: RawWebSocket) -> None:
        self._raw_socket: RawWebSocket | None = raw_socket

    def _open_socket(self) -> RawWebSocket:
        if self._raw_socket is None:
            raise RuntimeError("DSH WebSocket transport rejected: connection_closed")
        return self._raw_socket

    def send_json(self, frame: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(frame, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("DSH WebSocket transport rejected: invalid_json_frame") from exc
        try:
            self._open_socket().send(encoded)
        except BaseException as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("DSH "):
                raise
            raise RuntimeError("DSH WebSocket transport failed: send_failed") from exc

    def receive_json(self) -> Mapping[str, Any]:
        try:
            raw = self._open_socket().recv()
        except BaseException as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("DSH "):
                raise
            raise RuntimeError("DSH WebSocket transport failed: receive_failed") from exc
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("DSH WebSocket transport failed: invalid_text_frame") from exc
        if not isinstance(raw, str):
            raise RuntimeError("DSH WebSocket transport failed: invalid_text_frame")
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("DSH WebSocket transport failed: invalid_json_frame") from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError("DSH WebSocket transport failed: invalid_json_frame")
        return decoded

    def close(self) -> None:
        raw_socket = self._raw_socket
        self._raw_socket = None
        if raw_socket is not None:
            try:
                raw_socket.close()
            except BaseException:
                pass


def make_json_websocket_dialer(raw_dialer: RawWebSocketDialer) -> WebSocketDialer:
    """Adapt a credential-owning raw dialer to the reviewed JSON interface."""

    if not callable(raw_dialer):
        raise TypeError("raw_dialer must be callable")

    def dial(url: str, headers: Mapping[str, str]) -> WebSocketJsonConnection:
        try:
            raw_socket = raw_dialer(url, headers)
        except BaseException as exc:
            raise RuntimeError("DSH WebSocket transport failed: dial_failed") from exc
        return JsonWebSocketConnection(raw_socket)

    return dial
