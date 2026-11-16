from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
import math

from .dsh_one_shot_bridge import OneShotPromptGrant, prompt_sha256
from .dsh_one_shot_flow import OneShotAdvisorFlow
from .dsh_session_identity import VerifiedSessionIdentity
from .dsh_systemone_bridge import DshSystemOneBridge, DshSystemOneBridgeResult


SystemOneCall = Callable[[Mapping[str, Any]], Mapping[str, Any]]
AuditRecord = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class AdvisorInvocation:
    """One fully reviewed, one-use DSH invocation bundle."""

    verified_session: VerifiedSessionIdentity
    grant: OneShotPromptGrant
    request_id: str
    flow: OneShotAdvisorFlow

    def validate_for_prompt(self, prompt: str) -> None:
        if self.verified_session.alias != self.grant.session_alias:
            raise RuntimeError("runtime composition rejected: session_alias_mismatch")
        if self.grant.expected_prompt_sha256 != prompt_sha256(prompt):
            raise RuntimeError("runtime composition rejected: prompt_hash_mismatch")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("runtime composition rejected: invalid_request_id")


InvocationFactory = Callable[[str], AdvisorInvocation]


class ScoreOnlyCoordinator:
    """Score eligible feedback without constructing a DSH invocation."""

    def __init__(self, system_one_call: SystemOneCall, audit_record: AuditRecord) -> None:
        self._score = system_one_call
        self._audit = audit_record

    def __call__(self, request: Mapping[str, Any], _prompt: str) -> DshSystemOneBridgeResult:
        try:
            gate = DshSystemOneBridge.parse_gate(self._score(request), 0.70)
        except Exception:
            self._audit({"event_type": "bridge_failed", "status": "score_only_failed",
                         "content_persisted": False})
            raise
        self._audit({"event_type": "bridge_completed", "status": "score_only",
                     "p_yes": gate.p_yes, "p_no": gate.p_no, "label_mass": gate.label_mass,
                     "argmax_is_label": True, "threshold": gate.threshold,
                     "triggered": gate.triggered, "dsh_started": False,
                     "advisor_called": False, "content_persisted": False})
        return DshSystemOneBridgeResult(gate, False, None, score_only=True)


class RuntimeBridgeCoordinator:
    """Create one fresh System-One/DSH bridge for each eligible observation.

    The invocation factory owns DSH authentication and must return a newly
    verified one-use session reference, prompt grant, and Advisor flow. This
    coordinator owns no URLs, ports, credentials, or sessions.
    """

    def __init__(
        self,
        *,
        system_one_call: SystemOneCall,
        invocation_factory: InvocationFactory,
        audit_record: AuditRecord,
        threshold: float = 0.70,
    ) -> None:
        if not callable(system_one_call) or not callable(invocation_factory):
            raise TypeError("runtime composition requires callable transports")
        if not callable(audit_record):
            raise TypeError("runtime composition requires audit callback")
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise ValueError("runtime composition rejected: invalid_threshold")
        self._system_one_call = system_one_call
        self._invocation_factory = invocation_factory
        self._audit_record = audit_record
        self._threshold = float(threshold)

    def __call__(
        self,
        system_one_request: Mapping[str, Any],
        advisor_prompt: str,
    ) -> DshSystemOneBridgeResult:
        invocation = self._invocation_factory(advisor_prompt)
        if not isinstance(invocation, AdvisorInvocation):
            raise RuntimeError("runtime composition rejected: invalid_invocation")
        invocation.validate_for_prompt(advisor_prompt)
        bridge = DshSystemOneBridge(
            system_one_call=self._system_one_call,
            advisor_flow_call=invocation.flow.run_once,
            threshold=self._threshold,
        )
        try:
            result = bridge.run_once(
                system_one_request=system_one_request,
                verified_session=invocation.verified_session,
                grant=invocation.grant,
                request_id=invocation.request_id,
                prompt=advisor_prompt,
            )
        except BaseException:
            self._audit_record(
                {
                    "event_type": "bridge_failed",
                    "content_persisted": False,
                }
            )
            raise
        record = {
            "event_type": "bridge_completed",
            "p_yes": result.gate.p_yes,
            "p_no": result.gate.p_no,
            "label_mass": result.gate.label_mass,
            "threshold": result.gate.threshold,
            "triggered": result.gate.triggered,
            "dsh_started": result.dsh_started,
            "advisor_called": bool(result.flow and result.flow.advisor_called),
            "status": result.flow.status if result.flow else "not_started",
            "content_persisted": False,
        }
        self._audit_record(record)
        return result


@dataclass(frozen=True)
class DeploymentCheck:
    """Content-free readiness result; values and addresses are never returned."""

    ok: bool
    required_inputs_present: bool
    activation_disabled: bool
    threshold: float
    advisor_call_limit: int
    detection_start_tool_step: int


_REQUIRED_ENV = (
    "ASK_ADVISOR_MODEL",
    "ASK_ADVISOR_PROXY_BIND_HOST",
    "ASK_ADVISOR_PROXY_BIND_PORT",
    "ASK_ADVISOR_PROXY_API_KEY",
    "ASK_ADVISOR_VLLM_BASE_URL",
    "ASK_ADVISOR_DSH_BIN",
    "ASK_ADVISOR_PIN_DIR",
)


def check_deployment_environment(env: Mapping[str, str]) -> DeploymentCheck:
    """Validate presence and frozen policy without opening files or sockets."""

    if not isinstance(env, Mapping):
        raise TypeError("deployment check requires environment mapping")
    present = all(isinstance(env.get(name), str) and bool(env[name]) for name in _REQUIRED_ENV)
    activation_disabled = env.get("ASK_ADVISOR_ENABLE_RUNTIME") != "1"
    return DeploymentCheck(
        ok=present and activation_disabled,
        required_inputs_present=present,
        activation_disabled=activation_disabled,
        threshold=0.70,
        advisor_call_limit=3,
        detection_start_tool_step=3,
    )
