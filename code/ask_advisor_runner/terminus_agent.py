"""Harbor custom agent import path: ask_advisor_runner.terminus_agent:AskAdvisorTerminus2."""

from harbor.agents.terminus_2.terminus_2 import Terminus2

from .terminus_feedback import command_feedback_chat


class AskAdvisorTerminus2(Terminus2):
    async def _query_llm(self, chat, prompt, original_instruction="", session=None):
        if chat is self._chat:
            chat = command_feedback_chat(chat, self._trajectory_steps, prompt)
        return await super()._query_llm(chat, prompt, original_instruction, session)
