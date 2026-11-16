from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner.runtime_entrypoint import (  # noqa: E402
    PinnedDshInvocationFactory,
    latest_bootstrap_url,
)


class RuntimeEntrypointTests(unittest.TestCase):
    def test_latest_bootstrap_url_uses_last_loopback_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dsh.log"
            path.write_text(
                "ready http://127.0.0.1:1111/?token=old\n"
                "ready http://127.0.0.1:2222/?token=new\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            self.assertEqual(
                latest_bootstrap_url(path),
                "http://127.0.0.1:2222/?token=new",
            )

    def test_bootstrap_log_must_be_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dsh.log"
            path.write_text("http://127.0.0.1:1/?token=x\n", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "unsafe_file_permissions"):
                latest_bootstrap_url(path)

    def test_approval_policy_allows_only_exact_advisor_tool(self) -> None:
        from ask_advisor_runner import ApprovalPresentation

        decision = PinnedDshInvocationFactory._approval_policy(
            ApprovalPresentation("ask_advisor", "call", False)
        )
        self.assertEqual(decision.outcome, "allowed-once")
        with self.assertRaisesRegex(RuntimeError, "tool_not_allowed"):
            PinnedDshInvocationFactory._approval_policy(
                ApprovalPresentation("other", "call", False)
            )


if __name__ == "__main__":
    unittest.main()
