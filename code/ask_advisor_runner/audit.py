from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class AppendOnlyJsonl:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Any) -> None:
        payload = asdict(record) if is_dataclass(record) else dict(record)
        payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, encoded.encode("utf-8") + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
