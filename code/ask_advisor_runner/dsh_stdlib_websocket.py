from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import urllib.parse
from collections.abc import Mapping


_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class StdlibRawWebSocket:
    """Minimal loopback-only RFC 6455 text client for DSH Remote Mux."""

    def __init__(self, sock: socket.socket, *, timeout: float) -> None:
        self._socket: socket.socket | None = sock
        self._timeout = timeout

    def _open_socket(self) -> socket.socket:
        if self._socket is None:
            raise RuntimeError("DSH WebSocket transport rejected: connection_closed")
        return self._socket

    @staticmethod
    def _masked_frame(opcode: int, payload: bytes) -> bytes:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return header + mask + masked

    def send(self, data: str) -> None:
        if not isinstance(data, str):
            raise TypeError("DSH WebSocket transport rejected: text_required")
        try:
            self._open_socket().sendall(self._masked_frame(0x1, data.encode("utf-8")))
        except BaseException as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("DSH "):
                raise
            raise RuntimeError("DSH WebSocket transport failed: send_failed") from exc

    @staticmethod
    def _receive_exact(sock: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = sock.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("DSH WebSocket transport failed: connection_ended")
            chunks.extend(chunk)
        return bytes(chunks)

    def recv(self) -> str:
        sock = self._open_socket()
        while True:
            try:
                first, second = self._receive_exact(sock, 2)
                if first & 0x70 or not first & 0x80:
                    raise RuntimeError("DSH WebSocket transport failed: unsupported_frame")
                opcode = first & 0x0F
                if second & 0x80:
                    raise RuntimeError("DSH WebSocket transport failed: masked_server_frame")
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._receive_exact(sock, 2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._receive_exact(sock, 8))[0]
                if length > 8 * 1024 * 1024:
                    raise RuntimeError("DSH WebSocket transport failed: frame_too_large")
                payload = self._receive_exact(sock, length)
                if opcode == 0x1:
                    return payload.decode("utf-8")
                if opcode == 0x8:
                    self.close()
                    raise RuntimeError("DSH WebSocket transport failed: connection_ended")
                if opcode == 0x9:
                    sock.sendall(self._masked_frame(0xA, payload))
                    continue
                if opcode == 0xA:
                    continue
                raise RuntimeError("DSH WebSocket transport failed: unsupported_frame")
            except UnicodeDecodeError as exc:
                raise RuntimeError("DSH WebSocket transport failed: invalid_text_frame") from exc
            except BaseException as exc:
                if isinstance(exc, RuntimeError) and str(exc).startswith("DSH "):
                    raise
                raise RuntimeError("DSH WebSocket transport failed: receive_failed") from exc

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.sendall(self._masked_frame(0x8, b""))
            except BaseException:
                pass
            try:
                sock.close()
            except BaseException:
                pass


def dial_loopback_websocket(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout: float = 10.0,
) -> StdlibRawWebSocket:
    """Open a dependency-free, loopback-only ws:// connection."""

    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DSH WebSocket dial rejected: invalid_authority") from exc
    if (
        parsed.scheme != "ws"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("DSH WebSocket dial rejected: non_loopback_authority")
    if set(headers) - {"Cookie"}:
        raise ValueError("DSH WebSocket dial rejected: unsupported_header")
    cookie = headers.get("Cookie")
    if not isinstance(cookie, str) or not cookie:
        raise ValueError("DSH WebSocket dial rejected: cookie_required")
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = f"[{parsed.hostname}]:{port}" if ":" in parsed.hostname else f"{parsed.hostname}:{port}"
    request = (
        f"GET {target} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        f"Cookie: {cookie}\r\n\r\n"
    ).encode("ascii")
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("DSH WebSocket dial failed: handshake_ended")
            response.extend(chunk)
            if len(response) > 16384:
                raise RuntimeError("DSH WebSocket dial failed: handshake_too_large")
        header_block = bytes(response).split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
        lines = header_block.split("\r\n")
        response_headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if (
            not lines[0].startswith("HTTP/1.1 101 ")
            or response_headers.get("upgrade", "").lower() != "websocket"
            or response_headers.get("sec-websocket-accept") != expected
        ):
            raise RuntimeError("DSH WebSocket dial failed: handshake_rejected")
        return StdlibRawWebSocket(sock, timeout=timeout)
    except BaseException as exc:
        if sock is not None:
            sock.close()
        if isinstance(exc, (ValueError, RuntimeError)) and str(exc).startswith("DSH "):
            raise
        raise RuntimeError("DSH WebSocket dial failed: connection_failed") from exc
