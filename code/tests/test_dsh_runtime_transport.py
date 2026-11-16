from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    JsonWebSocketConnection,
    OneShotBootstrapUrlProvider,
    make_json_websocket_dialer,
)


class TrackedTextSource(io.StringIO):
    pass


class FailingTextSource(TrackedTextSource):
    def readline(self, limit: int = -1) -> str:
        raise RuntimeError("sensitive-source-failure")


class FakeRawSocket:
    def __init__(self, incoming: str | bytes = '{"type":"item"}') -> None:
        self.incoming = incoming
        self.sent: list[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> str | bytes:
        return self.incoming

    def close(self) -> None:
        self.closed = True


class RuntimeTransportTests(unittest.TestCase):
    def test_bootstrap_provider_is_one_shot_and_closes_source(self) -> None:
        source = TrackedTextSource(
            "http://127.0.0.1:43123/?token=synthetic_secret\n"
        )
        provider = OneShotBootstrapUrlProvider(source=source)
        self.assertNotIn("synthetic_secret", repr(provider))
        value = provider()
        self.assertEqual(value, "http://127.0.0.1:43123/?token=synthetic_secret")
        self.assertTrue(source.closed)
        self.assertNotIn("synthetic_secret", repr(provider))
        with self.assertRaisesRegex(RuntimeError, "already_consumed"):
            provider()

    def test_provider_close_without_reading_closes_source(self) -> None:
        source = TrackedTextSource("secret")
        provider = OneShotBootstrapUrlProvider(source=source)
        provider.close()
        self.assertTrue(source.closed)
        with self.assertRaisesRegex(RuntimeError, "already_consumed"):
            provider()

    def test_provider_rejects_oversized_value_without_echoing_it(self) -> None:
        source = TrackedTextSource("sensitive-value")
        provider = OneShotBootstrapUrlProvider(source=source, max_chars=4)
        with self.assertRaisesRegex(RuntimeError, "invalid_source_value") as caught:
            provider()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertTrue(source.closed)

    def test_provider_read_failure_is_redacted_and_closes_source(self) -> None:
        source = FailingTextSource("unused")
        provider = OneShotBootstrapUrlProvider(source=source)
        with self.assertRaisesRegex(RuntimeError, "source_read_failed") as caught:
            provider()
        self.assertNotIn("sensitive-source-failure", str(caught.exception))
        self.assertTrue(source.closed)

    def test_json_websocket_encodes_and_decodes_frames(self) -> None:
        raw = FakeRawSocket(b'{"type":"item","streamId":"s"}')
        connection = JsonWebSocketConnection(raw)
        connection.send_json({"type": "open", "streamId": "s"})
        self.assertEqual(
            json.loads(raw.sent[0]),
            {"type": "open", "streamId": "s"},
        )
        self.assertEqual(
            connection.receive_json(),
            {"type": "item", "streamId": "s"},
        )
        connection.close()
        self.assertTrue(raw.closed)

    def test_invalid_incoming_frame_does_not_leak_content(self) -> None:
        connection = JsonWebSocketConnection(FakeRawSocket("secret-not-json"))
        with self.assertRaisesRegex(RuntimeError, "invalid_json_frame") as caught:
            connection.receive_json()
        self.assertNotIn("secret-not-json", str(caught.exception))

    def test_send_and_receive_failures_are_redacted(self) -> None:
        class FailingSocket(FakeRawSocket):
            def send(self, data: str) -> None:
                raise RuntimeError("sensitive-send-failure")

            def recv(self) -> str:
                raise RuntimeError("sensitive-receive-failure")

        connection = JsonWebSocketConnection(FailingSocket())
        with self.assertRaisesRegex(RuntimeError, "send_failed") as send_error:
            connection.send_json({"type": "open"})
        with self.assertRaisesRegex(RuntimeError, "receive_failed") as receive_error:
            connection.receive_json()
        self.assertNotIn("sensitive-send-failure", str(send_error.exception))
        self.assertNotIn("sensitive-receive-failure", str(receive_error.exception))

    def test_dialer_passes_credentials_only_to_raw_boundary(self) -> None:
        calls: list[tuple[str, Mapping[str, str], FakeRawSocket]] = []

        def raw_dialer(url: str, headers: Mapping[str, str]) -> FakeRawSocket:
            socket = FakeRawSocket()
            calls.append((url, headers, socket))
            return socket

        dialer = make_json_websocket_dialer(raw_dialer)
        connection = dialer(
            "ws://127.0.0.1:43123/api/remote.mux",
            {"Cookie": "synthetic_secret"},
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("synthetic_secret", repr(connection))
        connection.close()
        self.assertTrue(calls[0][2].closed)

    def test_dial_failure_is_redacted(self) -> None:
        def failing_dialer(_url: str, _headers: Mapping[str, str]) -> FakeRawSocket:
            raise RuntimeError("sensitive-dial-failure")

        dialer = make_json_websocket_dialer(failing_dialer)
        with self.assertRaisesRegex(RuntimeError, "dial_failed") as caught:
            dialer("ws://127.0.0.1:43123/api/remote.mux", {"Cookie": "secret"})
        self.assertNotIn("sensitive-dial-failure", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
