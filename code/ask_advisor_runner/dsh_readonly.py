from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


UnaryCall = Callable[[str, dict[str, Any]], Mapping[str, Any]]
StreamOpen = Callable[[str, dict[str, Any]], Iterable[Mapping[str, Any]]]


@dataclass(frozen=True)
class RootSessionCandidate:
    """Non-content metadata for one root Session candidate."""

    opaque_id: str
    running: bool
    blank: bool
    updated_at: int
    last_prompt_at: int | None


@dataclass(frozen=True)
class FollowSnapshotSummary:
    """Content-free summary of the first session/follow snapshot."""

    cursor_present: bool
    record_count: int
    has_more: bool
    projection_present: bool
    event_type_counts: tuple[tuple[str, int], ...]


class ReadonlyDshAdapter:
    """Whitelist-only DSH adapter with all mutating capabilities absent.

    Transport and browser authentication are injected by the caller. This
    class never accepts a Token, Cookie, URL, port, or credential path and it
    never returns event payloads. It only constructs the two reviewed Remote
    calls and projects their responses into content-free metadata.
    """

    _LIST_ENDPOINT = "session/list"
    _FOLLOW_ENDPOINT = "session/follow"

    def __init__(self, *, unary_call: UnaryCall, stream_open: StreamOpen) -> None:
        self._unary_call = unary_call
        self._stream_open = stream_open

    @staticmethod
    def _plain_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"DSH read-only response rejected: invalid_{label}")
        return value

    @classmethod
    def _root_candidate(cls, item: Any) -> RootSessionCandidate | None:
        record = cls._plain_mapping(item, label="session_summary")
        if record.get("parentSessionId") is not None or record.get("origin") == "subagent":
            return None
        session_id = record.get("sessionId")
        updated_at = record.get("updatedAt")
        running = record.get("running")
        blank = record.get("blank")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("DSH read-only response rejected: invalid_session_id")
        if not isinstance(updated_at, int) or isinstance(updated_at, bool):
            raise ValueError("DSH read-only response rejected: invalid_updated_at")
        if not isinstance(running, bool) or not isinstance(blank, bool):
            raise ValueError("DSH read-only response rejected: invalid_session_flags")

        last_prompt_at: int | None = None
        projections = record.get("projections")
        if isinstance(projections, Mapping):
            values = projections.get("values")
            if isinstance(values, Mapping):
                metadata = values.get("sessionListMetadata")
                if isinstance(metadata, Mapping):
                    candidate = metadata.get("lastPromptAt")
                    if candidate is not None:
                        if not isinstance(candidate, int) or isinstance(candidate, bool):
                            raise ValueError(
                                "DSH read-only response rejected: invalid_last_prompt_at"
                            )
                        last_prompt_at = candidate
        return RootSessionCandidate(
            opaque_id=session_id,
            running=running,
            blank=blank,
            updated_at=updated_at,
            last_prompt_at=last_prompt_at,
        )

    def list_root_sessions(self) -> tuple[RootSessionCandidate, ...]:
        response = self._plain_mapping(
            self._unary_call(self._LIST_ENDPOINT, {"_request": {}}),
            label="list_response",
        )
        items = response.get("items")
        if not isinstance(items, list):
            raise ValueError("DSH read-only response rejected: invalid_session_items")
        roots = [candidate for item in items if (candidate := self._root_candidate(item))]
        return tuple(roots)

    @staticmethod
    def select_latest_nonblank_root(
        roots: Iterable[RootSessionCandidate],
    ) -> RootSessionCandidate:
        candidates = [root for root in roots if not root.blank]
        if not candidates:
            raise ValueError("DSH read-only selection rejected: no_nonblank_root")
        return max(
            candidates,
            key=lambda root: (
                root.last_prompt_at if root.last_prompt_at is not None else root.updated_at,
                root.updated_at,
                root.opaque_id,
            ),
        )

    def follow_once(
        self,
        root: RootSessionCandidate,
        *,
        max_messages: int = 1,
    ) -> FollowSnapshotSummary:
        if root.blank:
            raise ValueError("DSH read-only follow rejected: blank_session")
        if not isinstance(max_messages, int) or isinstance(max_messages, bool) or max_messages != 1:
            raise ValueError("DSH read-only follow rejected: max_messages_must_equal_one")
        frames = iter(
            self._stream_open(
                self._FOLLOW_ENDPOINT,
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": root.opaque_id},
                        "maxMessages": 1,
                    }
                },
            )
        )
        try:
            frame = self._plain_mapping(next(frames), label="follow_frame")
        except StopIteration as exc:
            raise ValueError("DSH read-only follow rejected: no_opening_frame") from exc
        if frame.get("type") != "snapshot":
            raise ValueError("DSH read-only follow rejected: first_frame_not_snapshot")
        records = frame.get("records")
        if not isinstance(records, list):
            raise ValueError("DSH read-only follow rejected: invalid_records")
        counts: dict[str, int] = {}
        for record in records:
            entry = self._plain_mapping(record, label="history_record")
            event = self._plain_mapping(entry.get("event"), label="history_event")
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise ValueError("DSH read-only follow rejected: invalid_event_type")
            counts[event_type] = counts.get(event_type, 0) + 1
        return FollowSnapshotSummary(
            cursor_present=isinstance(frame.get("cursor"), int)
            and not isinstance(frame.get("cursor"), bool),
            record_count=len(records),
            has_more=frame.get("hasMore") is True,
            projection_present=isinstance(frame.get("projections"), Mapping),
            event_type_counts=tuple(sorted(counts.items())),
        )

    @staticmethod
    def _write_disabled() -> None:
        raise RuntimeError("DSH write capability is intentionally disabled")

    def create_session(self, *_args: Any, **_kwargs: Any) -> None:
        self._write_disabled()

    def send_prompt(self, *_args: Any, **_kwargs: Any) -> None:
        self._write_disabled()

    def answer_approval(self, *_args: Any, **_kwargs: Any) -> None:
        self._write_disabled()

    def call_advisor(self, *_args: Any, **_kwargs: Any) -> None:
        self._write_disabled()
