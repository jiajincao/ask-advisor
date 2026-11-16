from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .audit import AppendOnlyJsonl
from .redaction import require_clean


_ALLOWED_OUTCOMES = {"allowed-once", "rejected", "cancelled", "unavailable"}
_FINAL_STATUSES = {"success", "unknown", "cancelled", "rejected", "unavailable"}


@dataclass(frozen=True)
class SimulatedDshEvent:
    seq: int
    event_type: str
    session_alias: str
    data: dict[str, Any]


@dataclass
class AuditChain:
    run_id: str
    trigger_id: str
    event_id: str
    session_request_id: str
    dsh_session_alias: str
    tool_call_id: str | None = None
    approval_request_id: str | None = None
    approval_outcome: str | None = None
    superadvisor_request_id: str | None = None
    advisor_status: str | None = None
    truncated: bool | None = None

    @property
    def complete(self) -> bool:
        return all(
            (
                self.tool_call_id,
                self.approval_request_id,
                self.approval_outcome,
                self.superadvisor_request_id,
                self.advisor_status,
            )
        )


class OfflineDshEventAdapter:
    """Validate simulated DSH event ordering without any transport capability."""

    def __init__(
        self,
        *,
        log_path: Path,
        run_id: str,
        trigger_id: str,
        event_id: str,
        session_request_id: str,
        dsh_session_alias: str,
    ) -> None:
        required = (run_id, trigger_id, event_id, session_request_id, dsh_session_alias)
        if any(not value for value in required):
            raise ValueError("all audit-chain identifiers are required")
        self.chain = AuditChain(
            run_id=run_id,
            trigger_id=trigger_id,
            event_id=event_id,
            session_request_id=session_request_id,
            dsh_session_alias=dsh_session_alias,
        )
        self.log = AppendOnlyJsonl(log_path)
        self.last_seq = -1
        self.prompt_accepted = False

    @staticmethod
    def _required_string(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"event rejected: missing_{key}")
        return value

    def ingest(self, event: SimulatedDshEvent) -> None:
        if not isinstance(event.seq, int) or isinstance(event.seq, bool) or event.seq <= self.last_seq:
            raise ValueError("event rejected: non_monotonic_seq")
        if event.session_alias != self.chain.dsh_session_alias:
            raise ValueError("event rejected: session_alias_mismatch")
        encoded = json.dumps(asdict(event), ensure_ascii=False, sort_keys=True)
        require_clean(encoded)

        data = event.data
        if event.event_type == "session/prompt.accepted":
            if self.prompt_accepted:
                raise ValueError("event rejected: duplicate_prompt_acceptance")
            request_id = self._required_string(data, "request_id")
            if request_id != self.chain.session_request_id:
                raise ValueError("event rejected: session_request_id_mismatch")
            self.prompt_accepted = True

        elif event.event_type == "tool/call":
            if not self.prompt_accepted or self.chain.tool_call_id is not None:
                raise ValueError("event rejected: tool_call_out_of_order")
            if data.get("tool_name") != "ask_advisor":
                raise ValueError("event rejected: tool_not_allowed")
            self.chain.tool_call_id = self._required_string(data, "call_id")

        elif event.event_type == "approval/asked":
            if self.chain.tool_call_id is None or self.chain.approval_request_id is not None:
                raise ValueError("event rejected: approval_asked_out_of_order")
            if data.get("tool_name") != "ask_advisor":
                raise ValueError("event rejected: tool_not_allowed")
            call_id = self._required_string(data, "call_id")
            if call_id != self.chain.tool_call_id:
                raise ValueError("event rejected: call_id_mismatch")
            self.chain.approval_request_id = self._required_string(
                data, "approval_request_id"
            )

        elif event.event_type == "approval/decided":
            if self.chain.approval_request_id is None or self.chain.approval_outcome is not None:
                raise ValueError("event rejected: approval_decided_out_of_order")
            approval_request_id = self._required_string(data, "approval_request_id")
            if approval_request_id != self.chain.approval_request_id:
                raise ValueError("event rejected: approval_request_id_mismatch")
            outcome = self._required_string(data, "outcome")
            if outcome not in _ALLOWED_OUTCOMES:
                raise ValueError("event rejected: invalid_approval_outcome")
            self.chain.approval_outcome = outcome

        elif event.event_type == "superadvisor/result":
            if self.chain.approval_outcome is None or self.chain.advisor_status is not None:
                raise ValueError("event rejected: advisor_result_out_of_order")
            request_id = self._required_string(data, "request_id")
            status = self._required_string(data, "status")
            if status not in _FINAL_STATUSES:
                raise ValueError("event rejected: invalid_advisor_status")
            if status == "success" and self.chain.approval_outcome != "allowed-once":
                raise ValueError("event rejected: success_without_allowed_once")
            truncated = data.get("truncated")
            if not isinstance(truncated, bool):
                raise ValueError("event rejected: invalid_truncated")
            self.chain.superadvisor_request_id = request_id
            self.chain.advisor_status = status
            self.chain.truncated = truncated

        else:
            raise ValueError("event rejected: unsupported_event_type")

        self.last_seq = event.seq
        self.log.append(
            {
                "event_type": "simulated_dsh_event_accepted",
                "event": asdict(event),
                "audit_chain": asdict(self.chain),
                "chain_complete": self.chain.complete,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {**asdict(self.chain), "complete": self.chain.complete, "last_seq": self.last_seq}

    def connect(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("DSH transport is intentionally disabled in the offline adapter")
