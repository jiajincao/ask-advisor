from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .bridge import ApprovalDecision
from .dsh_approval_transport import ApprovalPresentation
from .dsh_one_shot_bridge import OneShotPromptGrant
from .dsh_session_identity import VerifiedSessionIdentity
from .redaction import require_clean


class PromptGate(Protocol):
    def send_once(
        self,
        *,
        verified_session: VerifiedSessionIdentity,
        grant: OneShotPromptGrant,
        request_id: str,
        prompt: str,
    ) -> Mapping[str, Any]: ...


class ApprovalChannel(Protocol):
    def open(self) -> None: ...

    def receive_once(self) -> ApprovalPresentation: ...

    def answer_once(self, outcome: str) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class AdvisorResult:
    status: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class OneShotFlowResult:
    status: str
    advisor_called: bool
    suggestion: str | None
    truncated: bool | None


ApprovalPolicy = Callable[[ApprovalPresentation], ApprovalDecision]
ResultReader = Callable[[str], AdvisorResult]


class OneShotAdvisorFlow:
    """Coordinate one prompt, one approval, and one Advisor suggestion."""

    _BOUNDARY = "[EXTERNAL_ADVISOR_SUGGESTION]"

    def __init__(
        self,
        *,
        prompt_gate: PromptGate,
        approval_channel: ApprovalChannel,
        approval_policy: ApprovalPolicy,
        result_reader: ResultReader,
        max_suggestion_bytes: int = 32768,
    ) -> None:
        if not callable(approval_policy) or not callable(result_reader):
            raise TypeError("flow callbacks must be callable")
        if (
            not isinstance(max_suggestion_bytes, int)
            or isinstance(max_suggestion_bytes, bool)
            or max_suggestion_bytes < 1
        ):
            raise ValueError("one-shot flow rejected: invalid_suggestion_limit")
        self._prompt_gate = prompt_gate
        self._approval_channel = approval_channel
        self._approval_policy = approval_policy
        self._result_reader = result_reader
        self._max_suggestion_bytes = max_suggestion_bytes
        self._used = False

    def run_once(
        self,
        *,
        verified_session: VerifiedSessionIdentity,
        grant: OneShotPromptGrant,
        request_id: str,
        prompt: str,
    ) -> OneShotFlowResult:
        if self._used:
            raise RuntimeError("one-shot flow rejected: flow_replayed")
        self._used = True
        channel = self._approval_channel
        answered = False
        try:
            channel.open()
            self._prompt_gate.send_once(
                verified_session=verified_session,
                grant=grant,
                request_id=request_id,
                prompt=prompt,
            )
            presentation = channel.receive_once()
            decision = self._approval_policy(presentation)
            if (
                decision.call_id != presentation.call_id
                or decision.outcome not in ("allowed-once", "rejected")
            ):
                channel.answer_once("rejected")
                answered = True
                raise RuntimeError("one-shot flow rejected: approval_correlation_mismatch")
            channel.answer_once(decision.outcome)
            answered = True
            if decision.outcome != "allowed-once":
                return OneShotFlowResult(
                    status="rejected",
                    advisor_called=False,
                    suggestion=None,
                    truncated=None,
                )
            advisor = self._result_reader(presentation.call_id)
            if advisor.status != "success":
                raise RuntimeError("one-shot flow failed: advisor_not_successful")
            if not isinstance(advisor.text, str) or not advisor.text.strip():
                raise RuntimeError("one-shot flow failed: empty_advisor_suggestion")
            if len(advisor.text.encode("utf-8")) > self._max_suggestion_bytes:
                raise RuntimeError("one-shot flow failed: advisor_suggestion_too_large")
            require_clean(advisor.text)
            suggestion = f"{self._BOUNDARY}\n{advisor.text}"
            return OneShotFlowResult(
                status="success",
                advisor_called=True,
                suggestion=suggestion,
                truncated=advisor.truncated,
            )
        except BaseException:
            if not answered:
                try:
                    channel.answer_once("rejected")
                except BaseException:
                    pass
            raise
        finally:
            channel.close()

