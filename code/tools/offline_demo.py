from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from ask_advisor_runner import OfflineRunner  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = (
        PROJECT_ROOT / "output" / f"stage0-offline-demo-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    output_root.mkdir(parents=True, exist_ok=False)

    runner = OfflineRunner(output_root)

    group_a = runner.create_run("offline-demo-task", "A", 1)
    a_event = runner.record_observable_event(
        group_a,
        step_number=3,
        action_type="diagnose_reproduce",
        summary="A simulated test produced a new observable error.",
        is_new_information=True,
    )
    runner.record_system_one_result(
        group_a,
        event_id=a_event,
        p_yes=0.82,
        p_no=0.18,
        label_mass=0.91,
        threshold=0.70,
    )
    try:
        runner.build_advisor_preview(group_a, a_event, "Public simulated context only.")
    except ValueError as error:
        group_a_outcome = str(error)
    else:
        raise AssertionError("group A unexpectedly created an Advisor preview")

    group_c = runner.create_run("offline-demo-task", "C", 1)
    c_event_unknown = runner.record_observable_event(
        group_c,
        step_number=3,
        action_type="test_validate",
        summary="A simulated response omitted the yes probability.",
        is_new_information=True,
    )
    runner.record_system_one_result(
        group_c,
        event_id=c_event_unknown,
        p_yes=None,
        p_no=0.30,
        label_mass=0.80,
        threshold=0.70,
    )
    c_event_valid = runner.record_observable_event(
        group_c,
        step_number=4,
        action_type="diagnose_reproduce",
        summary="A different simulated error appeared after a code change.",
        is_new_information=True,
    )
    runner.record_system_one_result(
        group_c,
        event_id=c_event_valid,
        p_yes=0.86,
        p_no=0.14,
        label_mass=0.93,
        threshold=0.70,
    )
    preview = runner.build_advisor_preview(
        group_c,
        c_event_valid,
        "Task public statement, recent observable steps, and a simulated failure summary.",
    )
    try:
        runner.send(preview)
    except RuntimeError as error:
        transport_outcome = str(error)
    else:
        raise AssertionError("offline transport unexpectedly sent data")

    manifest = {
        "purpose": "stage0_offline_demo_only",
        "network_used": False,
        "real_service_used": False,
        "group_a_run_id": group_a.run_id,
        "group_a_outcome": group_a_outcome,
        "group_c_run_id": group_c.run_id,
        "group_c_unknown_event_id": c_event_unknown,
        "group_c_valid_event_id": c_event_valid,
        "group_c_preview": preview,
        "transport_outcome": transport_outcome,
        "output_root": str(output_root.relative_to(PROJECT_ROOT)),
    }
    manifest_path = output_root / "demo-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    checksums = []
    for path in sorted(output_root.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            checksums.append(f"{sha256(path)}  {path.relative_to(output_root)}")
    (output_root / "checksums.sha256").write_text("\n".join(checksums) + "\n")

    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
