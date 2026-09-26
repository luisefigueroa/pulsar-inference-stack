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
import sys

# code: (what is wrong, the one next step). {spec} and {placement} are filled
# from the selected spec and its node arguments.
BLOCKERS = {
    "topology_incomplete": ("the confirmed topology has fewer nodes than this spec needs",
                            "./pulsar topology configure"),
    "node_unreachable": ("the node is unreachable over SSH", "./pulsar topology check"),
    "docker_unavailable": ("Docker is unavailable or could not confirm the pinned image", "./pulsar doctor"),
    "image_missing": ("the pinned image is missing", "./pulsar start {spec} {placement}--pull-image"),
    "model_files_not_ready": ("model files are not prepared on every serving rank",
                              "./pulsar model prepare {spec} {placement}--yes (acquire or restore first if no home exists)"),
    "memory_insufficient": ("not enough free memory for this spec", "./pulsar inventory (then stop other GPU services)"),
    "memory_warning": ("free memory is within the warning margin",
                       "./pulsar start {spec} {placement}--accept-memory-warn"),
    "memory_check_failed": ("the memory check could not complete", "./pulsar start {spec} {placement}--verbose"),
    "service_exists": ("a service for this spec already exists",
                       "./pulsar status {spec} {placement}(pass --replace only with explicit replacement approval)"),
    "preflight_failed": ("the multi-node preflight failed", "./pulsar doctor"),
}


def blocker(code: str, *, spec: str = "", placement: str = "", node: str | None = None,
            rank: int | None = None, detail: str | None = None) -> dict:
    if code not in BLOCKERS:
        raise ValueError(f"unknown start blocker: {code}")
    summary, fix = BLOCKERS[code]
    placement = placement.strip() + " " if placement.strip() else ""
    fix = " ".join(fix.format(spec=spec[:12] or "SPEC", placement=placement).split())
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
    args = parser.parse_args(argv)
    value = blocker(args.code, spec=args.spec, placement=args.placement, node=args.node,
                    rank=args.rank, detail=args.detail)
    print(human(value))
    target = os.environ.get("PULSAR_START_BLOCKERS_FILE")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
