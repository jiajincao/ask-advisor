from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class HttpJsonResponse:
    """Credential-free result returned by an injected authenticated transport."""

    status: int
    body: Mapping[str, Any]


class WebSocketJsonConnection(Protocol):
    """Small interface implemented by the externally authenticated transport."""

    def send_json(self, frame: Mapping[str, Any]) -> None: ...

    def receive_json(self) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


HttpPost = Callable[[str, Mapping[str, Any]], HttpJsonResponse]
WebSocketConnect = Callable[[str], WebSocketJsonConnection]
IdFactory = Callable[[], str]


class _OneSnapshotIterator(Iterator[Mapping[str, Any]]):
    """Return one Remote Mux item and always cancel/close before exposing it."""

    def __init__(
        self,
        connection: WebSocketJsonConnection,
        *,
        stream_id: str,
    ) -> None:
        self._connection = connection
        self._stream_id = stream_id
        self._finished = False

    def __iter__(self) -> _OneSnapshotIterator:
        return self

    def __next__(self) -> Mapping[str, Any]:
        if self._finished:
            raise StopIteration
        self._finished = True
        try:
            frame = self._connection.receive_json()
            if not isinstance(frame, Mapping):
                raise ValueError("DSH read-only transport rejected: invalid_stream_frame")
            if (
                frame.get("type") != "item"
                or frame.get("streamId") != self._stream_id
            ):
                raise ValueError("DSH read-only transport rejected: unexpected_stream_frame")
            item = frame.get("value")
            if not isinstance(item, Mapping):
                raise ValueError("DSH read-only transport rejected: invalid_stream_item")
            return item
        finally:
            try:
                self._connection.send_json(
                    {"type": "cancel", "streamId": self._stream_id}
                )
            finally:
                self._connection.close()


class ReadonlyDshTransportExecutor:
    """Execute only the reviewed DSH read-only HTTP and WebSocket protocols.

    Authentication, authority, URL selection, Cookie handling, and socket
    creation belong to the two injected transport functions. This executor
    accepts none of those values, retains no response history, uses only fixed
    relative paths, and never includes response bodies in its exceptions.
    """

    _HTTP_PATH = "/api/session/list"
    _MUX_PATH = "/api/remote.mux"
    _LIST_ENDPOINT = "session/list"
    _FOLLOW_ENDPOINT = "session/follow"

    def __init__(
        self,
        *,
        http_post: HttpPost,
        websocket_connect: WebSocketConnect,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._http_post = http_post
        self._websocket_connect = websocket_connect
        self._id_factory = id_factory or (lambda: uuid4().hex)

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value:
            raise ValueError("DSH read-only transport rejected: invalid_ephemeral_id")
        return value

    def unary_call(self, endpoint: str, args: dict[str, Any]) -> Mapping[str, Any]:
        if endpoint != self._LIST_ENDPOINT or args != {"_request": {}}:
            raise RuntimeError("DSH read-only transport rejected: unary_call_not_allowed")
        rpc_id = self._new_id()
        response = self._http_post(
            self._HTTP_PATH,
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": self._LIST_ENDPOINT,
                "payload": {"args": {"_request": {}}},
            },
        )
        if response.status != 200:
            raise RuntimeError("DSH read-only transport failed: http_status_not_ok")
        body = response.body
        if not isinstance(body, Mapping) or body.get("rpcId") != rpc_id:
            raise RuntimeError("DSH read-only transport failed: rpc_id_mismatch")
        result = body.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise RuntimeError("DSH read-only transport failed: rpc_result_not_ok")
        value = result.get("value")
        if not isinstance(value, Mapping):
            raise RuntimeError("DSH read-only transport failed: invalid_rpc_value")
        return value

    def stream_open(
        self,
        endpoint: str,
        args: dict[str, Any],
    ) -> Iterator[Mapping[str, Any]]:
        request = args.get("request") if isinstance(args, dict) else None
        if (
            endpoint != self._FOLLOW_ENDPOINT
            or not isinstance(request, dict)
            or set(args) != {"request"}
            or set(request) != {"address", "maxMessages"}
            or request.get("maxMessages") != 1
            or isinstance(request.get("maxMessages"), bool)
        ):
            raise RuntimeError("DSH read-only transport rejected: stream_open_not_allowed")
        address = request.get("address")
        if (
            not isinstance(address, dict)
            or set(address) != {"kind", "sessionId"}
            or address.get("kind") != "session"
            or not isinstance(address.get("sessionId"), str)
            or not address["sessionId"]
        ):
            raise RuntimeError("DSH read-only transport rejected: invalid_session_address")

        stream_id = self._new_id()
        connection = self._websocket_connect(self._MUX_PATH)
        try:
            connection.send_json(
                {
                    "type": "open",
                    "streamId": stream_id,
                    "endpoint": self._FOLLOW_ENDPOINT,
                    "payload": {"args": args},
                }
            )
        except BaseException:
            connection.close()
            raise
        return _OneSnapshotIterator(connection, stream_id=stream_id)

    @staticmethod
    def _write_disabled() -> None:
        raise RuntimeError("DSH write capability is intentionally disabled")

    create_session = _write_disabled
    send_prompt = _write_disabled
    answer_approval = _write_disabled
    call_advisor = _write_disabled
