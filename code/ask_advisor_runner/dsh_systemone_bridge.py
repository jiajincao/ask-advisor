from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
import math

from .dsh_one_shot_flow import OneShotFlowResult
from .dsh_one_shot_bridge import OneShotPromptGrant
from .dsh_session_identity import VerifiedSessionIdentity


SystemOneCall = Callable[[Mapping[str, Any]], Mapping[str, Any]]
AdvisorFlowCall = Callable[..., OneShotFlowResult]


@dataclass(frozen=True)
class SystemOneGateResult:
    p_yes: float
    p_no: float
    label_mass: float
    threshold: float
    triggered: bool


@dataclass(frozen=True)
class DshSystemOneBridgeResult:
    gate: SystemOneGateResult
    dsh_started: bool
    flow: OneShotFlowResult | None
    score_only: bool = False


class DshSystemOneBridge:
    """One-use fail-closed gate in front of an existing DSH Advisor flow.

    The injected System One transport owns networking. This class accepts no
    URL, Cookie, Token, port, or credential path. It starts the DSH flow only
    after a valid native response meets the frozen probability threshold.
    """

    def __init__(
        self,
        *,
        system_one_call: SystemOneCall,
        advisor_flow_call: AdvisorFlowCall,
        threshold: float = 0.70,
    ) -> None:
        if not callable(system_one_call) or not callable(advisor_flow_call):
            raise TypeError("System One bridge transports must be callable")
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise ValueError("System One bridge rejected: invalid_threshold")
        self._system_one_call = system_one_call
        self._advisor_flow_call = advisor_flow_call
        self._threshold = float(threshold)
        self._used = False

    @staticmethod
    def _probability(value: Any, label: str) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise RuntimeError(f"System One bridge rejected: invalid_{label}")
        return float(value)

    @classmethod
    def parse_gate(cls, response: Any, threshold: float) -> SystemOneGateResult:
        if not isinstance(response, Mapping):
            raise RuntimeError("System One bridge rejected: invalid_response")
        answers = response.get("answers")
        diagnostics = response.get("diagnostics")
        if not isinstance(answers, Mapping) or not isinstance(diagnostics, Mapping):
            raise RuntimeError("System One bridge rejected: missing_result_sections")
        answer = answers.get("need_advisor")
        diagnostic = diagnostics.get("need_advisor")
        if not isinstance(answer, Mapping) or not isinstance(diagnostic, Mapping):
            raise RuntimeError("System One bridge rejected: missing_need_advisor")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping):
            raise RuntimeError("System One bridge rejected: missing_probabilities")
        p_yes = cls._probability(probabilities.get("yes"), "p_yes")
        p_no = cls._probability(probabilities.get("no"), "p_no")
        label_mass = cls._probability(diagnostic.get("label_mass"), "label_mass")
        if not math.isclose(p_yes + p_no, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise RuntimeError("System One bridge rejected: probabilities_not_normalized")
        if diagnostic.get("argmax_is_label") is not True:
            raise RuntimeError("System One bridge rejected: argmax_not_label")
        return SystemOneGateResult(
            p_yes=p_yes,
            p_no=p_no,
            label_mass=label_mass,
            threshold=threshold,
            triggered=p_yes >= threshold,
        )

    def run_once(
        self,
        *,
        system_one_request: Mapping[str, Any],
        verified_session: VerifiedSessionIdentity,
        grant: OneShotPromptGrant,
        request_id: str,
        prompt: str,
    ) -> DshSystemOneBridgeResult:
        if self._used:
            raise RuntimeError("System One bridge rejected: bridge_replayed")
        self._used = True
        if not isinstance(system_one_request, Mapping):
            raise TypeError("System One bridge rejected: request_mapping_required")
        gate = self.parse_gate(self._system_one_call(system_one_request), self._threshold)
        if not gate.triggered:
            return DshSystemOneBridgeResult(gate=gate, dsh_started=False, flow=None)
        flow = self._advisor_flow_call(
            verified_session=verified_session,
            grant=grant,
            request_id=request_id,
            prompt=prompt,
        )
        if not isinstance(flow, OneShotFlowResult):
            raise RuntimeError("System One bridge rejected: invalid_flow_result")
        return DshSystemOneBridgeResult(gate=gate, dsh_started=True, flow=flow)

