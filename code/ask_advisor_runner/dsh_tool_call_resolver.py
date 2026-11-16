from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .bridge import canonical_request_hash
from .dsh_transport import IdFactory, WebSocketConnect
from .redaction import require_clean


class OneShotToolCallResolver:
    """Read one bounded snapshot and verify the exact pending ask_advisor call."""

    _MUX_PATH = "/api/remote.mux"
    _FOLLOW_ENDPOINT = "session/follow"

    def __init__(
        self,
        *,
        websocket_connect: WebSocketConnect,
        session_id: str,
        expected_arguments: Mapping[str, Any],
        id_factory: IdFactory | None = None,
        max_messages: int = 50,
    ) -> None:
        if not callable(websocket_connect):
            raise TypeError("websocket_connect must be callable")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("tool call resolver rejected: invalid_session_id")
        if not isinstance(expected_arguments, Mapping):
            raise ValueError("tool call resolver rejected: invalid_expected_arguments")
        if not isinstance(max_messages, int) or isinstance(max_messages, bool) or not 1 <= max_messages <= 50:
            raise ValueError("tool call resolver rejected: invalid_message_limit")
        encoded = json.dumps(expected_arguments, ensure_ascii=False, sort_keys=True)
        require_clean(encoded)
        self._connect = websocket_connect
        self._session_id = session_id
        self._expected_arguments = dict(expected_arguments)
        self._expected_hash = canonical_request_hash(self._expected_arguments)
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._max_messages = max_messages
        self._used = False

    def resolve_once(self, call_id: str) -> str:
        if self._used:
            raise RuntimeError("tool call resolver rejected: resolver_replayed")
        self._used = True
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool call resolver rejected: invalid_call_id")
        stream_id = self._id_factory()
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError("tool call resolver rejected: invalid_ephemeral_id")
        connection = self._connect(self._MUX_PATH)
        try:
            connection.send_json(
                {
                    "type": "open",
                    "streamId": stream_id,
                    "endpoint": self._FOLLOW_ENDPOINT,
                    "payload": {
                        "args": {
                            "request": {
                                "address": {
                                    "kind": "session",
                                    "sessionId": self._session_id,
                                },
                                "maxMessages": self._max_messages,
                            }
                        }
                    },
                }
            )
            frame = connection.receive_json()
            if (
                not isinstance(frame, Mapping)
                or frame.get("type") != "item"
                or frame.get("streamId") != stream_id
                or not isinstance(frame.get("value"), Mapping)
            ):
                raise RuntimeError("tool call resolver rejected: unexpected_stream_frame")
            snapshot = frame["value"]
            if snapshot.get("type") != "snapshot" or not isinstance(snapshot.get("records"), list):
                raise RuntimeError("tool call resolver rejected: invalid_snapshot")
            matches: list[Mapping[str, Any]] = []
            for record in snapshot["records"]:
                if not isinstance(record, Mapping) or not isinstance(record.get("event"), Mapping):
                    continue
                event = record["event"]
                if event.get("type") != "tool/call" or not isinstance(event.get("data"), Mapping):
                    continue
                data = event["data"]
                if data.get("callId") == call_id:
                    matches.append(data)
            if len(matches) != 1:
                raise RuntimeError("tool call resolver rejected: call_not_unique")
            data = matches[0]
            if data.get("name") != "ask_advisor" or not isinstance(data.get("arguments"), str):
                raise RuntimeError("tool call resolver rejected: tool_not_allowed")
            try:
                arguments = json.loads(data["arguments"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("tool call resolver rejected: invalid_arguments") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError("tool call resolver rejected: invalid_arguments")
            require_clean(json.dumps(arguments, ensure_ascii=False, sort_keys=True))
            if arguments != self._expected_arguments:
                raise RuntimeError("tool call resolver rejected: arguments_mismatch")
            return self._expected_hash
        finally:
            try:
                connection.send_json({"type": "cancel", "streamId": stream_id})
            finally:
                connection.close()
                self._session_id = ""

