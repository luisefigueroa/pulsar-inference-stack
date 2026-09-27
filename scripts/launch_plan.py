#!/usr/bin/env python3
"""Internal launch-plan CLI and shared node-probe summaries.

All current launch configuration is compiled directly from serving specs in
container_runtime. Historical launch compilers are deliberately not retained.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.platform_reference import PlatformReferenceError, load_current_platform
from scripts.container_runtime import PLAN_SCHEMA_VERSION, validate_plan as validate_launch_plan
from scripts.container_runtime import docker_argv as rank_docker_argv, rank_spec as rank_container_spec
PROBE_SCHEMA_VERSION=1
PROBE_KIND='pulsar-serving-probe'
LaunchPlanError=ValueError
LEGACY_PAIR_IMAGE_STATES = {
    "worker-unreachable": "rank-unreachable",
    "worker-docker-error": "rank-docker-error",
    "missing-on-worker": "missing-on-rank",
    "missing-on-head": "missing-on-rank",
    "head-docker-error": "rank-docker-error",
}

def fail(message: str) -> None:
    raise LaunchPlanError(message)

def require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or isinstance(value, bool):
        fail(f"{field}: expected an object")
    return value

def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or any(
        char in value for char in ("\0", "\t", "\r", "\n")
    ):
        fail(f"{field}: missing or contains control characters")
    return value

def require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{field}: expected an integer")
    if value < minimum or value > maximum:
        fail(f"{field}: expected {minimum}..{maximum}")
    return value

def rank_image_aggregate_state(rank_states: list[str]) -> str:
    """N-rank image aggregate. Never emits pair-only worker/head aliases."""
    if not rank_states:
        fail("image ranks: expected at least one rank state")
    normalized: list[str] = []
    for index, state in enumerate(rank_states):
        text = require_text(state, f"ranks[{index}].image_state")
        normalized.append(LEGACY_PAIR_IMAGE_STATES.get(text, text))
    if all(state == "ok" for state in normalized):
        return "ok"
    if any(state == "need-topology" for state in normalized):
        return "need-topology"
    if any(state in {"unreachable", "rank-unreachable", "target-unreachable"} for state in normalized):
        return "rank-unreachable"
    if any(state in {"docker-error", "rank-docker-error", "target-docker-error"} for state in normalized):
        return "rank-docker-error"
    if any(state in {"missing", "missing-on-rank", "missing-on-target"} for state in normalized):
        return "missing-on-rank"
    fail(f"image ranks: unsupported state {normalized!r}")

def serving_rank_probe_from_node_probe(
    probe: Any,
    rank: int,
    *,
    node_id: str | None = None,
    require_rdma: bool = False,
) -> dict[str, Any]:
    document = require_object(probe, "probe")
    rank_no = require_int(rank, "rank", 0, 254)
    gpu = str(document.get("gpu") or "")
    docker_ok = bool(document.get("docker_ok"))
    docker_nvidia = bool(document.get("docker_nvidia"))
    rdma = document.get("rdma") if isinstance(document.get("rdma"), list) else []
    reasons = [
        str(item)
        for item in (document.get("reject_reasons") or [])
        if str(item)
    ]
    checks = []

    def add(level: str, check_id: str, message: str) -> None:
        checks.append({"level": level, "id": check_id, "message": message})

    try:
        expected_gpu = load_current_platform()["gpu_name"]
    except PlatformReferenceError as exc:
        fail(str(exc))
    if gpu == expected_gpu:
        add("ok", "gpu", f"GPU {gpu}")
    else:
        add("fail", "gpu", f"GPU '{gpu}' (want {expected_gpu})")
    if docker_ok and docker_nvidia:
        add("ok", "docker_nvidia", "Docker NVIDIA ready")
    elif docker_ok:
        add("fail", "docker_nvidia", "Docker NVIDIA runtime/CDI missing")
    else:
        add("fail", "docker", "Docker daemon unavailable")
    if require_rdma:
        active = [item for item in rdma if isinstance(item, dict)]
        if active:
            add("ok", "rdma", f"{len(active)} RDMA links")
        else:
            add("fail", "rdma", "no active RDMA links")
    ok = all(item["level"] != "fail" for item in checks)
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "kind": PROBE_KIND,
        "rank": rank_no,
        "node_id": node_id or document.get("node_id") or "",
        "hostname": document.get("hostname") or "",
        "ok": ok,
        "gpu": gpu,
        "docker_ok": docker_ok,
        "docker_nvidia": docker_nvidia,
        "reject_reasons": reasons,
        "checks": checks,
    }

def validate_serving_probe(document: Any, *, nodes: int) -> dict[str, Any]:
    probe = require_object(document, "probe")
    if type(probe.get("schema_version")) is not int or probe.get("schema_version") != PROBE_SCHEMA_VERSION:
        fail("probe schema_version is unsupported")
    if probe.get("kind") != PROBE_KIND:
        fail("probe kind is unsupported")
    ranks = probe.get("ranks")
    if not isinstance(ranks, list) or len(ranks) != nodes:
        fail("probe.ranks: length must equal nodes")
    cleaned_ranks = []
    for index, row in enumerate(ranks):
        item = require_object(row, f"probe.ranks[{index}]")
        rank_no = require_int(item.get("rank"), f"probe.ranks[{index}].rank", 0, 254)
        if rank_no != index:
            fail(f"probe.ranks[{index}].rank must equal {index}")
        cleaned_ranks.append(item)
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "kind": PROBE_KIND,
        "ok": all(bool(row.get("ok")) for row in cleaned_ranks),
        "ranks": cleaned_ranks,
    }


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['validate','rank-spec','docker-argv','probe-from-node'])
    parser.add_argument('file')
    parser.add_argument('--rank',type=int,default=0)
    parser.add_argument('--detach',action='store_true')
    parser.add_argument('--require-rdma',action='store_true')
    args=parser.parse_args(argv)
    try:
        document=json.loads(Path(args.file).read_text())
        if args.command=='validate': result=validate_launch_plan(document)
        elif args.command=='rank-spec': result=rank_container_spec(document,args.rank)
        elif args.command=='docker-argv': result=rank_docker_argv(document,args.rank,detach=args.detach)
        else: result=serving_rank_probe_from_node_probe(document,args.rank,require_rdma=args.require_rdma)
        print(json.dumps(result,sort_keys=True,indent=2))
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'error: launch plan: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__': raise SystemExit(main())
