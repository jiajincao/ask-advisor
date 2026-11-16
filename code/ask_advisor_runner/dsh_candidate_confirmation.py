from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dsh_readonly import RootSessionCandidate
from .dsh_session_identity import SessionIdentityPin, create_session_identity_pin


@dataclass(frozen=True)
class AnonymousSessionCandidate:
    """Content-free, identifier-free row for human target confirmation."""

    anonymous_index: int
    blank: bool
    running: bool
    recency_rank: int
    recency_source: str
    recommended: bool


class SessionCandidateConfirmation:
    """One-use in-memory mapping from anonymous rows to real root ids."""

    def __init__(self, roots: tuple[RootSessionCandidate, ...]) -> None:
        if not roots:
            raise ValueError("DSH candidate confirmation rejected: no_roots")
        if len({root.opaque_id for root in roots}) != len(roots):
            raise ValueError("DSH candidate confirmation rejected: duplicate_root")
        ordered = sorted(
            roots,
            key=lambda root: (
                root.last_prompt_at
                if root.last_prompt_at is not None
                else root.updated_at,
                root.updated_at,
                root.opaque_id,
            ),
            reverse=True,
        )
        eligible = [root for root in ordered if not root.blank and not root.running]
        recommended_id = eligible[0].opaque_id if eligible else None
        self._roots: dict[int, RootSessionCandidate] = {
            index: root for index, root in enumerate(ordered, start=1)
        }
        self._summaries = tuple(
            AnonymousSessionCandidate(
                anonymous_index=index,
                blank=root.blank,
                running=root.running,
                recency_rank=index,
                recency_source=(
                    "last_prompt_at"
                    if root.last_prompt_at is not None
                    else "updated_at_fallback"
                ),
                recommended=root.opaque_id == recommended_id,
            )
            for index, root in self._roots.items()
        )
        self._consumed = False

    def summaries(self) -> tuple[AnonymousSessionCandidate, ...]:
        if self._consumed:
            raise RuntimeError("DSH candidate confirmation rejected: already_consumed")
        return self._summaries

    def freeze_confirmed(
        self,
        anonymous_index: int,
        *,
        alias: str | None = None,
        salt: bytes | None = None,
    ) -> SessionIdentityPin:
        if self._consumed:
            raise RuntimeError("DSH candidate confirmation rejected: already_consumed")
        if not isinstance(anonymous_index, int) or isinstance(anonymous_index, bool):
            raise ValueError("DSH candidate confirmation rejected: invalid_index")
        root = self._roots.get(anonymous_index)
        if root is None:
            raise ValueError("DSH candidate confirmation rejected: invalid_index")
        if root.blank:
            raise RuntimeError("DSH candidate confirmation rejected: blank_root")
        if root.running:
            raise RuntimeError("DSH candidate confirmation rejected: running_root")
        self._consumed = True
        try:
            return create_session_identity_pin(
                root.opaque_id,
                alias=alias,
                salt=salt,
            )
        finally:
            self._roots.clear()
            self._summaries = ()

    def close(self) -> None:
        self._consumed = True
        self._roots.clear()
        self._summaries = ()

    def __repr__(self) -> str:
        state = "consumed" if self._consumed else "pending"
        return (
            f"{type(self).__name__}(state={state!r}, "
            f"candidate_count={len(self._roots)!r}, opaque_ids='<redacted>')"
        )
