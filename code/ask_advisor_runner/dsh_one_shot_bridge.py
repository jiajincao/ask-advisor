from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .bridge import (
    ApprovalDecision,
    ApprovalRequest,
    RestrictedApprovalAnswerer,
)
from .dsh_session_identity import VerifiedSessionIdentity
from .redaction import require_clean
from .state import RunState


PromptTransport = Callable[[str, dict[str, Any]], Mapping[str, Any]]


def prompt_sha256(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("DSH one-shot prompt rejected: invalid_prompt")
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclass
class OneShotPromptGrant:
    session_alias: str
    expected_prompt_sha256: str
    consumed: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_alias, str)
            or not self.session_alias
            or not isinstance(self.expected_prompt_sha256, str)
            or len(self.expected_prompt_sha256) != 64
        ):
            raise ValueError("DSH one-shot prompt rejected: invalid_grant")


class OneShotPromptGate:
    """Allow one exact, pre-hashed prompt to one verified frozen session."""

    def __init__(self, *, transport: PromptTransport, max_bytes: int = 32768) -> None:
        if not callable(transport):
            raise TypeError("prompt transport must be callable")
        self._transport = transport
        self._max_bytes = max_bytes

    def send_once(
        self,
        *,
        verified_session: VerifiedSessionIdentity,
        grant: OneShotPromptGrant,
        request_id: str,
        prompt: str,
    ) -> dict[str, Any]:
        if grant.consumed:
            raise RuntimeError("DSH one-shot prompt rejected: grant_replayed")
        if verified_session.alias != grant.session_alias:
            raise RuntimeError("DSH one-shot prompt rejected: session_alias_mismatch")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("DSH one-shot prompt rejected: invalid_request_id")
        encoded = prompt.encode("utf-8") if isinstance(prompt, str) else b""
        if not encoded or len(encoded) > self._max_bytes:
            raise ValueError("DSH one-shot prompt rejected: invalid_prompt_size")
        require_clean(prompt)
        if prompt_sha256(prompt) != grant.expected_prompt_sha256:
            raise RuntimeError("DSH one-shot prompt rejected: prompt_hash_mismatch")
        session_id = verified_session.take_opaque_id()
        grant.consumed = True
        response = self._transport(
            "session/prompt",
            {
                "sessionId": session_id,
                "requestId": request_id,
                "mode": "queue",
                "content": [{"type": "text", "text": prompt}],
            },
        )
        session_id = ""
        if not isinstance(response, Mapping) or response.get("accepted") is not True:
            raise RuntimeError("DSH one-shot prompt failed: not_accepted")
        return {
            "accepted": True,
            "request_id": request_id,
            "session_alias": grant.session_alias,
            "prompt_sha256": grant.expected_prompt_sha256,
        }


class RestrictedApprovalDispatcher:
    """Replay-safe wrapper around the deterministic approval answerer."""

    def __init__(self, *, answerer: RestrictedApprovalAnswerer) -> None:
        self._answerer = answerer
        self._seen_approval_ids: set[str] = set()

    def decide_once(
        self,
        request: ApprovalRequest,
        *,
        state: RunState,
        expected_trigger_id: str,
        expected_request_hash: str,
        call_limit: int = 3,
    ) -> ApprovalDecision:
        if request.approval_request_id in self._seen_approval_ids:
            return ApprovalDecision(
                outcome="rejected",
                reason="approval_replay_rejected",
                approval_request_id=request.approval_request_id,
                call_id=request.call_id,
                request_hash=request.request_hash,
            )
        self._seen_approval_ids.add(request.approval_request_id)
        return self._answerer.decide(
            request,
            state=state,
            expected_trigger_id=expected_trigger_id,
            expected_request_hash=expected_request_hash,
            call_limit=call_limit,
        )
