from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from socket import AF_INET, SOCK_STREAM, SOL_SOCKET, SO_LINGER, socket
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    AuthenticatedProxyServer,
    ChatRequestInterceptor,
    DshSystemOneBridgeResult,
    OneShotFlowResult,
    OpenAIChatProxy,
    ProxyResponse,
    ProxyStream,
    StdlibVllmTransport,
    SystemOneGateResult,
)


TOKEN = "synthetic-proxy-token-1234567890"


def bridge_result() -> DshSystemOneBridgeResult:
    gate = SystemOneGateResult(0.2, 0.8, 0.99, 0.70, False)
    return DshSystemOneBridgeResult(gate, False, None)


class MockVllm:
    def __enter__(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                body = self.rfile.read(length)
                seen.append((self.path, self.headers.get("Authorization"), body))
                if self.path == "/v1/systemone":
                    response = json.dumps(
                        {
                            "answers": {
                                "need_advisor": {
                                    "probabilities": {"yes": 0.2, "no": 0.8}
                                }
                            },
                            "diagnostics": {
                                "need_advisor": {
                                    "label_mass": 0.99,
                                    "argmax_is_label": True,
                                }
                            },
                        }
                    ).encode()
                    content_type = "application/json"
                else:
                    response = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
                    content_type = "text/event-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class StreamingMockVllm:
    def __enter__(self):
        disconnected = self.disconnected = Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                chunk = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                frame = f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
                try:
                    while True:
                        self.wfile.write(frame)
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    disconnected.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ProxyRuntimeTests(unittest.TestCase):
    def test_baseline_entrypoint_forwards_only_authenticated_chat(self):
        import os
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from ask_advisor_runner import baseline_entrypoint

        messages = [{"role": "tool", "tool_call_id": str(i), "content": str(i)}
                    for i in range(4)]
        payload = {"model": "local", "messages": messages, "stream": True}
        with MockVllm() as upstream, TemporaryDirectory() as tmp:
            def exercise(server, transport):
                try:
                    server.start()
                    url = f"http://127.0.0.1:{server._server.server_port}/v1/chat/completions"
                    request = Request(url, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json"})
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(request, timeout=3)
                    self.assertEqual(denied.exception.code, 401)
                    denied.exception.close()
                    self.assertFalse(upstream.seen)
                    request.add_header("Authorization", f"Bearer {TOKEN}")
                    with urlopen(request, timeout=3) as response:
                        self.assertIn(b"data: [DONE]", response.read())
                    self.assertEqual(len(upstream.seen), 1)
                    route, auth, body = upstream.seen[0]
                    self.assertEqual(route, "/v1/chat/completions")
                    self.assertIsNone(auth)
                    self.assertEqual(json.loads(body)["messages"], messages)
                finally:
                    server.close()
                    transport.close()
                return 0

            env = {"ASK_ADVISOR_ENABLE_RUNTIME": "1", "ASK_ADVISOR_MODEL": "local",
                   "ASK_ADVISOR_VLLM_BASE_URL": upstream.base_url,
                   "ASK_ADVISOR_PROXY_BIND_PORT": "12345", "ASK_ADVISOR_PROXY_API_KEY": TOKEN,
                   "ASK_ADVISOR_AUDIT_LOG": str(Path(tmp) / "audit.jsonl")}
            with patch.dict(os.environ, env, clear=True), patch.object(
                baseline_entrypoint, "_port", return_value=0
            ), patch.object(baseline_entrypoint, "serve_until_stopped", exercise):
                self.assertEqual(baseline_entrypoint.main(), 0)

    def test_vllm_transport_allows_only_loopback_and_redacts_repr(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback_required"):
            StdlibVllmTransport(base_url="http://192.0.2.1:8000")
        transport = StdlibVllmTransport(
            base_url="http://127.0.0.1:8000", bearer_token="hidden"
        )
        self.assertNotIn("hidden", repr(transport))

    def test_chat_and_systemone_use_only_fixed_routes(self) -> None:
        with MockVllm() as mock:
            transport = StdlibVllmTransport(
                base_url=mock.base_url, bearer_token="synthetic-upstream"
            )
            chat = transport.chat({"messages": [], "stream": True})
            self.assertIsInstance(chat.body, ProxyStream)
            self.assertIn(b"data: [DONE]", b"".join(chat.body.iter_chunks()))
            decision = transport.systemone({"state": {}, "questions": {}})
            self.assertEqual(chat.content_type, "text/event-stream")
            self.assertIn("answers", decision)
            self.assertEqual(
                [entry[0] for entry in mock.seen],
                ["/v1/chat/completions", "/v1/systemone"],
            )
            chat_payload = json.loads(mock.seen[0][2])
            self.assertTrue(chat_payload["stream_options"]["include_usage"])
            self.assertTrue(
                all(entry[1] == "Bearer synthetic-upstream" for entry in mock.seen)
            )
            transport.close()
            with self.assertRaisesRegex(RuntimeError, "transport_closed"):
                transport.chat({"messages": []})

    def _application(self):
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda _request, _prompt: bridge_result(),
            model="local-model",
        )
        return OpenAIChatProxy(
            interceptor=interceptor,
            upstream_chat_call=lambda _payload: ProxyResponse(
                200, "application/json", b'{"choices":[]}'
            ),
            enabled=True,
        )

    def test_proxy_server_requires_explicit_enable_and_rejects_wildcard(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe_bind_host"):
            AuthenticatedProxyServer(
                application=self._application(),
                bind_host="0.0.0.0",
                bind_port=0,
                bearer_token=TOKEN,
                enabled=True,
            )
        server = AuthenticatedProxyServer(
            application=self._application(),
            bind_host="127.0.0.1",
            bind_port=0,
            bearer_token=TOKEN,
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            server.start()
        self.assertFalse(server.started)

    def test_authenticated_loopback_server_forwards_once(self) -> None:
        server = AuthenticatedProxyServer(
            application=self._application(),
            bind_host="127.0.0.1",
            bind_port=0,
            bearer_token=TOKEN,
            enabled=True,
        )
        try:
            server.start()
            port = server._server.server_port  # test-only ephemeral address
            url = f"http://127.0.0.1:{port}/v1/chat/completions"
            body = json.dumps(
                {"model": "local", "messages": [{"role": "user", "content": "x"}]}
            ).encode()
            unauthorized = Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(unauthorized, timeout=2)
            self.assertEqual(caught.exception.code, 401)

            authorized = Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TOKEN}",
                },
            )
            with urlopen(authorized, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'{"choices":[]}')
        finally:
            server.close()
        self.assertFalse(server.started)
        self.assertNotIn(TOKEN, repr(server))

    def test_streaming_server_records_completed_usage(self) -> None:
        records = []
        payload = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"completion_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda _request, _prompt: bridge_result(),
            model="local-model",
        )
        application = OpenAIChatProxy(
            interceptor=interceptor,
            upstream_chat_call=lambda _payload: ProxyResponse(
                200,
                "text/event-stream",
                ProxyStream(BytesIO(payload), max_bytes=4096, chunk_size=7),
            ),
            enabled=True,
        )
        server = AuthenticatedProxyServer(
            application=application,
            bind_host="127.0.0.1",
            bind_port=0,
            bearer_token=TOKEN,
            enabled=True,
            delivery_record=records.append,
        )
        try:
            server.start()
            port = server._server.server_port
            body = json.dumps(
                {
                    "model": "local-model",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": True,
                }
            ).encode()
            request = Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TOKEN}",
                },
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.read(), payload)
            deadline = time.monotonic() + 2
            while not records and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            server.close()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["termination"], "completed")
        self.assertTrue(records[0]["response_fully_delivered"])
        self.assertEqual(records[0]["completion_tokens"], 5)
        self.assertGreater(records[0]["accepted_output_tokens_per_second"], 0)
        self.assertEqual(
            records[0]["throughput_basis"],
            "completion_tokens/upstream_elapsed_seconds",
        )

    def test_client_disconnect_closes_stream_source(self) -> None:
        class RepeatingSource:
            def __init__(self) -> None:
                self.closed_event = Event()

            def read(self, _size: int = -1) -> bytes:
                if self.closed_event.wait(0.02):
                    return b""
                return b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'

            read1 = read

            def close(self) -> None:
                self.closed_event.set()

        source = RepeatingSource()
        records = []
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda _request, _prompt: bridge_result(),
            model="local-model",
        )
        application = OpenAIChatProxy(
            interceptor=interceptor,
            upstream_chat_call=lambda _payload: ProxyResponse(
                200,
                "text/event-stream",
                ProxyStream(source, max_bytes=1024 * 1024),
            ),
            enabled=True,
        )
        server = AuthenticatedProxyServer(
            application=application,
            bind_host="127.0.0.1",
            bind_port=0,
            bearer_token=TOKEN,
            enabled=True,
            delivery_record=records.append,
        )
        client = None
        try:
            server.start()
            body = json.dumps(
                {
                    "model": "local-model",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": True,
                }
            ).encode()
            request = (
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{server._server.server_port}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode()
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            client = socket(AF_INET, SOCK_STREAM)
            client.settimeout(2)
            client.connect(("127.0.0.1", server._server.server_port))
            client.sendall(request)
            received = b""
            while b"data:" not in received:
                received += client.recv(4096)
            client.setsockopt(SOL_SOCKET, SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            client = None
            self.assertTrue(source.closed_event.wait(2.0))
            deadline = time.monotonic() + 2
            while not records and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            if client is not None:
                client.close()
            server.close()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["termination"], "client_disconnected")
        self.assertFalse(records[0]["response_fully_delivered"])
        self.assertIsNone(records[0]["completion_tokens"])

    def test_client_disconnect_propagates_to_http_upstream(self) -> None:
        records = []
        with StreamingMockVllm() as upstream:
            transport = StdlibVllmTransport(base_url=upstream.base_url, timeout_sec=5)
            interceptor = ChatRequestInterceptor(
                gate_and_advise=lambda _request, _prompt: bridge_result(),
                model="local-model",
            )
            application = OpenAIChatProxy(
                interceptor=interceptor,
                upstream_chat_call=transport.chat,
                enabled=True,
            )
            server = AuthenticatedProxyServer(
                application=application,
                bind_host="127.0.0.1",
                bind_port=0,
                bearer_token=TOKEN,
                enabled=True,
                delivery_record=records.append,
            )
            client = None
            try:
                server.start()
                body = json.dumps(
                    {
                        "model": "local-model",
                        "messages": [{"role": "user", "content": "x"}],
                        "stream": True,
                    }
                ).encode()
                request = (
                    b"POST /v1/chat/completions HTTP/1.1\r\n"
                    + f"Host: 127.0.0.1:{server._server.server_port}\r\n".encode()
                    + b"Content-Type: application/json\r\n"
                    + f"Authorization: Bearer {TOKEN}\r\n".encode()
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                client = socket(AF_INET, SOCK_STREAM)
                client.settimeout(2)
                client.connect(("127.0.0.1", server._server.server_port))
                client.sendall(request)
                received = b""
                while b"data:" not in received:
                    received += client.recv(4096)
                client.setsockopt(SOL_SOCKET, SO_LINGER, struct.pack("ii", 1, 0))
                client.close()
                client = None
                self.assertTrue(upstream.disconnected.wait(3.0))
                deadline = time.monotonic() + 2
                while not records and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                if client is not None:
                    client.close()
                server.close()
                transport.close()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["termination"], "client_disconnected")
        self.assertFalse(records[0]["response_fully_delivered"])


if __name__ == "__main__":
    unittest.main()
