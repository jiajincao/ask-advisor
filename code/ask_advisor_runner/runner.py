from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
import hashlib
import json
import math

from .audit import AppendOnlyJsonl
from .config import ExperimentConfig, Group
from .identifiers import new_run_id, new_scoped_id
from .redaction import require_clean
from .state import RunState


class OfflineRunner:
    """State and audit skeleton with no transport implementation."""

    def __init__(self, output_root: Path, config: ExperimentConfig | None = None) -> None:
        self.output_root = output_root
        self.config = config or ExperimentConfig()
        self.config.validate()

    def _run_root(self, run_id: str) -> Path:
        return self.output_root / run_id

    def _checkpoint_state(self, state: RunState, reason: str) -> None:
        AppendOnlyJsonl(self._run_root(state.run_id) / "state.jsonl").append(
            {
                "event_type": "state_checkpoint",
                "reason": reason,
                "state": asdict(state),
            }
        )

    def create_run(self, task_id: str, group: Group, repeat: int) -> RunState:
        run_id = new_run_id(task_id, group, repeat)
        state = RunState(run_id=run_id, task_id=task_id, group=group, repeat=repeat)
        audit = AppendOnlyJsonl(self.output_root / run_id / "events.jsonl")
        audit.append(
            {
                "event_type": "run_created_offline",
                "run": asdict(state),
                "config": asdict(self.config),
            }
        )
        self._checkpoint_state(state, "run_created")
        return state

    def recover_run(self, run_id: str) -> RunState:
        """Recover the latest complete append-only state checkpoint."""
        path = self._run_root(run_id) / "state.jsonl"
        if not path.is_file():
            raise ValueError("recovery rejected: state_log_missing")
        lines = path.read_text(encoding="utf-8").splitlines()
        latest: dict[str, Any] | None = None
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    raise ValueError("recovery rejected: corrupt_state_log") from None
                continue
            state_record = record.get("state") if isinstance(record, dict) else None
            if isinstance(state_record, dict):
                latest = state_record
        if latest is None:
            raise ValueError("recovery rejected: no_complete_state_checkpoint")
        state = RunState.from_record(latest)
        if state.run_id != run_id:
            raise ValueError("recovery rejected: run_id_mismatch")
        return state

    def record_observable_event(
        self,
        state: RunState,
        *,
        step_number: int,
        action_type: str,
        summary: str,
        is_new_information: bool,
        substantive_progress: bool = False,
    ) -> str:
        require_clean(summary)
        state.record_progress(substantive=substantive_progress)
        event_id = new_scoped_id("event")
        eligible = step_number >= self.config.detection_start_tool_step and is_new_information
        AppendOnlyJsonl(self.output_root / state.run_id / "events.jsonl").append(
            {
                "run_id": state.run_id,
                "event_id": event_id,
                "event_type": "observable_event",
                "step_number": step_number,
                "action_type": action_type,
                "summary": summary,
                "is_new_information": is_new_information,
                "substantive_progress": substantive_progress,
                "eligible_for_detection": eligible,
                "ineligible_reason": None if eligible else "before_step_3_or_no_new_information",
            }
        )
        self._checkpoint_state(state, "observable_event_recorded")
        return event_id

    def record_system_one_result(
        self,
        state: RunState,
        *,
        event_id: str,
        p_yes: float | None,
        p_no: float | None,
        label_mass: float | None,
        threshold: float | None = None,
        argmax_is_label: bool | None = None,
        confidence: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: float | None = None,
        aux_stalled_result: bool | None = None,
        aux_high_risk_result: bool | None = None,
    ) -> str:
        detection_id = new_scoped_id("detection")
        probabilities_valid = all(
            value is not None
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0.0 <= value <= 1.0
            for value in (p_yes, p_no, label_mass)
        )
        normalized = bool(
            probabilities_valid
            and p_yes is not None
            and p_no is not None
            and math.isclose(p_yes + p_no, 1.0, rel_tol=0.0, abs_tol=1e-6)
        )
        threshold_valid = threshold is None or (
            isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
            and math.isfinite(threshold)
            and 0.0 <= threshold <= 1.0
        )
        valid = probabilities_valid and normalized and threshold_valid
        decision_status = "unknown" if not valid else ("yes" if p_yes >= p_no else "no")
        would_trigger = bool(valid and threshold is not None and p_yes is not None and p_yes >= threshold)
        AppendOnlyJsonl(self.output_root / state.run_id / "system_one.jsonl").append(
            {
                "run_id": state.run_id,
                "event_id": event_id,
                "detection_id": detection_id,
                "p_yes": p_yes,
                "p_no": p_no,
                "label_mass": label_mass,
                "argmax_is_label": argmax_is_label,
                "confidence_for_diagnostics_only": confidence,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "aux_stalled_result": aux_stalled_result,
                "aux_high_risk_result": aux_high_risk_result,
                "threshold": threshold,
                "probabilities_normalized": normalized,
                "decision_status": decision_status,
                "would_trigger": would_trigger,
                "effect_trigger_allowed": would_trigger and state.group == "C",
            }
        )
        if would_trigger and state.group == "C":
            state.trigger_eligible_event_ids.add(event_id)
        self._checkpoint_state(state, "system_one_result_recorded")
        return detection_id

    def record_system_one_response(
        self,
        state: RunState,
        *,
        event_id: str,
        response: Any,
        threshold: float | None,
        latency_ms: float | None = None,
        aux_stalled_result: bool | None = None,
        aux_high_risk_result: bool | None = None,
    ) -> str:
        """Parse the native endpoint fail-closed; never use confidence as p_yes."""
        answer: Any = None
        diagnostics: Any = None
        usage: Any = None
        if isinstance(response, dict):
            answers = response.get("answers")
            all_diagnostics = response.get("diagnostics")
            usage = response.get("usage")
            if isinstance(answers, dict):
                answer = answers.get("need_advisor")
            if isinstance(all_diagnostics, dict):
                diagnostics = all_diagnostics.get("need_advisor")

        probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
        p_yes = probabilities.get("yes") if isinstance(probabilities, dict) else None
        p_no = probabilities.get("no") if isinstance(probabilities, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        label_mass = diagnostics.get("label_mass") if isinstance(diagnostics, dict) else None
        argmax_is_label = (
            diagnostics.get("argmax_is_label") if isinstance(diagnostics, dict) else None
        )
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
        return self.record_system_one_result(
            state,
            event_id=event_id,
            p_yes=p_yes,
            p_no=p_no,
            label_mass=label_mass,
            threshold=threshold,
            argmax_is_label=argmax_is_label,
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            aux_stalled_result=aux_stalled_result,
            aux_high_risk_result=aux_high_risk_result,
        )

    def build_advisor_preview(self, state: RunState, event_id: str, context: str) -> dict[str, Any]:
        """Create an offline preview only; it cannot send or approve anything."""
        require_clean(context)
        if event_id not in state.trigger_eligible_event_ids:
            raise ValueError("preview rejected: system_one_threshold_not_met")
        allowed, reason = state.may_trigger(event_id, self.config.advisor_call_limit)
        if not allowed:
            raise ValueError(f"preview rejected: {reason}")
        return {
            "run_id": state.run_id,
            "trigger_id": new_scoped_id("trigger"),
            "event_id": event_id,
            "context": context,
            "transport": "DISABLED_STAGE0",
            "approval": "NOT_REQUESTED",
        }

    def mark_request_started(self, state: RunState, event_id: str) -> None:
        state.mark_request_started(event_id, self.config.advisor_call_limit)
        self._checkpoint_state(state, "advisor_request_started_offline")

    def mark_confirmed_send(self, state: RunState) -> None:
        state.mark_confirmed_send()
        self._checkpoint_state(state, "advisor_send_confirmed_offline")

    def mark_unknown_send(self, state: RunState) -> None:
        state.mark_unknown_send()
        self._checkpoint_state(state, "advisor_send_unknown_offline")

    def write_checksum_manifest(self, state: RunState) -> Path:
        """Create one immutable SHA-256 manifest for the current run directory."""
        run_root = self._run_root(state.run_id)
        manifest_path = run_root / "checksums.sha256"
        entries: list[str] = []
        for path in sorted(run_root.rglob("*")):
            if not path.is_file() or path == manifest_path:
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append(f"{digest.hexdigest()}  {path.relative_to(run_root)}")
        with manifest_path.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(entries) + "\n")
        return manifest_path

    def send(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("network transport is intentionally disabled in the stage-0 skeleton")
