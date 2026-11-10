#!/usr/bin/env python3
"""Read-only topology observations; canonical membership stays in topology_manifest."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import sys

import topology_manifest as manifest
from terminal_format import TerminalWriter


def saved(path):
    topology = manifest.extract_topology(manifest.load_json(path))
    manifest.validate_manifest(topology, require_verified=True)
    return topology


def discovery_membership(path, document, remove_missing=False):
    """A partial rediscovery must not erase confirmed members.

    With REMOVE_MISSING (`topology configure --remove-missing`), confirmed
    members that did not answer may be left out; a member that answered and
    was rejected still blocks, because no flag drops a node that answered.
    """
    document["saved_membership"] = {"missing": [], "issues": []}
    if not Path(path).exists():
        return document
    try:
        old = saved(path)
    except (manifest.TopologyError, KeyError, TypeError, ValueError, AttributeError):
        document["saved_membership"]["issues"].append(
            "Saved topology is invalid; discovery cannot replace it automatically.")
    else:
        found = {node["node_id"] for node in (document.get("topology") or {}).get("nodes", [])}
        # A member that answered but was rejected (not qualified, outside the
        # largest connected group, or a duplicate identity) answered; only a
        # member whose identity no probe returned did not.
        rejected = {item["node_id"]: item for item in document.get("rejected") or [] if item.get("node_id")}
        document["saved_membership"]["missing"] = [
            {"rank": node["rank"], "node_id": node["node_id"], "hostname": node["hostname"],
             "answered": node["node_id"] in rejected,
             **({"reasons": list(rejected[node["node_id"]].get("reasons") or [])}
                if node["node_id"] in rejected else {})}
            for node in old["nodes"] if node["node_id"] not in found
        ]
        missing = document["saved_membership"]["missing"]
        answered = [item for item in missing if item["answered"]]
        if missing and not (remove_missing and not answered):
            document["saved_membership"]["issues"].append(
                "Confirmed nodes are missing from discovery. Restore access or investigate their identity and fabric; membership was not changed.")
            for item in answered:
                reasons = manifest.reason_words(item.get("reasons") or ["not qualified"])
                document["saved_membership"]["issues"].append(
                    f"{item['hostname']} answered but cannot be a member: {reasons}. "
                    "--remove-missing leaves out only nodes that did not answer; fix it first.")
            if not answered:
                document["saved_membership"]["issues"].append(
                    "To save membership without the nodes that did not answer, run "
                    "./pulsar topology configure --remove-missing.")
        document["membership_changes"] = {
            "added": [node["hostname"] for node in (document.get("topology") or {}).get("nodes", [])
                      if node["node_id"] not in {item["node_id"] for item in old["nodes"]}],
            "removed": [node["hostname"] for node in document["saved_membership"]["missing"]],
            "changed": [node["hostname"] for node in (document.get("topology") or {}).get("nodes", [])
                        if any(previous["node_id"] == node["node_id"] and any(previous.get(key) != node.get(key)
                               for key in ("rank", "control", "rdma")) for previous in old["nodes"])],
        }
    if document["saved_membership"]["issues"]:
        document["result"] = "incomplete"
    return document


def _links(node):
    return sorted(({"hca": link["hca"], "netdev": link["netdev"], "cidrs": sorted(link.get("cidrs") or [])}
                   for link in node.get("rdma") or []), key=lambda link: (link["hca"], link["netdev"]))


def untrusted_addresses(document, untrusted, saved_nodes=()):
    """The addresses SSH here did not trust, less those of machines that answered.

    A machine that answered discovery under one name is not "untrusted" under
    another: mDNS advertises each member's .local name and address too.
    SAVED_NODES are the saved members, whose aliases are known as well."""
    nodes = (document.get("topology") or {}).get("nodes") or []
    found = {item["node_id"] for item in nodes}
    answered = [*nodes, *(document.get("rejected") or []),
                *(node for node in saved_nodes if node["node_id"] in found)]
    known = {node["ssh_host"] for node in saved_nodes}
    for node in answered:
        hostname = node.get("hostname")
        known.update(value for value in (hostname, f"{hostname}.local" if hostname else None, node.get("ssh_host"),
                                         (node.get("control") or {}).get("ip")) if value)
    seen = []
    for address in untrusted:
        if address and address not in known and address not in seen and not _loopback(address):
            seen.append(address)
    return seen


def _loopback(address):
    """Whether ADDRESS names this machine's own loopback, which mDNS may advertise."""
    if address.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def changes_noticed(old, document, untrusted=(), issue=None):
    """What one discovery saw against the saved membership OLD.

    The body of the saved "changes noticed" record (scripts/saved_observations.py)
    and of `topology detect --json`'s `changes_noticed`. `result` is
    `not-checked` when discovery itself failed (ISSUE says why), `changed` when
    a machine was found that is not a member, a member did not answer or was
    rejected, or a member's control address, links or position changed, and
    `unchanged` otherwise. Candidates that SSH on this machine does not trust
    are listed but do not make a change by themselves: discovery cannot tell
    whether they are GB10 machines.
    """
    topology = document.get("topology") or {}
    issues = [issue] if issue else []
    if not issue and document.get("result") != "ok" and not topology:
        issues.extend(document.get("discovery_issues") or [])
        issues.extend(f"{item.get('hostname') or item.get('ssh_host') or 'a machine'}: "
                      f"{'; '.join(item.get('reasons') or ['not qualified'])}"
                      for item in document.get("rejected") or [] if item.get("ssh_host") == "local")
        issues = issues or ["Discovery found no usable topology."]
    saved_by_id = {node["node_id"]: node for node in old["nodes"]}
    nodes = topology.get("nodes") or []
    # Without a discovered topology no member was compared, so none is missing.
    missing = (document.get("saved_membership") or {}).get("missing") or [] if nodes else []
    gone = {item["hostname"] for item in missing if not item.get("answered")}
    gone_addresses = {saved_by_id[item["node_id"]]["control"]["ip"] for item in missing
                      if not item.get("answered") and item["node_id"] in saved_by_id}
    found = [{"hostname": node["hostname"], "node_id": node["node_id"], "ssh_host": node["ssh_host"],
              "possible_reinstall": node["hostname"] in gone}
             for node in nodes if node["node_id"] not in saved_by_id]
    changed = []
    for node in nodes:
        previous = saved_by_id.get(node["node_id"])
        if previous is None:
            continue
        fields = {}
        if previous.get("control") != node.get("control"):
            fields["control"] = {"old": previous.get("control"), "new": node.get("control")}
        if _links(previous) != _links(node):
            fields["links"] = {"old": _links(previous), "new": _links(node)}
        if previous["rank"] != node["rank"]:
            fields["position"] = {"old": previous["rank"], "new": node["rank"]}
        if fields:
            changed.append({"hostname": node["hostname"], "node_id": node["node_id"], "changes": fields})
    seen = untrusted_addresses(document, untrusted, old["nodes"])
    untrusted_rows = [{"address": address,
                       "possible_reinstall": address.removesuffix(".local") in gone or address in gone_addresses}
                      for address in seen]
    missing_rows = [{"hostname": item["hostname"], "node_id": item["node_id"], "position": item["rank"],
                     "answered": bool(item.get("answered")),
                     **({"reasons": item["reasons"]} if item.get("reasons") else {})} for item in missing]
    shape = cabling = None
    if nodes and not issues:
        shape, cabling = shape_changes(old, document)
    if issues:
        result = "not-checked"
    elif found or missing_rows or changed or shape or cabling:
        result = "changed"
    else:
        result = "unchanged"
    body = {"topology_id": old["topology_id"], "result": result, "issues": issues, "found": found,
            "missing": missing_rows, "changed": changed, "untrusted": untrusted_rows}
    if nodes and not issues:
        # The order a save would keep: discovery's ranks, which follow a ring's cables.
        body["order"] = [{"hostname": node["hostname"], "node_id": node["node_id"]} for node in nodes]
    if shape:
        body["shape"] = shape
    if cabling:
        body["cables"] = cabling
    return body


def _pairs(topology):
    """The cabled pairs of a membership as node_id pairs: its links."""
    nodes = topology.get("nodes") or []
    return {frozenset((nodes[link["ranks"][0]]["node_id"], nodes[link["ranks"][1]]["node_id"]))
            for link in topology.get("links") or []}


def shape_changes(old, document):
    """How the cluster's shape and cables differ from saved membership OLD.

    (shape, cables): shape is {"old", "new"} validation classes when they
    differ; cables is {"gone", "new"}, hostname pairs, when a ring is saved
    or found and the cables between machines that answered differ. Discovery
    lists every cable between qualified machines (`cabling`) whenever it
    found something other than a full mesh; a full mesh's cables are its
    links."""
    topology = document.get("topology") or {}
    old_class = (old.get("validation") or {}).get("class")
    new_class = (topology.get("validation") or {}).get("class")
    shape = {"old": old_class, "new": new_class} if old_class != new_class else None
    if manifest.RING_CLASS not in (old_class, new_class) and "cabling" not in document:
        return shape, None
    names = {node["node_id"]: node["hostname"] for node in old.get("nodes") or []}
    names.update({node["node_id"]: node["hostname"] for node in topology.get("nodes") or []})
    names.update({item["node_id"]: item.get("hostname") or item["node_id"]
                  for item in document.get("rejected") or [] if item.get("node_id")})
    if "cabling" in document:
        now = {frozenset(pair) for pair in document["cabling"]}
        qualified = {node["node_id"] for node in topology.get("nodes") or []}
        qualified |= {node_id for pair in now for node_id in pair}
        qualified |= {item["node_id"] for item in document.get("rejected") or []
                      if manifest.left_out_for_cabling(item)}
    else:
        now = _pairs(topology)
        qualified = {node["node_id"] for node in topology.get("nodes") or []}
    saved = _pairs(old)
    gone = [pair for pair in saved - now if pair <= qualified]
    new = [pair for pair in now - saved if pair <= qualified]

    def named(pairs):
        return sorted(sorted(names.get(node_id, node_id) for node_id in pair) for pair in pairs)

    cables = {"gone": named(gone), "new": named(new)} if gone or new else None
    return shape, cables


def fabric_failures(topology, path):
    """The saved links that did not answer the fabric check, each with the nodes at its ends.

    PATH holds one tab-separated row per failed ping (see topology_check_fabric
    in scripts/topology-probes.sh); it is missing when the check could not say
    which link failed."""
    nodes = topology["nodes"]
    failures = []
    try:
        rows = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return failures
    for row in rows:
        fields = row.split("\t")
        if len(fields) != 5 or not all(field.isdigit() for field in fields[:2]):
            continue
        source, target = int(fields[0]), int(fields[1])
        if source >= len(nodes) or target >= len(nodes):
            continue
        failures.append({"from": nodes[source]["hostname"], "to": nodes[target]["hostname"],
                         "ranks": [source, target], "address": fields[2], "network": fields[3],
                         "interface": fields[4]})
    return failures


def ring_cables_changed(topology, live):
    """Why a saved ring's cables differ from what its nodes report, or None.

    LIVE maps each rank that answered as itself to its probe. Only a saved
    ring is compared, and only when every node answered."""
    nodes = topology["nodes"]
    if (topology.get("validation") or {}).get("class") != manifest.RING_CLASS or len(live) != len(nodes):
        return None
    probes = [live[rank] for rank in range(len(nodes))]
    neighbours = manifest.cabled_neighbours(probes)
    now = {(a, b) for a in range(len(nodes)) for b in neighbours[a] if a < b}
    saved = {tuple(link["ranks"]) for link in topology["links"]}
    if now == saved:
        return None
    names = [node["hostname"] for node in nodes]
    if len(nodes) >= manifest.RING_MIN_NODES and all(len(items) == 2 for items in neighbours) \
            and len(manifest.cabled_group(neighbours)) == len(nodes):
        order = manifest.ring_walk(probes, neighbours, 1 if 1 in neighbours[0] else min(neighbours[0]))
        return ("The ring's cables run in another order now: "
                + manifest.ring_loop([names[index] for index in order]) + " (saved: " + manifest.ring_loop(names)
                + "). Review what changed… saves the new order.")
    parts = [f"{names[a]} and {names[b]} are neighbours in the saved ring but share no link now; check the cable "
             "between them" for a, b in sorted(saved - now)]
    parts += [f"{names[a]} and {names[b]} share a link now, which the saved ring does not have"
              for a, b in sorted(now - saved)]
    return "; ".join(parts) + "."


def observation(path, directory=None):
    result = {"schema_version": 1, "kind": "pulsar-topology-observation",
              "status": "missing", "topology": None, "nodes": [], "issues": []}
    if not Path(path).exists():
        result["issues"].append("No saved topology. Run ./pulsar topology setup.")
        return result
    try:
        topology = saved(path)
    except (manifest.TopologyError, KeyError, TypeError, ValueError, AttributeError):
        result["status"] = "invalid"
        result["issues"].append("Saved topology is invalid, and setup cannot replace it. To start over, run "
                                "./pulsar topology forget; it keeps a private backup copy.")
        return result
    result.update(status="saved", topology=topology)
    if directory is None:
        return result
    directory = Path(directory)
    if (directory / "configuration-error").exists():
        result["issues"].append("Saved SSH configuration cannot be used. Run pulsar ssh-trust check.")
    if not manifest.topology_has_ssh_trust(topology):
        result["issues"].append("SSH identity is not enrolled. Complete first-use setup with pulsar topology setup or pulsar ssh-trust enroll.")
    live = {}
    for node in topology["nodes"]:
        row = {key: node[key] for key in ("rank", "node_id", "hostname")}
        row.update(status="ready", issues=[])
        try:
            probe = manifest.normalize_probe(manifest.load_json(directory / f"rank-{node['rank']}.json"), "live probe")
            if probe["node_id"] == node["node_id"]:
                live[node["rank"]] = probe
            if probe["node_id"] != node["node_id"]:
                row["issues"].append("Node identity changed at the confirmed endpoint.")
            if not probe["qualified"]:
                row["issues"].extend(probe["reject_reasons"] or ["Node does not meet platform requirements."])
            if probe["control"] != node["control"]:
                row["issues"].append("Control endpoint or interface differs from saved membership.")
            for link in node.get("rdma", []):
                if not any(current["hca"] == link["hca"] and current["netdev"] == link["netdev"]
                           and set(link["cidrs"]).issubset(current["cidrs"]) for current in probe["rdma"]):
                    row["issues"].append("A saved RDMA interface or address is unavailable.")
        except (manifest.TopologyError, KeyError, TypeError, ValueError, AttributeError):
            row["issues"].append("Node could not be checked through its confirmed control endpoint.")
        if row["issues"]:
            row["status"] = "blocked"
        result["nodes"].append(row)
    cables = ring_cables_changed(topology, live)
    if cables:
        result["issues"].append(cables)
    if (directory / "fabric-failed").exists():
        result["issues"].append("Pairwise RoCE connectivity failed; inspect the saved fabric links.")
        result["fabric_failures"] = fabric_failures(topology, directory / "fabric-failures")
    if not (directory / "fabric-checked").exists():
        result["issues"].append("Fabric connectivity could not be checked.")
    result["status"] = "blocked" if result["issues"] or any(row["issues"] for row in result["nodes"]) else "ready"
    return result


def render_observation(document):
    writer = TerminalWriter()
    writer.emit(f"Cluster topology: {document['status']}", role="heading")
    topology = document.get("topology")
    if topology and not document["nodes"]:
        # Nodes are named by hostname; the saved order (ordinary start's rank
        # order) is a position, never a rank.
        for node in topology["nodes"]:
            writer.emit(f"{node['hostname']} (position {node['rank']})", initial_indent="  ")
            writer.emit(f"Control: {node['control']['ip']}", initial_indent="    ")
        writer.emit("Saved membership only; run `pulsar topology check` for current readiness.", markup=True)
    for node in document["nodes"]:
        line = f"  {node['hostname']} (position {node['rank']}) — {node['status']}"
        if len(line) <= writer.width:
            # A node that is not ready takes the fail style.
            role = None if node["status"] == "ready" else "fail"
            print(writer.paint(line, [None] * (len(line) - len(node["status"])) + [role] * len(node["status"])),
                  file=writer.stream)
        else:
            writer.emit(line.strip(), initial_indent="  ")
        for issue in node["issues"]:
            writer.emit(issue, initial_indent="    ", subsequent_indent="    ")
    for issue in document["issues"]:
        writer.emit(issue, initial_indent="  ", subsequent_indent="  ")
    for failure in document.get("fabric_failures") or []:
        writer.emit(manifest.failure_words(failure), initial_indent="    ", subsequent_indent="      ")
    writer.emit("Model geometry remains the selected recipe's exact configuration.")


def saved_age(timestamp, now=None):
    """How long ago a saved record was written, or that its time is unknown."""
    try:
        written = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        seconds = int(((now or datetime.now(timezone.utc)) - written).total_seconds())
    except (TypeError, ValueError):
        return "at an unknown time"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "less than a minute ago"


def render_summary(document, now=None):
    """The topology menu's frame, from saved membership only: no node is probed."""
    writer = TerminalWriter()

    def field(label, value):
        writer.field(label, value, label_width=12)

    writer.emit("Saved membership (no probes)")
    topology = document.get("topology")
    if document["status"] == "missing":
        field("Membership", "not saved; choose Change membership… → First-use setup")
        return
    if document["status"] != "saved" or not topology:
        # Setup, detect and configure all refuse to replace invalid saved state;
        # forget removes it, after a private backup copy.
        field("Membership", "saved but invalid, and setup cannot replace it; inspect it with "
                            "Show saved membership, or start over with ./pulsar topology forget")
        return
    nodes = sorted(topology["nodes"], key=lambda node: node["rank"])
    field("Nodes", ", ".join(node["hostname"] for node in nodes))
    # Both times are stamped when the change is prepared, before its
    # confirmation, so they date the discovery and the key reading, not the save.
    field("Membership", f"saved from a discovery {saved_age(topology.get('generated_at'), now)}")
    # Enrollment is required even for one node; topology check blocks without it.
    if manifest.topology_has_ssh_trust(topology):
        enrolled = topology.get("ssh_trust_enrolled_at")
        trust = f"enrolled from host keys read {saved_age(enrolled, now)}" if enrolled else "enrolled"
    else:
        trust = "not enrolled; choose Change membership… → Enroll SSH trust"
    field("SSH trust", trust)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("show", "check", "discovery", "changes", "summary",
                                              "notice", "notice-saved", "untrusted"))
    parser.add_argument("path")
    parser.add_argument("--observations")
    parser.add_argument("--document")
    parser.add_argument("--failure")
    parser.add_argument("--untrusted", help="a file of candidate addresses SSH does not trust, one per line")
    parser.add_argument("--remove-missing", action="store_true",
                        help="discovery: confirmed members that did not answer may be left out")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.operation == "untrusted":
        # PATH is the saved membership, or a path that does not exist before
        # first use; one address per line that SSH here does not trust.
        addresses = Path(args.untrusted).read_text(encoding="utf-8").split() if args.untrusted else []
        try:
            saved_nodes = saved(args.path)["nodes"] if Path(args.path).exists() else []
        except (manifest.TopologyError, KeyError, TypeError, ValueError, AttributeError):
            saved_nodes = []
        for address in untrusted_addresses(manifest.load_json(args.document), addresses, saved_nodes):
            print(address)
        return 0
    if args.operation in ("notice", "notice-saved"):
        # PATH is the saved membership the discovery is compared with. notice
        # prints the discovery document with `changes_noticed` added;
        # notice-saved prints the record body after a save: unchanged.
        untrusted = []
        if args.untrusted and Path(args.untrusted).exists():
            untrusted = Path(args.untrusted).read_text(encoding="utf-8").split()
        old = saved(args.path)
        if args.operation == "notice-saved":
            body = changes_noticed(old, {"result": "ok", "topology": old,
                                         "saved_membership": {"missing": []}}, untrusted)
            print(json.dumps(body, sort_keys=True))
            return 0
        document = manifest.load_json(args.document)
        document["changes_noticed"] = changes_noticed(old, document, untrusted, args.failure)
        print(json.dumps(document, sort_keys=True))
        return 0
    if args.operation == "changes":
        document = manifest.load_json(args.document)
        writer = TerminalWriter()
        for change, names in document.get("membership_changes", {}).items():
            if names:
                writer.emit(f"Membership {change}: {', '.join(names)}")
        for node in document.get("saved_membership", {}).get("missing", []):
            writer.emit(f"Missing confirmed node: {node['hostname']} (position {node['rank']})")
        for issue in document.get("saved_membership", {}).get("issues", []):
            writer.emit(issue)
        for issue in document.get("discovery_issues", []):
            writer.emit(issue)
        return 0
    if args.operation == "summary":
        # PATH "-": the `topology show --json` document arrives on stdin.
        render_summary(json.load(sys.stdin) if args.path == "-" else observation(args.path))
        return 0
    if args.operation == "discovery":
        source = ({"schema_version": 1, "result": "incomplete", "topology": None,
                   "rejected": [], "discovery_issues": [args.failure]} if args.failure
                  else manifest.load_json(args.document))
        document = discovery_membership(args.path, source, remove_missing=args.remove_missing)
        print(json.dumps(document, sort_keys=True))
        return 0 if document["result"] == "ok" else 1
    document = observation(args.path, args.observations if args.operation == "check" else None)
    if args.json:
        print(json.dumps(document, sort_keys=True))
    else:
        render_observation(document)
    return 0 if document["status"] in {"saved", "ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
