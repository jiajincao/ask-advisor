from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from test_chat_interceptor import bridge_result
from ask_advisor_runner.chat_interceptor import TerminusChatInterceptor
from ask_advisor_runner.terminus_feedback import FEEDBACK_FIELD, command_feedback_chat


class TerminusFeedbackTest(IsolatedAsyncioTestCase):
    async def test_batch_feedback_reaches_gate_once_and_retries_keep_advice(self):
        calls = []

        def gate(request, prompt):
            calls.append(request["state"]["tool_step"])
            return bridge_result(.8, advisor_called=True,
                                 suggestion="[EXTERNAL_ADVISOR_SUGGESTION]\nCheck the output.")

        interceptor = TerminusChatInterceptor(gate_and_advise=gate, model="offline")
        chat = SimpleNamespace(chat=AsyncMock())
        for i in range(1, 4):
            prompt = f"public feedback {i}"
            step = SimpleNamespace(tool_calls=[SimpleNamespace(function_name="bash_command")] * 3,
                                   observation=SimpleNamespace(results=[SimpleNamespace(content=prompt)]))
            self.assertIs(command_feedback_chat(chat, [step], "ordinary message"), chat)
            wrapper = command_feedback_chat(chat, [step], prompt)
            await wrapper.chat(prompt)
            field = chat.chat.call_args.kwargs["extra_body"]
            request = {**field, "messages": [{"role": "user", "content": prompt}]}
            result = interceptor.inspect(request)
            self.assertNotIn(FEEDBACK_FIELD, result.request)
            self.assertEqual(result.suggestion_injected, i == 3)
            retry = interceptor.inspect(request)
            self.assertEqual(retry.request, result.request)
            self.assertFalse(retry.advisor_called)
        self.assertEqual(calls, [3])
        self.assertIsNone(interceptor.inspect({"messages": request["messages"]}).gate)

    async def test_score_only_records_high_score_without_advice(self):
        from ask_advisor_runner.runtime_composition import ScoreOnlyCoordinator
        from test_runtime_composition import systemone

        audit = []
        scorer = ScoreOnlyCoordinator(lambda request: systemone(.95), audit.append)
        interceptor = TerminusChatInterceptor(gate_and_advise=scorer, model="offline")
        chat = SimpleNamespace(chat=AsyncMock())
        for i in range(1, 4):
            prompt = f"public feedback {i}"
            step = SimpleNamespace(tool_calls=[SimpleNamespace(function_name="bash_command")],
                                   observation=SimpleNamespace(results=[SimpleNamespace(content=prompt)]))
            await command_feedback_chat(chat, [step], prompt).chat(prompt)
            request = {**chat.chat.call_args.kwargs["extra_body"],
                       "messages": [{"role": "user", "content": prompt}]}
            for _ in range(2):
                result = interceptor.inspect(request)
                self.assertFalse(result.advisor_called)
                self.assertFalse(result.suggestion_injected)
                self.assertEqual(len(audit), int(i == 3))
        self.assertTrue(audit[0]["triggered"])
        self.assertFalse(audit[0]["dsh_started"])
        self.assertEqual(audit[0]["p_yes"], .95)
