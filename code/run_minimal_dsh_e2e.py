from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ask_advisor_runner import (
    ApprovalDecision,
    BrowserAuthenticatedDshSession,
    ManagedDshProcess,
    OneShotAdvisorFlow,
    OneShotAdvisorResultReader,
    OneShotApprovalChannel,
    OneShotPromptGate,
    OneShotPromptGrant,
    OneShotPromptTransportExecutor,
    OneShotToolCallResolver,
    ReadonlyDshAdapter,
    ReadonlyDshTransportExecutor,
    SessionPinStore,
    dial_loopback_websocket,
    make_json_websocket_dialer,
    prompt_sha256,
    verify_session_identity,
)


EXPECTED_ARGUMENTS = {
    "question": "What is the safest next diagnostic step when two public synthetic observations conflict?",
    "goal": "Choose one small, reversible next step that distinguishes the competing explanations.",
    "constraints": "Advice only. Do not request credentials, private data, hidden tests, or state changes.",
    "attempts": "Two synthetic checks were performed; their observations conflict, so an independent reasoning review is requested.",
    "evidence": [],
}


def build_prompt() -> str:
    exact = json.dumps(EXPECTED_ARGUMENTS, ensure_ascii=False, sort_keys=True)
    return (
        "This is an explicitly approved one-shot integration check. "
        "Call ask_advisor exactly once now, use no other tool, and pass exactly "
        "the following JSON object without changing any field or text: "
        f"{exact} After the tool returns, do not perform any action; only acknowledge receipt."
    )


def main() -> None:
    dsh_bin = os.environ.get("ASK_ADVISOR_DSH_BIN")
    pin_dir_value = os.environ.get("ASK_ADVISOR_PIN_DIR")
    if not dsh_bin or not pin_dir_value:
        raise RuntimeError("minimal e2e rejected: missing_runtime_input")
    pin_dir = Path(pin_dir_value)
    integrity_key = (pin_dir / "integrity.key").read_bytes()
    store = SessionPinStore(integrity_key=integrity_key)
    integrity_key = b""
    pin = store.load(pin_dir / "session-pin.json")
    store.close()

    command = [dsh_bin, "web", "--host", "127.0.0.1", "--port", "0", "--no-open"]
    raw_dialer = lambda url, headers: dial_loopback_websocket(  # noqa: E731
        url, headers, timeout=180.0
    )
    websocket_dialer = make_json_websocket_dialer(raw_dialer)

    with ManagedDshProcess(command=command, startup_timeout=60.0) as process:
        bootstrap_url = process.bootstrap_url()
        session = BrowserAuthenticatedDshSession(
            bootstrap_url=bootstrap_url,
            websocket_dialer=websocket_dialer,
            timeout=60.0,
        )
        bootstrap_url = ""
        try:
            readonly_transport = ReadonlyDshTransportExecutor(
                http_post=session.post_json,
                websocket_connect=session.connect_json,
            )
            adapter = ReadonlyDshAdapter(
                unary_call=readonly_transport.unary_call,
                stream_open=readonly_transport.stream_open,
            )
            roots = adapter.list_root_sessions()
            matches = []
            for root in roots:
                try:
                    verified = verify_session_identity(pin, root.opaque_id)
                except RuntimeError:
                    continue
                matches.append((root, verified))
            if len(matches) != 1:
                raise RuntimeError("minimal e2e rejected: pinned_root_unavailable")
            root, verified = matches[0]
            session_id = root.opaque_id

            prompt = build_prompt()
            prompt_transport = OneShotPromptTransportExecutor(http_post=session.post_json)
            prompt_gate = OneShotPromptGate(transport=prompt_transport)
            approval_channel = OneShotApprovalChannel(
                http_post=session.post_json,
                websocket_connect=session.connect_json,
                expected_agent_id=session_id,
            )
            resolver = OneShotToolCallResolver(
                websocket_connect=session.connect_json,
                session_id=session_id,
                expected_arguments=EXPECTED_ARGUMENTS,
            )
            result_reader = OneShotAdvisorResultReader(
                websocket_connect=session.connect_json,
                session_id=session_id,
            )

            def policy(presentation):
                request_hash = resolver.resolve_once(presentation.call_id)
                return ApprovalDecision(
                    outcome="allowed-once",
                    reason="exact_tool_call_verified",
                    approval_request_id="remote-event-redacted",
                    call_id=presentation.call_id,
                    request_hash=request_hash,
                )

            flow = OneShotAdvisorFlow(
                prompt_gate=prompt_gate,
                approval_channel=approval_channel,
                approval_policy=policy,
                result_reader=result_reader,
            )
            result = flow.run_once(
                verified_session=verified,
                grant=OneShotPromptGrant(pin.alias, prompt_sha256(prompt)),
                request_id=os.urandom(16).hex(),
                prompt=prompt,
            )
            suggestion = result.suggestion or ""
            print("prompt_accepted=true")
            print("approval=allowed-once")
            print(f"advisor_status={result.status}")
            print(f"suggestion_bytes={len(suggestion.encode('utf-8'))}")
            print(f"suggestion_sha256={hashlib.sha256(suggestion.encode('utf-8')).hexdigest()}")
            print(f"truncated={str(bool(result.truncated)).lower()}")
        finally:
            session.close()


if __name__ == "__main__":
    main()
