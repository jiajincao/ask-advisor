"""Attach content-only evidence to the model request following a command batch."""

from hashlib import sha256

FEEDBACK_FIELD = "ask_advisor_terminus_feedback"


def feedback_digest(content: str) -> str:
    return sha256(content.strip().encode("utf-8")).hexdigest()


class FeedbackChat:
    """Delegate to Harbor's Chat, tagging only the matching feedback prompt."""

    def __init__(self, chat, feedback):
        self._chat = chat
        self._feedback = feedback

    def __getattr__(self, name):
        return getattr(self._chat, name)

    async def chat(self, prompt, **kwargs):
        if prompt == self._feedback:
            extra = dict(kwargs.get("extra_body") or {})
            extra[FEEDBACK_FIELD] = feedback_digest(prompt)
            kwargs["extra_body"] = extra
        return await self._chat.chat(prompt, **kwargs)


def command_feedback_chat(chat, steps, prompt):
    if not steps or not prompt.strip():
        return chat
    step = steps[-1]
    if not any(c.function_name == "bash_command" for c in step.tool_calls or []):
        return chat
    if step.observation and any(r.content == prompt for r in step.observation.results):
        return FeedbackChat(chat, prompt)
    return chat
