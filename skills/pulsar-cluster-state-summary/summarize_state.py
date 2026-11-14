#!/usr/bin/env python3
"""Print the fields of a saved JSON output of `pulsar inventory --json`,
`pulsar status --json SPEC` or `pulsar doctor --json` that a decision needs.

    python3 skills/pulsar-cluster-state-summary/summarize_state.py FILE

It reads one file and contacts nothing. Where the document does not establish
a value it prints `not observed`: unknown is not absent. The output names
nodes by hostname, so keep it out of tracked files. An error envelope is
shown by its code, whatever command wrote it; a successful result of any
other command is refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

STAMP = "%Y-%m-%dT%H:%M:%SZ"
STATUS_KINDS = ("pulsar-serving-observation", "pulsar-service-status")


def shown(value) -> str:
    """A value for a line: `not observed` for a missing one, yes/no for a flag."""
    if value is None or value == "":
        return "not observed"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def age(stamp, now: datetime) -> str:
    try:
        taken = datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "age not known"
    seconds = int((now - taken).total_seconds())
    if seconds < 0:
        return "time is in the future"
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def service_lines(service: dict, names: dict | None = None) -> list[str]:
    """One service; NAMES maps an inventory node key to that node's hostname."""
    names = names or {}
    name = service.get("served_name") or service.get("service_id") or "service"
    observed, expected = service.get("observed_ranks"), service.get("expected_ranks")
    ranks = (f"ranks {len(observed)}/{len(expected)} observed"
             if isinstance(observed, list) and isinstance(expected, list) else "ranks not observed")
    by_rank = sorted(service.get("ranks") or [], key=lambda rank: str(rank.get("rank") or "0"))
    running_port = by_rank[0].get("observed_api_port") if by_rank else None
    lines = [f"service {name}: {shown(service.get('state'))}, {shown(service.get('ownership'))}, {ranks}",
             f"  API port: runs with {shown(running_port)} · configured now {shown(service.get('api_port'))}",
             f"  safe to stop {shown(service.get('safe_to_stop'))}"
             f" · matches selected spec {shown(service.get('matches_selected_spec'))}"]
    if service.get("spec_id"):
        lines.append(f"  effective spec {service['spec_id']}")
    if service.get("matches_selected_spec") is False:
        lines.append(f"  selected spec {shown(service.get('selected_spec_id'))}")
    for reason in (service.get("reasons") or [])[:3]:
        lines.append(f"  reason: {reason}")
    for rank in service.get("ranks") or []:
        state = "running" if rank.get("running") else shown(rank.get("status"))
        measured = (rank.get("gpu_memory") or {}).get("measured_mib")
        node = names.get(rank.get("node"), rank.get("node"))
        lines.append(f"  rank {shown(rank.get('rank'))} on {shown(node)}: {state}"
                     f", GPU memory {shown(measured)}{' MiB' if measured is not None else ''}")
    return lines


def inventory_lines(document: dict, now: datetime) -> list[str]:
    lines = [f"inventory taken {shown(document.get('generated_at'))} ({age(document.get('generated_at'), now)})"
             f" · saved membership {shown(document.get('topology_id'))}"]
    nodes = document.get("nodes") or {}
    names = {key: node.get("hostname") or key for key, node in nodes.items()}
    unobserved = []
    for key, node in nodes.items():
        probe = node.get("probe_status") or "unknown"
        if probe == "unset" and not (node.get("confirmed") or node.get("node_id")):
            continue  # the placeholder that inventory writes when no other node is confirmed
        if probe == "ok":
            observed = "observed"
        else:
            unobserved.append(names[key])
            reason = f": {node['probe_reason']}" if node.get("probe_reason") else ""
            observed = f"not observed ({probe}{reason})"
        lines.append(f"node {names[key]}: {observed} · memory available "
                     f"{shown(node.get('mem_available_gib'))} of {shown(node.get('mem_total_gib'))} GiB")
    if unobserved:
        lines.append(f"NOT OBSERVED: {', '.join(unobserved)}. Services and GPU processes there are unknown, "
                     "not absent.")
    where = "on the nodes that answered" if unobserved else "on any node"
    services = document.get("services") or []
    if not services:
        lines.append(f"services: none observed {where}")
    for service in services:
        lines.extend(service_lines(service, names))
    processes = document.get("unmanaged_gpu_processes") or []
    if not processes:
        lines.append(f"unmanaged GPU processes: none observed {where}")
    for process in processes:
        node = names.get(process.get("node"), process.get("node"))
        lines.append(f"unmanaged GPU process on {shown(node)}: {shown(process.get('process_name'))}"
                     f", {shown(process.get('used_memory_mib'))} MiB")
    return lines


def doctor_lines(document: dict) -> list[str]:
    lines = [f"doctor result {shown(document.get('result'))}: {shown(document.get('fail'))} fail, "
             f"{shown(document.get('warn'))} warn · memory available on this node "
             f"{shown(document.get('mem_available_gib'))} GiB"]
    for check in document.get("checks") or []:
        if check.get("level") != "ok":
            lines.append(f"  {shown(check.get('level'))} {shown(check.get('id'))}: {shown(check.get('message'))}")
    return lines


def envelope_lines(document: dict, now: datetime) -> list[str]:
    if not document.get("ok"):
        error = document.get("error")
        if not (isinstance(error, dict) and isinstance(error.get("code"), str)):
            raise ValueError("the envelope has no error code")
        lines = [f"error {error['code']}: {shown(error.get('message'))}"]
        for detail in error.get("details") or []:
            if not (isinstance(detail, dict) and ("node" in detail or "node_id" in detail)):
                continue
            if detail.get("node") or detail.get("node_id"):
                who = f"node {detail.get('node') or 'with no name'} ({detail.get('node_id') or 'node ID not known'})"
            else:
                who = "a node that the inventory could not name"
            message = f": {detail['message']}" if detail.get("message") else ""
            lines.append(f"  not observed: {who}{message}")
        return lines
    result = document.get("result")
    kind = result.get("kind") if isinstance(result, dict) else None
    if kind == "pulsar-doctor":
        return doctor_lines(result)
    if kind not in STATUS_KINDS:
        raise ValueError("the result is not a status or doctor document")
    lines = [f"{kind}: state {shown(result.get('state'))} · verified "
             f"{shown(result.get('verified'))} · healthy {shown(result.get('healthy'))}",
             f"  API {shown(result.get('api_url'))}"]
    if result.get("reason"):
        lines.append(f"  reason: {result['reason']}")
    selected, effective = result.get("selected_spec_id"), result.get("spec_id")
    if selected:
        lines.append(f"  selected spec {selected}")
    if effective and effective != selected:
        lines.append(f"  effective spec {effective}")
    if "matches_selected_spec" in result:
        lines.append(f"  matches selected spec {shown(result.get('matches_selected_spec'))}")
    matches = result.get("matches_selected_spec")
    differs = matches is False or (matches is None and bool(selected and effective and effective != selected))
    if result.get("verified") and differs:
        lines.append("  MODIFIED RECIPE: the service runs with execution overrides, not the selected recipe")
    for service in result.get("services") or []:
        lines.extend(service_lines(service))
    return lines


def summarize(document, now: datetime | None = None) -> list[str]:
    """The summary lines of one saved document; ValueError when it is none of the three."""
    now = now or datetime.now(timezone.utc)
    try:
        if isinstance(document, dict):
            if "ok" in document:
                return envelope_lines(document, now)
            if document.get("kind") == "pulsar-doctor":
                return doctor_lines(document)
            if isinstance(document.get("nodes"), dict) and "services" in document:
                return inventory_lines(document, now)
    except (AttributeError, KeyError, TypeError) as error:
        # A part that must be an object or a list is something else: not one of the three documents.
        raise ValueError(f"the document is malformed ({error})") from error
    raise ValueError("not an inventory, status or doctor document")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file", help="the saved JSON output")
    options = parser.parse_args(argv)
    try:
        with open(options.file, encoding="utf-8") as handle:
            document = json.load(handle)
        lines = summarize(document)
    except (OSError, ValueError) as error:
        print(f"summarize_state: {options.file}: {error}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
