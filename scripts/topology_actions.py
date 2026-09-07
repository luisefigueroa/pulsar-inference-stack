#!/usr/bin/env python3
"""Read-only topology observations; canonical membership stays in topology_manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import topology_manifest as manifest
from terminal_format import TerminalWriter


def saved(path):
    topology = manifest.extract_topology(manifest.load_json(path))
    manifest.validate_manifest(topology, require_verified=True)
    return topology


def discovery_membership(path, document):
    """A partial rediscovery must not erase confirmed members."""
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
        document["saved_membership"]["missing"] = [
            {"rank": node["rank"], "node_id": node["node_id"], "hostname": node["hostname"]}
            for node in old["nodes"] if node["node_id"] not in found
        ]
        if document["saved_membership"]["missing"]:
            document["saved_membership"]["issues"].append(
                "Confirmed nodes are missing from discovery. Restore access or investigate their identity and fabric; membership was not changed.")
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


def observation(path, directory=None):
    result = {"schema_version": 1, "kind": "pulsar-topology-observation",
              "status": "missing", "topology": None, "nodes": [], "issues": []}
    if not Path(path).exists():
        result["issues"].append("No saved topology. Run pulsar topology detect, then explicitly configure membership.")
        return result
    try:
        topology = saved(path)
    except (manifest.TopologyError, KeyError, TypeError, ValueError, AttributeError):
        result["status"] = "invalid"
        result["issues"].append("Saved topology is invalid. Inspect it before configuring membership again.")
        return result
    result.update(status="saved", topology=topology)
    if directory is None:
        return result
    directory = Path(directory)
    if (directory / "configuration-error").exists():
        result["issues"].append("Saved SSH configuration cannot be used. Run pulsar ssh-trust check.")
    if not manifest.topology_has_ssh_trust(topology):
        result["issues"].append("SSH identity is not enrolled. Complete first-use setup with pulsar topology setup or pulsar ssh-trust enroll.")
    for node in topology["nodes"]:
        row = {key: node[key] for key in ("rank", "node_id", "hostname")}
        row.update(status="ready", issues=[])
        try:
            probe = manifest.normalize_probe(manifest.load_json(directory / f"rank-{node['rank']}.json"), "live probe")
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
    if (directory / "fabric-failed").exists():
        result["issues"].append("Pairwise RoCE connectivity failed; inspect the saved fabric links.")
    if not (directory / "fabric-checked").exists():
        result["issues"].append("Fabric connectivity could not be checked.")
    result["status"] = "blocked" if result["issues"] or any(row["issues"] for row in result["nodes"]) else "ready"
    return result


def render_observation(document):
    writer = TerminalWriter()
    writer.emit(f"Cluster topology: {document['status']}")
    topology = document.get("topology")
    if topology and not document["nodes"]:
        for node in topology["nodes"]:
            writer.emit(f"Node {node['rank']}: {node['hostname']}", initial_indent="  ")
            writer.emit(f"Control: {node['control']['ip']}", initial_indent="    ")
        writer.emit("Saved membership only; run pulsar topology check for current readiness.")
    for node in document["nodes"]:
        writer.emit(f"Node {node['rank']}: {node['hostname']} — {node['status']}", initial_indent="  ")
        for issue in node["issues"]:
            writer.emit(issue, initial_indent="    ", subsequent_indent="    ")
    for issue in document["issues"]:
        writer.emit(issue, initial_indent="  ", subsequent_indent="  ")
    writer.emit("Model geometry remains the selected recipe's exact configuration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("show", "check", "discovery", "changes"))
    parser.add_argument("path")
    parser.add_argument("--observations")
    parser.add_argument("--document")
    parser.add_argument("--failure")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.operation == "changes":
        document = manifest.load_json(args.document)
        writer = TerminalWriter()
        for change, names in document.get("membership_changes", {}).items():
            if names:
                writer.emit(f"Membership {change}: {', '.join(names)}")
        for node in document.get("saved_membership", {}).get("missing", []):
            writer.emit(f"Missing confirmed node {node['rank']}: {node['hostname']}")
        for issue in document.get("saved_membership", {}).get("issues", []):
            writer.emit(issue)
        for issue in document.get("discovery_issues", []):
            writer.emit(issue)
        return 0
    if args.operation == "discovery":
        source = ({"schema_version": 1, "result": "incomplete", "topology": None,
                   "rejected": [], "discovery_issues": [args.failure]} if args.failure
                  else manifest.load_json(args.document))
        document = discovery_membership(args.path, source)
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
