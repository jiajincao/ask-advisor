from __future__ import annotations

from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from socket import MSG_PEEK
from threading import Event, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import hmac
import json
import select
import time
from uuid import uuid4

from .proxy_http import OpenAIChatProxy, ProxyExchange, ProxyResponse, ProxyStream


DeliveryRecord = Callable[[Mapping[str, Any]], None]


class _SseUsageTracker:
    """Extract only terminal usage metadata from bytes already delivered."""

    def __init__(self) -> None:
        self._buffer = b""
        self.completion_tokens: int | None = None
        self.done = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        for raw_line in lines:
            line = raw_line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                self.done = True
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            usage = payload.get("usage")
            if not isinstance(usage, Mapping):
                continue
            value = usage.get("completion_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.completion_tokens = value


class StdlibVllmTransport:
    """Credential-owning, loopback-only vLLM HTTP transport.

    The base URL and optional bearer value remain in memory and are never
    exposed by repr or stable exceptions. Only chat completions and the native
    System One route are available.
    """

    _ROUTES = {
        "chat": "/v1/chat/completions",
        "systemone": "/v1/systemone",
    }

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str | None = None,
        timeout_sec: float = 300.0,
        max_response_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("vLLM transport rejected: invalid_base_url")
        try:
            host = ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("vLLM transport rejected: numeric_loopback_required") from exc
        if not host.is_loopback:
            raise ValueError("vLLM transport rejected: loopback_required")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("vLLM transport rejected: invalid_base_url")
        if not isinstance(timeout_sec, (int, float)) or timeout_sec <= 0:
            raise ValueError("vLLM transport rejected: invalid_timeout")
        if not isinstance(max_response_bytes, int) or max_response_bytes < 1:
            raise ValueError("vLLM transport rejected: invalid_response_limit")
        if bearer_token is not None and (
            not isinstance(bearer_token, str) or not bearer_token
        ):
            raise ValueError("vLLM transport rejected: invalid_bearer_token")
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._timeout_sec = float(timeout_sec)
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return "StdlibVllmTransport(loopback=True, credentials=redacted)"

    def close(self) -> None:
        self._base_url = ""
        self._bearer_token = None

    def _post(self, route: str, payload: Mapping[str, Any]) -> ProxyResponse:
        if not self._base_url:
            raise RuntimeError("vLLM transport rejected: transport_closed")
        if route not in self._ROUTES:
            raise ValueError("vLLM transport rejected: route_not_allowed")
        if not isinstance(payload, Mapping):
            raise TypeError("vLLM transport rejected: payload_mapping_required")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        request = Request(
            self._base_url + self._ROUTES[route],
            data=body,
            headers=headers,
            method="POST",
        )
        upstream_started = time.monotonic()
        try:
            response = urlopen(request, timeout=self._timeout_sec)
        except HTTPError as exc:
            raise RuntimeError("vLLM transport failed: http_status_not_ok") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("vLLM transport failed: request_failed") from exc
        content_type = response.headers.get_content_type()
        status = response.status
        if route == "chat" and content_type == "text/event-stream":
            return ProxyResponse(
                status,
                content_type,
                ProxyStream(response, max_bytes=self._max_response_bytes),
                upstream_started,
            )
        try:
            with response:
                response_body = response.read(self._max_response_bytes + 1)
        except (TimeoutError, OSError) as exc:
            raise RuntimeError("vLLM transport failed: response_read_failed") from exc
        if len(response_body) > self._max_response_bytes:
            raise RuntimeError("vLLM transport failed: response_too_large")
        if route == "systemone":
            if content_type != "application/json":
                raise RuntimeError("vLLM transport failed: systemone_json_required")
        elif content_type not in ("application/json", "text/event-stream"):
            raise RuntimeError("vLLM transport failed: invalid_chat_content_type")
        return ProxyResponse(status, content_type, response_body, upstream_started)

    def chat(self, payload: Mapping[str, Any]) -> ProxyResponse:
        forwarded = dict(payload)
        if forwarded.get("stream") is True:
            options = forwarded.get("stream_options")
            if options is None:
                forwarded["stream_options"] = {"include_usage": True}
            elif isinstance(options, Mapping):
                updated_options = dict(options)
                updated_options.setdefault("include_usage", True)
                forwarded["stream_options"] = updated_options
        return self._post("chat", forwarded)

    def systemone(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._post("systemone", payload)
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("vLLM transport failed: invalid_systemone_json") from exc
        if not isinstance(parsed, Mapping):
            raise RuntimeError("vLLM transport failed: invalid_systemone_object")
        return parsed


class AuthenticatedProxyServer:
    """Explicitly enabled HTTP shell around ``OpenAIChatProxy``.

    It never starts in the constructor. Wildcard bind addresses are rejected,
    inbound authorization is compared in constant time, and request logging is
    disabled. Runtime code must call ``start`` and later ``close`` explicitly.
    """

    def __init__(
        self,
        *,
        application: OpenAIChatProxy,
        bind_host: str,
        bind_port: int,
        bearer_token: str,
        enabled: bool = False,
        delivery_record: DeliveryRecord | None = None,
    ) -> None:
        if not isinstance(application, OpenAIChatProxy):
            raise TypeError("proxy server requires OpenAIChatProxy")
        try:
            address = ip_address(bind_host)
        except ValueError as exc:
            raise ValueError("proxy server rejected: numeric_bind_host_required") from exc
        if address.is_unspecified or address.is_multicast:
            raise ValueError("proxy server rejected: unsafe_bind_host")
        if not isinstance(bind_port, int) or isinstance(bind_port, bool) or not 0 <= bind_port <= 65535:
            raise ValueError("proxy server rejected: invalid_bind_port")
        if not isinstance(bearer_token, str) or len(bearer_token) < 24:
            raise ValueError("proxy server rejected: bearer_token_too_short")
        self._application = application
        self._bind = (bind_host, bind_port)
        self._bearer_token = bearer_token
        self._enabled = bool(enabled)
        if delivery_record is not None and not callable(delivery_record):
            raise TypeError("proxy server rejected: invalid_delivery_record")
        self._delivery_record = delivery_record
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def __repr__(self) -> str:
        return "AuthenticatedProxyServer(started=%s, credentials=redacted)" % (
            self._server is not None,
        )

    @property
    def started(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if not self._enabled:
            raise RuntimeError("proxy server disabled")
        if self._server is not None:
            raise RuntimeError("proxy server rejected: already_started")
        application = self._application
        delivery_record = self._delivery_record
        expected_authorization = f"Bearer {self._bearer_token}"

        class Handler(BaseHTTPRequestHandler):
            server_version = "AskAdvisorProxy"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _error(self, status: int, code: str) -> None:
                body = json.dumps({"error": code}, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                request_started = time.monotonic()
                request_id = uuid4().hex
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied, expected_authorization):
                    self._error(401, "unauthorized")
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._error(400, "invalid_request")
                    return
                if length < 1:
                    self._error(400, "invalid_request")
                    return
                body = self.rfile.read(length)
                try:
                    exchange: ProxyExchange = application.handle(
                        method="POST",
                        path=self.path,
                        content_type=self.headers.get("Content-Type", ""),
                        body=body,
                    )
                except (TypeError, ValueError):
                    self._error(400, "invalid_request")
                    return
                except BaseException:
                    self._error(502, "upstream_failed")
                    return
                response = exchange.response
                timing_started = (
                    float(response.upstream_started_monotonic)
                    if response.upstream_started_monotonic is not None
                    else request_started
                )
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                if isinstance(response.body, bytes):
                    self.send_header("Content-Length", str(len(response.body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(response.body)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        termination = "client_disconnected"
                    else:
                        termination = "completed"
                    if delivery_record is not None:
                        completion_tokens = None
                        try:
                            payload = json.loads(response.body.decode("utf-8"))
                            usage = payload.get("usage") if isinstance(payload, Mapping) else None
                            value = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                                completion_tokens = value
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                        elapsed = max(time.monotonic() - timing_started, 0.0)
                        delivery_record(
                            {
                                "event_type": "chat_delivery",
                                "request_id": request_id,
                                "termination": termination,
                                "streamed": False,
                                "response_fully_delivered": termination == "completed",
                                "bytes_forwarded": (
                                    len(response.body) if termination == "completed" else 0
                                ),
                                "chunks_forwarded": 1 if termination == "completed" else 0,
                                "completion_tokens": completion_tokens,
                                "upstream_elapsed_seconds": elapsed,
                                "throughput_basis": (
                                    "completion_tokens/upstream_elapsed_seconds"
                                ),
                                "accepted_output_tokens_per_second": (
                                    completion_tokens / elapsed
                                    if termination == "completed"
                                    and completion_tokens is not None
                                    and elapsed > 0.0
                                    else None
                                ),
                            }
                        )
                    self.close_connection = True
                    return

                stream = response.body
                tracker = _SseUsageTracker()
                bytes_forwarded = 0
                chunks_forwarded = 0
                termination = "completed"
                monitor_stop = Event()
                client_disconnected = Event()

                def monitor_client() -> None:
                    while not monitor_stop.wait(0.1):
                        try:
                            readable, _, _ = select.select([self.connection], [], [], 0)
                            if not readable:
                                continue
                            if not self.connection.recv(1, MSG_PEEK):
                                client_disconnected.set()
                                stream.close()
                                return
                        except (ConnectionResetError, OSError, ValueError):
                            client_disconnected.set()
                            stream.close()
                            return

                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                monitor = Thread(target=monitor_client, name="proxy-client-monitor", daemon=True)
                monitor.start()
                try:
                    for chunk in stream.iter_chunks():
                        if client_disconnected.is_set():
                            termination = "client_disconnected"
                            break
                        frame = f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
                        self.wfile.write(frame)
                        self.wfile.flush()
                        bytes_forwarded += len(chunk)
                        chunks_forwarded += 1
                        tracker.feed(chunk)
                    if client_disconnected.is_set():
                        termination = "client_disconnected"
                    else:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    termination = "client_disconnected"
                except BaseException:
                    termination = (
                        "client_disconnected"
                        if client_disconnected.is_set()
                        else "upstream_failed"
                    )
                finally:
                    monitor_stop.set()
                    stream.close()
                    monitor.join(timeout=1.0)
                    self.close_connection = True

                elapsed = max(time.monotonic() - timing_started, 0.0)
                fully_delivered = termination == "completed" and tracker.done
                completion_tokens = tracker.completion_tokens if fully_delivered else None
                if delivery_record is not None:
                    delivery_record(
                        {
                            "event_type": "chat_delivery",
                            "request_id": request_id,
                            "termination": termination,
                            "streamed": True,
                            "response_fully_delivered": fully_delivered,
                            "bytes_forwarded": bytes_forwarded,
                            "chunks_forwarded": chunks_forwarded,
                            "completion_tokens": completion_tokens,
                            "upstream_elapsed_seconds": elapsed,
                            "throughput_basis": (
                                "completion_tokens/upstream_elapsed_seconds"
                            ),
                            "accepted_output_tokens_per_second": (
                                completion_tokens / elapsed
                                if completion_tokens is not None and elapsed > 0.0
                                else None
                            ),
                        }
                    )

        server = ThreadingHTTPServer(self._bind, Handler)
        thread = Thread(target=server.serve_forever, name="ask-advisor-proxy", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self._bearer_token = ""
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
