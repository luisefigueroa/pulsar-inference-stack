#!/usr/bin/env python3
"""Snapshot file-list constants and the live normalized-profile checksum schema.

Lab expected-identity files and the archived combined identity format are not
a live product (ADR 0012). Snapshot hashing and ADR 0004 Model Serving Release
objects live elsewhere.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any


SNAPSHOT_MANIFEST_SCHEMA_VERSION = 1
SNAPSHOT_MANIFEST_KIND = "model-library-snapshot-manifest"
SNAPSHOT_INTEGRITY_SCHEME = "sha256-snapshot-manifest-v1"

SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
HF_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
HF_MODEL_ID_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
IMAGE_DIGEST_RE = re.compile(r"@sha256:([0-9a-f]{64})$")
SAFE_REV = re.compile(r"^[A-Za-z0-9._-]+$")


class ModelIdentityError(ValueError):
    """A malformed or mismatched model identity contract."""


def fail(message: str) -> None:
    raise ModelIdentityError(message)


def pretty_json_bytes(value: Any) -> bytes:
    """Return deterministic pretty JSON bytes for published candidate files.

    Identity digests stay on compact ``canonical_json_digest``. This encoder is
    the shared publication form used by draft planner and capture
    candidates and by staged issuance proposals.
    """
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_json_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_decimal(
    value: Any,
    *,
    label: str,
    allow_empty: bool = False,
) -> str | None:
    if allow_empty and (value is None or value == ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        fail(f"{label} must be numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        fail(f"{label} must be numeric")
    if not parsed.is_finite() or parsed < 0:
        fail(f"{label} must be a non-negative finite number")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"", "-0"}:
        normalized = "0"
    return normalized


def _validate_string_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or "\x00" in item for item in value
    ):
        fail(f"{label} must be a list of strings")
    return list(value)


def _engine_arg_value(args: list[str], flag: str, default: str) -> str:
    for index, item in enumerate(args):
        if item == flag:
            if index + 1 >= len(args):
                fail(f"profile contract {flag} requires a value")
            return args[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return default


def build_profile_contract(
    *,
    model_id: str,
    served_name: str,
    image: str,
    nodes: int,
    port: int,
    gpu_mem_util: str,
    engine_args: list[str],
    container_env: list[str],
    spec_decode_args: list[str],
    recommended_spec: bool,
    profile_purpose: str,
    topology_class: str,
    min_rails_per_pair: int,
    weights_gib: str | None = None,
    weights_ram_gib: str | None = None,
    kv_gib: str | None = None,
    overhead_gib: str | None = None,
    mem_min_free_gib: str | None = None,
) -> dict[str, Any]:
    """Build the canonical behavior/safety contract for one sourced profile."""
    if HF_MODEL_ID_RE.fullmatch(model_id or "") is None:
        fail("profile contract model_id must be an exact Hugging Face repository ID")
    if not isinstance(served_name, str) or not served_name:
        fail("profile contract served_name is invalid")
    image_match = IMAGE_DIGEST_RE.search(image or "")
    if image_match is None:
        fail("profile contract image must be pinned by @sha256 digest")
    if not isinstance(nodes, int) or isinstance(nodes, bool) or nodes < 1:
        fail("profile contract nodes must be positive")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        fail("profile contract port is invalid")
    engine_args = _validate_string_list(
        engine_args, label="profile contract engine_args"
    )
    container_env = _validate_string_list(
        container_env, label="profile contract container_env"
    )
    spec_decode_args = _validate_string_list(
        spec_decode_args, label="profile contract spec_decode_args"
    )
    if not isinstance(recommended_spec, bool):
        fail("profile contract recommended_spec must be boolean")
    if recommended_spec and not spec_decode_args:
        fail("profile contract recommended_spec requires spec_decode_args")
    if profile_purpose not in {"serving", "diagnostic"}:
        fail("profile contract profile_purpose is invalid")

    try:
        tp = int(_engine_arg_value(engine_args, "--tensor-parallel-size", "1"))
        pp = int(_engine_arg_value(engine_args, "--pipeline-parallel-size", "1"))
    except ValueError:
        fail("profile contract tensor/pipeline parallel size must be integer")
    if tp < 1 or pp < 1 or tp * pp != nodes:
        fail("profile contract TP x PP must equal nodes")
    if nodes == 1:
        if topology_class != "single" or min_rails_per_pair != 0:
            fail("single-node profile contract requires single topology and zero rails")
    else:
        if topology_class != "roce-full-mesh" or min_rails_per_pair < 1:
            fail(
                "multi-node profile contract requires roce-full-mesh and positive rails"
            )
        if _engine_arg_value(
            engine_args, "--distributed-executor-backend", ""
        ) != "mp":
            fail("multi-node profile contract requires distributed backend mp")

    normalized_gpu = _normalize_decimal(
        gpu_mem_util, label="profile contract gpu_mem_util"
    )
    assert normalized_gpu is not None
    if Decimal(normalized_gpu) <= 0 or Decimal(normalized_gpu) > 1:
        fail("profile contract gpu_mem_util must be greater than zero and at most one")
    memory_values = {
        "weights_gib": weights_gib,
        "weights_ram_gib": weights_ram_gib,
        "kv_gib": kv_gib,
        "overhead_gib": overhead_gib,
        "mem_min_free_gib": mem_min_free_gib,
    }
    memory_policy = {
        key: _normalize_decimal(
            value,
            label=f"profile contract {key}",
            allow_empty=True,
        )
        for key, value in memory_values.items()
    }
    return {
        "model_id": model_id,
        "served_name": served_name,
        "image": {
            "reference": image,
            "digest": "sha256:" + image_match.group(1),
        },
        "runtime": {
            "port": port,
            "gpu_mem_util": normalized_gpu,
            "engine_args": engine_args,
            "container_env": container_env,
            "spec_decode_args": spec_decode_args,
            "recommended_spec": recommended_spec,
        },
        "geometry": {
            "nodes": nodes,
            "tensor_parallel_size": tp,
            "pipeline_parallel_size": pp,
            "topology_class": topology_class,
            "min_rails_per_pair": min_rails_per_pair,
        },
        "profile_purpose": profile_purpose,
        "memory_policy": memory_policy,
    }


def validate_profile_contract_document(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict) or set(contract) != {
        "model_id",
        "served_name",
        "image",
        "runtime",
        "geometry",
        "profile_purpose",
        "memory_policy",
    }:
        fail("profile contract fields are invalid")
    image = contract.get("image")
    runtime = contract.get("runtime")
    geometry = contract.get("geometry")
    memory = contract.get("memory_policy")
    if not isinstance(image, dict) or set(image) != {"reference", "digest"}:
        fail("profile contract image is invalid")
    if not isinstance(runtime, dict) or set(runtime) != {
        "port",
        "gpu_mem_util",
        "engine_args",
        "container_env",
        "spec_decode_args",
        "recommended_spec",
    }:
        fail("profile contract runtime is invalid")
    if not isinstance(geometry, dict) or set(geometry) != {
        "nodes",
        "tensor_parallel_size",
        "pipeline_parallel_size",
        "topology_class",
        "min_rails_per_pair",
    }:
        fail("profile contract geometry is invalid")
    if not isinstance(memory, dict) or set(memory) != {
        "weights_gib",
        "weights_ram_gib",
        "kv_gib",
        "overhead_gib",
        "mem_min_free_gib",
    }:
        fail("profile contract memory_policy is invalid")
    rebuilt = build_profile_contract(
        model_id=contract.get("model_id"),
        served_name=contract.get("served_name"),
        image=image.get("reference"),
        nodes=geometry.get("nodes"),
        port=runtime.get("port"),
        gpu_mem_util=runtime.get("gpu_mem_util"),
        engine_args=runtime.get("engine_args"),
        container_env=runtime.get("container_env"),
        spec_decode_args=runtime.get("spec_decode_args"),
        recommended_spec=runtime.get("recommended_spec"),
        profile_purpose=contract.get("profile_purpose"),
        topology_class=geometry.get("topology_class"),
        min_rails_per_pair=geometry.get("min_rails_per_pair"),
        weights_gib=memory.get("weights_gib"),
        weights_ram_gib=memory.get("weights_ram_gib"),
        kv_gib=memory.get("kv_gib"),
        overhead_gib=memory.get("overhead_gib"),
        mem_min_free_gib=memory.get("mem_min_free_gib"),
    )
    if rebuilt != contract:
        fail("profile contract is not canonical")
    if image.get("digest") != rebuilt["image"]["digest"]:
        fail("profile contract image digest differs from reference")
    return contract
