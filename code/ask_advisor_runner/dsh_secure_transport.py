from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any, Protocol

from .dsh_readonly import FollowSnapshotSummary, ReadonlyDshAdapter, RootSessionCandidate
from .dsh_transport import (
    HttpJsonResponse,
    ReadonlyDshTransportExecutor,
    WebSocketJsonConnection,
)
from .dsh_session_identity import VerifiedSessionIdentity


class AuthenticatedDshSession(Protocol):
    """Credential-owning session implemented outside the experiment runner.

    Implementations may own an authority, browser Cookie, URL, port, and
    sockets. None of those values cross this interface: callers can only use
    fixed relative paths and JSON values, then close the whole session.
    """

    def post_json(self, path: str, body: dict[str, Any]) -> HttpJsonResponse: ...

    def connect_json(self, path: str) -> WebSocketJsonConnection: ...

    def close(self) -> None: ...


SessionFactory = Callable[[], AuthenticatedDshSession]


class SecureReadonlyDshClient:
    """Scoped composition of authentication, transport executor, and adapter.

    Construction does not authenticate or open a socket. Entering the context
    obtains one opaque authenticated session; exiting always closes it. The
    object deliberately exposes only the two reviewed read-only operations.
    """

    def __init__(self, *, session_factory: SessionFactory) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self._session_factory = session_factory
        self._session: AuthenticatedDshSession | None = None
        self._adapter: ReadonlyDshAdapter | None = None

    def __enter__(self) -> SecureReadonlyDshClient:
        if self._session is not None:
            raise RuntimeError("DSH secure transport rejected: client_already_open")
        session = self._session_factory()
        if session is None:
            raise RuntimeError("DSH secure transport rejected: session_unavailable")
        try:
            executor = ReadonlyDshTransportExecutor(
                http_post=session.post_json,
                websocket_connect=session.connect_json,
            )
            adapter = ReadonlyDshAdapter(
                unary_call=executor.unary_call,
                stream_open=executor.stream_open,
            )
        except BaseException:
            session.close()
            raise
        self._session = session
        self._adapter = adapter
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        session = self._session
        self._adapter = None
        self._session = None
        if session is not None:
            session.close()

    def _open_adapter(self) -> ReadonlyDshAdapter:
        if self._adapter is None:
            raise RuntimeError("DSH secure transport rejected: client_not_open")
        return self._adapter

    def list_root_sessions(self) -> tuple[RootSessionCandidate, ...]:
        return self._open_adapter().list_root_sessions()

    def select_latest_nonblank_root(
        self,
        roots: tuple[RootSessionCandidate, ...],
    ) -> RootSessionCandidate:
        return self._open_adapter().select_latest_nonblank_root(roots)

    def follow_once(
        self,
        _root: RootSessionCandidate,
    ) -> FollowSnapshotSummary:
        raise RuntimeError("DSH secure transport rejected: verified_identity_required")

    def follow_verified_once(
        self,
        verified: VerifiedSessionIdentity,
    ) -> FollowSnapshotSummary:
        if not isinstance(verified, VerifiedSessionIdentity):
            raise TypeError("verified session identity is required")
        opaque_id = verified.take_opaque_id()
        adapter = self._open_adapter()
        roots = adapter.list_root_sessions()
        matches = [root for root in roots if root.opaque_id == opaque_id]
        opaque_id = ""
        if len(matches) != 1:
            raise RuntimeError("DSH secure transport rejected: pinned_root_unavailable")
        return adapter.follow_once(matches[0], max_messages=1)

    @staticmethod
    def _write_disabled() -> None:
        raise RuntimeError("DSH write capability is intentionally disabled")

    create_session = _write_disabled
    send_prompt = _write_disabled
    answer_approval = _write_disabled
    call_advisor = _write_disabled
