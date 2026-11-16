from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import (  # noqa: E402
    DshSystemOneBridge,
    OneShotFlowResult,
    OneShotPromptGrant,
    create_session_identity_pin,
    prompt_sha256,
    verify_session_identity,
)


def response(p_yes: float = 0.80, *, argmax_is_label: bool = True):
    return {
        "answers": {
            "need_advisor": {
                "probabilities": {"yes": p_yes, "no": 1.0 - p_yes},
                "confidence": 0.99,
            }
        },
        "diagnostics": {
            "need_advisor": {
                "label_mass": 0.98,
                "argmax_is_label": argmax_is_label,
            }
        },
    }


class DshSystemOneBridgeTests(unittest.TestCase):
    def inputs(self):
        prompt = "Public synthetic request for one Advisor suggestion."
        pin = create_session_identity_pin("synthetic-session")
        return {
            "system_one_request": {"state": "synthetic", "questions": {}},
            "verified_session": verify_session_identity(pin, "synthetic-session"),
            "grant": OneShotPromptGrant(pin.alias, prompt_sha256(prompt)),
            "request_id": "request-1",
            "prompt": prompt,
        }

    def test_below_threshold_never_starts_dsh(self) -> None:
        dsh_calls = []
        bridge = DshSystemOneBridge(
            system_one_call=lambda _request: response(0.4073334000459302),
            advisor_flow_call=lambda **kwargs: dsh_calls.append(kwargs),
        )
        result = bridge.run_once(**self.inputs())
        self.assertFalse(result.gate.triggered)
        self.assertFalse(result.dsh_started)
        self.assertIsNone(result.flow)
        self.assertEqual(dsh_calls, [])

    def test_at_threshold_starts_exactly_one_dsh_flow(self) -> None:
        calls = []

        def flow(**kwargs):
            calls.append(kwargs)
            return OneShotFlowResult("success", True, "suggestion", False)

        bridge = DshSystemOneBridge(
            system_one_call=lambda _request: response(0.70),
            advisor_flow_call=flow,
        )
        result = bridge.run_once(**self.inputs())
        self.assertTrue(result.gate.triggered)
        self.assertTrue(result.dsh_started)
        self.assertTrue(result.flow and result.flow.advisor_called)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(RuntimeError, "bridge_replayed"):
            bridge.run_once(**self.inputs())
        self.assertEqual(len(calls), 1)

    def test_invalid_native_result_fails_closed(self) -> None:
        invalid_results = [
            {},
            response(0.80, argmax_is_label=False),
            {
                "answers": {
                    "need_advisor": {
                        "probabilities": {"yes": 0.80, "no": 0.30}
                    }
                },
                "diagnostics": {
                    "need_advisor": {
                        "label_mass": 0.98,
                        "argmax_is_label": True,
                    }
                },
            },
        ]
        for native in invalid_results:
            with self.subTest(native=native):
                dsh_calls = []
                bridge = DshSystemOneBridge(
                    system_one_call=lambda _request, native=native: native,
                    advisor_flow_call=lambda **kwargs: dsh_calls.append(kwargs),
                )
                with self.assertRaises(RuntimeError):
                    bridge.run_once(**self.inputs())
                self.assertEqual(dsh_calls, [])

    def test_confidence_never_substitutes_for_yes_probability(self) -> None:
        native = response(0.20)
        native["answers"]["need_advisor"]["confidence"] = 0.999
        dsh_calls = []
        bridge = DshSystemOneBridge(
            system_one_call=lambda _request: native,
            advisor_flow_call=lambda **kwargs: dsh_calls.append(kwargs),
        )
        result = bridge.run_once(**self.inputs())
        self.assertFalse(result.gate.triggered)
        self.assertEqual(dsh_calls, [])


if __name__ == "__main__":
    unittest.main()
