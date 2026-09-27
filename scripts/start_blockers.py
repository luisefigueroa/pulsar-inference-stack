#!/usr/bin/env python3
"""Start blockers: one stable code, the affected object and one fix each.

`pulsar start` records every blocker it finds through `record`. The human line
and the JSON record come from this one catalog, so they cannot drift apart.
`start --json` returns the records in `error.details`; `pulsar contract --json`
publishes the codes. Recording a blocker never probes a node.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

# code: (what is wrong, the one next step). {spec} is the selected spec,
# {placement} its node arguments, {candidate} its --spec-file and {start_args}
# every argument that shapes the effective recipe (--spec-file,
# --override-file, memory estimate), so following a fix never changes which
# recipe runs.
BLOCKERS = {
    "topology_incomplete": ("the confirmed topology has fewer nodes than this spec needs",
                            "./pulsar topology configure"),
    "node_unreachable": ("the node is unreachable over SSH", "./pulsar topology check"),
    "docker_unavailable": ("Docker is unavailable or could not confirm the pinned image", "./pulsar doctor"),
    "image_missing": ("the pinned image is missing", "./pulsar start {spec} {start_args}{placement}--pull-image"),
    "model_files_not_ready": ("model files are not prepared on every serving rank",
                              "./pulsar model prepare {spec} {candidate}{placement}--yes (acquire or restore first if no home exists)"),
    "memory_insufficient": ("not enough free memory for this spec", "./pulsar inventory (then stop other GPU services)"),
    "memory_warning": ("free memory is within the warning margin",
                       "./pulsar start {spec} {start_args}{placement}--accept-memory-warn"),
    "memory_check_failed": ("the memory check could not complete", "./pulsar start {spec} {start_args}{placement}--verbose"),
    "service_exists": ("a service for this spec already exists",
                       "./pulsar status {spec} {candidate}{placement}(pass --replace only with explicit replacement approval)"),
    "preflight_failed": ("the multi-node preflight failed", "./pulsar doctor"),
    # The one blocker no ./pulsar command resolves: this Stack validates guard
    # documents but has no guard execution, so the fix names the guard docs.
    "guard_unsupported": ("this spec requires serving-guard enforcement (recipe.container.guard), "
                          "which this Stack cannot run",
                          "no start is possible from this Stack; see docs/SERVING_GUARD_SCHEMA.md"),
    # Schema-1 records stay readable and stoppable; no launch compiler exists.
    "historical_spec": ("this is a historical schema-1 spec, which this Stack reads but cannot start",
                        "no start is possible from this Stack; use a schema-2 or 3 spec for this model (see docs/OPERATIONS.md)"),
}


def _args(*pairs: tuple[str, str | None]) -> str:
    return "".join(f"{flag} {shlex.quote(value)} " for flag, value in pairs if value)


def blocker(code: str, *, spec: str = "", placement: str = "", node: str | None = None,
            rank: int | None = None, detail: str | None = None, spec_file: str | None = None,
            override_file: str | None = None, memory_estimate_file: str | None = None,
            memory_estimate_id: str | None = None) -> dict:
    if code not in BLOCKERS:
        raise ValueError(f"unknown start blocker: {code}")
    summary, fix = BLOCKERS[code]
    placement = placement.strip() + " " if placement.strip() else ""
    candidate = _args(("--spec-file", spec_file))
    start_args = candidate + _args(("--override-file", override_file),
                                   ("--memory-estimate-file", memory_estimate_file),
                                   ("--memory-estimate-id", memory_estimate_id))
    # A candidate spec file needs the complete ID; catalog prefixes do not cover it.
    shown = (spec if spec_file else spec[:12]) or "SPEC"
    fix = " ".join(fix.format(spec=shown, placement=placement, candidate=candidate,
                              start_args=start_args).split())
    where = f"{node} (rank {rank})" if node and rank is not None else (node or "")
    message = f"{where}: {summary}" if where else summary
    if detail:
        message += f" ({detail})"
    return {"field": "blocker", "blocker": code, "node": node, "rank": rank,
            "message": message, "fix": fix}


def human(record: dict) -> str:
    return f"BLOCKED {record['blocker']}: {record['message']}. Next: {record['fix']}"


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
    record.add_argument("--rank", type=int)
    record.add_argument("--detail")
    for flag in ("--spec-file", "--override-file", "--memory-estimate-file", "--memory-estimate-id"):
        record.add_argument(flag)
    args = parser.parse_args(argv)
    value = blocker(args.code, spec=args.spec, placement=args.placement, node=args.node,
                    rank=args.rank, detail=args.detail, spec_file=args.spec_file,
                    override_file=args.override_file, memory_estimate_file=args.memory_estimate_file,
                    memory_estimate_id=args.memory_estimate_id)
    print(human(value))
    target = os.environ.get("PULSAR_START_BLOCKERS_FILE")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
