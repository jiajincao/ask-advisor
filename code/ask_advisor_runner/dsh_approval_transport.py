from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from .dsh_transport import HttpJsonResponse, WebSocketConnect, WebSocketJsonConnection


ApprovalWireDecision = Literal["allowed-once", "rejected"]
IdFactory = Callable[[], str]
HttpPost = Callable[[str, Mapping[str, Any]], HttpJsonResponse]


@dataclass(frozen=True)
class ApprovalPresentation:
    tool_name: str
    call_id: str
    has_reason: bool


class OneShotApprovalChannel:
    """Receive and settle one exact DSH Remote Event approval waterfall."""

    _MUX_PATH = "/api/remote.mux"
    _EVENTS_ENDPOINT = "$events"
    _RESULT_ENDPOINT = "$events/result"

    def __init__(
        self,
        *,
        http_post: HttpPost,
        websocket_connect: WebSocketConnect,
        expected_agent_id: str,
        id_factory: IdFactory | None = None,
        max_frames: int = 200,
    ) -> None:
        if not callable(http_post) or not callable(websocket_connect):
            raise TypeError("approval transports must be callable")
        if not isinstance(expected_agent_id, str) or not expected_agent_id:
            raise ValueError("DSH approval transport rejected: invalid_agent_id")
        if not isinstance(max_frames, int) or isinstance(max_frames, bool) or not 1 <= max_frames <= 1000:
            raise ValueError("DSH approval transport rejected: invalid_frame_limit")
        self._http_post = http_post
        self._websocket_connect = websocket_connect
        self._expected_agent_id = expected_agent_id
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._connection: WebSocketJsonConnection | None = None
        self._stream_id: str | None = None
        self._client_id: str | None = None
        self._event_id: str | None = None
        self._answered = False
        self._max_frames = max_frames

    def __repr__(self) -> str:
        state = "answered" if self._answered else "open" if self._connection else "new"
        return f"{type(self).__name__}(state={state!r}, ids='<redacted>')"

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value:
            raise ValueError("DSH approval transport rejected: invalid_ephemeral_id")
        return value

    def _receive_item(self) -> Mapping[str, Any]:
        connection = self._connection
        stream_id = self._stream_id
        if connection is None or stream_id is None:
            raise RuntimeError("DSH approval transport rejected: channel_not_open")
        try:
            frame = connection.receive_json()
        except BaseException as exc:
            raise RuntimeError("DSH approval transport failed: receive_failed") from exc
        if (
            not isinstance(frame, Mapping)
            or set(frame) != {"type", "streamId", "value"}
            or frame.get("type") != "item"
            or frame.get("streamId") != stream_id
            or not isinstance(frame.get("value"), Mapping)
        ):
            raise RuntimeError("DSH approval transport rejected: unexpected_stream_frame")
        return frame["value"]

    def open(self) -> None:
        if self._connection is not None or self._answered:
            raise RuntimeError("DSH approval transport rejected: channel_reuse")
        stream_id = self._new_id()
        connection = self._websocket_connect(self._MUX_PATH)
        self._stream_id = stream_id
        self._connection = connection
        try:
            connection.send_json(
                {
                    "type": "open",
                    "streamId": stream_id,
                    "endpoint": self._EVENTS_ENDPOINT,
                    "payload": {"args": {}},
                }
            )
            ready = self._receive_item()
            if (
                set(ready) != {"type", "clientId", "host"}
                or ready.get("type") != "ready"
                or not isinstance(ready.get("clientId"), str)
                or not ready["clientId"]
                or not isinstance(ready.get("host"), Mapping)
                or set(ready["host"]) != {"home"}
                or not isinstance(ready["host"].get("home"), str)
            ):
                raise RuntimeError("DSH approval transport rejected: invalid_ready_frame")
            self._client_id = ready["clientId"]
        except BaseException:
            self.close()
            raise

    def receive_once(self) -> ApprovalPresentation:
        if self._client_id is None or self._event_id is not None:
            raise RuntimeError("DSH approval transport rejected: invalid_receive_state")
        value: Mapping[str, Any] | None = None
        for _ in range(self._max_frames):
            candidate = self._receive_item()
            if candidate.get("type") in {"emit", "cancel"}:
                continue
            if (
                candidate.get("type") == "waterfall"
                and candidate.get("event") == "approval/request"
                and candidate.get("agentId") == self._expected_agent_id
            ):
                value = candidate
                break
        if (
            value is None
            or set(value) != {"type", "event", "eventId", "agentId", "request"}
            or not isinstance(value.get("eventId"), str)
            or not value["eventId"]
            or not isinstance(value.get("request"), Mapping)
        ):
            raise RuntimeError("DSH approval transport rejected: approval_not_found")
        request = value["request"]
        if not set(request).issubset({"toolName", "callId", "reason"}):
            raise RuntimeError("DSH approval transport rejected: invalid_approval_request")
        tool_name = request.get("toolName")
        call_id = request.get("callId")
        reason = request.get("reason")
        if (
            tool_name != "ask_advisor"
            or not isinstance(call_id, str)
            or not call_id
            or (reason is not None and not isinstance(reason, str))
        ):
            raise RuntimeError("DSH approval transport rejected: approval_not_allowed")
        self._event_id = value["eventId"]
        return ApprovalPresentation(
            tool_name="ask_advisor",
            call_id=call_id,
            has_reason=reason is not None,
        )

    def answer_once(self, outcome: ApprovalWireDecision) -> dict[str, Any]:
        if outcome not in ("allowed-once", "rejected"):
            raise ValueError("DSH approval transport rejected: invalid_outcome")
        if self._answered:
            raise RuntimeError("DSH approval transport rejected: answer_replayed")
        if self._client_id is None or self._event_id is None:
            raise RuntimeError("DSH approval transport rejected: no_pending_approval")
        self._answered = True
        rpc_id = self._new_id()
        try:
            response = self._http_post(
                f"/api/{self._RESULT_ENDPOINT}",
                {
                    "type": "client-request",
                    "rpcId": rpc_id,
                    "method": self._RESULT_ENDPOINT,
                    "payload": {
                        "args": {
                            "clientId": self._client_id,
                            "eventId": self._event_id,
                            "outcome": {"kind": "result", "value": outcome},
                        }
                    },
                },
            )
            if response.status != 200:
                raise RuntimeError("DSH approval transport failed: http_status_not_ok")
            body = response.body
            if not isinstance(body, Mapping) or body.get("rpcId") != rpc_id:
                raise RuntimeError("DSH approval transport failed: rpc_id_mismatch")
            result = body.get("result")
            if not isinstance(result, Mapping) or result.get("ok") is not True:
                raise RuntimeError("DSH approval transport failed: rpc_result_not_ok")
            return {"answered": True, "outcome": outcome}
        finally:
            self.close()

    def close(self) -> None:
        connection = self._connection
        stream_id = self._stream_id
        self._connection = None
        self._stream_id = None
        self._client_id = None
        self._event_id = None
        self._expected_agent_id = ""
        if connection is not None:
            try:
                if stream_id is not None:
                    connection.send_json({"type": "cancel", "streamId": stream_id})
            finally:
                connection.close()

