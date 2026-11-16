from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import ReadonlyDshAdapter  # noqa: E402


class ReadonlyDshAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unary_calls: list[tuple[str, dict[str, Any]]] = []
        self.stream_calls: list[tuple[str, dict[str, Any]]] = []

        def unary(endpoint: str, args: dict[str, Any]):
            self.unary_calls.append((endpoint, args))
            return {
                "items": [
                    {
                        "sessionId": "root-old",
                        "updatedAt": 10,
                        "running": False,
                        "blank": False,
                        "projections": {
                            "values": {"sessionListMetadata": {"lastPromptAt": 10}}
                        },
                    },
                    {
                        "sessionId": "root-latest",
                        "updatedAt": 30,
                        "running": False,
                        "blank": False,
                        "projections": {
                            "values": {"sessionListMetadata": {"lastPromptAt": 20}}
                        },
                    },
                    {
                        "sessionId": "root-blank",
                        "updatedAt": 40,
                        "running": False,
                        "blank": True,
                    },
                    {
                        "sessionId": "child",
                        "updatedAt": 50,
                        "running": False,
                        "blank": False,
                        "parentSessionId": "root-old",
                        "origin": "subagent",
                    },
                ]
            }

        def stream(endpoint: str, args: dict[str, Any]):
            self.stream_calls.append((endpoint, args))
            return iter(
                [
                    {
                        "type": "snapshot",
                        "cursor": 99,
                        "records": [
                            {
                                "type": "event",
                                "event": {
                                    "type": "assistant/message",
                                    "seq": 98,
                                    "time": 1,
                                    "data": {"secret_body": "must-not-be-projected"},
                                },
                            },
                            {
                                "type": "event",
                                "event": {
                                    "type": "turn/end",
                                    "seq": 99,
                                    "time": 2,
                                    "data": {},
                                },
                            },
                        ],
                        "hasMore": True,
                        "projections": {"asOfSeq": 99, "values": {}},
                    },
                    {"type": "event", "event": {"type": "future", "data": {}}},
                ]
            )

        self.adapter = ReadonlyDshAdapter(unary_call=unary, stream_open=stream)

    def test_only_reviewed_list_payload_is_used(self) -> None:
        roots = self.adapter.list_root_sessions()
        self.assertEqual(self.unary_calls, [("session/list", {"_request": {}})])
        self.assertEqual([root.opaque_id for root in roots], ["root-old", "root-latest", "root-blank"])

    def test_latest_nonblank_root_is_selected(self) -> None:
        selected = self.adapter.select_latest_nonblank_root(self.adapter.list_root_sessions())
        self.assertEqual(selected.opaque_id, "root-latest")

    def test_follow_projects_structure_without_event_data(self) -> None:
        root = self.adapter.select_latest_nonblank_root(self.adapter.list_root_sessions())
        summary = self.adapter.follow_once(root)
        self.assertEqual(
            self.stream_calls,
            [
                (
                    "session/follow",
                    {
                        "request": {
                            "address": {"kind": "session", "sessionId": "root-latest"},
                            "maxMessages": 1,
                        }
                    },
                )
            ],
        )
        self.assertEqual(summary.record_count, 2)
        self.assertTrue(summary.cursor_present)
        self.assertTrue(summary.has_more)
        self.assertTrue(summary.projection_present)
        self.assertEqual(
            summary.event_type_counts,
            (("assistant/message", 1), ("turn/end", 1)),
        )
        self.assertNotIn("secret_body", repr(summary))

    def test_follow_rejects_more_than_one_message(self) -> None:
        root = self.adapter.select_latest_nonblank_root(self.adapter.list_root_sessions())
        with self.assertRaisesRegex(ValueError, "max_messages_must_equal_one"):
            self.adapter.follow_once(root, max_messages=2)

    def test_all_write_capabilities_are_disabled(self) -> None:
        for method in (
            self.adapter.create_session,
            self.adapter.send_prompt,
            self.adapter.answer_approval,
            self.adapter.call_advisor,
        ):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(RuntimeError, "intentionally disabled"):
                    method()


if __name__ == "__main__":
    unittest.main()
