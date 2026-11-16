from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


DecisionStatus = Literal["yes", "no", "unknown"]


@dataclass
class RunState:
    run_id: str
    task_id: str
    group: Literal["A", "C"]
    repeat: int
    advisor_calls: int = 0
    request_in_flight: bool = False
    armed: bool = True
    handled_event_ids: set[str] = field(default_factory=set)
    trigger_eligible_event_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "RunState":
        payload = dict(record)
        for field_name in ("handled_event_ids", "trigger_eligible_event_ids"):
            payload[field_name] = set(payload.get(field_name, []))
        return cls(**payload)

    def may_trigger(self, event_id: str, call_limit: int) -> tuple[bool, str]:
        if self.group == "A":
            return False, "group_a_shadow_only"
        if self.request_in_flight:
            return False, "request_in_flight"
        if event_id in self.handled_event_ids:
            return False, "event_already_handled"
        if not self.armed:
            return False, "awaiting_substantive_progress"
        if self.advisor_calls >= call_limit:
            return False, "advisor_call_limit_reached"
        return True, "eligible"

    def mark_request_started(self, event_id: str, call_limit: int) -> None:
        allowed, reason = self.may_trigger(event_id, call_limit)
        if not allowed:
            raise ValueError(f"request rejected: {reason}")
        self.request_in_flight = True
        self.handled_event_ids.add(event_id)

    def mark_confirmed_send(self) -> None:
        if not self.request_in_flight:
            raise ValueError("no request is in flight")
        self.advisor_calls += 1
        self.request_in_flight = False
        self.armed = False

    def mark_unknown_send(self) -> None:
        if not self.request_in_flight:
            raise ValueError("no request is in flight")
        # Conservatively consume one call: the remote side may have received it.
        self.advisor_calls += 1
        self.request_in_flight = False
        self.armed = False

    def record_progress(self, *, substantive: bool) -> None:
        if substantive and not self.request_in_flight:
            self.armed = True
