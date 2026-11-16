from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .dsh_one_shot_flow import AdvisorResult
from .dsh_transport import IdFactory, WebSocketConnect
from .redaction import require_clean


class OneShotAdvisorResultReader:
    """Read one correlated ask_advisor tool result from a bounded follow stream."""

    def __init__(
        self,
        *,
        websocket_connect: WebSocketConnect,
        session_id: str,
        id_factory: IdFactory | None = None,
        max_messages: int = 50,
        max_frames: int = 200,
    ) -> None:
        if not callable(websocket_connect):
            raise TypeError("websocket_connect must be callable")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("advisor result reader rejected: invalid_session_id")
        if not 1 <= max_messages <= 50 or not 1 <= max_frames <= 1000:
            raise ValueError("advisor result reader rejected: invalid_limit")
        self._connect = websocket_connect
        self._session_id = session_id
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._max_messages = max_messages
        self._max_frames = max_frames
        self._used = False

    @staticmethod
    def _records(value: Mapping[str, Any]) -> list[Any]:
        if value.get("type") == "snapshot" and isinstance(value.get("records"), list):
            return value["records"]
        if isinstance(value.get("event"), Mapping):
            return [value]
        return []

    @staticmethod
    def _parse_record(record: Any, call_id: str) -> AdvisorResult | None:
        if not isinstance(record, Mapping) or not isinstance(record.get("event"), Mapping):
            return None
        event = record["event"]
        if event.get("type") != "tool/result" or not isinstance(event.get("data"), Mapping):
            return None
        message = event["data"].get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("source"), Mapping):
            return None
        if message["source"].get("callId") != call_id:
            return None
        content = message.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
            raise RuntimeError("advisor result reader rejected: invalid_tool_result")
        block = content[0]
        inner = block.get("content")
        if (
            block.get("type") != "tool-result"
            or block.get("toolCallId") != call_id
            or not isinstance(inner, list)
            or len(inner) != 1
            or not isinstance(inner[0], Mapping)
            or inner[0].get("type") != "text"
            or not isinstance(inner[0].get("text"), str)
        ):
            raise RuntimeError("advisor result reader rejected: invalid_tool_result")
        try:
            value = json.loads(inner[0]["text"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("advisor result reader rejected: invalid_result_json") from exc
        if not isinstance(value, dict) or set(value) != {"status", "text", "request_id", "truncated"}:
            raise RuntimeError("advisor result reader rejected: invalid_result_schema")
        if (
            not isinstance(value["status"], str)
            or not isinstance(value["text"], str)
            or not isinstance(value["request_id"], str)
            or not isinstance(value["truncated"], bool)
        ):
            raise RuntimeError("advisor result reader rejected: invalid_result_schema")
        require_clean(value["text"])
        status = "success" if value["status"] == "ok" else value["status"]
        return AdvisorResult(status, value["text"], value["truncated"])

    def __call__(self, call_id: str) -> AdvisorResult:
        if self._used:
            raise RuntimeError("advisor result reader rejected: reader_replayed")
        self._used = True
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("advisor result reader rejected: invalid_call_id")
        stream_id = self._id_factory()
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError("advisor result reader rejected: invalid_ephemeral_id")
        connection = self._connect("/api/remote.mux")
        matches: list[AdvisorResult] = []
        try:
            connection.send_json(
                {
                    "type": "open",
                    "streamId": stream_id,
                    "endpoint": "session/follow",
                    "payload": {
                        "args": {
                            "request": {
                                "address": {"kind": "session", "sessionId": self._session_id},
                                "maxMessages": self._max_messages,
                            }
                        }
                    },
                }
            )
            for _ in range(self._max_frames):
                frame = connection.receive_json()
                if not isinstance(frame, Mapping) or frame.get("streamId") != stream_id:
                    raise RuntimeError("advisor result reader rejected: unexpected_stream_frame")
                if frame.get("type") == "error" or frame.get("type") == "end":
                    raise RuntimeError("advisor result reader failed: stream_ended")
                if frame.get("type") != "item" or not isinstance(frame.get("value"), Mapping):
                    raise RuntimeError("advisor result reader rejected: unexpected_stream_frame")
                for record in self._records(frame["value"]):
                    result = self._parse_record(record, call_id)
                    if result is not None:
                        matches.append(result)
                if len(matches) > 1:
                    raise RuntimeError("advisor result reader rejected: duplicate_result")
                if matches:
                    return matches[0]
            raise RuntimeError("advisor result reader failed: frame_limit_exceeded")
        finally:
            try:
                connection.send_json({"type": "cancel", "streamId": stream_id})
            finally:
                connection.close()
                self._session_id = ""

