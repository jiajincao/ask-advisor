from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import ExperimentConfig, OfflineRunner  # noqa: E402


class OfflineRunnerTests(unittest.TestCase):
    def test_group_a_cannot_build_advisor_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = OfflineRunner(Path(directory))
            state = runner.create_run("task-one", "A", 1)
            event_id = runner.record_observable_event(
                state,
                step_number=3,
                action_type="test_validate",
                summary="A new failing test result was observed.",
                is_new_information=True,
            )
            runner.record_system_one_result(
                state,
                event_id=event_id,
                p_yes=0.9,
                p_no=0.1,
                label_mass=0.9,
                threshold=0.7,
            )
            with self.assertRaisesRegex(ValueError, "system_one_threshold_not_met"):
                runner.build_advisor_preview(state, event_id, "Minimal public context")

    def test_sensitive_context_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = OfflineRunner(Path(directory))
            state = runner.create_run("task-two", "C", 1)
            with self.assertRaisesRegex(ValueError, "sensitive content"):
                runner.build_advisor_preview(state, "event-1", "Authorization: secret")

    def test_unknown_probability_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-three", "C", 1)
            runner.record_system_one_result(
                state,
                event_id="event-1",
                p_yes=None,
                p_no=0.2,
                label_mass=0.9,
            )
            record = json.loads((root / state.run_id / "system_one.jsonl").read_text().splitlines()[0])
            self.assertEqual(record["decision_status"], "unknown")
            self.assertFalse(record["effect_trigger_allowed"])

    def test_non_normalized_probabilities_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-probability", "C", 1)
            runner.record_system_one_result(
                state,
                event_id="event-1",
                p_yes=0.8,
                p_no=0.8,
                label_mass=0.9,
                threshold=0.7,
            )
            record = json.loads((root / state.run_id / "system_one.jsonl").read_text().splitlines()[0])
            self.assertEqual(record["decision_status"], "unknown")
            self.assertFalse(record["effect_trigger_allowed"])

    def test_threshold_controls_c_but_group_a_remains_shadow_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state_c = runner.create_run("task-threshold", "C", 1)
            runner.record_system_one_result(
                state_c,
                event_id="event-c",
                p_yes=0.8,
                p_no=0.2,
                label_mass=0.9,
                threshold=0.7,
            )
            c_record = json.loads(
                (root / state_c.run_id / "system_one.jsonl").read_text().splitlines()[0]
            )
            self.assertTrue(c_record["would_trigger"])
            self.assertTrue(c_record["effect_trigger_allowed"])

            state_a = runner.create_run("task-threshold", "A", 1)
            runner.record_system_one_result(
                state_a,
                event_id="event-a",
                p_yes=0.8,
                p_no=0.2,
                label_mass=0.9,
                threshold=0.7,
            )
            a_record = json.loads(
                (root / state_a.run_id / "system_one.jsonl").read_text().splitlines()[0]
            )
            self.assertTrue(a_record["would_trigger"])
            self.assertFalse(a_record["effect_trigger_allowed"])

    def test_dedup_inflight_rearm_and_call_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = OfflineRunner(Path(directory))
            state = runner.create_run("task-state", "C", 1)

            state.mark_request_started("event-1", 3)
            with self.assertRaisesRegex(ValueError, "request_in_flight"):
                state.mark_request_started("event-2", 3)
            state.mark_confirmed_send()

            with self.assertRaisesRegex(ValueError, "event_already_handled"):
                state.mark_request_started("event-1", 3)
            with self.assertRaisesRegex(ValueError, "awaiting_substantive_progress"):
                state.mark_request_started("event-2", 3)

            state.record_progress(substantive=False)
            with self.assertRaisesRegex(ValueError, "awaiting_substantive_progress"):
                state.mark_request_started("event-2", 3)
            state.record_progress(substantive=True)
            state.mark_request_started("event-2", 3)
            state.mark_unknown_send()

            state.record_progress(substantive=True)
            state.mark_request_started("event-3", 3)
            state.mark_confirmed_send()
            self.assertEqual(state.advisor_calls, 3)

            state.record_progress(substantive=True)
            with self.assertRaisesRegex(ValueError, "advisor_call_limit_reached"):
                state.mark_request_started("event-4", 3)

    def test_native_systemone_response_is_parsed_without_aux_triggering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-native", "C", 1)
            response = {
                "answers": {
                    "need_advisor": {
                        "type": "noul",
                        "noul": 0.5621765008857981,
                        "probabilities": {
                            "yes": 0.5621765008857981,
                            "no": 0.4378234991142019,
                        },
                        "confidence": 0.5563926983938249,
                    }
                },
                "usage": {"input_tokens": 130, "output_tokens": 1},
                "diagnostics": {
                    "need_advisor": {
                        "label_mass": 0.9897117675981477,
                        "argmax_is_label": True,
                    }
                },
            }
            runner.record_system_one_response(
                state,
                event_id="event-native",
                response=response,
                threshold=0.7,
                latency_ms=1794.211,
                aux_stalled_result=True,
                aux_high_risk_result=True,
            )
            record = json.loads(
                (root / state.run_id / "system_one.jsonl").read_text().splitlines()[0]
            )
            self.assertAlmostEqual(record["p_yes"], 0.5621765008857981)
            self.assertFalse(record["would_trigger"])
            self.assertFalse(record["effect_trigger_allowed"])
            self.assertTrue(record["aux_stalled_result"])
            self.assertTrue(record["aux_high_risk_result"])

    def test_verified_k8_response_schema_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-k8-schema", "C", 1)
            response = {
                "id": "decision-redacted",
                "object": "structured_decision",
                "model": "qwen3.8-27b-nvfp4-stage0",
                "answers": {
                    "need_advisor": {
                        "type": "noul",
                        "noul": 0.7981867825403176,
                        "probabilities": {
                            "yes": 0.7981867825403176,
                            "no": 0.2018132174596823,
                        },
                        "confidence": 0.7940736149421067,
                    }
                },
                "usage": {"input_tokens": 97, "output_tokens": 1},
                "diagnostics": {
                    "need_advisor": {
                        "label_mass": 0.9948468607997738,
                        "argmax_is_label": True,
                    }
                },
            }
            runner.record_system_one_response(
                state,
                event_id="event-k8-schema",
                response=response,
                threshold=0.7,
                latency_ms=384.0,
            )
            record = json.loads(
                (root / state.run_id / "system_one.jsonl").read_text().splitlines()[0]
            )
            self.assertAlmostEqual(record["p_yes"], 0.7981867825403176)
            self.assertAlmostEqual(record["p_no"], 0.2018132174596823)
            self.assertAlmostEqual(record["label_mass"], 0.9948468607997738)
            self.assertAlmostEqual(
                record["confidence_for_diagnostics_only"], 0.7940736149421067
            )
            self.assertTrue(record["effect_trigger_allowed"])

    def test_malformed_native_response_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-malformed", "C", 1)
            runner.record_system_one_response(
                state,
                event_id="event-malformed",
                response={"answers": {"need_advisor": {"confidence": 0.99}}},
                threshold=0.7,
            )
            record = json.loads(
                (root / state.run_id / "system_one.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(record["decision_status"], "unknown")
            self.assertFalse(record["effect_trigger_allowed"])
            with self.assertRaisesRegex(ValueError, "system_one_threshold_not_met"):
                runner.build_advisor_preview(state, "event-malformed", "Public context")

    def test_c_preview_requires_main_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = OfflineRunner(Path(directory))
            state = runner.create_run("task-preview", "C", 1)
            runner.record_system_one_result(
                state,
                event_id="event-low",
                p_yes=0.6,
                p_no=0.4,
                label_mass=0.9,
                threshold=0.7,
            )
            with self.assertRaisesRegex(ValueError, "system_one_threshold_not_met"):
                runner.build_advisor_preview(state, "event-low", "Public context")

            runner.record_system_one_result(
                state,
                event_id="event-high",
                p_yes=0.8,
                p_no=0.2,
                label_mass=0.9,
                threshold=0.7,
            )
            preview = runner.build_advisor_preview(
                state, "event-high", "Public context only"
            )
            self.assertEqual(preview["event_id"], "event-high")
            self.assertEqual(preview["transport"], "DISABLED_STAGE0")

    def test_call_limit_is_exactly_three(self) -> None:
        with self.assertRaisesRegex(ValueError, "must remain 3"):
            ExperimentConfig(advisor_call_limit=4).validate()

    def test_transport_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = OfflineRunner(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "intentionally disabled"):
                runner.send()

    def test_state_can_recover_after_inflight_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = OfflineRunner(Path(directory))
            state = runner.create_run("task-recovery", "C", 1)
            event_id = runner.record_observable_event(
                state,
                step_number=3,
                action_type="diagnose_reproduce",
                summary="A new observable failure was recorded.",
                is_new_information=True,
            )
            runner.record_system_one_result(
                state,
                event_id=event_id,
                p_yes=0.8,
                p_no=0.2,
                label_mass=0.9,
                threshold=0.7,
            )
            runner.mark_request_started(state, event_id)

            recovered = runner.recover_run(state.run_id)
            self.assertTrue(recovered.request_in_flight)
            self.assertIn(event_id, recovered.handled_event_ids)
            with self.assertRaisesRegex(ValueError, "request_in_flight"):
                runner.mark_request_started(recovered, "event-other")

    def test_recovery_ignores_only_a_truncated_final_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-truncated", "A", 1)
            state_log = root / state.run_id / "state.jsonl"
            with state_log.open("a", encoding="utf-8") as handle:
                handle.write('{"event_type":"state_checkpoint"')
            recovered = runner.recover_run(state.run_id)
            self.assertEqual(recovered.run_id, state.run_id)

    def test_checksum_manifest_is_exclusive_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = OfflineRunner(root)
            state = runner.create_run("task-checksum", "A", 1)
            manifest = runner.write_checksum_manifest(state)
            entries = manifest.read_text().splitlines()
            self.assertTrue(any(line.endswith("  events.jsonl") for line in entries))
            self.assertTrue(any(line.endswith("  state.jsonl") for line in entries))
            with self.assertRaises(FileExistsError):
                runner.write_checksum_manifest(state)


if __name__ == "__main__":
    unittest.main()
