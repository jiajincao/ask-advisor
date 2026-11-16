from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ask_advisor_runner import ManagedDshProcess  # noqa: E402


def child_command(source: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source)


class ManagedDshProcessTests(unittest.TestCase):
    def test_reads_bootstrap_once_and_stops_on_context_exit(self) -> None:
        runtime = ManagedDshProcess(
            command=child_command(
                "import time; print('ready http://127.0.0.1:43123/?token=synthetic_secret', flush=True); time.sleep(30)"
            ),
            startup_timeout=2,
            stop_timeout=1,
        )
        with runtime:
            value = runtime.bootstrap_url()
            self.assertEqual(
                value,
                "http://127.0.0.1:43123/?token=synthetic_secret",
            )
            self.assertTrue(runtime.running)
            self.assertNotIn("synthetic_secret", repr(runtime))
            with self.assertRaisesRegex(RuntimeError, "bootstrap_already_consumed"):
                runtime.bootstrap_url()
        self.assertFalse(runtime.running)

    def test_startup_timeout_stops_process_and_redacts_output(self) -> None:
        runtime = ManagedDshProcess(
            command=child_command("import time; print('sensitive-line', flush=True); time.sleep(30)"),
            startup_timeout=0.1,
            stop_timeout=1,
        )
        with runtime:
            with self.assertRaisesRegex(RuntimeError, "startup_timeout") as caught:
                runtime.bootstrap_url()
            self.assertNotIn("sensitive-line", str(caught.exception))
            self.assertFalse(runtime.running)

    def test_early_exit_is_fail_closed(self) -> None:
        runtime = ManagedDshProcess(
            command=child_command("print('not-ready', flush=True)"),
            startup_timeout=2,
            stop_timeout=1,
        )
        with runtime:
            with self.assertRaisesRegex(RuntimeError, "exited_before_ready"):
                runtime.bootstrap_url()
        self.assertFalse(runtime.running)

    def test_close_before_bootstrap_stops_process(self) -> None:
        runtime = ManagedDshProcess(
            command=child_command("import time; time.sleep(30)"),
            startup_timeout=2,
            stop_timeout=1,
        )
        runtime.__enter__()
        self.assertTrue(runtime.running)
        runtime.close()
        runtime.close()
        self.assertFalse(runtime.running)

    def test_invalid_command_is_rejected_before_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_command"):
            ManagedDshProcess(command=())


if __name__ == "__main__":
    unittest.main()
