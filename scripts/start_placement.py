#!/usr/bin/env python3
"""May an ordinary Start run a multi-node recipe on nodes off the default order?

    python3 scripts/start_placement.py check --topology FILE --spec FILE NODE_ID...

The one place that decides it. The default order (role rank r on saved rank r)
is not judged here: Start always uses it. An ordinary Start runs a multi-node
recipe on the nodes the operator names, in that order (the first named runs
the API), when they are saved members cabled as the recipe needs; the launch
check (require_profile_topology in cluster/topology.sh) decides the cabling
from the saved links and refuses an uncabled placement before any node is
contacted. This decision refuses only what the launch check does not judge: a
guarded spec, which runs only as a guarded trial, and a node that is not a
saved member. `check` prints nothing and exits 0 when the placement is
accepted, or prints the reason and exits 1.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.topology_manifest import RING_CLASS  # noqa: E402  (the saved shape's name)


def ordinary_start_refusal(topology: dict, node_ids: list[str], *, guarded: bool) -> str | None:
    """Why an ordinary Start may not run a recipe on NODE_IDS (role order) off
    the default order, or None when it may; the launch check then judges the
    cabling."""
    nodes = topology.get("topology", topology).get("nodes") or []
    rank_of = {node["node_id"]: node["rank"] for node in nodes}
    if guarded:
        return "a guarded recipe runs only as a guarded trial"
    if any(node not in rank_of for node in node_ids):
        return "a named node is not in the saved cluster"
    return None


def cabling_refusal(topology: dict, node_ids: list[str], *, loop: bool) -> str | None:
    """Why the launch check refuses NODE_IDS (role order) for their cabling, read
    from the saved links, in its own words; None when every pair the recipe
    needs is cabled. LOOP: the recipe passes data around a closed loop of its
    nodes (NCCL_ALGO=Ring), so it needs each node cabled to the next and the
    last to the first; any other recipe needs every pair cabled. Start itself
    asks the launch check (require_profile_topology in cluster/topology.sh),
    which also counts each pair's links; this reading is for pages that offer
    a Start without running it, so they never offer one the check refuses for
    its cabling."""
    from scripts.topology_manifest import ring_loop_refusal, ring_refusal
    document = topology.get("topology", topology)
    rank_of = {node["node_id"]: node["rank"] for node in document.get("nodes") or []}
    hostname = {node["node_id"]: node.get("hostname") or node["node_id"] for node in document.get("nodes") or []}
    if any(node not in rank_of for node in node_ids):
        return "a named node is not in the saved cluster"
    cabled = {tuple(sorted(link["ranks"])) for link in document.get("links") or []}
    count = len(node_ids)
    if loop:
        pairs = [(node_ids[index], node_ids[(index + 1) % count]) for index in range(count if count > 2 else 1)]
    else:
        pairs = [(node_ids[a], node_ids[b]) for a in range(count) for b in range(a + 1, count)]
    ring = (document.get("validation") or {}).get("class") == RING_CLASS
    for first, second in pairs:
        if tuple(sorted((rank_of[first], rank_of[second]))) not in cabled:
            if not ring:
                return f"{hostname[first]} and {hostname[second]} share no link in the saved cluster"
            words = ring_loop_refusal if loop else ring_refusal
            return words(hostname[first], hostname[second])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--topology", required=True)
    check.add_argument("--spec", required=True)
    check.add_argument("node_ids", nargs="+")
    args = parser.parse_args(argv)
    topology = json.loads(Path(args.topology).read_text(encoding="utf-8"))
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    guarded = bool(((spec.get("recipe") or {}).get("container") or {}).get("guard"))
    reason = ordinary_start_refusal(topology, args.node_ids, guarded=guarded)
    if reason is None:
        return 0
    print(reason)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
