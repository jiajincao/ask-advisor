from __future__ import annotations

import base64
import hashlib
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    StdlibRawWebSocket,
    dial_loopback_websocket,
)


class StdlibWebSocketTests(unittest.TestCase):
    def test_text_frames_are_masked_and_server_text_is_decoded(self) -> None:
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(server_sock.close)
        connection = StdlibRawWebSocket(client_sock, timeout=1)
        received: list[str] = []

        def server() -> None:
            first, second = server_sock.recv(2)
            self.assertEqual(first & 0x0F, 0x1)
            self.assertTrue(second & 0x80)
            length = second & 0x7F
            mask = server_sock.recv(4)
            payload = server_sock.recv(length)
            decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            received.append(decoded.decode())
            response = b'{"type":"item"}'
            server_sock.sendall(bytes((0x81, len(response))) + response)

        thread = threading.Thread(target=server)
        thread.start()
        connection.send("hello")
        self.assertEqual(connection.recv(), '{"type":"item"}')
        thread.join(timeout=1)
        self.assertEqual(received, ["hello"])
        connection.close()

    def test_ping_is_answered_before_text(self) -> None:
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(server_sock.close)
        connection = StdlibRawWebSocket(client_sock, timeout=1)
        server_sock.sendall(b"\x89\x01x\x81\x02ok")
        self.assertEqual(connection.recv(), "ok")
        first, second = server_sock.recv(2)
        self.assertEqual(first & 0x0F, 0xA)
        self.assertTrue(second & 0x80)
        length = second & 0x7F
        mask = server_sock.recv(4)
        payload = server_sock.recv(length)
        decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.assertEqual(decoded, b"x")
        connection.close()

    def test_real_loopback_handshake_carries_cookie(self) -> None:
        class HandshakeSocket:
            def __init__(self) -> None:
                self.request = b""
                self.response = b""
                self.closed = False

            def settimeout(self, _timeout: float) -> None:
                pass

            def sendall(self, data: bytes) -> None:
                if data.startswith(b"GET "):
                    self.request = data
                    text = data.decode("iso-8859-1")
                    key = next(
                        line.split(":", 1)[1].strip()
                        for line in text.split("\r\n")
                        if line.lower().startswith("sec-websocket-key:")
                    )
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                        ).digest()
                    ).decode()
                    self.response = (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()

            def recv(self, size: int) -> bytes:
                result, self.response = self.response[:size], self.response[size:]
                return result

            def close(self) -> None:
                self.closed = True

        fake = HandshakeSocket()
        with patch("socket.create_connection", return_value=fake):
            connection = dial_loopback_websocket(
                "ws://127.0.0.1:43123/api/remote.mux",
                {"Cookie": "synthetic_cookie"},
                timeout=1,
            )
        request = fake.request.decode("iso-8859-1")
        self.assertIn("GET /api/remote.mux HTTP/1.1", request)
        self.assertIn("Cookie: synthetic_cookie", request)
        connection.close()
        self.assertTrue(fake.closed)

    def test_non_loopback_and_missing_cookie_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non_loopback_authority"):
            dial_loopback_websocket(
                "ws://example.invalid:80/api/remote.mux",
                {"Cookie": "x"},
            )
        with self.assertRaisesRegex(ValueError, "cookie_required"):
            dial_loopback_websocket(
                "ws://127.0.0.1:1/api/remote.mux",
                {},
            )

    def test_masked_server_frame_is_rejected(self) -> None:
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(server_sock.close)
        connection = StdlibRawWebSocket(client_sock, timeout=1)
        server_sock.sendall(b"\x81\x80" + b"abcd")
        with self.assertRaisesRegex(RuntimeError, "masked_server_frame"):
            connection.recv()
        connection.close()


if __name__ == "__main__":
    unittest.main()
