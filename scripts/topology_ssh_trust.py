#!/usr/bin/env python3
"""Verify topology-bound SSH identity on every confirmed control/RoCE endpoint."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import socket
import subprocess
import sys
from typing import Any

try:
    from scripts.terminal_format import TerminalWriter
    from scripts.topology_manifest import (
        TopologyError,
        extract_topology,
        load_json,
        normalize_host_keys,
        ssh_host_alias,
        validate_manifest,
        validate_ssh_config_file,
    )
except ModuleNotFoundError:
    from terminal_format import TerminalWriter
    from topology_manifest import (
        TopologyError,
        extract_topology,
        load_json,
        normalize_host_keys,
        ssh_host_alias,
        validate_manifest,
        validate_ssh_config_file,
    )

SERVER_KEY_PATTERNS = (
    re.compile(r"Server host key:\s+(\S+)\s+(SHA256:[A-Za-z0-9+/=]+)"),
    re.compile(r"fingerprint is\s+(SHA256:[A-Za-z0-9+/=]+)"),
)


def fingerprints(raw_keys: Any, field: str) -> list[str]:
    return sorted(
        key["fingerprint"] for key in normalize_host_keys(raw_keys, field)
    )


def parse_server_fingerprints(stderr: str) -> list[str]:
    found: set[str] = set()
    for pattern in SERVER_KEY_PATTERNS:
        for match in pattern.finditer(stderr):
            fingerprint = match.group(match.lastindex or 1).rstrip("=")
            found.add(fingerprint)
    return sorted(found)


def resolved_ipv4(alias: str) -> list[str]:
    try:
        records = socket.getaddrinfo(
            alias,
            22,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return []
    return sorted({record[4][0] for record in records})


def summarize_stderr(stderr: str) -> str:
    ignored = (
        "debug1:",
        "debug2:",
        "debug3:",
        "Pseudo-terminal will not be allocated",
    )
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(ignored)
    ]
    return lines[-1][:240] if lines else "SSH did not return a diagnostic"


def classify_failed_connection(
    *,
    expected: set[str],
    observed: set[str],
    owners: dict[str, int],
    rank: int,
    stderr: str,
    endpoint_kind: str,
    endpoint: str,
    resolved: list[str] | None = None,
) -> tuple[str, str]:
    other_ranks = sorted(
        {
            owners[item]
            for item in observed
            if item in owners and owners[item] != rank
        }
    )
    if other_ranks:
        return (
            "wrong-node-address-collision",
            "endpoint presented a host key enrolled to rank(s) "
            + ",".join(str(item) for item in other_ranks),
        )
    if observed - expected:
        return (
            "host-key-changed",
            "endpoint presented an unenrolled SSH host key",
        )
    lower = stderr.lower()
    if "knownhostscommand" in lower or "host key verification failed" in lower:
        return (
            "ssh-trust-policy-failed",
            summarize_stderr(stderr),
        )
    if "permission denied" in lower:
        return (
            "authentication-failed",
            "host key matched, but SSH user authentication failed",
        )
    if (
        endpoint_kind == "control"
        and resolved
        and endpoint not in set(resolved)
    ):
        return (
            "stale-control-endpoint",
            f"saved control IP is {endpoint}; alias currently resolves to "
            + ",".join(resolved),
        )
    return "endpoint-unreachable", summarize_stderr(stderr)


def endpoint_plan(
    topology: dict[str, Any], node: dict[str, Any]
) -> list[dict[str, Any]]:
    rank = node["rank"]
    endpoints: list[dict[str, Any]] = []
    if rank != 0:
        endpoints.append(
            {
                "kind": "control",
                "interface": node["control"]["interface"],
                "endpoint": node["control"]["ip"],
                "source_rank": 0,
            }
        )
    seen: set[str] = set()
    for link in topology["links"]:
        a, b = link["ranks"]
        if rank == a:
            side = "a"
            source_rank = b
        elif rank == b:
            side = "b"
            source_rank = a
        else:
            continue
        for rail in link["rails"]:
            target = rail[side]
            endpoint = target["ip"]
            if endpoint in seen:
                continue
            seen.add(endpoint)
            endpoints.append(
                {
                    "kind": "roce",
                    "interface": target["netdev"],
                    "endpoint": endpoint,
                    "source_rank": source_rank,
                }
            )
    return endpoints


def probe_identity(
    argv: list[str],
    probe_source: str | None,
    timeout: int,
) -> tuple[int, dict[str, Any] | None, str]:
    try:
        proc = subprocess.run(
            argv,
            input=probe_source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = str(exc.stderr or "") + "\nSSH identity probe timed out"
        return 124, None, stderr
    except OSError as exc:
        return 127, None, str(exc)
    document = None
    if proc.returncode == 0:
        try:
            value = json.loads(proc.stdout)
            if isinstance(value, dict):
                document = value
        except json.JSONDecodeError:
            pass
    return proc.returncode, document, proc.stderr


def check_local_node(
    node: dict[str, Any],
    probe_path: pathlib.Path,
    timeout: int,
) -> dict[str, Any]:
    expected = fingerprints(node["ssh_host_keys"], "local.expected_keys")
    rc, observed, stderr = probe_identity(
        [
            sys.executable,
            str(probe_path),
            "--local",
            "--ssh-host",
            "local",
            "--identity-only",
            "--include-ssh-host-keys",
        ],
        None,
        timeout,
    )
    result = {
        "kind": "local",
        "interface": "local",
        "endpoint": "local",
        "expected_fingerprints": expected,
        "observed_fingerprints": [],
        "observed_node_id": None,
        "ok": False,
        "state": "probe-failed",
        "detail": summarize_stderr(stderr),
    }
    if rc != 0 or observed is None:
        return result
    result["observed_node_id"] = observed.get("node_id")
    try:
        actual = fingerprints(
            observed.get("ssh_host_keys"), "local.observed_keys"
        )
    except TopologyError as exc:
        result["detail"] = str(exc)
        return result
    result["observed_fingerprints"] = actual
    if observed.get("node_id") != node["node_id"]:
        result["state"] = "replacement-node"
        result["detail"] = "local machine identity differs from topology"
    elif actual != expected:
        result["state"] = "host-key-changed"
        result["detail"] = "local SSH host-key set differs from topology"
    else:
        result.update(ok=True, state="pass", detail=None)
    return result


def check_remote_endpoint(
    *,
    node: dict[str, Any],
    endpoint: dict[str, Any],
    config_path: pathlib.Path,
    probe_path: pathlib.Path,
    ssh_bin: str,
    timeout: int,
    owners: dict[str, int],
    node_id_owners: dict[str, int],
    source_node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alias = ssh_host_alias(node["ssh_host"])
    expected = fingerprints(
        node["ssh_host_keys"], f"rank_{node['rank']}.expected_keys"
    )
    remote_command = " ".join(
        [
            "python3",
            "-",
            "--identity-only",
            "--include-ssh-host-keys",
            "--ssh-host",
            shlex.quote(alias),
        ]
    )
    argv = [
        ssh_bin,
        "-vv",
        "-F",
        str(config_path),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"HostName={endpoint['endpoint']}",
        "-o",
        f"HostKeyAlias={alias}",
    ]
    if source_node is not None:
        argv.extend(["-J", ssh_host_alias(source_node["ssh_host"])])
    argv.extend(["--", alias, remote_command])
    rc, observed, stderr = probe_identity(
        argv,
        probe_path.read_text(encoding="utf-8"),
        timeout + 5,
    )
    observed_fingerprints = parse_server_fingerprints(stderr)
    if source_node is not None:
        source_rank = source_node["rank"]
        observed_fingerprints = [
            item
            for item in observed_fingerprints
            if owners.get(item) != source_rank
        ]
    result = {
        **endpoint,
        "expected_fingerprints": expected,
        "observed_fingerprints": observed_fingerprints,
        "observed_node_id": observed.get("node_id") if observed else None,
        "ok": False,
        "state": "probe-failed",
        "detail": None,
    }
    if rc != 0:
        resolutions = (
            resolved_ipv4(alias) if endpoint["kind"] == "control" else []
        )
        state, detail = classify_failed_connection(
            expected=set(expected),
            observed=set(observed_fingerprints),
            owners=owners,
            rank=node["rank"],
            stderr=stderr,
            endpoint_kind=endpoint["kind"],
            endpoint=endpoint["endpoint"],
            resolved=resolutions,
        )
        result.update(state=state, detail=detail)
        return result
    if observed is None:
        result["detail"] = "SSH succeeded but identity probe returned invalid JSON"
        return result
    actual_node_id = str(observed.get("node_id") or "")
    if actual_node_id != node["node_id"]:
        owner = node_id_owners.get(actual_node_id)
        if owner is not None:
            result["state"] = "wrong-node-address-collision"
            result["detail"] = f"endpoint returned node identity enrolled to rank {owner}"
        else:
            result["state"] = "replacement-node"
            result["detail"] = "endpoint returned an unenrolled machine identity"
        return result
    try:
        actual_keys = fingerprints(
            observed.get("ssh_host_keys"),
            f"rank_{node['rank']}.observed_keys",
        )
    except TopologyError as exc:
        result["detail"] = str(exc)
        return result
    result["observed_fingerprints"] = actual_keys
    if actual_keys != expected:
        result["state"] = "host-key-changed"
        result["detail"] = "remote SSH host-key set differs from topology"
        return result
    result.update(ok=True, state="pass", detail=None)
    return result


def check_topology(
    topology: dict[str, Any],
    *,
    topology_path: pathlib.Path,
    config_path: pathlib.Path,
    probe_path: pathlib.Path,
    ssh_bin: str,
    timeout: int,
) -> dict[str, Any]:
    validate_manifest(
        topology, require_verified=True, require_ssh_trust=True
    )
    validate_ssh_config_file(
        topology,
        str(config_path),
        topology_path=str(topology_path),
    )
    owners: dict[str, int] = {}
    node_id_owners: dict[str, int] = {}
    for node in topology["nodes"]:
        node_id_owners[node["node_id"]] = node["rank"]
        for fingerprint in fingerprints(
            node["ssh_host_keys"], f"rank_{node['rank']}.keys"
        ):
            owners[fingerprint] = node["rank"]

    nodes = []
    for node in topology["nodes"]:
        endpoints = []
        if node["rank"] == 0:
            endpoints.append(check_local_node(node, probe_path, timeout))
        for endpoint in endpoint_plan(topology, node):
            source_rank = endpoint["source_rank"]
            source_node = (
                topology["nodes"][source_rank]
                if source_rank != 0
                else None
            )
            endpoints.append(
                check_remote_endpoint(
                    node=node,
                    endpoint=endpoint,
                    config_path=config_path,
                    probe_path=probe_path,
                    ssh_bin=ssh_bin,
                    timeout=timeout,
                    owners=owners,
                    node_id_owners=node_id_owners,
                    source_node=source_node,
                )
            )
        nodes.append(
            {
                "rank": node["rank"],
                "node_id": node["node_id"],
                "hostname": node["hostname"],
                "ssh_host": node["ssh_host"],
                "ok": all(item["ok"] for item in endpoints),
                "endpoints": endpoints,
            }
        )
    ok = all(node["ok"] for node in nodes)
    return {
        "schema_version": 1,
        "kind": "topology-ssh-trust-check",
        "topology_id": topology["topology_id"],
        "ok": ok,
        "state": "pass" if ok else "failed",
        "nodes": nodes,
    }


def short_fingerprints(values: list[str]) -> str:
    return ",".join(value[:20] for value in values)


def doctor_rows(report: dict[str, Any]) -> None:
    for node in report.get("nodes") or []:
        rank = node["rank"]
        label = "this node" if rank == 0 else f"cluster node {rank + 1}"
        failed = [item for item in node["endpoints"] if not item["ok"]]
        if not failed:
            endpoints = node["endpoints"]
            expected = endpoints[0]["expected_fingerprints"]
            message = (
                f"{label} · topology-bound SSH identity passed on "
                f"{len(endpoints)} endpoint(s) · key {short_fingerprints(expected)}"
            )
            print(f"ok\tssh_trust_rank_{rank}\t{message}")
            continue
        for index, item in enumerate(failed):
            observed = short_fingerprints(item["observed_fingerprints"]) or "none"
            expected = short_fingerprints(item["expected_fingerprints"])
            expected_node_id = str(node.get("node_id") or "none")[:12]
            observed_node_id = str(item.get("observed_node_id") or "none")[:12]
            via = ""
            if item.get("source_rank") not in (None, 0):
                via = f" via cluster node {item['source_rank'] + 1}"
            message = (
                f"{label} · {item['kind']} {item['endpoint']}{via} · "
                f"{item['state']} · node expected {expected_node_id}; "
                f"observed {observed_node_id} · key expected {expected}; "
                f"observed {observed}"
            )
            if item.get("detail"):
                message += f" · {item['detail']}"
            print(f"fail\tssh_trust_rank_{rank}_{index}\t{message}")


def render_human(report: dict[str, Any]) -> None:
    term = TerminalWriter()
    term.emit("TOPOLOGY SSH TRUST")
    term.field("Cluster", report["topology_id"][:12])
    term.field("Result", "PASS" if report["ok"] else "FAIL")
    for node in report["nodes"]:
        term.blank()
        term.emit(
            f"cluster node {node['rank'] + 1} · {node['hostname']}",
            initial_indent="  ",
            subsequent_indent="    ",
        )
        term.field("Alias", node["ssh_host"], indent=4)
        for endpoint in node["endpoints"]:
            status = "PASS" if endpoint["ok"] else endpoint["state"]
            via = ""
            if endpoint.get("source_rank") not in (None, 0):
                via = f" via cluster node {endpoint['source_rank'] + 1}"
            value = f"{endpoint['endpoint']}{via} · {status}"
            if endpoint.get("detail"):
                value += f" · {endpoint['detail']}"
            term.field(endpoint["kind"].upper(), value, indent=4)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--topology", required=True)
    check.add_argument("--ssh-config", required=True)
    check.add_argument("--probe", required=True)
    check.add_argument("--ssh-bin", default="ssh")
    check.add_argument("--timeout", type=int, default=8)
    check.add_argument("--json", action="store_true")
    rows = sub.add_parser("doctor-rows")
    rows.add_argument("report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "doctor-rows":
            doctor_rows(load_json(args.report))
            return 0
        topology = extract_topology(load_json(args.topology))
        report = check_topology(
            topology,
            topology_path=pathlib.Path(args.topology),
            config_path=pathlib.Path(args.ssh_config),
            probe_path=pathlib.Path(args.probe),
            ssh_bin=args.ssh_bin,
            timeout=args.timeout,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            render_human(report)
        return 0 if report["ok"] else 1
    except (TopologyError, OSError) as exc:
        print(f"topology-ssh-trust: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
