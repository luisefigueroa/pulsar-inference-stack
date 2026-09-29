#!/usr/bin/env python3
"""Start blockers: one stable code, the affected object and one next step each.

`pulsar start` records every blocker it finds through `record`. The human line
and the JSON record come from this one catalog, so they cannot drift apart.
`start --json` returns the records in `error.details`; `pulsar contract --json`
publishes the codes. Recording a blocker never probes a node; it reads only
local catalog records to choose between download and prepare.

`fix` is always one command that can be pasted as written, or null when no
command resolves the blocker. Any explanation goes in `note`. Codes with
stage "check" refused the start; codes with stage "launch" describe a service
that was started (or removed) after the checks passed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Operator start flags repeated in suggested start commands, so following a
# suggestion keeps a dry run dry and keeps permissions already granted.
CARRIED_START_FLAGS = ("--dry-run", "--pull-image", "--accept-memory-warn", "--replace", "--verbose",
                       "--skip-preflight")
START = "./pulsar start {spec} {start_args}"
STOP = "./pulsar stop {spec} {candidate}{placement}"
# Added to a check-stage note when --replace already removed the previous
# service; scripts/lib.sh uses the same sentence for launch refusals.
AFTER_REPLACE_NOTE = "The previous service was already removed, so nothing is running for this spec now."


@dataclass(frozen=True)
class Kind:
    summary: str
    command: str | None
    note: str | None = None
    adds: str | None = None  # the start flag this fix adds
    stage: str = "check"
    # A launch record that can say nothing runs once --replace removed the
    # previous service; check-stage records always can.
    nothing_runs: bool = False


BLOCKERS = {
    "topology_incomplete": Kind("the confirmed topology has fewer nodes than this spec needs",
                                "./pulsar topology setup"),
    "fabric_incomplete": Kind("the confirmed fabric does not meet this spec's requirements",
                              "./pulsar topology check"),
    "node_unreachable": Kind("the node is unreachable over SSH", "./pulsar topology check"),
    "docker_unavailable": Kind("Docker is unavailable or could not confirm the pinned image", "./pulsar doctor"),
    "image_missing": Kind("the pinned image is missing", START, adds="--pull-image"),
    "image_check_failed": Kind("the image check could not complete", START, adds="--verbose"),
    "model_files_not_ready": Kind("model files are not prepared on every serving rank",
                                  "./pulsar model prepare {spec} {candidate}{placement}--yes"),
    "model_files_check_failed": Kind("the model-files check could not complete", START, adds="--verbose"),
    "memory_insufficient": Kind("not enough free memory for this spec", "./pulsar inventory",
                                note="Stop GPU services it lists that are no longer needed, or choose a smaller spec."),
    "memory_warning": Kind("free memory is within the warning margin", START, adds="--accept-memory-warn"),
    "memory_check_failed": Kind("the memory check could not complete", START, adds="--verbose"),
    "service_exists": Kind("a service for this spec already exists",
                           "./pulsar status {spec} {candidate}{placement}",
                           note="To replace it, rerun start with --replace, only with explicit replacement approval."),
    "port_in_use": Kind("the service port is already in use", "./pulsar inventory",
                        note="Free the port, or change the deployment port."),
    "preflight_failed": Kind("the multi-node preflight failed", START, adds="--verbose"),
    # No ./pulsar command resolves these two; the note says what to use instead.
    "guard_unsupported": Kind("this spec requires serving-guard enforcement (recipe.container.guard), "
                              "which this Stack cannot run", None,
                              note="No start is possible from this Stack; see docs/SERVING_GUARD_SCHEMA.md."),
    "historical_spec": Kind("this is a historical schema-1 spec, which this Stack reads but cannot start", None,
                            note="No start is possible from this Stack; use a schema-2 or 3 spec for this model "
                                 "(see docs/OPERATIONS.md)."),
    # After the checks passed, when containers were started.
    "container_start_failed": Kind("the service container could not be started", "./pulsar doctor",
                                   note="Docker's error is shown above.", stage="launch", nothing_runs=True),
    "smoke_test_failed": Kind("the service started and passed its health check, but the test completion failed; "
                              "it is still running", STOP, stage="launch"),
    "health_timeout": Kind("the service did not become healthy in time", STOP,
                           note="It is still running; its logs are shown above.", stage="launch"),
    "container_exited": Kind("the service container exited before it became healthy", STOP,
                             note="Stop clears the exited container; its logs are shown above.", stage="launch"),
    "service_stopped": Kind("the service was removed before it became healthy, for example by pulsar stop", None,
                            note="Nothing is running for this spec now.", stage="launch"),
}


def _args(*pairs: tuple[str, str | None]) -> str:
    return "".join(f"{flag} {shlex.quote(value)} " for flag, value in pairs if value)


def _files_command(spec_id: str, shown: str, *, spec_file: str | None, candidate: str, placement: str,
                   home_node: str | None, state_root: str | None) -> tuple[str, str | None] | None:
    """Download (or restore) when a required snapshot has no recorded home.

    Reads only local catalog records. Returns None to keep the prepare command,
    including when the records cannot be read. The new home goes on the start
    placement, or on rank 0 (home_node) for a multi-node spec.
    """
    try:
        from model_library.catalog import entries, project
        from model_library.state import Store
        store = Store(state_root or os.environ.get("PULSAR_MODEL_LIBRARY_DIR") or ROOT / ".model-library")
        if spec_file:
            from release_spec import load_spec
            row = project(load_spec(spec_file), store)
        else:
            row = entries(ROOT, store, spec_id=spec_id)[0]
    except Exception:  # noqa: BLE001 - any unreadable record keeps the prepare suggestion
        return None
    if isinstance(row.get("snapshots"), dict):
        members = row["snapshots"]
    else:
        members = {None: {"home": row.get("home"), "archive": row.get("archive")}}
    missing = [(name, member) for name, member in members.items() if not member.get("home")]
    if not missing:
        return None
    name, member = missing[0]
    where = placement or _args(("--node", home_node))
    snapshot = _args(("--snapshot", name)) if name else ""
    command = f"./pulsar model acquire {shown} {candidate}{snapshot}{where}--yes"
    note = None
    if member.get("archive"):
        note = ("A verified archive exists; restoring it also works: "
                f"./pulsar model restore {shown} {candidate}{snapshot}{where}--yes")
    return command, note


def blocker(code: str, *, spec: str = "", placement: str = "", node: str | None = None,
            node_id: str | None = None, rank: int | None = None, detail: str | None = None,
            spec_file: str | None = None, override_file: str | None = None,
            memory_estimate_file: str | None = None, memory_estimate_id: str | None = None,
            start_flags: str = "", home_node: str | None = None, state_root: str | None = None,
            service_id: str | None = None, no_command: bool = False, note: str | None = None,
            after_replace: bool = False, unconfirmed: str | None = None) -> dict:
    """One blocker record. ``unconfirmed`` names nodes where removing a failed
    launch's containers could not be confirmed; stop then becomes the fix."""
    if code not in BLOCKERS:
        raise ValueError(f"unknown start blocker: {code}")
    kind = BLOCKERS[code]
    placement = placement.strip() + " " if placement.strip() else ""
    candidate = _args(("--spec-file", spec_file))
    carried = [flag for flag in start_flags.split() if flag in CARRIED_START_FLAGS and flag != kind.adds]
    start_args = (candidate
                  + _args(("--override-file", override_file), ("--memory-estimate-file", memory_estimate_file),
                          ("--memory-estimate-id", memory_estimate_id))
                  + placement + "".join(f"{flag} " for flag in carried)
                  + (f"{kind.adds} " if kind.adds else ""))
    # A candidate spec file needs the complete ID; catalog prefixes do not cover it.
    shown = (spec if spec_file else spec[:12]) or "SPEC"
    fix = None
    resolved_note = kind.note
    if kind.command and not no_command:
        fix = kind.command.format(spec=shown, placement=placement, candidate=candidate, start_args=start_args)
        if code == "model_files_not_ready":
            download = _files_command(spec, shown, spec_file=spec_file, candidate=candidate, placement=placement,
                                      home_node=home_node, state_root=state_root)
            if download:
                fix, resolved_note = download
        fix = " ".join(fix.split())
    if note is not None:
        resolved_note = note
    if unconfirmed:
        fix = " ".join(STOP.format(spec=shown, candidate=candidate, placement=placement).split())
        resolved_note = f"Removing its containers could not be confirmed on {unconfirmed}; stop removes what remains."
    elif after_replace and (kind.stage == "check" or kind.nothing_runs):
        resolved_note = f"{resolved_note} {AFTER_REPLACE_NOTE}" if resolved_note else AFTER_REPLACE_NOTE
    where = f"{node} (rank {rank})" if node and rank is not None else (node or "")
    message = f"{where}: {kind.summary}" if where else kind.summary
    if detail:
        message += f" ({detail})"
    return {"field": "blocker", "blocker": code, "stage": kind.stage, "node": node, "node_id": node_id,
            "rank": rank, "message": message, "fix": fix, "note": resolved_note,
            "service_id": service_id}


def human(record: dict) -> str:
    prefix = "FAILED" if record.get("stage") == "launch" else "BLOCKED"
    line = f"{prefix} {record['blocker']}: {record['message']}."
    if record.get("fix"):
        line += f" Next: {record['fix']}"
        if record.get("note"):
            line += f"\n  {record['note']}"
    elif record.get("note"):
        line += f" Note: {record['note']}"
    return line


def read(path: str | Path) -> list[dict]:
    """Recorded blockers in order; a missing file means none were recorded."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("code", choices=sorted(BLOCKERS))
    record.add_argument("--spec", default="")
    record.add_argument("--placement", default="")
    record.add_argument("--node")
    record.add_argument("--node-id")
    record.add_argument("--rank", type=int)
    record.add_argument("--detail")
    record.add_argument("--start-flags", default="")
    record.add_argument("--home-node")
    record.add_argument("--state-root")
    record.add_argument("--service-id")
    record.add_argument("--no-command", action="store_true")
    record.add_argument("--note")
    record.add_argument("--after-replace", action="store_true")
    record.add_argument("--unconfirmed")
    for flag in ("--spec-file", "--override-file", "--memory-estimate-file", "--memory-estimate-id"):
        record.add_argument(flag)
    args = parser.parse_args(argv)
    value = blocker(args.code, spec=args.spec, placement=args.placement, node=args.node or None,
                    node_id=args.node_id or None, rank=args.rank, detail=args.detail or None,
                    spec_file=args.spec_file or None, override_file=args.override_file or None,
                    memory_estimate_file=args.memory_estimate_file or None,
                    memory_estimate_id=args.memory_estimate_id or None, start_flags=args.start_flags,
                    home_node=args.home_node or None, state_root=args.state_root or None,
                    service_id=args.service_id or None, no_command=args.no_command, note=args.note,
                    after_replace=args.after_replace, unconfirmed=args.unconfirmed or None)
    print(human(value))
    target = os.environ.get("PULSAR_START_BLOCKERS_FILE")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
