from __future__ import annotations

import os
import json
from collections import Counter
from pathlib import Path

from ask_advisor_runner import (
    BrowserAuthenticatedDshSession,
    ManagedDshProcess,
    ReadonlyDshAdapter,
    ReadonlyDshTransportExecutor,
    SessionPinStore,
    dial_loopback_websocket,
    make_json_websocket_dialer,
    verify_session_identity,
)
from run_minimal_dsh_e2e import EXPECTED_ARGUMENTS


def main() -> None:
    dsh_bin = os.environ["ASK_ADVISOR_DSH_BIN"]
    pin_dir = Path(os.environ["ASK_ADVISOR_PIN_DIR"])
    key = (pin_dir / "integrity.key").read_bytes()
    store = SessionPinStore(integrity_key=key)
    key = b""
    pin = store.load(pin_dir / "session-pin.json")
    store.close()
    dialer = make_json_websocket_dialer(
        lambda url, headers: dial_loopback_websocket(url, headers, timeout=60.0)
    )
    with ManagedDshProcess(
        command=[dsh_bin, "web", "--host", "127.0.0.1", "--port", "0", "--no-open"],
        startup_timeout=60.0,
    ) as process:
        session = BrowserAuthenticatedDshSession(
            bootstrap_url=process.bootstrap_url(), websocket_dialer=dialer, timeout=60.0
        )
        try:
            transport = ReadonlyDshTransportExecutor(
                http_post=session.post_json, websocket_connect=session.connect_json
            )
            adapter = ReadonlyDshAdapter(
                unary_call=transport.unary_call, stream_open=transport.stream_open
            )
            matches = []
            for root in adapter.list_root_sessions():
                try:
                    verify_session_identity(pin, root.opaque_id)
                except RuntimeError:
                    continue
                matches.append(root)
            if len(matches) != 1:
                raise RuntimeError("structure inspection rejected: pinned_root_unavailable")
            root = matches[0]
            connection = session.connect_json("/api/remote.mux")
            stream_id = os.urandom(12).hex()
            try:
                connection.send_json(
                    {
                        "type": "open",
                        "streamId": stream_id,
                        "endpoint": "session/follow",
                        "payload": {
                            "args": {
                                "request": {
                                    "address": {"kind": "session", "sessionId": root.opaque_id},
                                    "maxMessages": 50,
                                }
                            }
                        },
                    }
                )
                frame = connection.receive_json()
                value = frame.get("value", {})
                counts = Counter()
                event_types = []
                exact_calls = 0
                exact_call_ids = set()
                exact_results = 0
                for record in value.get("records", []):
                    event = record.get("event", {}) if isinstance(record, dict) else {}
                    event_type = event.get("type")
                    if isinstance(event_type, str):
                        counts[event_type] += 1
                        event_types.append(event_type)
                    data = event.get("data", {}) if isinstance(event, dict) else {}
                    if event_type == "tool/call" and isinstance(data, dict):
                        try:
                            arguments = json.loads(data.get("arguments", ""))
                        except (TypeError, ValueError):
                            arguments = None
                        if data.get("name") == "ask_advisor" and arguments == EXPECTED_ARGUMENTS:
                            exact_calls += 1
                            if isinstance(data.get("callId"), str):
                                exact_call_ids.add(data["callId"])
                    if event_type == "tool/result" and isinstance(data, dict):
                        message = data.get("message", {})
                        source = message.get("source", {}) if isinstance(message, dict) else {}
                        if source.get("callId") in exact_call_ids:
                            exact_results += 1
                print(f"running={str(bool(root.running)).lower()}")
                print(f"exact_ask_advisor_calls={exact_calls}")
                print(f"exact_ask_advisor_results={exact_results}")
                print("tail_event_types=" + ",".join(event_types[-20:]))
                for event_type in sorted(counts):
                    print(f"event_type={event_type} count={counts[event_type]}")
            finally:
                connection.send_json({"type": "cancel", "streamId": stream_id})
                connection.close()
        finally:
            session.close()


if __name__ == "__main__":
    main()
