#!/usr/bin/env python3
"""Assemble, validate, render, and atomically write cluster topology manifests."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import itertools
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

try:
    from scripts.terminal_format import TerminalWriter
except ModuleNotFoundError:
    from terminal_format import TerminalWriter

try:
    from scripts.platform_reference import (
        PlatformReferenceError,
        load_current_platform,
    )
except ModuleNotFoundError:
    from platform_reference import (  # type: ignore[no-redef]
        PlatformReferenceError,
        load_current_platform,
    )


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION)
PROBE_SCHEMA_VERSION = 2
SAFE_ENDPOINT = re.compile(r"^[A-Za-z0-9._:@%+-]+$")
SAFE_IFACE = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
SAFE_SSH_ALIAS = re.compile(r"^[A-Za-z0-9._+-]+$")
SAFE_KEY_ALGORITHM = re.compile(r"^[A-Za-z0-9@._+-]+$")


class TopologyError(ValueError):
    pass


def fail(message: str) -> None:
    raise TopologyError(message)


def load_json(path: str | pathlib.Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path}: expected a JSON object")
    return value


def clean_text(value: Any, field: str) -> str:
    text = str(value or "")
    if not text or "\t" in text or "\r" in text or "\n" in text:
        fail(f"{field}: missing or contains control characters")
    return text


def safe_endpoint(value: Any, field: str) -> str:
    text = clean_text(value, field)
    if text.startswith("-") or not SAFE_ENDPOINT.fullmatch(text):
        fail(f"{field}: unsafe endpoint {text!r}")
    return text


def safe_interface(value: Any, field: str) -> str:
    text = clean_text(value, field)
    if not SAFE_IFACE.fullmatch(text):
        fail(f"{field}: unsafe interface {text!r}")
    return text


def valid_ip(value: Any, field: str) -> str:
    text = clean_text(value, field)
    try:
        return str(ipaddress.ip_address(text))
    except ValueError as exc:
        fail(f"{field}: {exc}")


def valid_cidr(value: Any, field: str) -> str:
    text = clean_text(value, field)
    try:
        return str(ipaddress.ip_interface(text))
    except ValueError as exc:
        fail(f"{field}: {exc}")


def decode_host_public_key(public_key: str, field: str) -> tuple[bytes, str]:
    if len(public_key) % 4 == 1:
        fail(f"{field}: SSH host public key is not valid base64")
    padded = public_key + "=" * (-len(public_key) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (ValueError, binascii.Error):
        fail(f"{field}: SSH host public key is not valid base64")
    if len(raw) < 5:
        fail(f"{field}: SSH host public key is malformed")
    algorithm_size = int.from_bytes(raw[:4], byteorder="big")
    algorithm_raw = raw[4 : 4 + algorithm_size]
    if len(algorithm_raw) != algorithm_size:
        fail(f"{field}: SSH host public key is malformed")
    try:
        embedded_algorithm = algorithm_raw.decode("ascii")
    except UnicodeDecodeError:
        fail(f"{field}: SSH host public key algorithm is not ASCII")
    return raw, embedded_algorithm


def host_key_fingerprint(public_key: str, field: str = "public_key") -> str:
    raw, _ = decode_host_public_key(public_key, field)
    digest = hashlib.sha256(raw).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def normalize_host_keys(
    raw_keys: Any,
    field: str,
    *,
    required: bool = True,
) -> list[dict[str, str]]:
    if not isinstance(raw_keys, list):
        fail(f"{field}: expected an array")
    result: list[dict[str, str]] = []
    seen_algorithms: set[str] = set()
    seen_fingerprints: set[str] = set()
    for index, raw in enumerate(raw_keys):
        if not isinstance(raw, dict):
            fail(f"{field}[{index}]: expected an object")
        algorithm = clean_text(
            raw.get("algorithm"), f"{field}[{index}].algorithm"
        )
        if not SAFE_KEY_ALGORITHM.fullmatch(algorithm):
            fail(f"{field}[{index}].algorithm: unsafe value")
        public_key = clean_text(
            raw.get("public_key"), f"{field}[{index}].public_key"
        )
        key_blob, embedded_algorithm = decode_host_public_key(
            public_key, f"{field}[{index}].public_key"
        )
        if embedded_algorithm != algorithm:
            fail(
                f"{field}[{index}].algorithm: does not match public key blob"
            )
        digest = hashlib.sha256(key_blob).digest()
        fingerprint = "SHA256:" + base64.b64encode(digest).decode(
            "ascii"
        ).rstrip("=")
        supplied = raw.get("fingerprint")
        if supplied is not None and str(supplied) != fingerprint:
            fail(f"{field}[{index}].fingerprint: does not match public key")
        if algorithm in seen_algorithms:
            fail(f"{field}: duplicate algorithm {algorithm}")
        if fingerprint in seen_fingerprints:
            fail(f"{field}: duplicate fingerprint")
        seen_algorithms.add(algorithm)
        seen_fingerprints.add(fingerprint)
        result.append(
            {
                "algorithm": algorithm,
                "fingerprint": fingerprint,
                "public_key": public_key,
            }
        )
    result.sort(key=lambda item: (item["algorithm"], item["fingerprint"]))
    if required and not result:
        fail(f"{field}: at least one SSH host key is required")
    return result


def ssh_host_alias(ssh_host: Any, field: str = "ssh_host") -> str:
    value = safe_endpoint(ssh_host, field)
    if value == "local":
        return value
    if "@" in value or ":" in value or not SAFE_SSH_ALIAS.fullmatch(value):
        fail(
            f"{field}: trusted SSH aliases must be plain host aliases "
            "(no user, port, or address syntax)"
        )
    return value


def topology_has_ssh_trust(topology: dict[str, Any]) -> bool:
    return (
        topology.get("schema_version") == SCHEMA_VERSION
        and (topology.get("validation") or {}).get("ssh_identity_enrolled")
        is True
    )


def normalize_probe(raw: dict[str, Any], path: str) -> dict[str, Any]:
    probe_schema = raw.get("probe_schema_version")
    if probe_schema not in (1, PROBE_SCHEMA_VERSION):
        fail(f"{path}: unsupported probe schema")
    node = {
        "node_id": clean_text(raw.get("node_id"), f"{path}.node_id"),
        "hostname": clean_text(raw.get("hostname"), f"{path}.hostname"),
        "ssh_host": safe_endpoint(raw.get("ssh_host"), f"{path}.ssh_host"),
        "local": bool(raw.get("local")),
        "arch": str(raw.get("arch") or ""),
        "gpu": str(raw.get("gpu") or ""),
        "docker_ok": bool(raw.get("docker_ok")),
        "docker_nvidia": bool(raw.get("docker_nvidia")),
        "qualified": bool(raw.get("qualified")),
        "reject_reasons": [str(x) for x in raw.get("reject_reasons") or []],
    }
    control = raw.get("control") or {}
    try:
        node["control"] = {
            "interface": safe_interface(
                control.get("interface"), f"{path}.control.interface"
            ),
            "ip": valid_ip(control.get("ip"), f"{path}.control.ip"),
        }
    except TopologyError:
        if node["qualified"]:
            raise
        node["control"] = {"interface": "", "ip": ""}

    rdma: list[dict[str, Any]] = []
    for index, raw_link in enumerate(raw.get("rdma") or []):
        hca = safe_interface(raw_link.get("hca"), f"{path}.rdma[{index}].hca")
        netdev = safe_interface(
            raw_link.get("netdev"), f"{path}.rdma[{index}].netdev"
        )
        cidrs = [
            valid_cidr(cidr, f"{path}.rdma[{index}].cidrs")
            for cidr in raw_link.get("cidrs") or []
        ]
        rdma.append({"hca": hca, "netdev": netdev, "cidrs": sorted(set(cidrs))})
    node["rdma"] = rdma
    node["ssh_host_keys"] = normalize_host_keys(
        raw.get("ssh_host_keys") or [],
        f"{path}.ssh_host_keys",
        required=probe_schema >= PROBE_SCHEMA_VERSION,
    )
    return node


def rdma_endpoints(node: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for link in node.get("rdma") or []:
        for cidr in link.get("cidrs") or []:
            interface = ipaddress.ip_interface(cidr)
            network = str(interface.network)
            result.setdefault(network, []).append(
                {
                    "hca": link["hca"],
                    "netdev": link["netdev"],
                    "ip": str(interface.ip),
                }
            )
    return result


def rails_between(
    first: dict[str, Any], second: dict[str, Any]
) -> list[dict[str, Any]]:
    a_endpoints = rdma_endpoints(first)
    b_endpoints = rdma_endpoints(second)
    rails: list[dict[str, Any]] = []
    for network in sorted(set(a_endpoints) & set(b_endpoints)):
        candidates = [
            (a, b)
            for a in a_endpoints[network]
            for b in b_endpoints[network]
            if a["ip"] != b["ip"]
        ]
        if not candidates:
            continue
        a, b = sorted(
            candidates,
            key=lambda pair: (
                pair[0]["hca"],
                pair[0]["ip"],
                pair[1]["hca"],
                pair[1]["ip"],
            ),
        )[0]
        rails.append({"network": network, "a": a, "b": b})
    return rails


def largest_mesh(
    local: dict[str, Any], remotes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if len(remotes) <= 14:
        for size in range(len(remotes), -1, -1):
            for subset in itertools.combinations(remotes, size):
                nodes = [local, *subset]
                if all(
                    rails_between(nodes[a], nodes[b])
                    for a in range(len(nodes))
                    for b in range(a + 1, len(nodes))
                ):
                    return nodes
    selected = [local]
    for candidate in remotes:
        if all(rails_between(candidate, present) for present in selected):
            selected.append(candidate)
    return selected


def topology_digest(topology: dict[str, Any]) -> str:
    identity = {
        "nodes": topology["nodes"],
        "links": topology["links"],
    }
    payload = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def assemble(paths: list[str], existing_path: str | None) -> dict[str, Any]:
    if not paths:
        fail("no node probes supplied")
    probes = [normalize_probe(load_json(path), path) for path in paths]
    local_candidates = [probe for probe in probes if probe["local"]]
    if len(local_candidates) != 1:
        fail("discovery must contain exactly one local probe")
    local = local_candidates[0]

    rejected: list[dict[str, Any]] = []
    if not local["qualified"]:
        return {
            "schema_version": LEGACY_SCHEMA_VERSION,
            "result": "incomplete",
            "topology": None,
            "rejected": [
                {
                    "ssh_host": local["ssh_host"],
                    "hostname": local["hostname"],
                    "reasons": local["reject_reasons"],
                }
            ],
        }

    by_id: dict[str, dict[str, Any]] = {local["node_id"]: local}

    def ssh_preference(probe: dict[str, Any]) -> tuple[int, str]:
        host = probe["ssh_host"].lower()
        hostname = probe["hostname"].lower()
        if host in (hostname, f"{hostname}.local"):
            return (0, host)
        if host == str(probe["control"]["ip"]).lower():
            return (1, host)
        rdma_ips = {
            str(ipaddress.ip_interface(cidr).ip)
            for link in probe.get("rdma") or []
            for cidr in link.get("cidrs") or []
        }
        if probe["ssh_host"] in rdma_ips:
            return (3, host)
        return (2, host)

    for probe in probes:
        if probe is local:
            continue
        if not probe["qualified"]:
            rejected.append(
                {
                    "ssh_host": probe["ssh_host"],
                    "hostname": probe["hostname"],
                    "reasons": probe["reject_reasons"],
                }
            )
            continue
        if probe["node_id"] in by_id:
            current = by_id[probe["node_id"]]
            if current is not local and ssh_preference(probe) < ssh_preference(current):
                by_id[probe["node_id"]] = probe
                probe = current
            rejected.append(
                {
                    "ssh_host": probe["ssh_host"],
                    "hostname": probe["hostname"],
                    "reasons": ["duplicate node identity"],
                }
            )
            continue
        by_id[probe["node_id"]] = probe

    existing_order: dict[str, int] = {}
    if existing_path and pathlib.Path(existing_path).is_file():
        old = load_json(existing_path)
        if "topology" in old:
            old = old.get("topology") or {}
        for item in old.get("nodes") or []:
            node_id = str(item.get("node_id") or "")
            rank = item.get("rank")
            if node_id and isinstance(rank, int):
                existing_order[node_id] = rank

    remotes = [node for node_id, node in by_id.items() if node_id != local["node_id"]]
    remotes.sort(
        key=lambda node: (
            existing_order.get(node["node_id"], 1_000_000),
            node["hostname"].lower(),
            node["node_id"],
        )
    )
    selected = largest_mesh(local, remotes)
    selected_ids = {node["node_id"] for node in selected}
    for node in remotes:
        if node["node_id"] not in selected_ids:
            rejected.append(
                {
                    "ssh_host": node["ssh_host"],
                    "hostname": node["hostname"],
                    "reasons": [
                        "not part of the largest directly connected RoCE group containing this node"
                    ],
                }
            )

    # Discovery proves membership and RoCE geometry, but it never promotes
    # SSH identity. Only the explicit enrollment ceremony may create schema 2.
    topology_schema = LEGACY_SCHEMA_VERSION
    nodes: list[dict[str, Any]] = []
    for rank, probe in enumerate(selected):
        nodes.append(
            {
                "rank": rank,
                "node_id": probe["node_id"],
                "hostname": probe["hostname"],
                "ssh_host": "local" if rank == 0 else probe["ssh_host"],
                "control": probe["control"],
                "gpu": probe["gpu"],
                "rdma": probe["rdma"],
            }
        )

    links: list[dict[str, Any]] = []
    for a in range(len(nodes)):
        for b in range(a + 1, len(nodes)):
            rails = rails_between(nodes[a], nodes[b])
            links.append({"ranks": [a, b], "rails": rails})

    rail_counts = [len(link["rails"]) for link in links]
    topology = {
        "schema_version": topology_schema,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "links": links,
        "validation": {
            "class": "roce-full-mesh",
            "full_mesh": all(rail_counts) if links else True,
            "connectivity_verified": False,
            "min_rails_per_pair": min(rail_counts) if rail_counts else 0,
        },
    }
    topology["topology_id"] = topology_digest(topology)
    validate_manifest(topology)
    return {
        "schema_version": topology_schema,
        "result": "ok",
        "topology": topology,
        "rejected": rejected,
    }


def validate_manifest(
    topology: dict[str, Any],
    require_verified: bool = False,
    require_ssh_trust: bool = False,
) -> None:
    schema_version = topology.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        fail("unsupported topology schema")
    nodes = topology.get("nodes")
    links = topology.get("links")
    validation = topology.get("validation")
    if not isinstance(nodes, list) or not nodes:
        fail("topology.nodes must be a non-empty array")
    if not isinstance(links, list) or not isinstance(validation, dict):
        fail("topology.links/validation malformed")
    if [node.get("rank") for node in nodes] != list(range(len(nodes))):
        fail("node ranks must be contiguous and start at zero")

    node_ids: set[str] = set()
    control_ips: set[str] = set()
    host_key_owners: dict[str, int] = {}
    try:
        expected_gpu = load_current_platform()["gpu_name"]
    except PlatformReferenceError as exc:
        fail(str(exc))
    for rank, node in enumerate(nodes):
        node_id = clean_text(node.get("node_id"), f"nodes[{rank}].node_id")
        if node_id in node_ids:
            fail("duplicate node_id")
        node_ids.add(node_id)
        clean_text(node.get("hostname"), f"nodes[{rank}].hostname")
        ssh_host = safe_endpoint(
            node.get("ssh_host"), f"nodes[{rank}].ssh_host"
        )
        if schema_version == SCHEMA_VERSION:
            ssh_host_alias(ssh_host, f"nodes[{rank}].ssh_host")
            keys = normalize_host_keys(
                node.get("ssh_host_keys"),
                f"nodes[{rank}].ssh_host_keys",
            )
            if keys != node.get("ssh_host_keys"):
                fail(f"nodes[{rank}].ssh_host_keys: not canonical")
            for key in keys:
                owner = host_key_owners.get(key["fingerprint"])
                if owner is not None and owner != rank:
                    fail(
                        "SSH host-key fingerprint is shared by multiple nodes"
                    )
                host_key_owners[key["fingerprint"]] = rank
        elif node.get("ssh_host_keys") is not None:
            fail("legacy topology cannot carry SSH host keys")
        control = node.get("control") or {}
        control_ip = valid_ip(control.get("ip"), f"nodes[{rank}].control.ip")
        safe_interface(
            control.get("interface"), f"nodes[{rank}].control.interface"
        )
        if control_ip in control_ips:
            fail("duplicate control-plane IP")
        control_ips.add(control_ip)
        if node.get("gpu") != expected_gpu:
            fail(f"rank {rank}: not an {expected_gpu}")
        for index, rdma in enumerate(node.get("rdma") or []):
            safe_interface(rdma.get("hca"), f"nodes[{rank}].rdma[{index}].hca")
            safe_interface(
                rdma.get("netdev"), f"nodes[{rank}].rdma[{index}].netdev"
            )
            for cidr in rdma.get("cidrs") or []:
                valid_cidr(cidr, f"nodes[{rank}].rdma[{index}].cidr")

    expected_pairs = {
        (a, b) for a in range(len(nodes)) for b in range(a + 1, len(nodes))
    }
    observed_pairs: set[tuple[int, int]] = set()
    rail_counts: list[int] = []
    for index, link in enumerate(links):
        ranks = link.get("ranks")
        if (
            not isinstance(ranks, list)
            or len(ranks) != 2
            or not all(isinstance(rank, int) for rank in ranks)
        ):
            fail(f"links[{index}].ranks malformed")
        pair = (ranks[0], ranks[1])
        if pair not in expected_pairs or pair in observed_pairs:
            fail(f"links[{index}]: invalid or duplicate rank pair")
        observed_pairs.add(pair)
        rails = link.get("rails")
        if not isinstance(rails, list) or not rails:
            fail(f"links[{index}]: no shared RoCE rails")
        rail_counts.append(len(rails))
        for rail_index, rail in enumerate(rails):
            valid_cidr(
                rail.get("network"), f"links[{index}].rails[{rail_index}].network"
            )
            for side in ("a", "b"):
                endpoint = rail.get(side) or {}
                safe_interface(
                    endpoint.get("hca"),
                    f"links[{index}].rails[{rail_index}].{side}.hca",
                )
                safe_interface(
                    endpoint.get("netdev"),
                    f"links[{index}].rails[{rail_index}].{side}.netdev",
                )
                valid_ip(
                    endpoint.get("ip"),
                    f"links[{index}].rails[{rail_index}].{side}.ip",
                )
    if observed_pairs != expected_pairs:
        fail("links do not cover every rank pair")
    if validation.get("class") != "roce-full-mesh":
        fail("unsupported validation class")
    if validation.get("full_mesh") is not True:
        fail("topology is not a full mesh")
    actual_min = min(rail_counts) if rail_counts else 0
    if validation.get("min_rails_per_pair") != actual_min:
        fail("min_rails_per_pair does not match links")
    if require_verified and validation.get("connectivity_verified") is not True:
        fail("pairwise RoCE connectivity has not been verified")
    if schema_version == SCHEMA_VERSION:
        if validation.get("ssh_identity_enrolled") is not True:
            fail("topology SSH identity is not enrolled")
    elif validation.get("ssh_identity_enrolled") not in (None, False):
        fail("legacy topology cannot claim SSH identity enrollment")
    if require_ssh_trust and not topology_has_ssh_trust(topology):
        fail(
            "topology SSH identity is not enrolled; run "
            "scripts/topology-ssh-trust.sh enroll"
        )

    topology_id = clean_text(topology.get("topology_id"), "topology_id")
    if topology_id != topology_digest(topology):
        fail("topology_id digest mismatch")


def extract_topology(document: dict[str, Any]) -> dict[str, Any]:
    topology = document.get("topology") if "topology" in document else document
    if not isinstance(topology, dict):
        fail("document has no usable topology")
    return topology


def render(document: dict[str, Any], skipped_ssh: int = 0) -> None:
    topology = extract_topology(document)
    validate_manifest(topology)
    if skipped_ssh < 0:
        fail("skipped SSH address count cannot be negative")

    term = TerminalWriter()

    def field(label: str, value: str, indent: int = 0) -> None:
        term.field(label, value, indent=indent, label_width=12)

    nodes = topology["nodes"]
    validation = topology["validation"]
    link_count = validation["min_rails_per_pair"]
    link_word = "link" if link_count == 1 else "links"
    node_word = "system" if len(nodes) == 1 else "systems"
    check_count = sum(2 * len(link["rails"]) for link in topology["links"])

    term.emit("CLUSTER DISCOVERY")
    try:
        display_name = load_current_platform()["display_name"]
    except PlatformReferenceError as exc:
        fail(str(exc))
    field("Nodes", f"{len(nodes)} {display_name} {node_word}")
    if len(nodes) == 1:
        field("Fabric", "single node · no cluster links required")
        field("Checks", "not needed · no other cluster nodes found")
    else:
        field(
            "Fabric",
            f"full mesh · at least {link_count} RoCE {link_word} "
            "between each node pair",
        )
        if validation.get("connectivity_verified") is True:
            field(
                "Checks",
                f"PASS · {check_count}/{check_count} connections reachable",
            )
        else:
            field("Checks", "not yet verified")
    field("Cluster ID", topology["topology_id"][:12])

    term.blank()
    term.emit("NODES")
    for node in nodes:
        hcas = [link["hca"] for link in node.get("rdma") or []]
        if node["rank"] == 0:
            position = "this node"
        else:
            position = f"cluster node {node['rank'] + 1}"
        if node["rank"] > 0:
            term.blank()
        term.emit(
            f"{position} · {node['hostname']}",
            initial_indent="  ",
            subsequent_indent="    ",
        )
        field("SSH", node["ssh_host"], indent=4)
        if topology_has_ssh_trust(topology):
            fingerprints = [
                key["fingerprint"] for key in node["ssh_host_keys"]
            ]
            field(
                "SSH trust",
                f"enrolled · {len(fingerprints)} host "
                f"{'key' if len(fingerprints) == 1 else 'keys'}",
                indent=4,
            )
        else:
            field("SSH trust", "legacy · fingerprints not enrolled", indent=4)
        field(
            "Cluster IP",
            f"{node['control']['ip']} · interface "
            f"{node['control']['interface']}",
            indent=4,
        )
        adapter_word = "adapter" if len(hcas) == 1 else "adapters"
        field(
            "RoCE",
            f"{len(hcas)} {adapter_word} · {', '.join(hcas) if hcas else 'none'}",
            indent=4,
        )

    rejected = document.get("rejected") or []
    if rejected or skipped_ssh:
        term.blank()
        term.emit("DISCOVERY NOTES")
    for item in rejected:
        ssh_host = item.get("ssh_host") or "?"
        hostname = item.get("hostname") or "?"
        reasons = item.get("reasons") or ["not qualified"]
        if reasons == ["duplicate node identity"]:
            detail = f"{ssh_host} · same system already listed as {hostname}"
        else:
            detail = f"{ssh_host} · {'; '.join(reasons)}"
        field("Ignored", detail)
    if skipped_ssh:
        address_word = "address" if skipped_ssh == 1 else "addresses"
        field(
            "SSH",
            f"{skipped_ssh} advertised {address_word} could not be checked "
            "with saved keys",
        )
        field(
            "Help",
            "These are usually unrelated devices. If a GB10 is missing, set "
            "up key-based SSH and rerun with --candidate HOST.",
        )


def render_save(document: dict[str, Any], destination: str) -> None:
    topology = extract_topology(document)
    validate_manifest(topology, require_verified=True)
    clean_text(destination, "destination")
    term = TerminalWriter()

    def field(label: str, value: str) -> None:
        term.field(label, value, label_width=12)

    nodes = topology["nodes"]
    node_word = "node" if len(nodes) == 1 else "nodes"

    term.emit("SAVE CLUSTER MEMBERSHIP")
    field("File", destination)
    field("Membership", f"{len(nodes)} {node_word} shown above")
    field(
        "Models",
        "unchanged · wizard will offer exact configurations that fit capacity",
    )
    field(
        "Effect",
        "saves cluster membership only; does not validate or create a model "
        "profile",
    )


def rows(topology: dict[str, Any]) -> None:
    validate_manifest(topology, require_verified=True)
    validation = topology["validation"]
    print(
        "\t".join(
            [
                "META",
                str(len(topology["nodes"])),
                topology["topology_id"],
                "1" if validation["full_mesh"] else "0",
                str(validation["min_rails_per_pair"]),
                str(topology["schema_version"]),
                "1" if topology_has_ssh_trust(topology) else "0",
            ]
        )
    )
    for node in topology["nodes"]:
        hcas = ",".join(link["hca"] for link in node.get("rdma") or [])
        netdevs = ",".join(
            sorted({link["netdev"] for link in node.get("rdma") or []})
        )
        print(
            "\t".join(
                [
                    "NODE",
                    str(node["rank"]),
                    node["node_id"],
                    node["hostname"],
                    node["ssh_host"],
                    node["control"]["ip"],
                    node["control"]["interface"],
                    hcas,
                    netdevs,
                ]
            )
        )
    if topology_has_ssh_trust(topology):
        for node in topology["nodes"]:
            alias = ssh_host_alias(node["ssh_host"])
            fingerprints = ",".join(
                key["fingerprint"] for key in node["ssh_host_keys"]
            )
            print(
                "\t".join(
                    [
                        "TRUST",
                        str(node["rank"]),
                        alias,
                        fingerprints,
                    ]
                )
            )
    for link in topology["links"]:
        print(
            "\t".join(
                [
                    "LINK",
                    str(link["ranks"][0]),
                    str(link["ranks"][1]),
                    str(len(link["rails"])),
                ]
            )
        )


def profile_fabric(topology: dict[str, Any], node_count: int) -> None:
    """Emit per-rank HCAs restricted to links inside an exact profile."""
    validate_manifest(topology, require_verified=True)
    if node_count < 1 or node_count > len(topology["nodes"]):
        fail(
            f"profile node count {node_count} is outside confirmed capacity "
            f"{len(topology['nodes'])}"
        )

    selected: dict[int, list[tuple[str, str]]] = {
        rank: [] for rank in range(node_count)
    }

    def add_endpoint(rank: int, endpoint: dict[str, Any]) -> None:
        item = (endpoint["hca"], endpoint["netdev"])
        if item not in selected[rank]:
            selected[rank].append(item)

    if node_count == 1:
        for rdma in topology["nodes"][0].get("rdma") or []:
            add_endpoint(0, rdma)
    else:
        for link in topology["links"]:
            a, b = link["ranks"]
            if a >= node_count or b >= node_count:
                continue
            for rail in link["rails"]:
                add_endpoint(a, rail["a"])
                add_endpoint(b, rail["b"])

    for rank in range(node_count):
        endpoints = selected[rank]
        if not endpoints:
            fail(f"rank {rank}: no RDMA HCA participates in selected profile")
        hcas = ",".join(hca for hca, _netdev in endpoints)
        netdevs = ",".join(dict.fromkeys(netdev for _hca, netdev in endpoints))
        print("\t".join([str(rank), hcas, netdevs]))



def ping_plan(topology: dict[str, Any]) -> None:
    validate_manifest(topology)
    nodes = topology["nodes"]
    for link in topology["links"]:
        a, b = link["ranks"]
        for rail in link["rails"]:
            print(
                "\t".join(
                    [
                        str(a),
                        nodes[a]["ssh_host"],
                        str(b),
                        rail["b"]["ip"],
                        rail["network"],
                    ]
                )
            )
            print(
                "\t".join(
                    [
                        str(b),
                        nodes[b]["ssh_host"],
                        str(a),
                        rail["a"]["ip"],
                        rail["network"],
                    ]
                )
            )


def mark_verified(document: dict[str, Any]) -> dict[str, Any]:
    topology = extract_topology(document)
    validate_manifest(topology)
    topology["validation"]["connectivity_verified"] = True
    topology["topology_id"] = topology_digest(topology)
    validate_manifest(topology, require_verified=True)
    if "topology" in document:
        document["topology"] = topology
        return document
    return topology


def atomic_write(topology: dict[str, Any], destination: str) -> None:
    validate_manifest(topology, require_verified=True)
    path = pathlib.Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(topology, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def enroll_ssh_trust(
    topology: dict[str, Any],
    probes: list[dict[str, Any]],
    *,
    allow_key_change: bool = False,
) -> dict[str, Any]:
    validate_manifest(topology, require_verified=True)
    normalized = [
        normalize_probe(probe, f"probe[{index}]")
        for index, probe in enumerate(probes)
    ]
    by_id: dict[str, dict[str, Any]] = {}
    for probe in normalized:
        node_id = probe["node_id"]
        if node_id in by_id:
            fail(f"duplicate trust probe for node_id {node_id}")
        by_id[node_id] = probe

    updated = json.loads(json.dumps(topology))
    expected_ids = {node["node_id"] for node in updated["nodes"]}
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id))
        extra = sorted(set(by_id) - expected_ids)
        fail(
            "trust probes do not exactly match topology nodes "
            f"(missing={missing}, extra={extra})"
        )

    changed: list[int] = []
    for node in updated["nodes"]:
        rank = node["rank"]
        probe = by_id[node["node_id"]]
        if probe["hostname"] != node["hostname"]:
            fail(f"rank {rank}: probe hostname differs from topology")
        expected_ssh_host = "local" if rank == 0 else node["ssh_host"]
        if probe["ssh_host"] != expected_ssh_host:
            fail(f"rank {rank}: probe SSH alias differs from topology")
        if probe["control"]["ip"] != node["control"]["ip"]:
            fail(f"rank {rank}: probe control IP differs from topology")
        new_keys = probe["ssh_host_keys"]
        old_keys = node.get("ssh_host_keys") or []
        old_fingerprints = {
            str(item.get("fingerprint") or "") for item in old_keys
        }
        new_fingerprints = {item["fingerprint"] for item in new_keys}
        if old_fingerprints and old_fingerprints != new_fingerprints:
            changed.append(rank)
        node["ssh_host_keys"] = new_keys

    if changed and not allow_key_change:
        ranks = ",".join(str(rank) for rank in changed)
        fail(
            "SSH host keys changed for rank(s) "
            f"{ranks}; verify out of band, update normal OpenSSH trust, then "
            "rerun with --accept-key-change"
        )

    updated["schema_version"] = SCHEMA_VERSION
    updated["validation"]["ssh_identity_enrolled"] = True
    updated["ssh_trust_enrolled_at"] = datetime.now(timezone.utc).isoformat()
    updated["topology_id"] = topology_digest(updated)
    validate_manifest(
        updated, require_verified=True, require_ssh_trust=True
    )
    return updated


def known_hosts(topology: dict[str, Any], alias: str) -> None:
    validate_manifest(
        topology, require_verified=True, require_ssh_trust=True
    )
    matches = [
        node
        for node in topology["nodes"]
        if ssh_host_alias(node["ssh_host"]) == alias
    ]
    if len(matches) != 1:
        fail(f"SSH alias {alias!r} is not uniquely enrolled")
    for key in matches[0]["ssh_host_keys"]:
        print(f"{alias} {key['algorithm']} {key['public_key']}")


def render_ssh_config_text(
    topology: dict[str, Any],
    *,
    topology_path: str,
    tool_path: str | None = None,
) -> str:
    validate_manifest(
        topology, require_verified=True, require_ssh_trust=True
    )
    manifest_path = str(pathlib.Path(topology_path).resolve())
    helper_path = str(
        pathlib.Path(tool_path or __file__).resolve()
    )
    command = " ".join(
        [
            shlex.quote(helper_path),
            "known-hosts",
            shlex.quote(manifest_path),
            "%H",
        ]
    )
    lines = [
        "# Generated by Pulsar; edit topology through the enrollment CLI.",
        f"# topology_id={topology['topology_id']}",
    ]
    for node in topology["nodes"]:
        if node["rank"] == 0:
            continue
        alias = ssh_host_alias(node["ssh_host"])
        lines.extend(
            [
                f"Host {alias}",
                f"    HostName {node['control']['ip']}",
                f"    HostKeyAlias {alias}",
            ]
        )
    lines.extend(
        [
            "Host *",
            "    AddressFamily inet",
            "    CanonicalizeHostname no",
            "    CheckHostIP no",
            "    ProxyCommand none",
            "    ProxyJump none",
            "    StrictHostKeyChecking yes",
            "    UpdateHostKeys no",
            "    VerifyHostKeyDNS no",
            "    UserKnownHostsFile none",
            "    GlobalKnownHostsFile none",
            f"    KnownHostsCommand {command}",
            "    Include ~/.ssh/config",
            "    Include /etc/ssh/ssh_config",
            "",
        ]
    )
    return "\n".join(lines)


def validate_ssh_config_file(
    topology: dict[str, Any],
    config_path: str,
    *,
    topology_path: str,
) -> None:
    expected = render_ssh_config_text(
        topology, topology_path=topology_path
    )
    try:
        actual = pathlib.Path(config_path).read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"{config_path}: {exc}")
    if actual != expected:
        fail(
            "generated SSH config is missing or stale; run "
            "scripts/topology-ssh-trust.sh enroll"
        )


def trust_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    validate_manifest(old, require_verified=True)
    validate_manifest(
        new, require_verified=True, require_ssh_trust=True
    )
    old_by_id = {node["node_id"]: node for node in old["nodes"]}
    nodes = []
    for node in new["nodes"]:
        previous = old_by_id.get(node["node_id"]) or {}
        before = sorted(
            str(key.get("fingerprint") or "")
            for key in previous.get("ssh_host_keys") or []
        )
        after = sorted(
            key["fingerprint"] for key in node["ssh_host_keys"]
        )
        if not before:
            state = "new"
        elif before == after:
            state = "unchanged"
        else:
            state = "changed"
        nodes.append(
            {
                "rank": node["rank"],
                "node_id": node["node_id"],
                "hostname": node["hostname"],
                "ssh_host": node["ssh_host"],
                "state": state,
                "before": before,
                "after": after,
            }
        )
    return {
        "schema_version": 1,
        "kind": "topology-ssh-trust-diff",
        "old_topology_id": old["topology_id"],
        "new_topology_id": new["topology_id"],
        "nodes": nodes,
    }


def _prepared_json_file(
    topology: dict[str, Any], destination: pathlib.Path
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(topology, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_name, 0o600)
    return tmp_name


def _prepared_text_file(content: str, destination: pathlib.Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_name, 0o600)
    return tmp_name


def write_trust_bundle(
    topology: dict[str, Any],
    topology_destination: str,
    config_destination: str,
) -> None:
    validate_manifest(
        topology, require_verified=True, require_ssh_trust=True
    )
    topology_path = pathlib.Path(topology_destination)
    config_path = pathlib.Path(config_destination)
    config = render_ssh_config_text(
        topology, topology_path=str(topology_path)
    )
    topology_tmp = _prepared_json_file(topology, topology_path)
    config_tmp = _prepared_text_file(config, config_path)
    try:
        os.replace(config_tmp, config_path)
        config_tmp = ""
        os.replace(topology_tmp, topology_path)
        topology_tmp = ""
    finally:
        for tmp_name in (topology_tmp, config_tmp):
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass


def render_trust_diff(report: dict[str, Any]) -> None:
    term = TerminalWriter()
    term.emit("SSH TRUST ENROLLMENT")
    term.field("Old cluster", report["old_topology_id"][:12])
    term.field("New cluster", report["new_topology_id"][:12])
    for node in report["nodes"]:
        term.blank()
        term.emit(
            f"cluster node {node['rank'] + 1} · {node['hostname']}",
            initial_indent="  ",
            subsequent_indent="    ",
        )
        term.field("Alias", node["ssh_host"], indent=4)
        term.field("State", node["state"], indent=4)
        term.field(
            "Keys",
            ", ".join(item[:24] for item in node["after"]),
            indent=4,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    assemble_parser = sub.add_parser("assemble")
    assemble_parser.add_argument("--existing")
    assemble_parser.add_argument("probes", nargs="+")

    render_parser = sub.add_parser("render")
    render_parser.add_argument("document")
    render_parser.add_argument("--skipped-ssh", type=int, default=0)

    render_save_parser = sub.add_parser("render-save")
    render_save_parser.add_argument("document")
    render_save_parser.add_argument("destination")

    for name in ("rows", "ping-plan", "mark-verified", "validate"):
        command = sub.add_parser(name)
        command.add_argument("document")

    enroll_parser = sub.add_parser("enroll-ssh-trust")
    enroll_parser.add_argument("document")
    enroll_parser.add_argument("probes", nargs="+")
    enroll_parser.add_argument("--accept-key-change", action="store_true")

    known_hosts_parser = sub.add_parser("known-hosts")
    known_hosts_parser.add_argument("document")
    known_hosts_parser.add_argument("alias")

    render_ssh_parser = sub.add_parser("render-ssh-config")
    render_ssh_parser.add_argument("document")
    render_ssh_parser.add_argument("--topology-path", required=True)

    validate_ssh_parser = sub.add_parser("validate-ssh-config")
    validate_ssh_parser.add_argument("document")
    validate_ssh_parser.add_argument("config")

    trust_diff_parser = sub.add_parser("trust-diff")
    trust_diff_parser.add_argument("old")
    trust_diff_parser.add_argument("new")
    trust_diff_parser.add_argument("--json", action="store_true")

    trust_write_parser = sub.add_parser("write-trust-bundle")
    trust_write_parser.add_argument("document")
    trust_write_parser.add_argument("topology_destination")
    trust_write_parser.add_argument("config_destination")
    profile_parser = sub.add_parser("profile-fabric")
    profile_parser.add_argument("document")
    profile_parser.add_argument("nodes", type=int)

    write_parser = sub.add_parser("write")
    write_parser.add_argument("document")
    write_parser.add_argument("destination")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "assemble":
            document = assemble(args.probes, args.existing)
            print(json.dumps(document, indent=2, sort_keys=True))
        elif args.command == "render":
            render(load_json(args.document), args.skipped_ssh)
        elif args.command == "render-save":
            render_save(load_json(args.document), args.destination)
        elif args.command == "rows":
            rows(extract_topology(load_json(args.document)))
        elif args.command == "profile-fabric":
            profile_fabric(
                extract_topology(load_json(args.document)), args.nodes
            )
        elif args.command == "ping-plan":
            ping_plan(extract_topology(load_json(args.document)))
        elif args.command == "mark-verified":
            print(
                json.dumps(
                    mark_verified(load_json(args.document)), indent=2, sort_keys=True
                )
            )
        elif args.command == "validate":
            validate_manifest(extract_topology(load_json(args.document)))
        elif args.command == "enroll-ssh-trust":
            topology = extract_topology(load_json(args.document))
            probes = [load_json(path) for path in args.probes]
            print(
                json.dumps(
                    enroll_ssh_trust(
                        topology,
                        probes,
                        allow_key_change=args.accept_key_change,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "known-hosts":
            known_hosts(
                extract_topology(load_json(args.document)), args.alias
            )
        elif args.command == "render-ssh-config":
            print(
                render_ssh_config_text(
                    extract_topology(load_json(args.document)),
                    topology_path=args.topology_path,
                ),
                end="",
            )
        elif args.command == "validate-ssh-config":
            validate_ssh_config_file(
                extract_topology(load_json(args.document)),
                args.config,
                topology_path=args.document,
            )
        elif args.command == "trust-diff":
            report = trust_diff(
                extract_topology(load_json(args.old)),
                extract_topology(load_json(args.new)),
            )
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                render_trust_diff(report)
        elif args.command == "write-trust-bundle":
            write_trust_bundle(
                extract_topology(load_json(args.document)),
                args.topology_destination,
                args.config_destination,
            )
        elif args.command == "write":
            atomic_write(extract_topology(load_json(args.document)), args.destination)
        return 0
    except TopologyError as exc:
        print(f"topology: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
