#!/usr/bin/env python3
"""Check the cluster now: one report that answers "is the cluster healthy, has
it changed, and what is it doing?", composed from six read-only commands the
Cluster menu runs in turn: `topology detect --json` (discovery, which saves the
changes noticed), `topology check --json`, `ssh-trust check --json`,
`doctor --json` (with every node's prerequisites), `inventory --json` and
`model budget --json`.

    python3 scripts/cluster_overview.py DIR --started EPOCH --finished EPOCH
    python3 scripts/cluster_overview.py DIR --check-record

DIR holds each source's output as NAME.json, NAME.err and NAME.rc, for NAME
discovery, topology, identities, diagnostics, inventory and budget. A source
that failed or printed no JSON is reported as not observed, with its error;
the others still show. Nodes come from topology check, which lists every
saved node by hostname, and the other sources join them by node ID or
position. Memory is the node's available memory: on GB10 the GPU and the host
share it. Each finding carries its fix. `--check-record` prints what the
report found as the `check` body that scripts/saved_observations.py
save-cluster-check keeps beside the changes noticed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.render import can_still_copy, short_size  # noqa: E402
from scripts.terminal_format import TerminalWriter  # noqa: E402

SOURCES = {"discovery": "topology detect", "topology": "topology check", "identities": "ssh-trust check",
           "diagnostics": "doctor", "inventory": "inventory", "budget": "model budget"}
LABEL_WIDTH = 10
UNREACHABLE = "Node could not be checked through its confirmed control endpoint."
FABRIC_FAILED = "Pairwise RoCE connectivity failed; inspect the saved fabric links."
FABRIC_UNCHECKED = "Fabric connectivity could not be checked."
# doctor's checks that describe the controller's settings or the catalog, not a node.
SETTINGS_CHECKS = {"api_auth": "API key", "cold_storage": "Archives"}
CLUSTER_CHECKS = {"topology", "ssh_trust", "local_fabric", "catalog"}
REMOTE = re.compile(r"^rank_(\d+)_(.+)$")


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
    # A check that finds a problem exits nonzero; its JSON still holds the answer.
    if isinstance(document, dict):
        if name == "budget" and isinstance(document.get("result"), dict):
            return document["result"], None
        if name != "budget":
            return document, None
    if not text("rc") and not text("json") and not (directory / f"{name}.rc").exists():
        return None, f"`pulsar {SOURCES[name]}` did not run"
    error = (text("err").splitlines() or [""])[-1]
    error = error.removeprefix("error: ")
    reason = f"`pulsar {SOURCES[name]}` exited {status}" + (f": {error}" if error else " without a result")
    return None, reason


class Report:
    """The six sources, read once, and what they say about each node."""

    def __init__(self, directory: Path):
        self.documents, self.missing = {}, {}
        for name in SOURCES:
            self.documents[name], reason = read_source(directory, name)
            if reason:
                self.missing[name] = reason
        get = self.documents.get
        self.discovery, self.topology, self.identities = get("discovery"), get("topology"), get("identities")
        self.diagnostics, self.inventory, self.budget = get("diagnostics"), get("inventory"), get("budget")
        self.nodes = self._nodes()
        self.by_rank = {node["rank"]: node for node in self.nodes if node["rank"] is not None}

    def _nodes(self) -> list[dict]:
        """Every node to show, in membership order: saved nodes from topology check,
        else the nodes another source observed."""
        topology, inventory, budget = self.topology, self.inventory, self.budget
        if topology and isinstance(topology.get("nodes"), list) and topology["nodes"]:
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

    # -- per node --------------------------------------------------------

    def checks(self, node: dict) -> list[tuple[str, str]]:
        """doctor's rows for NODE as (level, message), the node's name left off."""
        rows = []
        for check in (self.diagnostics or {}).get("checks") or []:
            level, identifier, message = check.get("level"), str(check.get("id") or ""), str(check.get("message") or "")
            match = REMOTE.match(identifier)
            if match:
                if int(match.group(1)) != node["rank"]:
                    continue
                message = message.removeprefix(f"{node['hostname']}: ")
            elif node["rank"] != 0 or identifier in CLUSTER_CHECKS or identifier in SETTINGS_CHECKS:
                continue
            rows.append((level, message))
        return rows

    def answered(self, node: dict) -> bool | None:
        """Whether the node answered this check; None when nothing could tell."""
        changes = (self.discovery or {}).get("changes_noticed") or {}
        if any(item.get("node_id") == node["node_id"] and not item.get("answered")
               for item in changes.get("missing") or []):
            return False
        if node["status"] is not None:
            return UNREACHABLE not in node["issues"]
        return None

    def identity(self, node: dict) -> tuple[str, str | None]:
        """The node's SSH identity result and its fix."""
        if self.identities is None:
            return "not observed", None
        for item in self.identities.get("nodes") or []:
            if item.get("node_id") == node["node_id"] or item.get("rank") == node["rank"]:
                if item.get("ok"):
                    return "verified", None
                states = sorted({endpoint.get("state") or "failed" for endpoint in item.get("endpoints") or []
                                 if not endpoint.get("ok")})
                return (f"did not match ({', '.join(states)})",
                        "if the machine was reinstalled or its host key rotated, verify the new key with its "
                        "console or administrator, then run `./pulsar ssh-trust enroll --accept-key-change`")
        return "not observed", None

    # -- cluster wide ----------------------------------------------------

    def links(self) -> str:
        if self.topology is None:
            return "not-checked"
        issues = self.topology.get("issues") or []
        if FABRIC_FAILED in issues:
            return "failed"
        if FABRIC_UNCHECKED in issues or self.topology.get("status") not in ("ready", "blocked"):
            return "not-checked"
        return "verified"

    def identities_state(self) -> str:
        if self.identities is None:
            return "not-checked"
        return "verified" if self.identities.get("ok") else "failed"

    def check_record(self) -> dict:
        """The `check` body saved beside the changes noticed (scripts/saved_observations.py)."""
        topology_id = ((self.topology or {}).get("topology") or {}).get("topology_id") \
            or ((self.discovery or {}).get("changes_noticed") or {}).get("topology_id")
        nodes = []
        for node in self.nodes:
            failures = [issue for issue in node["issues"] if issue != UNREACHABLE]
            failures += [message for level, message in self.checks(node) if level == "fail"]
            warnings = [message for level, message in self.checks(node) if level == "warn"]
            nodes.append({"hostname": str(node["hostname"]), "node_id": str(node["node_id"] or ""),
                          "answered": self.answered(node), "failures": failures, "warnings": warnings})
        return {"topology_id": topology_id or "", "links": self.links(), "identities": self.identities_state(),
                "nodes": nodes}


def gib(mib) -> str:
    return f"{mib / 1024:.2f} GiB" if isinstance(mib, (int, float)) else "not measured"


def _change_lines(discovery: dict | None) -> list[tuple[str, str | None]]:
    """What discovery saw against saved membership, each with its fix, as (text, role)."""
    changes = (discovery or {}).get("changes_noticed")
    if not changes:
        return []
    review = "**Review what changed…** below"
    rows: list[tuple[str, str | None]] = []
    if changes.get("result") == "not-checked":
        issues = "; ".join(changes.get("issues") or []) or "no reason given"
        rows.append((f"discovery did not finish: {issues}. Fix it, then choose **Check again**.", "fail"))
    for item in changes.get("missing") or []:
        if item.get("answered"):
            reasons = "; ".join(item.get("reasons") or []) or "not qualified"
            rows.append((f"{item['hostname']} answered but cannot be a member: {reasons}; fix it on that machine",
                         "fail"))
        else:
            rows.append((f"{item['hostname']} did not answer; it may be down or gone. {review} keeps it or saves "
                         "the cluster without it", "warn"))
    for item in changes.get("found") or []:
        text = f"found {item['hostname']}, which qualifies but is not a member; {review} can add it"
        if item.get("possible_reinstall"):
            text += f"; it may be {item['hostname']} reinstalled"
        rows.append((text, "warn"))
    for item in changes.get("changed") or []:
        what = [{"control": "address", "links": "links", "position": "position"}[key]
                for key in ("control", "links", "position") if key in (item.get("changes") or {})]
        rows.append((f"{item['hostname']}: {', '.join(what) or 'details'} changed; {review} shows old against "
                     "new and saves it", "warn"))
    for item in changes.get("untrusted") or []:
        rows.append((f"{item['address']} answers, but SSH on this machine does not trust it yet; check its host key "
                     f"with `ssh -o StrictHostKeyChecking=ask {item['address']}`, then choose **Check again**",
                     None))
    return rows


def render(directory: Path, started: float, finished: float, writer: TerminalWriter | None = None) -> None:
    out = writer or TerminalWriter()
    report = Report(directory)
    inventory, budget = report.inventory, report.budget
    shown = report.nodes

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

    def top(label, value, role=None, markup=False):
        out.field(label, value, indent=2, label_width=LABEL_WIDTH, label_role=role, markup=markup)

    counts = {"fail": 0, "warn": 0}
    out.emit("Cluster check", role="heading")
    if report.topology:
        state = report.topology.get("status") or "unknown"
        top("Nodes", f"{len(shown)} saved · {state}",
            None if state == "ready" else "fail")
    when = datetime.fromtimestamp(started).strftime("%H:%M:%S")
    took = max(0, round(finished - started))
    top("Checked", f"{when}; the checks took {took} s")
    links = report.links()
    link_text = {"verified": "verified between every pair of nodes",
                 "failed": "failed between some nodes; check the cables and the links' addresses, then choose "
                           "**Check again**",
                 "not-checked": "not checked"}[links]
    top("Links", link_text, {"failed": "fail"}.get(links), markup=True)
    counts["fail"] += links == "failed"
    for issue in (report.topology or {}).get("issues") or []:
        if issue not in (FABRIC_FAILED, FABRIC_UNCHECKED):
            top("Issue", issue, "warn")
            counts["warn"] += 1

    changes = _change_lines(report.discovery)
    out.blank()
    out.emit("Changes noticed", role="heading")
    if report.discovery is None:
        out.emit("not observed: discovery did not answer", initial_indent="  ", subsequent_indent="    ")
    elif not changes:
        out.emit("none: every member answered as saved", initial_indent="  ", subsequent_indent="    ")
    for text, role in changes:
        out.emit(text, initial_indent="  ", subsequent_indent="    ", markup=True)
        if role:
            counts[role] += 1

    if not shown:
        out.blank()
        out.emit("No node was observed.")

    for node in shown:
        node_id = node["node_id"]
        observed = by_id.get(node_id) or {}
        out.blank()
        local = observed.get("local") or (node["rank"] == 0 and not observed)
        title = str(node["hostname"]) + (" (this node)" if local else "")
        out.emit(title, role="heading")

        def field(label, value, role=None, markup=False):
            out.field(label, value, indent=2, label_width=LABEL_WIDTH, label_role=role, markup=markup)

        if node["status"] is None:
            field("Status", "not observed")
        else:
            field("Status", node["status"], None if node["status"] == "ready" else "fail")
        for issue in node["issues"]:
            if issue == UNREACHABLE:
                field("Issue", f"{issue} Check that it is on and reachable; if it is gone, **Review what changed…** "
                               "can save the cluster without it.", "fail", markup=True)
            else:
                field("Issue", issue, "fail")
            counts["fail"] += 1
        identity, fix = report.identity(node)
        field("Identity", identity + (f"; {fix}" if fix else ""), "fail" if fix else None, markup=True)
        counts["fail"] += bool(fix)
        rows = report.checks(node)
        passed = sum(1 for level, _ in rows if level == "ok")
        problems = [(level, message) for level, message in rows if level in ("warn", "fail")]
        if report.diagnostics is None:
            field("Checks", "not observed")
        elif not rows:
            field("Checks", "not observed: diagnostics did not reach this node")
        else:
            field("Checks", f"{passed} in place" + ("" if problems else ", none needs attention"))
        for level, message in problems:
            field("FAIL" if level == "fail" else "WARN", message, level, markup=True)
            counts[level] += 1

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

        node_services = services.get(node_id) or []

        def managed_rank(row):
            # A mixed service keeps each rank's own ownership.
            service, rank = row
            return (rank.get("ownership") or service.get("ownership")) == "managed"
        managed = [row for row in node_services if managed_rank(row)]
        if not managed:
            # None only for a node inventory actually inspected; unknown is not absent.
            probed = bool(observed) and observed.get("probe_status") in (None, "ok")
            field("Services", "none" if probed else "not observed")
        # Pulsar services first, then containers Pulsar does not manage.
        for index, (service, rank) in enumerate(managed + [row for row in node_services if row not in managed]):
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

    settings = [(SETTINGS_CHECKS[check.get("id")], check.get("level"), str(check.get("message") or ""))
                for check in (report.diagnostics or {}).get("checks") or [] if check.get("id") in SETTINGS_CHECKS]
    if settings:
        out.blank()
        out.emit("Settings", role="heading")
        for label, level, message in settings:
            top(label, message + ("; **Settings…** says where it is set" if level != "ok" else ""),
                level if level in ("warn", "fail") else None, markup=True)
            if level in counts:
                counts[level] += 1

    if report.missing:
        out.blank()
        out.emit("Not observed", role="heading")
        for name, reason in report.missing.items():
            out.emit(reason, initial_indent="  ", subsequent_indent="    ", markup=True)
    out.blank()
    if counts["fail"]:
        verdict = f"{counts['fail']} blocking issue{'' if counts['fail'] == 1 else 's'}"
    else:
        verdict = "no blocking issue"
    if counts["warn"]:
        verdict += f" · {counts['warn']} warning{'' if counts['warn'] == 1 else 's'}"
    if report.missing:
        verdict += f" · {len(report.missing)} check{'' if len(report.missing) == 1 else 's'} not observed"
    out.field("Result", verdict + "; each finding above names its fix.", label_width=LABEL_WIDTH,
              label_role="fail" if counts["fail"] else None)
    out.field("Same as", "`./pulsar topology detect`, `topology check`, `ssh-trust check`, `doctor`, `inventory` "
                         "and `model budget`", label_width=LABEL_WIDTH, markup=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the cluster check from its sources' output.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--started", type=float)
    parser.add_argument("--finished", type=float)
    parser.add_argument("--check-record", action="store_true",
                        help="print the check body saved beside the changes noticed")
    args = parser.parse_args(argv)
    if args.check_record:
        print(json.dumps(Report(args.directory).check_record(), sort_keys=True))
        return 0
    if args.started is None or args.finished is None:
        parser.error("--started and --finished are required to render the report")
    render(args.directory, args.started, args.finished)
    return 0


if __name__ == "__main__":
    sys.exit(main())
