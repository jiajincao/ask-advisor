from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from .dsh_session_identity import SessionIdentityPin


_RECORD_KEYS = {"alias", "salt_hex", "fingerprint_sha256", "version"}
_DOCUMENT_KEYS = {"record", "integrity_hmac_sha256"}


class SessionPinStore:
    """Atomic, non-overwriting, integrity-protected session-pin storage."""

    def __init__(self, *, integrity_key: bytes, max_bytes: int = 8192) -> None:
        if not isinstance(integrity_key, bytes) or len(integrity_key) < 32:
            raise ValueError("DSH session pin store rejected: invalid_integrity_key")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 256:
            raise ValueError("DSH session pin store rejected: invalid_size_limit")
        self._integrity_key = integrity_key
        self._max_bytes = max_bytes

    @staticmethod
    def _record(pin: SessionIdentityPin) -> dict[str, Any]:
        return {
            "alias": pin.alias,
            "salt_hex": pin.salt_hex,
            "fingerprint_sha256": pin.fingerprint_sha256,
            "version": pin.version,
        }

    @staticmethod
    def _canonical(record: dict[str, Any]) -> bytes:
        return json.dumps(
            record,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    def _mac(self, record: dict[str, Any]) -> str:
        return hmac.new(
            self._integrity_key,
            self._canonical(record),
            hashlib.sha256,
        ).hexdigest()

    def save_new(self, path: Path, pin: SessionIdentityPin) -> None:
        target = Path(path)
        parent = target.parent
        if not parent.is_dir():
            raise RuntimeError("DSH session pin store failed: parent_unavailable")
        record = self._record(pin)
        document = {
            "record": record,
            "integrity_hmac_sha256": self._mac(record),
        }
        payload = json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        if len(payload) > self._max_bytes:
            raise RuntimeError("DSH session pin store failed: record_too_large")
        temporary = parent / f".{target.name}.tmp-{secrets.token_hex(8)}"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise RuntimeError("DSH session pin store rejected: target_exists") from exc
            os.unlink(temporary)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def load(self, path: Path) -> SessionIdentityPin:
        target = Path(path)
        try:
            metadata = target.lstat()
        except OSError as exc:
            raise RuntimeError("DSH session pin store failed: record_unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("DSH session pin store rejected: not_regular_file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("DSH session pin store rejected: unsafe_permissions")
        if metadata.st_size < 1 or metadata.st_size > self._max_bytes:
            raise RuntimeError("DSH session pin store rejected: invalid_record_size")
        try:
            raw = target.read_bytes()
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("DSH session pin store rejected: invalid_json") from exc
        if not isinstance(document, dict) or set(document) != _DOCUMENT_KEYS:
            raise RuntimeError("DSH session pin store rejected: invalid_document")
        record = document.get("record")
        supplied_mac = document.get("integrity_hmac_sha256")
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_KEYS
            or not isinstance(supplied_mac, str)
            or not hmac.compare_digest(self._mac(record), supplied_mac)
        ):
            raise RuntimeError("DSH session pin store rejected: integrity_mismatch")
        try:
            return SessionIdentityPin(**record)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("DSH session pin store rejected: invalid_pin") from exc

    def close(self) -> None:
        self._integrity_key = b""

    def __repr__(self) -> str:
        state = "closed" if not self._integrity_key else "open"
        return f"{type(self).__name__}(state={state!r}, integrity_key='<redacted>')"
