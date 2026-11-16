from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    ChatRequestInterceptor,
    DisabledProxyError,
    DshSystemOneBridgeResult,
    OneShotFlowResult,
    OpenAIChatProxy,
    ProxyResponse,
    ProxyStream,
    SystemOneGateResult,
)


def request_with_tools(count: int, *, stream: bool) -> dict:
    messages = [{"role": "user", "content": "Complete the public task."}]
    for index in range(1, count + 1):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "content": f"public result {index}",
            }
        )
    return {"model": "local-model", "messages": messages, "stream": stream}


def bridge_result(p_yes: float, *, advice: bool) -> DshSystemOneBridgeResult:
    gate = SystemOneGateResult(
        p_yes=p_yes,
        p_no=1.0 - p_yes,
        label_mass=0.99,
        threshold=0.70,
        triggered=p_yes >= 0.70,
    )
    if not gate.triggered:
        return DshSystemOneBridgeResult(gate, False, None)
    flow = OneShotFlowResult(
        status="success" if advice else "rejected",
        advisor_called=advice,
        suggestion=(
            "[EXTERNAL_ADVISOR_SUGGESTION]\nTry one reversible check."
            if advice
            else None
        ),
        truncated=False if advice else None,
    )
    return DshSystemOneBridgeResult(gate, True, flow)


class OpenAIChatProxyTests(unittest.TestCase):
    def make_proxy(self, *, enabled: bool, p_yes: float = 0.4, advice: bool = False):
        upstream_requests = []

        def upstream(payload):
            upstream_requests.append(payload)
            if payload.get("stream"):
                return ProxyResponse(
                    200,
                    "text/event-stream",
                    b'data: {"choices":[]}\n\ndata: [DONE]\n\n',
                )
            return ProxyResponse(
                200,
                "application/json",
                b'{"choices":[]}',
            )

        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda _request, _prompt: bridge_result(
                p_yes, advice=advice
            ),
            model="local-model",
        )
        return (
            OpenAIChatProxy(
                interceptor=interceptor,
                upstream_chat_call=upstream,
                enabled=enabled,
            ),
            upstream_requests,
        )

    def test_proxy_is_disabled_by_default(self) -> None:
        proxy, upstream = self.make_proxy(enabled=False)
        body = json.dumps(request_with_tools(3, stream=False)).encode()
        with self.assertRaisesRegex(DisabledProxyError, "disabled"):
            proxy.handle(
                method="POST",
                path="/v1/chat/completions",
                content_type="application/json",
                body=body,
            )
        self.assertEqual(upstream, [])

    def test_json_response_is_forwarded_opaquely(self) -> None:
        proxy, upstream = self.make_proxy(enabled=True)
        body = json.dumps(request_with_tools(3, stream=False)).encode()
        exchange = proxy.handle(
            method="POST",
            path="/v1/chat/completions",
            content_type="application/json; charset=utf-8",
            body=body,
        )
        self.assertEqual(exchange.response.body, b'{"choices":[]}')
        self.assertEqual(exchange.response.content_type, "application/json")
        self.assertEqual(exchange.reason, "below_threshold")
        self.assertEqual(len(upstream), 1)

    def test_sse_response_and_injected_suggestion_are_forwarded(self) -> None:
        proxy, upstream = self.make_proxy(enabled=True, p_yes=0.9, advice=True)
        body = json.dumps(request_with_tools(3, stream=True)).encode()
        exchange = proxy.handle(
            method="POST",
            path="/v1/chat/completions",
            content_type="application/json",
            body=body,
        )
        self.assertEqual(exchange.response.content_type, "text/event-stream")
        self.assertIn(b"data: [DONE]", exchange.response.body)
        self.assertTrue(exchange.system_one_evaluated)
        self.assertTrue(exchange.advisor_called)
        self.assertTrue(exchange.suggestion_injected)
        self.assertEqual(upstream[0]["messages"][-1]["role"], "system")

    def test_unapproved_routes_and_malformed_json_never_reach_upstream(self) -> None:
        proxy, upstream = self.make_proxy(enabled=True)
        for kwargs in (
            {
                "method": "GET",
                "path": "/v1/chat/completions",
                "content_type": "application/json",
                "body": b"{}",
            },
            {
                "method": "POST",
                "path": "/v1/models",
                "content_type": "application/json",
                "body": b"{}",
            },
            {
                "method": "POST",
                "path": "/v1/chat/completions",
                "content_type": "application/json",
                "body": b"not-json",
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    proxy.handle(**kwargs)
        self.assertEqual(upstream, [])

    def test_response_validation_rejects_unapproved_content_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_content_type"):
            ProxyResponse(200, "text/html", b"no")

    def test_response_accepts_only_bounded_proxy_streams(self) -> None:
        class Source:
            def read(self, _size=-1):
                return b""

            def close(self):
                return None

        response = ProxyResponse(
            200,
            "text/event-stream",
            ProxyStream(Source(), max_bytes=1024),
        )
        self.assertIsInstance(response.body, ProxyStream)
        with self.assertRaisesRegex(TypeError, "invalid_body"):
            ProxyResponse(200, "application/json", "not-bytes")


if __name__ == "__main__":
    unittest.main()
