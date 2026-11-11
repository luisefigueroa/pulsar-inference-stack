#!/usr/bin/env python3
"""May an ordinary Start run a multi-node recipe on nodes off the default order?

    python3 scripts/start_placement.py check --topology FILE --spec FILE NODE_ID...

The one place that decides it. The default order (role rank r on saved rank r)
is not judged here: Start always uses it. Today an ordinary Start accepts one
case off it, which the maintainer approved: a two-node recipe on two
neighbouring nodes of a saved ring, named in either order (the first named runs
the API). Every other placement off the default order is refused before
anything is contacted, with a sentence that says why. `check` prints nothing
and exits 0 when the placement is accepted, or prints the reason and exits 1.
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
    the default order, or None when it may."""
    nodes = topology.get("topology", topology).get("nodes") or []
    validation = topology.get("topology", topology).get("validation") or {}
    rank_of = {node["node_id"]: node["rank"] for node in nodes}
    hostname = {node["node_id"]: node.get("hostname") or node["node_id"] for node in nodes}
    if guarded:
        return "a guarded recipe runs only as a guarded trial"
    if any(node not in rank_of for node in node_ids):
        return "a named node is not in the saved cluster"
    count = len(node_ids)
    if validation.get("class") != RING_CLASS:
        return (f"on a full mesh Start runs a {count}-node recipe on the first {count} nodes in saved order; "
                "omit --placement-nodes")
    if count != 2:
        return (f"on a ring Start runs only a two-node recipe on named nodes; this recipe needs {count} nodes "
                "and runs on the ring's nodes in saved order; omit --placement-nodes")
    first, second = sorted(rank_of[node] for node in node_ids)
    cabled = {tuple(sorted(link["ranks"])) for link in topology.get("topology", topology).get("links") or []}
    if (first, second) not in cabled:
        return f"{hostname[node_ids[0]]} and {hostname[node_ids[1]]} are not neighbours on the ring"
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
