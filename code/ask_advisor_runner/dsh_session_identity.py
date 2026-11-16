from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any


_ALIAS_PATTERN = re.compile(r"^dsh-root-[a-f0-9]{16}$")
_HEX_64_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DOMAIN = b"ask-advisor-dsh-session-pin-v1\0"


@dataclass(frozen=True)
class SessionIdentityPin:
    """Persistable identity record containing no real DSH session id."""

    alias: str
    salt_hex: str
    fingerprint_sha256: str
    version: int = 1

    def __post_init__(self) -> None:
        if (
            self.version != 1
            or not isinstance(self.alias, str)
            or _ALIAS_PATTERN.fullmatch(self.alias) is None
            or not isinstance(self.salt_hex, str)
            or _HEX_64_PATTERN.fullmatch(self.salt_hex) is None
            or not isinstance(self.fingerprint_sha256, str)
            or _HEX_64_PATTERN.fullmatch(self.fingerprint_sha256) is None
        ):
            raise ValueError("DSH session identity rejected: invalid_pin")

    def audit_fields(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "fingerprint_sha256": self.fingerprint_sha256,
            "version": self.version,
        }


class VerifiedSessionIdentity:
    """One-use in-memory handle whose repr never exposes the real id."""

    def __init__(self, *, alias: str, opaque_id: str) -> None:
        self.alias = alias
        self._opaque_id: str | None = opaque_id

    def take_opaque_id(self) -> str:
        value = self._opaque_id
        self._opaque_id = None
        if value is None:
            raise RuntimeError("DSH session identity rejected: replayed_reference")
        return value

    def close(self) -> None:
        self._opaque_id = None

    def __repr__(self) -> str:
        state = "available" if self._opaque_id is not None else "consumed"
        return (
            f"{type(self).__name__}(alias={self.alias!r}, "
            f"state={state!r}, opaque_id='<redacted>')"
        )


def _validated_opaque_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("DSH session identity rejected: invalid_candidate")
    return value


def _fingerprint(opaque_id: str, salt: bytes) -> str:
    encoded = opaque_id.encode("utf-8")
    return hashlib.sha256(
        _DOMAIN + len(encoded).to_bytes(4, "big") + encoded + salt
    ).hexdigest()


def create_session_identity_pin(
    opaque_id: str,
    *,
    alias: str | None = None,
    salt: bytes | None = None,
) -> SessionIdentityPin:
    candidate = _validated_opaque_id(opaque_id)
    resolved_alias = alias or f"dsh-root-{secrets.token_hex(8)}"
    resolved_salt = salt if salt is not None else secrets.token_bytes(32)
    if not isinstance(resolved_salt, bytes) or len(resolved_salt) != 32:
        raise ValueError("DSH session identity rejected: invalid_salt")
    return SessionIdentityPin(
        alias=resolved_alias,
        salt_hex=resolved_salt.hex(),
        fingerprint_sha256=_fingerprint(candidate, resolved_salt),
    )


def verify_session_identity(
    pin: SessionIdentityPin,
    candidate_opaque_id: str,
) -> VerifiedSessionIdentity:
    candidate = _validated_opaque_id(candidate_opaque_id)
    try:
        salt = bytes.fromhex(pin.salt_hex)
    except ValueError as exc:
        raise ValueError("DSH session identity rejected: invalid_pin") from exc
    actual = _fingerprint(candidate, salt)
    if not hmac.compare_digest(actual, pin.fingerprint_sha256):
        raise RuntimeError("DSH session identity rejected: fingerprint_mismatch")
    return VerifiedSessionIdentity(alias=pin.alias, opaque_id=candidate)
