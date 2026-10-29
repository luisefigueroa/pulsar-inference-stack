#!/usr/bin/env python3
"""Cluster overview: one screen that answers "is the cluster healthy and what
is it doing?", composed from three read-only commands the menu runs in turn:
`topology check --json`, `inventory --json` and `model budget --json`.

    python3 scripts/cluster_overview.py DIR --started EPOCH --finished EPOCH

DIR holds each source's output as NAME.json, NAME.err and NAME.rc, for NAME
topology, inventory and budget. A source that failed or printed no JSON is
reported as not observed, with its error; the others still show. Nodes come
from topology check, which lists every saved node by hostname, and inventory
and budget join them by node ID. Memory is the node's available memory: on
GB10 the GPU and the host share it.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.render import can_still_copy, short_size  # noqa: E402
from scripts.terminal_format import TerminalWriter  # noqa: E402

SOURCES = {"topology": "topology check", "inventory": "inventory", "budget": "model budget"}
LABEL_WIDTH = 10


def read_source(directory: Path, name: str) -> tuple[dict | None, str | None]:
    """A source's JSON document, or why it is not observed."""
    def text(suffix: str) -> str:
        try:
            return (directory / f"{name}.{suffix}").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
    try:
        document = json.loads(text("json") or "null")
    except ValueError:
        document = None
    status = text("rc") or "?"
    # topology check exits nonzero when a node is blocked; its JSON still holds the answer.
    if isinstance(document, dict):
        if name == "budget" and isinstance(document.get("result"), dict):
            return document["result"], None
        if name != "budget":
            return document, None
    error = (text("err").splitlines() or [""])[-1]
    error = error.removeprefix("error: ")
    reason = f"`pulsar {SOURCES[name]}` exited {status}" + (f": {error}" if error else " without a result")
    return None, reason


def nodes(topology: dict | None, inventory: dict | None, budget: dict | None) -> list[dict]:
    """Every node to show, in membership order: saved nodes from topology check,
    else the nodes another source observed."""
    if topology and isinstance(topology.get("nodes"), list):
        return [{"node_id": n.get("node_id"), "hostname": n.get("hostname") or n.get("node_id") or "unnamed node",
                 "rank": n.get("rank"), "status": n.get("status"), "issues": n.get("issues") or []}
                for n in sorted(topology["nodes"], key=lambda n: (n.get("rank") is None, n.get("rank")))]
    if inventory and isinstance(inventory.get("nodes"), dict):
        found = sorted(inventory["nodes"].values(), key=lambda n: n.get("topology_index") or 0)
        return [{"node_id": n.get("node_id"), "hostname": n.get("hostname") or n.get("node_id") or "unnamed node",
                 "rank": n.get("topology_index"), "status": None, "issues": []} for n in found]
    if budget and isinstance(budget.get("nodes"), list):
        return [{"node_id": n.get("node_id"), "hostname": n.get("node_id") or "unnamed node", "rank": None,
                 "status": None, "issues": []} for n in budget["nodes"]]
    return []


def gib(mib) -> str:
    return f"{mib / 1024:.2f} GiB" if isinstance(mib, (int, float)) else "not measured"


def render(directory: Path, started: float, finished: float, writer: TerminalWriter | None = None) -> None:
    out = writer or TerminalWriter()
    documents, missing = {}, {}
    for name in SOURCES:
        documents[name], reason = read_source(directory, name)
        if reason:
            missing[name] = reason
    topology, inventory, budget = documents["topology"], documents["inventory"], documents["budget"]
    shown = nodes(topology, inventory, budget)

    roles = {}  # inventory's node keys (head, worker, rank-2) by node ID
    by_id = {}
    for key, node in ((inventory or {}).get("nodes") or {}).items():
        roles[key] = node.get("node_id")
        by_id[node.get("node_id")] = node
    budgets = {n.get("node_id"): n for n in ((budget or {}).get("nodes") or [])}
    services: dict[str, list[tuple[dict, dict]]] = {}
    for service in (inventory or {}).get("services") or []:
        for rank in service.get("ranks") or []:
            services.setdefault(roles.get(rank.get("node")), []).append((service, rank))
    unmanaged: dict[str, list[dict]] = {}
    for process in (inventory or {}).get("unmanaged_gpu_processes") or []:
        unmanaged.setdefault(roles.get(process.get("node")), []).append(process)

    out.emit("Cluster overview", role="heading")
    if topology:
        state = topology.get("status") or "unknown"
        out.field("Topology", f"{state} · {len(shown)} saved node{'' if len(shown) == 1 else 's'}", indent=2,
                  label_width=LABEL_WIDTH, label_role=None if state == "ready" else "fail")
        for issue in topology.get("issues") or []:
            out.field("Issue", issue, indent=2, label_width=LABEL_WIDTH, label_role="warn")
    when = datetime.fromtimestamp(started).strftime("%H:%M:%S")
    took = max(0, round(finished - started))
    out.field("Observed", f"{when}; the checks took {took} s", indent=2, label_width=LABEL_WIDTH)
    if not shown:
        out.blank()
        out.emit("No node was observed.")

    for node in shown:
        node_id = node["node_id"]
        observed = by_id.get(node_id) or {}
        out.blank()
        title = str(node["hostname"]) + (" (this node)" if observed.get("local") else "")
        out.emit(title, role="heading")

        def field(label, value, role=None, markup=False):
            out.field(label, value, indent=2, label_width=LABEL_WIDTH, label_role=role, markup=markup)

        if node["status"] is None:
            field("Status", "not observed")
        else:
            field("Status", node["status"], None if node["status"] == "ready" else "fail")
        for issue in node["issues"]:
            field("Issue", issue, "warn")

        if not observed:
            field("Memory", "not observed")
        elif observed.get("probe_status") not in (None, "ok"):
            field("Memory", f"not observed: {observed.get('probe_reason') or observed.get('probe_status')}", "warn")
        else:
            available, total = observed.get("mem_available_gib"), observed.get("mem_total_gib")
            text = f"{available:.2f} GiB available" if isinstance(available, (int, float)) else "not observed"
            if isinstance(available, (int, float)) and isinstance(total, (int, float)):
                text += f" of {total:.2f} GiB"
            field("Memory", text, None if observed.get("mem_status") in (None, "ok") else "warn")

        node_budget = budgets.get(node_id)
        if node_budget:
            # The storage budget table's terms, for one node.
            left = can_still_copy(node_budget)
            text = (f"can still copy {'unknown' if left is None else short_size(max(left, 0))} · "
                    f"{short_size(node_budget.get('available'))} free · {short_size(node_budget.get('used'))} Pulsar files")
            low = left is not None and left < 0
            if low:
                text += f" · {short_size(-left)} past its reserve or copy budget"
            field("Disk", text, "warn" if low else None)
        else:
            field("Disk", "not observed")

        rows = services.get(node_id) or []

        def managed_rank(row):
            # A mixed service keeps each rank's own ownership.
            service, rank = row
            return (rank.get("ownership") or service.get("ownership")) == "managed"
        managed = [row for row in rows if managed_rank(row)]
        if not managed:
            # None only for a node inventory actually inspected; unknown is not absent.
            probed = bool(observed) and observed.get("probe_status") in (None, "ok")
            field("Services", "none" if probed else "not observed")
        # Pulsar services first, then containers Pulsar does not manage.
        for index, (service, rank) in enumerate(managed + [row for row in rows if row not in managed]):
            state = service.get("state") or "unknown"
            measured = (rank.get("gpu_memory") or {}).get("measured_mib")
            in_use = f" · {gib(measured)} GPU memory in use" if isinstance(measured, (int, float)) else ""
            if managed_rank((service, rank)):
                spec = str(service.get("selected_spec_id") or service.get("spec_id") or "")
                text = f"{service.get('served_name') or 'served name not recorded'} · {state} · spec {spec[:12]}{in_use}"
                role = None if state == "running" else "warn"
            else:
                # Not a Pulsar service: name the container and leave it to its owner.
                name = rank.get("container_name") or service.get("container_name") or "unnamed container"
                field("Unmanaged", f"container {name} · {state}{in_use}; Pulsar does not manage it", "warn")
                continue
            field("Services" if index == 0 else "", text, role)
        for process in unmanaged.get(node_id) or []:
            name = Path(str(process.get("process_name") or "?").rstrip("/")).name or "?"
            field("Other GPU", f"PID {process.get('pid') or '?'} {name} · {gib(process.get('used_memory_mib'))}; "
                               "Pulsar does not manage it", "warn")

    if missing:
        out.blank()
        out.emit("Not observed", role="heading")
        for name, reason in missing.items():
            out.emit(reason, initial_indent="  ", subsequent_indent="    ", markup=True)
    out.blank()
    out.emit("Details: **Live service inventory**, **Host diagnostics** and **Cluster topology**.", markup=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the cluster overview from its sources' output.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--started", type=float, required=True)
    parser.add_argument("--finished", type=float, required=True)
    args = parser.parse_args(argv)
    render(args.directory, args.started, args.finished)
    return 0


if __name__ == "__main__":
    sys.exit(main())
