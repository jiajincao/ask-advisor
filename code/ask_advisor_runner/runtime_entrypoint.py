from __future__ import annotations

import os
import re
import signal
import stat
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from .audit import AppendOnlyJsonl
from .bridge import ApprovalDecision
from .chat_interceptor import ChatRequestInterceptor, TerminusChatInterceptor
from .dsh_advisor_result_reader import OneShotAdvisorResultReader
from .dsh_approval_transport import ApprovalPresentation, OneShotApprovalChannel
from .dsh_browser_session import BrowserAuthenticatedDshSession
from .dsh_one_shot_bridge import OneShotPromptGate, OneShotPromptGrant, prompt_sha256
from .dsh_one_shot_flow import OneShotAdvisorFlow
from .dsh_prompt_transport import OneShotPromptTransportExecutor
from .dsh_readonly import ReadonlyDshAdapter
from .dsh_runtime_transport import make_json_websocket_dialer
from .dsh_session_identity import SessionIdentityPin, verify_session_identity
from .dsh_session_pin_store import SessionPinStore
from .dsh_stdlib_websocket import dial_loopback_websocket
from .dsh_transport import ReadonlyDshTransportExecutor
from .proxy_http import OpenAIChatProxy
from .proxy_runtime import AuthenticatedProxyServer, StdlibVllmTransport
from .runtime_composition import AdvisorInvocation, RuntimeBridgeCoordinator, ScoreOnlyCoordinator


_BOOTSTRAP_PATTERN = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):[^\s]+")


def _regular_private_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("runtime startup failed: required_file_unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("runtime startup rejected: unsafe_file_permissions")
    if metadata.st_size < 1 or metadata.st_size > max_bytes:
        raise RuntimeError("runtime startup rejected: invalid_file_size")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RuntimeError("runtime startup failed: required_file_unreadable") from exc


def latest_bootstrap_url(log_path: Path) -> str:
    raw = _regular_private_file(log_path, max_bytes=8 * 1024 * 1024)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("runtime startup rejected: invalid_bootstrap_log") from exc
    matches = _BOOTSTRAP_PATTERN.findall(text)
    text = ""
    if not matches:
        raise RuntimeError("runtime startup failed: bootstrap_url_not_found")
    return matches[-1].rstrip(".,;)")


class _ClosingAdvisorFlow:
    def __init__(
        self,
        *,
        flow: OneShotAdvisorFlow,
        session: BrowserAuthenticatedDshSession,
    ) -> None:
        self._flow = flow
        self._session = session
        self._used = False

    def run_once(self, **kwargs: Any):
        if self._used:
            raise RuntimeError("runtime DSH flow rejected: flow_replayed")
        self._used = True
        try:
            return self._flow.run_once(**kwargs)
        finally:
            self._session.close()


class PinnedDshInvocationFactory:
    """Build a fresh authenticated, one-use DSH flow for one eligible prompt."""

    def __init__(self, *, bootstrap_log: Path, pin_dir: Path) -> None:
        self._bootstrap_log = Path(bootstrap_log)
        self._pin_dir = Path(pin_dir)

    def _load_pin(self) -> SessionIdentityPin:
        key = _regular_private_file(self._pin_dir / "integrity.key", max_bytes=4096)
        store = SessionPinStore(integrity_key=key)
        key = b""
        try:
            return store.load(self._pin_dir / "session-pin.json")
        finally:
            store.close()

    @staticmethod
    def _approval_policy(presentation: ApprovalPresentation) -> ApprovalDecision:
        if presentation.tool_name != "ask_advisor":
            raise RuntimeError("runtime approval rejected: tool_not_allowed")
        return ApprovalDecision(
            outcome="allowed-once",
            reason="one_shot_exact_tool_match",
            approval_request_id="ephemeral-redacted",
            call_id=presentation.call_id,
            request_hash="0" * 64,
        )

    def __call__(self, prompt: str) -> AdvisorInvocation:
        pin = self._load_pin()
        bootstrap_url = latest_bootstrap_url(self._bootstrap_log)
        session = BrowserAuthenticatedDshSession(
            bootstrap_url=bootstrap_url,
            websocket_dialer=make_json_websocket_dialer(dial_loopback_websocket),
            timeout=30.0,
        )
        bootstrap_url = ""
        try:
            executor = ReadonlyDshTransportExecutor(
                http_post=session.post_json,
                websocket_connect=session.connect_json,
            )
            roots = ReadonlyDshAdapter(
                unary_call=executor.unary_call,
                stream_open=executor.stream_open,
            ).list_root_sessions()
            matches: list[str] = []
            for root in roots:
                try:
                    verify_session_identity(pin, root.opaque_id).close()
                except RuntimeError:
                    continue
                matches.append(root.opaque_id)
            if len(matches) != 1:
                raise RuntimeError("runtime DSH rejected: pinned_root_unavailable")
            session_id = matches[0]
            prompt_gate = OneShotPromptGate(
                transport=OneShotPromptTransportExecutor(http_post=session.post_json)
            )
            approval_channel = OneShotApprovalChannel(
                http_post=session.post_json,
                websocket_connect=session.connect_json,
                expected_agent_id=session_id,
            )
            result_reader = OneShotAdvisorResultReader(
                websocket_connect=session.connect_json,
                session_id=session_id,
            )
            flow = OneShotAdvisorFlow(
                prompt_gate=prompt_gate,
                approval_channel=approval_channel,
                approval_policy=self._approval_policy,
                result_reader=result_reader,
            )
            verified = verify_session_identity(pin, session_id)
            session_id = ""
            return AdvisorInvocation(
                verified_session=verified,
                grant=OneShotPromptGrant(pin.alias, prompt_sha256(prompt)),
                request_id=uuid4().hex,
                flow=_ClosingAdvisorFlow(flow=flow, session=session),  # type: ignore[arg-type]
            )
        except BaseException:
            session.close()
            raise


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError("runtime startup failed: required_environment_missing")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise RuntimeError("runtime startup rejected: invalid_bind_port") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("runtime startup rejected: invalid_bind_port")
    return port


def main() -> int:
    if os.environ.get("ASK_ADVISOR_ENABLE_RUNTIME") != "1":
        raise RuntimeError("runtime startup rejected: activation_disabled")
    model = _required_env("ASK_ADVISOR_MODEL")
    bind_host = _required_env("ASK_ADVISOR_PROXY_BIND_HOST")
    bind_port = _port(_required_env("ASK_ADVISOR_PROXY_BIND_PORT"))
    proxy_key = _required_env("ASK_ADVISOR_PROXY_API_KEY")
    vllm_base_url = _required_env("ASK_ADVISOR_VLLM_BASE_URL")
    pin_dir = Path(_required_env("ASK_ADVISOR_PIN_DIR"))
    bootstrap_log = Path(_required_env("ASK_ADVISOR_DSH_BOOTSTRAP_LOG"))
    audit_log = Path(_required_env("ASK_ADVISOR_AUDIT_LOG"))

    audit = AppendOnlyJsonl(audit_log)
    transport = StdlibVllmTransport(base_url=vllm_base_url, timeout_sec=1800.0)
    mode = os.environ.get("ASK_ADVISOR_RUNTIME_MODE", "advisor")
    if mode == "score-only":
        coordinator = ScoreOnlyCoordinator(transport.systemone, audit.append)
    elif mode == "advisor":
        coordinator = RuntimeBridgeCoordinator(
            system_one_call=transport.systemone,
            invocation_factory=PinnedDshInvocationFactory(
                bootstrap_log=bootstrap_log,
                pin_dir=pin_dir,
            ),
            audit_record=audit.append,
            threshold=0.70,
        )
    else:
        raise ValueError("runtime startup rejected: unknown_runtime_mode")
    harness = os.environ.get("ASK_ADVISOR_HARNESS", "qwen-coder")
    interceptors = {"qwen-coder": ChatRequestInterceptor, "terminus-2": TerminusChatInterceptor}
    if harness not in interceptors:
        raise ValueError("runtime startup rejected: unknown_harness")
    interceptor = interceptors[harness](
        gate_and_advise=coordinator,
        model=model,
        threshold=0.70,
        detection_start_tool_step=3,
        advisor_call_limit=3,
    )
    application = OpenAIChatProxy(
        interceptor=interceptor,
        upstream_chat_call=transport.chat,
        enabled=True,
    )
    server = AuthenticatedProxyServer(
        application=application,
        bind_host=bind_host,
        bind_port=bind_port,
        bearer_token=proxy_key,
        enabled=True,
        delivery_record=audit.append,
    )
    return serve_until_stopped(server, transport)


def serve_until_stopped(server: AuthenticatedProxyServer, transport: StdlibVllmTransport) -> int:
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.start()
        print("ASK_ADVISOR_PROXY=RUNNING", flush=True)
        stopped.wait()
    finally:
        server.close()
        transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
