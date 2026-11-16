from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone


_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("-", value).strip("-")
    if not cleaned:
        raise ValueError("identifier component is empty after normalization")
    return cleaned


def new_run_id(task_id: str, group: str, repeat: int) -> str:
    if group not in {"A", "C"}:
        raise ValueError("group must be A or C")
    if repeat < 1:
        raise ValueError("repeat must be positive")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return f"tb21-{_safe(task_id)}-{group}-{repeat}-{stamp}-{suffix}"


def new_scoped_id(prefix: str) -> str:
    return f"{_safe(prefix)}-{uuid.uuid4()}"
