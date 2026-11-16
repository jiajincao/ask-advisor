from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO
import json
import math

from .chat_interceptor import ChatRequestInterceptor, InterceptionResult


class ProxyStream:
    """Bounded, closeable upstream response body for live SSE forwarding."""

    def __init__(
        self,
        source: BinaryIO,
        *,
        max_bytes: int,
        chunk_size: int = 16 * 1024,
    ) -> None:
        if not hasattr(source, "read") or not hasattr(source, "close"):
            raise TypeError("proxy stream rejected: invalid_source")
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("proxy stream rejected: invalid_response_limit")
        if not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("proxy stream rejected: invalid_chunk_size")
        self._source = source
        self._max_bytes = max_bytes
        self._chunk_size = chunk_size
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def iter_chunks(self) -> Iterator[bytes]:
        total = 0
        reader = getattr(self._source, "read1", self._source.read)
        try:
            while not self._closed:
                chunk = reader(self._chunk_size)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("proxy stream failed: bytes_chunk_required")
                total += len(chunk)
                if total > self._max_bytes:
                    raise RuntimeError("vLLM transport failed: response_too_large")
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._source.close()


@dataclass(frozen=True)
class ProxyResponse:
    """Opaque HTTP response returned by an injected upstream transport."""

    status: int
    content_type: str
    body: bytes | ProxyStream
    upstream_started_monotonic: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise ValueError("proxy response rejected: invalid_status")
        if not 100 <= self.status <= 599:
            raise ValueError("proxy response rejected: invalid_status")
        if self.content_type not in (
            "application/json",
            "text/event-stream",
        ):
            raise ValueError("proxy response rejected: invalid_content_type")
        if not isinstance(self.body, (bytes, ProxyStream)):
            raise TypeError("proxy response rejected: invalid_body")
        if self.upstream_started_monotonic is not None and (
            not isinstance(self.upstream_started_monotonic, (int, float))
            or isinstance(self.upstream_started_monotonic, bool)
            or not math.isfinite(self.upstream_started_monotonic)
            or self.upstream_started_monotonic < 0.0
        ):
            raise ValueError("proxy response rejected: invalid_start_time")


UpstreamChatCall = Callable[[Mapping[str, Any]], ProxyResponse]


@dataclass(frozen=True)
class ProxyExchange:
    """Content-free metadata plus the opaque upstream response."""

    response: ProxyResponse
    reason: str
    system_one_evaluated: bool
    advisor_called: bool
    suggestion_injected: bool


class DisabledProxyError(RuntimeError):
    pass


class BaselineChatForwarder:
    """Pass a request for the configured model through without any advisor hook."""

    def __init__(self, model: str) -> None:
        if not model:
            raise ValueError("baseline model required")
        self.model = model

    def inspect(self, payload: Mapping[str, Any]) -> InterceptionResult:
        if payload.get("model") != self.model:
            raise ValueError("baseline model mismatch")
        return InterceptionResult(dict(payload), None, None, False, False, "baseline")


class OpenAIChatProxy:
    """Default-disabled HTTP application core for one chat route.

    This object deliberately does not create sockets or own credentials. A
    reviewed runtime adapter may pass request bytes to ``handle`` and may
    implement the injected upstream callback. Only `/v1/chat/completions` is
    accepted; all other routes and methods fail closed.
    """

    _PATH = "/v1/chat/completions"

    def __init__(
        self,
        *,
        interceptor: ChatRequestInterceptor | BaselineChatForwarder,
        upstream_chat_call: UpstreamChatCall,
        enabled: bool = False,
        max_request_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if not isinstance(interceptor, (ChatRequestInterceptor, BaselineChatForwarder)):
            raise TypeError("chat proxy requires ChatRequestInterceptor")
        if not callable(upstream_chat_call):
            raise TypeError("chat proxy requires upstream callback")
        if (
            not isinstance(max_request_bytes, int)
            or isinstance(max_request_bytes, bool)
            or max_request_bytes < 1
        ):
            raise ValueError("chat proxy rejected: invalid_request_limit")
        self._interceptor = interceptor
        self._upstream_chat_call = upstream_chat_call
        self._enabled = bool(enabled)
        self._max_request_bytes = max_request_bytes

    @property
    def enabled(self) -> bool:
        return self._enabled

    def handle(
        self,
        *,
        method: str,
        path: str,
        content_type: str,
        body: bytes,
    ) -> ProxyExchange:
        if not self._enabled:
            raise DisabledProxyError("chat proxy disabled")
        if method != "POST" or path != self._PATH:
            raise ValueError("chat proxy rejected: route_not_allowed")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("chat proxy rejected: json_content_type_required")
        if not isinstance(body, bytes):
            raise TypeError("chat proxy rejected: bytes_body_required")
        if not body or len(body) > self._max_request_bytes:
            raise ValueError("chat proxy rejected: invalid_request_size")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("chat proxy rejected: invalid_json") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("chat proxy rejected: request_object_required")

        intercepted: InterceptionResult = self._interceptor.inspect(payload)
        response = self._upstream_chat_call(intercepted.request)
        if not isinstance(response, ProxyResponse):
            raise RuntimeError("chat proxy rejected: invalid_upstream_response")
        return ProxyExchange(
            response=response,
            reason=intercepted.reason,
            system_one_evaluated=intercepted.gate is not None,
            advisor_called=intercepted.advisor_called,
            suggestion_injected=intercepted.suggestion_injected,
        )
