from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
import json
import math
from threading import Lock

from .terminus_feedback import FEEDBACK_FIELD, feedback_digest

from .dsh_systemone_bridge import DshSystemOneBridgeResult, SystemOneGateResult
from .redaction import require_clean


GateAndAdvise = Callable[
    [Mapping[str, Any], str],
    DshSystemOneBridgeResult,
]


@dataclass(frozen=True)
class ToolObservation:
    """One content-bearing tool result visible in an OpenAI chat request."""

    step_number: int
    fingerprint: str
    tool_call_id: str | None
    content: str


@dataclass(frozen=True)
class InterceptionResult:
    """Result of inspecting one repeated OpenAI-compatible chat request."""

    request: dict[str, Any]
    observation: ToolObservation | None
    gate: SystemOneGateResult | None
    advisor_called: bool
    suggestion_injected: bool
    reason: str


class ChatRequestInterceptor:
    """Offline decision core for the future Qwen-to-vLLM proxy.

    Qwen Code sends the whole conversation again before each model turn.  This
    class finds the newest previously unseen tool result, calls an injected
    System-One/DSH coordinator, and (only after a successful Advisor flow)
    appends one bounded external suggestion to a copy of the request.

    Networking, credentials, HTTP serving, DSH sessions, and process startup
    deliberately remain outside this class.
    """

    _BOUNDARY = "[EXTERNAL_ADVISOR_SUGGESTION]"

    def __init__(
        self,
        *,
        gate_and_advise: GateAndAdvise,
        model: str,
        threshold: float = 0.70,
        detection_start_tool_step: int = 3,
        advisor_call_limit: int = 3,
        max_context_chars: int = 24_000,
        max_suggestion_bytes: int = 32_768,
    ) -> None:
        if not callable(gate_and_advise):
            raise TypeError("chat interceptor requires a gate_and_advise callback")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("chat interceptor rejected: invalid_model")
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise ValueError("chat interceptor rejected: invalid_threshold")
        if detection_start_tool_step != 3:
            raise ValueError("chat interceptor rejected: detection_must_start_at_step_3")
        if advisor_call_limit != 3:
            raise ValueError("chat interceptor rejected: advisor_call_limit_must_equal_3")
        if max_context_chars < 1 or max_suggestion_bytes < 1:
            raise ValueError("chat interceptor rejected: invalid_size_limit")

        self._gate_and_advise = gate_and_advise
        self._model = model
        self._threshold = float(threshold)
        self._detection_start_tool_step = detection_start_tool_step
        self._advisor_call_limit = advisor_call_limit
        self._max_context_chars = max_context_chars
        self._max_suggestion_bytes = max_suggestion_bytes
        self._seen: set[str] = set()
        self._advisor_calls = 0

    @property
    def advisor_calls(self) -> int:
        return self._advisor_calls

    @staticmethod
    def _plain_messages(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        messages = request.get("messages")
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ValueError("chat interceptor rejected: messages_list_required")
        result: list[Mapping[str, Any]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("chat interceptor rejected: invalid_message")
            result.append(message)
        return result

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            parts: list[str] = []
            for part in content:
                if isinstance(part, Mapping) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        return ""

    @classmethod
    def _tool_observations(
        cls, messages: Sequence[Mapping[str, Any]]
    ) -> list[ToolObservation]:
        observations: list[ToolObservation] = []
        for message in messages:
            if message.get("role") not in ("tool", "function"):
                continue
            content = cls._content_text(message.get("content"))
            if not content.strip():
                continue
            call_id = message.get("tool_call_id")
            if call_id is not None and not isinstance(call_id, str):
                raise ValueError("chat interceptor rejected: invalid_tool_call_id")
            canonical = json.dumps(
                {
                    "role": message.get("role"),
                    "tool_call_id": call_id,
                    "name": message.get("name"),
                    "content": content,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            observations.append(
                ToolObservation(
                    step_number=len(observations) + 1,
                    fingerprint=sha256(canonical.encode("utf-8")).hexdigest(),
                    tool_call_id=call_id,
                    content=content,
                )
            )
        return observations

    def _system_one_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        observation: ToolObservation,
    ) -> tuple[dict[str, Any], str]:
        context = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        context = context[-self._max_context_chars :]
        require_clean(context)
        state = {
            "task": "Complete the current public terminal task successfully.",
            "goal": "Choose the next terminal action using the available evidence.",
            "tool_step": observation.step_number,
            "recent_conversation": context,
            "uncertainty": (
                "Determine whether one external Advisor suggestion is likely to "
                "materially improve the final task success probability."
            ),
        }
        request = {
            "model": self._model,
            "state": state,
            "questions": {
                "need_advisor": {
                    "type": "noul",
                    "instructions": (
                        "Would one cloud Advisor suggestion now be likely to "
                        "materially improve the final task success probability?"
                    ),
                    "criteria": {
                        "true": "Independent advice is likely to materially improve success.",
                        "false": "The local evidence is already sufficient to continue.",
                    },
                }
            },
        }
        advisor_prompt = (
            "Give one bounded, actionable next-step suggestion for the current "
            "public terminal task. Do not execute tools or request secrets.\n\n"
            + context
        )
        return request, advisor_prompt

    def inspect(self, request: Mapping[str, Any]) -> InterceptionResult:
        if not isinstance(request, Mapping):
            raise TypeError("chat interceptor rejected: request_mapping_required")
        messages = self._plain_messages(request)
        forwarded = deepcopy(dict(request))
        observations = self._tool_observations(messages)
        unseen = [item for item in observations if item.fingerprint not in self._seen]
        if not unseen:
            return InterceptionResult(
                forwarded, None, None, False, False, "no_new_tool_observation"
            )

        # Repeated chat requests contain the full trajectory. Mark every prior
        # observation as seen, but evaluate only the newest one.
        self._seen.update(item.fingerprint for item in observations)
        observation = unseen[-1]
        return self._evaluate(messages, forwarded, observation)

    def _evaluate(self, messages, forwarded, observation):
        if observation.step_number < self._detection_start_tool_step:
            return InterceptionResult(
                forwarded, observation, None, False, False, "before_tool_step_3"
            )
        if self._advisor_calls >= self._advisor_call_limit:
            return InterceptionResult(
                forwarded, observation, None, False, False, "advisor_call_limit_reached"
            )

        system_one_request, advisor_prompt = self._system_one_request(
            messages, observation
        )
        result = self._gate_and_advise(system_one_request, advisor_prompt)
        if not isinstance(result, DshSystemOneBridgeResult):
            raise RuntimeError("chat interceptor rejected: invalid_bridge_result")
        if not math.isclose(
            result.gate.threshold, self._threshold, rel_tol=0.0, abs_tol=1e-12
        ):
            raise RuntimeError("chat interceptor rejected: threshold_mismatch")
        if result.score_only:
            return InterceptionResult(
                forwarded, observation, result.gate, False, False, "score_only"
            )
        if not result.gate.triggered:
            return InterceptionResult(
                forwarded, observation, result.gate, False, False, "below_threshold"
            )
        if result.flow is None or not result.dsh_started:
            raise RuntimeError("chat interceptor rejected: triggered_without_dsh_flow")
        if not result.flow.advisor_called:
            return InterceptionResult(
                forwarded,
                observation,
                result.gate,
                False,
                False,
                "advisor_not_called",
            )
        suggestion = result.flow.suggestion
        if not isinstance(suggestion, str) or not suggestion.strip():
            raise RuntimeError("chat interceptor rejected: missing_suggestion")
        if not suggestion.startswith(self._BOUNDARY):
            raise RuntimeError("chat interceptor rejected: missing_suggestion_boundary")
        if len(suggestion.encode("utf-8")) > self._max_suggestion_bytes:
            raise RuntimeError("chat interceptor rejected: suggestion_too_large")
        require_clean(suggestion)
        forwarded_messages = forwarded.get("messages")
        if not isinstance(forwarded_messages, list):
            raise RuntimeError("chat interceptor rejected: copied_messages_not_list")
        forwarded_messages.append({"role": "system", "content": suggestion})
        self._advisor_calls += 1
        return InterceptionResult(
            forwarded, observation, result.gate, True, True, "suggestion_injected"
        )

class TerminusChatInterceptor(ChatRequestInterceptor):
    """One instance per trial. Count only feedback marked by the harness."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lock = Lock()
        self._last_request_digest = None
        self._last_suggestion = None

    def inspect(self, request: Mapping[str, Any]) -> InterceptionResult:
        forwarded = deepcopy(dict(request))
        if FEEDBACK_FIELD not in forwarded:
            return InterceptionResult(forwarded, None, None, False, False, "no_new_tool_observation")
        marker = forwarded.pop(FEEDBACK_FIELD)
        messages = self._plain_messages(forwarded)
        latest = messages[-1] if messages else {}
        content = self._content_text(latest.get("content"))
        if latest.get("role") != "user" or not content.strip() or marker != feedback_digest(content):
            raise ValueError("chat interceptor rejected: invalid_terminus_feedback")
        key = sha256(json.dumps(forwarded, sort_keys=True).encode()).digest()
        with self._lock:
            if marker in self._seen:
                if key == self._last_request_digest and self._last_suggestion is not None:
                    forwarded["messages"].append(deepcopy(self._last_suggestion))
                    return InterceptionResult(forwarded, None, None, False, True, "suggestion_replayed")
                return InterceptionResult(forwarded, None, None, False, False, "no_new_tool_observation")
            observation = ToolObservation(len(self._seen) + 1, marker, None, content)
            # Reserve before external calls so an ambiguous failure cannot send two prompts.
            self._seen.add(marker)
            self._last_request_digest = key
            self._last_suggestion = None
            result = self._evaluate(messages, forwarded, observation)
            if result.suggestion_injected:
                self._last_suggestion = deepcopy(result.request["messages"][-1])
            return result
