from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    ChatRequestInterceptor,
    DshSystemOneBridgeResult,
    HarborQwenProxyConfig,
    OneShotFlowResult,
    SystemOneGateResult,
)


def bridge_result(
    p_yes: float,
    *,
    advisor_called: bool = False,
    suggestion: str | None = None,
) -> DshSystemOneBridgeResult:
    gate = SystemOneGateResult(
        p_yes=p_yes,
        p_no=1.0 - p_yes,
        label_mass=0.99,
        threshold=0.70,
        triggered=p_yes >= 0.70,
    )
    if not gate.triggered:
        return DshSystemOneBridgeResult(gate, False, None)
    return DshSystemOneBridgeResult(
        gate,
        True,
        OneShotFlowResult(
            status="success" if advisor_called else "rejected",
            advisor_called=advisor_called,
            suggestion=suggestion,
            truncated=False if advisor_called else None,
        ),
    )


def request_with_tools(count: int) -> dict:
    messages = [{"role": "user", "content": "Complete the public task."}]
    for index in range(1, count + 1):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "content": f"public result {index}",
                },
            ]
        )
    return {"model": "local-model", "messages": messages, "stream": True}


class ChatRequestInterceptorTests(unittest.TestCase):
    def test_first_two_tool_steps_are_not_evaluated(self) -> None:
        calls = []
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda request, prompt: calls.append((request, prompt)),
            model="local-model",
        )
        first = interceptor.inspect(request_with_tools(1))
        second = interceptor.inspect(request_with_tools(2))
        self.assertEqual(first.reason, "before_tool_step_3")
        self.assertEqual(second.reason, "before_tool_step_3")
        self.assertEqual(calls, [])

    def test_repeated_request_is_deduplicated(self) -> None:
        calls = []

        def decide(request, prompt):
            calls.append((request, prompt))
            return bridge_result(0.40)

        interceptor = ChatRequestInterceptor(
            gate_and_advise=decide, model="local-model"
        )
        request = request_with_tools(3)
        self.assertEqual(interceptor.inspect(request).reason, "below_threshold")
        self.assertEqual(
            interceptor.inspect(request).reason, "no_new_tool_observation"
        )
        self.assertEqual(len(calls), 1)

    def test_successful_advice_is_injected_without_mutating_input(self) -> None:
        def decide(request, prompt):
            self.assertEqual(request["questions"]["need_advisor"]["type"], "noul")
            self.assertIn("public result 3", prompt)
            return bridge_result(
                0.80,
                advisor_called=True,
                suggestion="[EXTERNAL_ADVISOR_SUGGESTION]\nTry one reversible check.",
            )

        interceptor = ChatRequestInterceptor(
            gate_and_advise=decide, model="local-model"
        )
        request = request_with_tools(3)
        original = deepcopy(request)
        result = interceptor.inspect(request)
        self.assertEqual(request, original)
        self.assertTrue(result.suggestion_injected)
        self.assertTrue(result.advisor_called)
        self.assertEqual(result.request["messages"][-1]["role"], "system")
        self.assertEqual(interceptor.advisor_calls, 1)

    def test_rejected_approval_does_not_inject_or_consume_advisor_call(self) -> None:
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda _request, _prompt: bridge_result(0.80),
            model="local-model",
        )
        result = interceptor.inspect(request_with_tools(3))
        self.assertEqual(result.reason, "advisor_not_called")
        self.assertFalse(result.suggestion_injected)
        self.assertEqual(interceptor.advisor_calls, 0)

    def test_three_call_limit_is_enforced(self) -> None:
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda _request, _prompt: bridge_result(
                0.90,
                advisor_called=True,
                suggestion="[EXTERNAL_ADVISOR_SUGGESTION]\nReversible advice.",
            ),
            model="local-model",
        )
        reasons = []
        for count in range(3, 7):
            reasons.append(interceptor.inspect(request_with_tools(count)).reason)
        self.assertEqual(
            reasons,
            [
                "suggestion_injected",
                "suggestion_injected",
                "suggestion_injected",
                "advisor_call_limit_reached",
            ],
        )
        self.assertEqual(interceptor.advisor_calls, 3)

    def test_sensitive_context_fails_before_callback(self) -> None:
        calls = []
        request = request_with_tools(3)
        request["messages"][-1]["content"] = "Authorization: secret-value"
        interceptor = ChatRequestInterceptor(
            gate_and_advise=lambda request, prompt: calls.append((request, prompt)),
            model="local-model",
        )
        with self.assertRaisesRegex(ValueError, "sensitive content"):
            interceptor.inspect(request)
        self.assertEqual(calls, [])

    def test_harbor_config_contains_no_runtime_credentials(self) -> None:
        config = HarborQwenProxyConfig(model_name="openai/local-qwen")
        agent = config.agent_config(runtime_base_url="http://proxy.invalid/v1")
        self.assertEqual(agent["name"], "qwen-coder")
        self.assertEqual(agent["model_api_key_name"], "ASK_ADVISOR_PROXY_API_KEY")
        self.assertNotIn("api_key", agent)


if __name__ == "__main__":
    unittest.main()
