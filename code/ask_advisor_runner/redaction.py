from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitiveMatch:
    kind: str
    excerpt: str


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("authorization", re.compile(r"(?i)authorization\s*:\s*\S+")),
    ("cookie", re.compile(r"(?i)(?:cookie|set-cookie)\s*:\s*\S+")),
    ("api_key", re.compile(r"(?i)(?:api[_-]?key|token|password)\s*[=:]\s*[^\s,;]+")),
    ("url_credential", re.compile(r"https?://[^\s/@]+:[^\s/@]+@")),
)


def scan_sensitive(text: str) -> list[SensitiveMatch]:
    matches: list[SensitiveMatch] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            matches.append(SensitiveMatch(kind=kind, excerpt=match.group(0)[:24]))
    return matches


def require_clean(text: str) -> None:
    matches = scan_sensitive(text)
    if matches:
        kinds = ", ".join(sorted({match.kind for match in matches}))
        raise ValueError(f"sensitive content rejected: {kinds}")
