#!/usr/bin/env python3
"""Versioned launch-plan, serving-probe, and rank-spec contracts (SIM-04).

A launch plan describes an intended serve action. It is not a permit:
mutable image, identity, topology, ownership, and health prerequisites must
be rechecked immediately before mutation. Bash remains the operator and
process boundary; this module owns the JSON contracts.

Current N=1 vs N>1 launcher differences preserved by ``rank_docker_argv``
(changing them would change launch behavior and needs physical revalidation):

* N=1 rank label remains ``single`` (inventory/ownership contract).
* N=1 uses published-port networking; N>1 uses host networking.
* N=1 containers carry a Docker ``/health`` check; N>1 liveness is a
  completion probe (rank-0 ``/health`` can stay OK after remote-rank loss).

JSON consumer migration (pair-only names → N-rank names):

* ``worker-unreachable`` → ``rank-unreachable``
* ``worker-docker-error`` → ``rank-docker-error``
* ``missing-on-worker`` → ``missing-on-rank``
* ``check-memory.sh`` ``worker_available_gib`` remains a pair-era field;
  new consumers should read ``rank_available_gib``.
* Inventory schema 1 is the only operator-facing ownership/state classifier.
  Launchers consume proven-ownership primitives from ``lib.sh`` and must not
  invent a second service-state vocabulary.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from typing import Any

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

PLAN_SCHEMA_VERSION = 1
PROBE_SCHEMA_VERSION = 1
RANK_SPEC_SCHEMA_VERSION = 1
PLAN_KIND = "pulsar-launch-plan"
PROBE_KIND = "pulsar-serving-probe"
RANK_SPEC_KIND = "pulsar-rank-container-spec"

SAFE_PROFILE = re.compile(r"^[A-Za-z0-9._-]+$")
HEX12 = re.compile(r"^[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[A-Za-z0-9._-]+$")
COMMIT_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
SAFE_IFACE = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
SAFE_ENDPOINT = re.compile(r"^[A-Za-z0-9._:@%+-]+$")
FORBIDDEN_PERMIT_KEYS = frozenset(
    {
        "authorized",
        "authorised",
        "approved",
        "permit",
        "permission",
        "token",
        "trusted",
        "authorization",
        "authorisation",
    }
)
IDENTITY_STATUSES = ("manifest-verified",)
LIFECYCLE_ACTIONS = ("start", "replace", "dry-run")
SPEC_DECODE_SOURCES = (
    "profile-default",
    "forced-on",
    "forced-off",
)
STORAGE_MECHANISM = "local-files"
OWNERSHIP_CLASSIFIER = "inventory"
MEMORY_RESULTS = ("unchecked", "pass", "warn", "fail")
DEFAULT_RUNTIME = {
    "engine_args": [],
    "container_env": [],
    "extra_env": [],
    "spec_decode_args": [],
    "vllm_extra_args": [],
    "hf_hub_offline": "1",
    "vllm_logging_level": "INFO",
    "restart_policy": "no",
    "health_start_period": "900s",
    "nccl_ib_qps": "4",
    "nccl_debug": "WARN",
    "master_port": 29500,
}

LABEL_MANAGED = "io.pulsar.gb10.managed"
LABEL_CONF = "io.pulsar.gb10.conf"
LABEL_RANK = "io.pulsar.gb10.rank"
LABEL_WORLD_SIZE = "io.pulsar.gb10.world-size"
LABEL_TOPOLOGY = "io.pulsar.gb10.topology"
LABEL_NODE_ID = "io.pulsar.gb10.node-id"
LABEL_WEIGHT_SOURCE = "io.pulsar.gb10.weight-source"
LABEL_WEIGHT_OWNER = "io.pulsar.gb10.weight-owner"
LABEL_WEIGHT_CONFIG = "io.pulsar.gb10.weight-config"
LABEL_MODEL_REVISION = "io.pulsar.gb10.model-revision"
LABEL_IDENTITY_STATUS = "io.pulsar.gb10.model-identity-status"
LABEL_LAUNCH_CONTRACT = "io.pulsar.gb10.launch-contract"
LABEL_SPEC_DECODE = "io.pulsar.gb10.spec-decode"

# Pair-only aggregate names that N-rank paths must not emit.
LEGACY_PAIR_IMAGE_STATES = {
    "worker-unreachable": "rank-unreachable",
    "worker-docker-error": "rank-docker-error",
    "missing-on-worker": "missing-on-rank",
    "missing-on-head": "missing-on-rank",
    "head-docker-error": "rank-docker-error",
}


class LaunchPlanError(ValueError):
    """Raised when a launch plan, probe, or rank spec is invalid."""


def fail(message: str) -> None:
    raise LaunchPlanError(message)


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_json(path: str | pathlib.Path) -> Any:
    try:
        with pathlib.Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON {path}: {exc}")


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


def require_profile(value: Any) -> str:
    text = require_text(value, "profile")
    if not SAFE_PROFILE.fullmatch(text):
        fail("profile: use letters, numbers, dot, underscore, or hyphen")
    return text


def require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{field}: expected an integer")
    if value < minimum or value > maximum:
        fail(f"{field}: expected {minimum}..{maximum}")
    return value


def require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        fail(f"{field}: expected a boolean")
    return value


def require_hex(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    text = require_text(value, field)
    if not pattern.fullmatch(text):
        fail(f"{field}: invalid identity")
    return text


def reject_permit_keys(value: Any, field: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in FORBIDDEN_PERMIT_KEYS:
                fail(
                    f"{field}.{key}: a launch plan is a description, not a permit"
                )
            reject_permit_keys(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_permit_keys(child, f"{field}[{index}]")


def require_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        fail(f"{field}: expected an array of strings")
    cleaned: list[str] = []
    for index, item in enumerate(value):
        cleaned.append(require_text(item, f"{field}[{index}]"))
    return cleaned


def _validate_runtime(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        runtime = {}
    runtime = require_object(runtime, "runtime")
    cleaned = {
        "engine_args": require_str_list(runtime.get("engine_args"), "runtime.engine_args"),
        "container_env": require_str_list(
            runtime.get("container_env"), "runtime.container_env"
        ),
        "extra_env": require_str_list(runtime.get("extra_env"), "runtime.extra_env"),
        "spec_decode_args": require_str_list(
            runtime.get("spec_decode_args"), "runtime.spec_decode_args"
        ),
        "vllm_extra_args": require_str_list(
            runtime.get("vllm_extra_args"), "runtime.vllm_extra_args"
        ),
        "hf_hub_offline": str(
            runtime.get("hf_hub_offline", DEFAULT_RUNTIME["hf_hub_offline"])
        ),
        "vllm_logging_level": str(
            runtime.get("vllm_logging_level", DEFAULT_RUNTIME["vllm_logging_level"])
        ),
        "restart_policy": str(
            runtime.get("restart_policy", DEFAULT_RUNTIME["restart_policy"])
        ),
        "health_start_period": str(
            runtime.get(
                "health_start_period", DEFAULT_RUNTIME["health_start_period"]
            )
        ),
        "nccl_ib_qps": str(runtime.get("nccl_ib_qps", DEFAULT_RUNTIME["nccl_ib_qps"])),
        "nccl_debug": str(runtime.get("nccl_debug", DEFAULT_RUNTIME["nccl_debug"])),
        "master_port": require_int(
            runtime.get("master_port", DEFAULT_RUNTIME["master_port"]),
            "runtime.master_port",
            1,
            65535,
        ),
    }
    extra = set(runtime) - set(cleaned)
    if extra:
        fail(f"runtime: unsupported fields {sorted(extra)}")
    return cleaned


def _validate_memory(memory: Any) -> dict[str, Any]:
    if memory is None:
        return {"advisory": True, "result": "unchecked"}
    memory = require_object(memory, "memory")
    result = require_text(memory.get("result"), "memory.result")
    if result not in MEMORY_RESULTS:
        fail("memory.result is unsupported")
    cleaned = {
        "advisory": require_bool(memory.get("advisory", True), "memory.advisory"),
        "result": result,
    }
    if "mode" in memory and memory["mode"] not in (None, ""):
        cleaned["mode"] = require_text(memory.get("mode"), "memory.mode")
    extra = set(memory) - {"advisory", "result", "mode"}
    if extra:
        fail(f"memory: unsupported fields {sorted(extra)}")
    if cleaned["advisory"] is not True:
        fail("memory.advisory must be true: memory never authorizes serving")
    return cleaned


def reject_removed_weight_axis(value: Any, field: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in {"weight_source", "weight_mode"}:
                fail(
                    f"{field}.{key}: weight-source/weight-mode are removed "
                    "(ADR 0006); storage.mechanism is local-files"
                )
            reject_removed_weight_axis(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_removed_weight_axis(child, f"{field}[{index}]")


def current_rank_label(nodes: int, rank: int) -> str:
    if nodes == 1:
        if rank != 0:
            fail("single-node plans only use rank 0")
        return "single"
    return str(rank)


def container_name(profile: str, nodes: int) -> str:
    require_profile(profile)
    if nodes > 1:
        return f"vllm-cluster-{profile}"
    return f"vllm-{profile}"


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


def _validate_storage(storage: dict[str, Any], *, nodes: int) -> dict[str, Any]:
    storage = require_object(storage, "storage")
    mechanism = require_text(storage.get("mechanism"), "storage.mechanism")
    if mechanism != STORAGE_MECHANISM:
        fail("storage.mechanism must be local-files (ADR 0006)")
    identity = require_text(
        storage.get("identity_status"), "storage.identity_status"
    )
    if identity not in IDENTITY_STATUSES:
        fail("storage.identity_status is not a servable local-files status")
    revision = require_text(storage.get("revision"), "storage.revision")
    if not REVISION.fullmatch(revision):
        fail("storage.revision is invalid")
    home = require_text(storage.get("home_node_id"), "storage.home_node_id")
    content_id = require_hex(
        storage.get("content_id"), "storage.content_id", HEX12
    )
    container_path = require_text(
        storage.get("container_model_path"), "storage.container_model_path"
    )
    if not container_path.endswith(f"/snapshots/{revision}"):
        fail("storage.container_model_path is not the exact revision")
    hub_path = require_text(storage.get("hub_path"), "storage.hub_path")
    if storage.get("model_seal_id") is not None or storage.get(
        "validation_bundle_id"
    ) is not None:
        fail("storage: lab expected-identity fields are retired (ADR 0012)")
    transport = require_text(storage.get("transport"), "storage.transport")
    if transport not in ("ssh-control", "ssh-roce"):
        fail(
            "storage.transport is not a current local-files transport "
            f"(nodes={nodes})"
        )
    cleaned = {
        "mechanism": STORAGE_MECHANISM,
        "identity_status": identity,
        "revision": revision,
        "home_node_id": home,
        "content_id": content_id,
        "hub_path": hub_path,
        "container_model_path": container_path,
        "transport": transport,
    }
    extra = set(storage) - set(cleaned)
    if extra:
        fail(f"storage: unsupported fields {sorted(extra)}")
    return cleaned


def _validate_rank(rank: Any, index: int, nodes: int) -> dict[str, Any]:
    row = require_object(rank, f"ranks[{index}]")
    rank_no = require_int(row.get("rank"), f"ranks[{index}].rank", 0, 254)
    if rank_no != index:
        fail(f"ranks[{index}].rank must equal {index}")
    node_id = require_text(row.get("node_id"), f"ranks[{index}].node_id")
    hostname = require_text(row.get("hostname"), f"ranks[{index}].hostname")
    ssh_host = require_text(row.get("ssh_host"), f"ranks[{index}].ssh_host")
    if ssh_host.startswith("-") or not SAFE_ENDPOINT.fullmatch(ssh_host):
        fail(f"ranks[{index}].ssh_host: unsafe endpoint")
    control_ip = require_text(
        row.get("control_ip"), f"ranks[{index}].control_ip"
    )
    control_if = require_text(
        row.get("control_if"), f"ranks[{index}].control_if"
    )
    if not SAFE_IFACE.fullmatch(control_if):
        fail(f"ranks[{index}].control_if: unsafe interface")
    raw_hcas = row.get("hcas")
    if nodes == 1 and raw_hcas in (None, ""):
        hcas = ""
    else:
        hcas = require_text(raw_hcas, f"ranks[{index}].hcas")
    api_role = rank_no == 0
    cleaned = {
        "rank": rank_no,
        "rank_label": current_rank_label(nodes, rank_no),
        "node_id": node_id,
        "hostname": hostname,
        "ssh_host": ssh_host,
        "control_ip": control_ip,
        "control_if": control_if,
        "hcas": hcas,
        "api_rank": api_role,
        "remote": ssh_host != "local",
    }
    if "hub_path" in row:
        hub_path = require_text(row["hub_path"], f"ranks[{index}].hub_path")
        if not pathlib.Path(hub_path).is_absolute():
            fail("rank hub_path must be absolute")
        cleaned["hub_path"] = hub_path
    extra = set(row) - {
        "hub_path",
        "rank",
        "rank_label",
        "node_id",
        "hostname",
        "ssh_host",
        "control_ip",
        "control_if",
        "hcas",
        "api_rank",
        "remote",
    }
    if extra:
        fail(f"ranks[{index}]: unsupported fields {sorted(extra)}")
    return cleaned


def validate_launch_plan(document: Any) -> dict[str, Any]:
    plan = require_object(document, "plan")
    reject_permit_keys(plan, "plan")
    reject_removed_weight_axis(plan, "plan")
    if type(plan.get("schema_version")) is not int or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        fail("plan schema_version is unsupported")
    if plan.get("kind") != PLAN_KIND:
        fail("plan kind is unsupported")
    if plan.get("is_permit") is not False:
        fail("plan.is_permit must be false")
    action = require_text(plan.get("lifecycle_action"), "lifecycle_action")
    if action not in LIFECYCLE_ACTIONS:
        fail("lifecycle_action is unsupported")
    profile = require_profile(plan.get("profile"))
    served = require_text(plan.get("served_name"), "served_name")
    model_id = require_text(plan.get("model_id"), "model_id")
    if model_id.startswith("/"):
        fail("model_id: absolute-path catalog profiles are removed (ADR 0006)")
    image = require_text(plan.get("image"), "image")
    nodes = require_int(plan.get("nodes"), "nodes", 1, 255)
    port = require_int(plan.get("port"), "port", 1, 65535)
    gpu_mem = plan.get("gpu_mem_util")
    if not isinstance(gpu_mem, (int, float)) or isinstance(gpu_mem, bool):
        fail("gpu_mem_util: expected a number")
    if not 0 < float(gpu_mem) <= 1:
        fail("gpu_mem_util: expected 0 < value <= 1")
    topology_id = require_hex(plan.get("topology_id"), "topology_id", HEX64)
    launch_contract_id = require_hex(
        plan.get("launch_contract_id"), "launch_contract_id", HEX64
    )
    spec = require_object(plan.get("spec_decode"), "spec_decode")
    spec_enabled = require_bool(spec.get("enabled"), "spec_decode.enabled")
    spec_source = require_text(spec.get("source"), "spec_decode.source")
    if spec_source not in SPEC_DECODE_SOURCES:
        fail("spec_decode.source is unsupported")
    ranks_raw = plan.get("ranks")
    if not isinstance(ranks_raw, list) or not ranks_raw:
        fail("ranks: expected a non-empty array")
    if len(ranks_raw) != nodes:
        fail("ranks: length must equal nodes")
    ranks = [_validate_rank(row, index, nodes) for index, row in enumerate(ranks_raw)]
    storage = _validate_storage(
        require_object(plan.get("storage"), "storage"), nodes=nodes
    )
    network_mode = require_text(plan.get("network_mode"), "network_mode")
    expected_network = "published-port" if nodes == 1 else "host"
    if network_mode != expected_network:
        fail(
            f"network_mode: current contract is {expected_network} for nodes={nodes}"
        )
    classifier = plan.get("ownership_classifier", OWNERSHIP_CLASSIFIER)
    if classifier != OWNERSHIP_CLASSIFIER:
        fail("ownership_classifier must be inventory")
    memory = _validate_memory(plan.get("memory"))
    runtime = _validate_runtime(plan.get("runtime"))
    cleaned = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "is_permit": False,
        "lifecycle_action": action,
        "platform_id": require_text(plan.get("platform_id", "dgx-spark-gb10"), "platform_id"),
        "profile": profile,
        "served_name": served,
        "model_id": model_id,
        "image": image,
        "nodes": nodes,
        "port": port,
        "gpu_mem_util": float(gpu_mem),
        "topology_id": topology_id,
        "launch_contract_id": launch_contract_id,
        "spec_decode": {"enabled": spec_enabled, "source": spec_source},
        "storage": storage,
        "ranks": ranks,
        "network_mode": network_mode,
        "container_name": container_name(profile, nodes),
        "ownership_classifier": OWNERSHIP_CLASSIFIER,
        "memory": memory,
        "runtime": runtime,
    }
    extra = set(plan) - set(cleaned)
    if extra:
        fail(f"plan: unsupported fields {sorted(extra)}")
    return cleaned


def build_launch_plan(facts: Any) -> dict[str, Any]:
    document = require_object(facts, "facts")
    document = dict(document)
    document.setdefault("schema_version", PLAN_SCHEMA_VERSION)
    document.setdefault("kind", PLAN_KIND)
    document.setdefault("is_permit", False)
    nodes = require_int(document.get("nodes"), "nodes", 1, 255)
    document.setdefault(
        "network_mode", "published-port" if nodes == 1 else "host"
    )
    document.setdefault(
        "container_name",
        container_name(require_profile(document.get("profile")), nodes),
    )
    ranks = document.get("ranks")
    if isinstance(ranks, list):
        rebuilt = []
        for index, row in enumerate(ranks):
            item = dict(require_object(row, f"ranks[{index}]"))
            item.setdefault("rank", index)
            item.setdefault("rank_label", current_rank_label(nodes, index))
            item.setdefault("api_rank", index == 0)
            ssh_host = item.get("ssh_host")
            if isinstance(ssh_host, str):
                item.setdefault("remote", ssh_host != "local")
            rebuilt.append(item)
        document["ranks"] = rebuilt
    return validate_launch_plan(document)


def rank_container_spec(plan: dict[str, Any], rank: int) -> dict[str, Any]:
    plan = validate_launch_plan(plan)
    nodes = plan["nodes"]
    row = plan["ranks"][require_int(rank, "rank", 0, nodes - 1)]
    storage = plan["storage"]
    spec_state = "on" if plan["spec_decode"]["enabled"] else "off"
    labels = {
        LABEL_MANAGED: "true",
        LABEL_CONF: plan["profile"],
        LABEL_RANK: row["rank_label"],
        LABEL_WORLD_SIZE: str(nodes),
        LABEL_TOPOLOGY: plan["topology_id"],
        LABEL_NODE_ID: row["node_id"],
        LABEL_LAUNCH_CONTRACT: plan["launch_contract_id"],
        LABEL_SPEC_DECODE: spec_state,
        LABEL_WEIGHT_SOURCE: STORAGE_MECHANISM,
        LABEL_WEIGHT_OWNER: storage["home_node_id"],
        LABEL_WEIGHT_CONFIG: storage["content_id"],
        LABEL_MODEL_REVISION: storage["revision"],
        LABEL_IDENTITY_STATUS: storage["identity_status"],
    }
    parts = [part for part in storage["container_model_path"].split("/") if part]
    try:
        hub_index = parts.index("hub")
        hub_name = parts[hub_index + 1]
        if parts[hub_index + 2] != "snapshots":
            raise ValueError
    except (ValueError, IndexError):
        fail("storage.container_model_path is not a hub snapshot path")
    mounts = [
        {
            "source": row.get("hub_path", storage["hub_path"]),
            "target": f"/root/.cache/huggingface/hub/{hub_name}",
            "mode": "ro",
        }
    ]
    if nodes == 1:
        network = {
            "mode": "published-port",
            "publish": [f"{plan['port']}:{plan['port']}"],
        }
        devices: list[str] = []
        health = {"kind": "docker-health-cmd", "path": "/health"}
        liveness = {"kind": "docker-health-cmd", "path": "/health"}
    else:
        network = {"mode": "host"}
        devices = ["/dev/infiniband"]
        health = {"kind": "none"}
        liveness = {
            "kind": "completion-probe",
            "reason": "rank-0 /health can stay OK after remote-rank loss",
        }
    return {
        "schema_version": RANK_SPEC_SCHEMA_VERSION,
        "kind": RANK_SPEC_KIND,
        "profile": plan["profile"],
        "container_name": plan["container_name"],
        "rank": row["rank"],
        "rank_label": row["rank_label"],
        "node_id": row["node_id"],
        "image": plan["image"],
        "api_rank": row["api_rank"],
        "api_auth_on_rank": row["api_rank"],
        "labels": labels,
        "mounts": mounts,
        "network": network,
        "devices": devices,
        "health": health,
        "liveness": liveness,
        "cleanup": {
            "by": "proven-ownership-then-immutable-id",
            "rank_label": row["rank_label"],
        },
    }


def rank_docker_argv(
    plan: dict[str, Any],
    rank: int,
    *,
    detach: bool = False,
) -> list[str]:
    """Reproduce current serve.sh / start-cluster.sh docker argv.

    Secrets are read from the process environment at apply time and are
    never stored on the plan. The plan remains a description, not a permit.
    """
    plan = validate_launch_plan(plan)
    spec = rank_container_spec(plan, rank)
    runtime = plan["runtime"]
    row = plan["ranks"][rank]
    argv = ["docker", "run"]
    if plan["nodes"] > 1:
        argv.append("-d")
    argv.extend(["--name", spec["container_name"]])
    if plan["nodes"] == 1 and detach:
        argv.append("-d")
    labels = spec["labels"]
    label_order = [
        LABEL_MANAGED,
        LABEL_CONF,
        LABEL_RANK,
        LABEL_WORLD_SIZE,
        LABEL_TOPOLOGY,
        LABEL_NODE_ID,
        LABEL_LAUNCH_CONTRACT,
        LABEL_SPEC_DECODE,
        LABEL_WEIGHT_SOURCE,
        LABEL_WEIGHT_OWNER,
        LABEL_WEIGHT_CONFIG,
        LABEL_MODEL_REVISION,
        LABEL_IDENTITY_STATUS,
    ]
    if plan["nodes"] == 1:
        # Match serve.sh: world-size is not currently labeled on N=1.
        early = [
            LABEL_MANAGED,
            LABEL_CONF,
            LABEL_RANK,
            LABEL_WORLD_SIZE,
            LABEL_WEIGHT_SOURCE,
            LABEL_LAUNCH_CONTRACT,
            LABEL_SPEC_DECODE,
        ]
        for key in early:
            argv.extend(["--label", f"{key}={labels[key]}"])
        argv.extend(
            [
                "--gpus",
                "all",
                "--ipc=host",
                "--ulimit",
                "memlock=-1",
                "--ulimit",
                "stack=67108864",
                "-p",
                f"{plan['port']}:{plan['port']}",
                "-v",
                f"{spec['mounts'][0]['source']}:{spec['mounts'][0]['target']}:ro",
                "-e",
                f"HF_TOKEN={os.environ.get('HF_TOKEN', '')}",
                "-e",
                f"HF_HUB_OFFLINE={runtime['hf_hub_offline']}",
                "-e",
                f"VLLM_LOGGING_LEVEL={runtime['vllm_logging_level']}",
                "--health-cmd",
                f"curl -fs http://localhost:{plan['port']}/health || exit 1",
                "--health-interval",
                "30s",
                "--health-timeout",
                "5s",
                "--health-retries",
                "3",
                "--health-start-period",
                runtime["health_start_period"],
                "--restart",
                runtime["restart_policy"],
            ]
        )
        for key in (
            LABEL_WEIGHT_OWNER,
            LABEL_WEIGHT_CONFIG,
            LABEL_MODEL_REVISION,
            LABEL_IDENTITY_STATUS,
            LABEL_TOPOLOGY,
            LABEL_NODE_ID,
        ):
            if key in labels and labels[key]:
                argv.extend(["--label", f"{key}={labels[key]}"])
    else:
        for key in label_order:
            if key in labels and labels[key]:
                argv.extend(["--label", f"{key}={labels[key]}"])
        argv.extend(
            [
                "--network",
                "host",
                "--ipc",
                "host",
                "--gpus",
                "all",
                "--ulimit",
                "memlock=-1",
                "--ulimit",
                "stack=67108864",
                "--device",
                "/dev/infiniband",
                "-v",
                f"{spec['mounts'][0]['source']}:{spec['mounts'][0]['target']}:ro",
                "-e",
                f"HF_HUB_OFFLINE={runtime['hf_hub_offline']}",
                "-e",
                f"VLLM_HOST_IP={row['control_ip']}",
                "-e",
                "NCCL_NET=IB",
                "-e",
                f"NCCL_IB_HCA={row['hcas']}",
                "-e",
                f"NCCL_IB_QPS_PER_CONNECTION={runtime['nccl_ib_qps']}",
                "-e",
                f"NCCL_SOCKET_IFNAME={row['control_if']}",
                "-e",
                f"GLOO_SOCKET_IFNAME={row['control_if']}",
                "-e",
                f"TP_SOCKET_IFNAME={row['control_if']}",
                "-e",
                "NCCL_IB_DISABLE=0",
                "-e",
                f"NCCL_DEBUG={runtime['nccl_debug']}",
            ]
        )
    for item in runtime["container_env"]:
        argv.extend(["-e", item])
    for item in runtime["extra_env"]:
        argv.extend(["-e", item])
    argv.extend(
        [
            plan["image"],
            "--model",
            plan["storage"]["container_model_path"],
            "--served-model-name",
            plan["served_name"],
            "--host",
            "0.0.0.0",
            "--port",
            str(plan["port"]),
            "--gpu-memory-utilization",
            str(plan["gpu_mem_util"]),
        ]
    )
    argv.extend(runtime["engine_args"])
    if plan["nodes"] > 1:
        argv.extend(
            [
                "--nnodes",
                str(plan["nodes"]),
                "--master-addr",
                plan["ranks"][0]["control_ip"],
                "--master-port",
                str(runtime["master_port"]),
            ]
        )
    if plan["spec_decode"]["enabled"]:
        argv.extend(runtime["spec_decode_args"])
    argv.extend(runtime["vllm_extra_args"])
    if plan["nodes"] > 1:
        argv.extend(["--node-rank", str(rank)])
        if rank != 0:
            argv.append("--headless")
    api_key = os.environ.get("VLLM_API_KEY") or os.environ.get("API_KEY") or ""
    if spec["api_auth_on_rank"] and api_key:
        argv.extend(["--api-key", api_key])
    return argv


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


def _print(value: Any) -> None:
    sys.stdout.write(pretty_json(value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate or build Pulsar launch-plan JSON (SIM-04)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build and validate a plan from facts")
    build.add_argument("facts", help="facts JSON path")
    validate = sub.add_parser("validate", help="validate an existing plan")
    validate.add_argument("plan", help="plan JSON path")
    spec = sub.add_parser("rank-spec", help="rank container spec from a plan")
    spec.add_argument("plan", help="plan JSON path")
    spec.add_argument("--rank", type=int, required=True)
    docker = sub.add_parser(
        "docker-argv", help="docker run argv for one rank (secrets from env)"
    )
    docker.add_argument("plan", help="plan JSON path")
    docker.add_argument("--rank", type=int, required=True)
    docker.add_argument("--detach", action="store_true")
    probe = sub.add_parser(
        "probe-from-node", help="map probe-node JSON to a serving rank probe"
    )
    probe.add_argument("probe", help="probe-node JSON path")
    probe.add_argument("--rank", type=int, required=True)
    probe.add_argument("--require-rdma", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            _print(build_launch_plan(load_json(args.facts)))
        elif args.command == "validate":
            _print(validate_launch_plan(load_json(args.plan)))
        elif args.command == "rank-spec":
            _print(rank_container_spec(load_json(args.plan), args.rank))
        elif args.command == "docker-argv":
            _print(
                rank_docker_argv(
                    load_json(args.plan), args.rank, detach=args.detach
                )
            )
        else:
            _print(
                serving_rank_probe_from_node_probe(
                    load_json(args.probe),
                    args.rank,
                    require_rdma=args.require_rdma,
                )
            )
    except LaunchPlanError as exc:
        print(f"launch-plan: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
