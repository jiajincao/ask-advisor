from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from collections.abc import Sequence
from types import TracebackType


_URL_PATTERN = re.compile(r"https?://[^\s]+")


class ManagedDshProcess:
    """Run one DSH process with credential-bearing output kept in memory."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        startup_timeout: float = 15.0,
        stop_timeout: float = 5.0,
    ) -> None:
        if (
            not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise ValueError("DSH managed process rejected: invalid_command")
        if (
            not isinstance(startup_timeout, (int, float))
            or isinstance(startup_timeout, bool)
            or startup_timeout <= 0
            or not isinstance(stop_timeout, (int, float))
            or isinstance(stop_timeout, bool)
            or stop_timeout <= 0
        ):
            raise ValueError("DSH managed process rejected: invalid_timeout")
        self._command = tuple(command)
        self._startup_timeout = float(startup_timeout)
        self._stop_timeout = float(stop_timeout)
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | BaseException | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._bootstrap_consumed = False

    def __enter__(self) -> ManagedDshProcess:
        if self._process is not None:
            raise RuntimeError("DSH managed process rejected: already_started")
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                close_fds=True,
            )
        except BaseException as exc:
            raise RuntimeError("DSH managed process failed: start_failed") from exc
        self._process = process
        self._reader = threading.Thread(
            target=self._read_output,
            name="dsh-startup-reader",
            daemon=True,
        )
        self._reader.start()
        return self

    def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._lines.put(RuntimeError("missing_stdout"))
            return
        try:
            for line in process.stdout:
                self._lines.put(line)
        except BaseException as exc:
            self._lines.put(exc)
        finally:
            self._lines.put(None)

    def bootstrap_url(self) -> str:
        if self._process is None:
            raise RuntimeError("DSH managed process rejected: not_started")
        if self._bootstrap_consumed:
            raise RuntimeError("DSH managed process rejected: bootstrap_already_consumed")
        self._bootstrap_consumed = True
        deadline = time.monotonic() + self._startup_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("DSH managed process failed: startup_timeout")
                try:
                    item = self._lines.get(timeout=remaining)
                except queue.Empty as exc:
                    raise RuntimeError("DSH managed process failed: startup_timeout") from exc
                if item is None:
                    raise RuntimeError("DSH managed process failed: exited_before_ready")
                if isinstance(item, BaseException):
                    raise RuntimeError("DSH managed process failed: startup_read_failed") from item
                match = _URL_PATTERN.search(item)
                if match is not None:
                    return match.group(0).rstrip(".,;)")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=self._stop_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=self._stop_timeout)
                    except subprocess.TimeoutExpired:
                        pass
                except BaseException:
                    try:
                        process.kill()
                    except BaseException:
                        pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except BaseException:
                    pass
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=self._stop_timeout)
        while True:
            try:
                self._lines.get_nowait()
            except queue.Empty:
                break

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(running={self.running!r}, "
            "command='<redacted>', output='<redacted>')"
        )
