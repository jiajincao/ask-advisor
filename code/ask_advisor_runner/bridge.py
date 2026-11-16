from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from .redaction import require_clean
from .state import RunState


ApprovalOutcome = Literal["allowed-once", "rejected"]
_ARG_KEYS = {"question", "goal", "constraints", "attempts", "evidence"}
_EVIDENCE_KEYS = {"kind", "source", "start_line", "end_line"}
_EVIDENCE_KINDS = {"file", "tool_result"}


def canonical_request_hash(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    trigger_id: str
    event_id: str
    session_alias: str
    is_root_session: bool
    tool_name: str
    call_id: str
    approval_request_id: str
    request_hash: str
    arguments: dict[str, Any]
    unknown_send_state: bool = False
    retry_of: str | None = None


@dataclass(frozen=True)
class ApprovalDecision:
    outcome: ApprovalOutcome
    reason: str
    approval_request_id: str
    call_id: str
    request_hash: str


class RestrictedApprovalAnswerer:
    """Offline deterministic policy for one exact SuperAdvisor approval.

    This class has no DSH transport, browser session, Cookie, or approval RPC.
    It only models whether a future exact request would be eligible for one
    `allowed-once` response.
    """

    def __init__(self, *, expected_session_alias: str, max_input_bytes: int = 32768) -> None:
        if not expected_session_alias:
            raise ValueError("expected session alias is required")
        self.expected_session_alias = expected_session_alias
        self.max_input_bytes = max_input_bytes

    @staticmethod
    def _reject(request: ApprovalRequest, reason: str) -> ApprovalDecision:
        return ApprovalDecision(
            outcome="rejected",
            reason=reason,
            approval_request_id=request.approval_request_id,
            call_id=request.call_id,
            request_hash=request.request_hash,
        )

    def _validate_arguments(self, arguments: dict[str, Any]) -> str | None:
        if set(arguments) != _ARG_KEYS:
            return "ask_advisor_schema_mismatch"
        for key in ("question", "goal", "constraints", "attempts"):
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"invalid_{key}"
        evidence = arguments.get("evidence")
        if not isinstance(evidence, list):
            return "invalid_evidence"
        for item in evidence:
            if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
                return "invalid_evidence_item"
            if item.get("kind") not in _EVIDENCE_KINDS:
                return "invalid_evidence_kind"
            if not isinstance(item.get("source"), str) or not item["source"].strip():
                return "invalid_evidence_source"
            start_line = item.get("start_line")
            end_line = item.get("end_line")
            if (
                not isinstance(start_line, int)
                or isinstance(start_line, bool)
                or not isinstance(end_line, int)
                or isinstance(end_line, bool)
                or start_line < 1
                or end_line < start_line
            ):
                return "invalid_evidence_range"
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > self.max_input_bytes:
            return "max_input_bytes_exceeded"
        try:
            require_clean(encoded)
        except ValueError:
            return "sensitive_content_rejected"
        return None

    def decide(
        self,
        request: ApprovalRequest,
        *,
        state: RunState,
        expected_trigger_id: str,
        expected_request_hash: str,
        call_limit: int,
    ) -> ApprovalDecision:
        required_ids = {
            "run_id": request.run_id,
            "trigger_id": request.trigger_id,
            "event_id": request.event_id,
            "call_id": request.call_id,
            "approval_request_id": request.approval_request_id,
            "request_hash": request.request_hash,
        }
        if any(not isinstance(value, str) or not value.strip() for value in required_ids.values()):
            return self._reject(request, "missing_correlation_id")
        if request.run_id != state.run_id:
            return self._reject(request, "run_id_mismatch")
        if request.trigger_id != expected_trigger_id:
            return self._reject(request, "trigger_id_mismatch")
        if request.session_alias != self.expected_session_alias or not request.is_root_session:
            return self._reject(request, "root_session_mismatch")
        if request.tool_name != "ask_advisor":
            return self._reject(request, "tool_not_allowed")
        if request.unknown_send_state or request.retry_of is not None:
            return self._reject(request, "unknown_send_or_retry_rejected")
        if state.group != "C":
            return self._reject(request, "group_a_shadow_only")
        if not state.request_in_flight or request.event_id not in state.handled_event_ids:
            return self._reject(request, "no_matching_request_in_flight")
        if request.event_id not in state.trigger_eligible_event_ids:
            return self._reject(request, "event_not_trigger_eligible")
        if state.advisor_calls >= call_limit:
            return self._reject(request, "advisor_call_limit_reached")
        argument_error = self._validate_arguments(request.arguments)
        if argument_error:
            return self._reject(request, argument_error)
        recomputed_hash = canonical_request_hash(request.arguments)
        if (
            request.request_hash != recomputed_hash
            or request.request_hash != expected_request_hash
        ):
            return self._reject(request, "request_hash_mismatch")
        return ApprovalDecision(
            outcome="allowed-once",
            reason="all_deterministic_checks_passed",
            approval_request_id=request.approval_request_id,
            call_id=request.call_id,
            request_hash=request.request_hash,
        )
