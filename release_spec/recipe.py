"""Pure projection of maintainer recipe fields into canonical spec identity.

This is the supported stack/workbench boundary. It performs no I/O and returns
structured conversion gaps rather than making catalog or launch decisions.
"""
from __future__ import annotations

import re
from typing import Any

from .identity import identity_block
from .normalize import (
    build_snapshot_manifest,
    normalize_container_env,
    normalize_engine_args,
)
from .schema import (
    FABRIC_LOCAL,
    FABRIC_ROCE_V2,
    FORBIDDEN_ENGINE_FLAGS,
    ReleaseSpecError,
)


IMAGE_PIN_RE = re.compile(r"@sha256:([0-9a-f]{64})$")
STRUCTURED_PROFILE_FLAGS = (
    "--gpu-memory-utilization",
    "--pipeline-parallel-size",
    "--tensor-parallel-size",
    "-pp",
    "-tp",
)
PARALLELISM_CANONICAL = {
    "--tensor-parallel-size": "--tensor-parallel-size",
    "-tp": "--tensor-parallel-size",
    "--pipeline-parallel-size": "--pipeline-parallel-size",
    "-pp": "--pipeline-parallel-size",
}
NCCL_QPS_ENV = "NCCL_IB_QPS_PER_CONNECTION"
DEFAULT_NCCL_IB_QPS = "4"


def blocking_gap(
    *, field: str, source: str, reason: str, section: str = "identity",
) -> dict[str, str]:
    return {
        "class": "blocking",
        "section": section,
        "field": field,
        "source": source,
        "reason": reason,
    }


def profile_image_digest(image: str) -> str:
    """Return the immutable digest from one digest-pinned image reference."""
    match = IMAGE_PIN_RE.search(image or "")
    if match is None:
        raise ReleaseSpecError("profile image must be pinned by @sha256 digest")
    return "sha256:" + match.group(1)


def _flag_and_value(
    item: str, index: int, tokens: list[str],
) -> tuple[str, str, int] | None:
    for flag in STRUCTURED_PROFILE_FLAGS:
        if item == flag:
            if index + 1 >= len(tokens):
                raise ReleaseSpecError(f"profile {item} requires a value")
            return flag, tokens[index + 1], index + 2
        prefix = flag + "="
        if item.startswith(prefix):
            return flag, item[len(prefix):], index + 1
    return None


def strip_profile_parallelism(
    engine_args: list[str],
) -> tuple[int, int, list[str]]:
    """Extract TP/PP while retaining every non-structured recipe token."""
    values = {"--tensor-parallel-size": 1, "--pipeline-parallel-size": 1}
    seen: set[str] = set()
    remaining: list[str] = []
    index = 0
    while index < len(engine_args):
        item = engine_args[index]
        matched = _flag_and_value(item, index, engine_args)
        if matched is None:
            remaining.append(item)
            index += 1
            continue
        flag, raw, next_index = matched
        canonical = PARALLELISM_CANONICAL.get(flag, flag)
        if canonical in seen:
            raise ReleaseSpecError(
                f"profile repeats structured engine argument {canonical}")
        seen.add(canonical)
        if canonical in values:
            try:
                parsed = int(raw)
            except ValueError as exc:
                raise ReleaseSpecError(
                    f"profile {item} must be an integer") from exc
            if parsed < 1:
                raise ReleaseSpecError(f"profile {item} must be positive")
            values[canonical] = parsed
        elif canonical == "--gpu-memory-utilization":
            raise ReleaseSpecError("profile engine_args duplicate GPU_MEM_UTIL")
        index = next_index
    return (
        values["--tensor-parallel-size"],
        values["--pipeline-parallel-size"],
        remaining,
    )


def _forbidden_flag(token: str) -> str | None:
    for flag in FORBIDDEN_ENGINE_FLAGS:
        if token == flag or token.startswith(flag + "="):
            return flag
    return None


def _freeze_nccl_qps(container_env: list[str], nodes: int) -> list[str]:
    """Make multi-node NCCL QPs explicit in recipe identity."""
    if nodes <= 1:
        return container_env
    if not any(item.startswith(NCCL_QPS_ENV + "=") for item in container_env):
        container_env = [*container_env, f"{NCCL_QPS_ENV}={DEFAULT_NCCL_IB_QPS}"]
    value = next(
        item.split("=", 1)[1]
        for item in container_env
        if item.startswith(NCCL_QPS_ENV + "=")
    )
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ReleaseSpecError(f"{NCCL_QPS_ENV} must be a positive integer")
    return normalize_container_env(container_env, path="identity.container_env")


def nccl_qps_from_identity(identity: dict[str, Any]) -> str:
    """Return frozen multi-node QPs, with the v1 default for legacy specs."""
    if identity["geometry"]["nodes"] <= 1:
        return DEFAULT_NCCL_IB_QPS
    for item in identity["container_env"]:
        if item.startswith(NCCL_QPS_ENV + "="):
            value = item.split("=", 1)[1]
            if re.fullmatch(r"[1-9][0-9]*", value) is None:
                raise ReleaseSpecError(f"{NCCL_QPS_ENV} must be a positive integer")
            return value
    return DEFAULT_NCCL_IB_QPS


def build_profile_identity(
    *,
    model_id: str,
    image: str,
    nodes: int,
    gpu_mem_util: str,
    engine_args: list[str],
    container_env: list[str],
    spec_decode_args: list[str],
    spec_decode: bool,
    platform_id: str,
    snapshot_revision: str | None,
    files: list[dict[str, Any]] | None,
    source_model_id: str | None = None,
    freeze_nccl_qps: bool = True,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Return ``(identity, blocking_gaps)`` without judging catalog status."""
    blocking: list[dict[str, str]] = []
    if source_model_id is not None and source_model_id != model_id:
        blocking.append(blocking_gap(
            field="model_id", source="snapshot manifest",
            reason="snapshot manifest model_id differs from the profile MODEL"))

    digest: str | None = None
    try:
        digest = profile_image_digest(image)
    except ReleaseSpecError as exc:
        blocking.append(blocking_gap(
            field="image", source="conf:IMAGE", reason=str(exc)))

    if spec_decode and not spec_decode_args:
        blocking.append(blocking_gap(
            field="engine_args", source="conf:SPEC_DECODE_ARGS",
            reason="profile has no SPEC_DECODE_ARGS; refusing --spec-decode"))

    remaining: list[str] | None = None
    tensor_parallel = 1
    pipeline_parallel = 1
    try:
        normalized = normalize_engine_args(
            list(engine_args), path="identity.engine_args")
        tensor_parallel, pipeline_parallel, remaining = strip_profile_parallelism(
            normalized)
    except (ReleaseSpecError, TypeError) as exc:
        blocking.append(blocking_gap(
            field="engine_args", source="conf:ENGINE_ARGS", reason=str(exc)))
        remaining = None

    if remaining is not None:
        forbidden = next(
            (_forbidden_flag(token) for token in remaining
             if _forbidden_flag(token) is not None),
            None,
        )
        if forbidden is not None:
            blocking.append(blocking_gap(
                field="engine_args", source="conf:ENGINE_ARGS",
                reason=(f"profile ENGINE_ARGS must not include {forbidden} "
                        "(geometry or deployment overlay owns this flag)")))
            remaining = None
        elif any(token == "--gpu-memory-utilization"
                 or token.startswith("--gpu-memory-utilization=")
                 for token in remaining):
            blocking.append(blocking_gap(
                field="engine_args", source="conf:ENGINE_ARGS",
                reason="profile engine_args duplicate GPU_MEM_UTIL"))
            remaining = None

    if not isinstance(gpu_mem_util, str) or not gpu_mem_util:
        blocking.append(blocking_gap(
            field="engine_args", source="conf:GPU_MEM_UTIL",
            reason="GPU_MEM_UTIL must be a non-empty string"))
        remaining = None

    if remaining is not None:
        remaining = [*remaining, "--gpu-memory-utilization", gpu_mem_util]
        if spec_decode and spec_decode_args:
            remaining.extend(list(spec_decode_args))
        try:
            remaining = normalize_engine_args(
                remaining, path="identity.engine_args")
        except ReleaseSpecError as exc:
            blocking.append(blocking_gap(
                field="engine_args", source="conf:ENGINE_ARGS", reason=str(exc)))
            remaining = None

    if remaining is not None:
        forbidden = next(
            (_forbidden_flag(token) for token in remaining
             if _forbidden_flag(token) is not None),
            None,
        )
        if forbidden is not None:
            blocking.append(blocking_gap(
                field="engine_args", source="conf:ENGINE_ARGS",
                reason=(f"profile ENGINE_ARGS must not include {forbidden} "
                        "(geometry or deployment overlay owns this flag)")))
            remaining = None

    if remaining is not None and tensor_parallel * pipeline_parallel != nodes:
        blocking.append(blocking_gap(
            field="geometry", source="conf:NODES", reason="tp * pp must equal nodes"))

    env_tokens: list[str] | None
    try:
        env_tokens = normalize_container_env(
            list(container_env), path="identity.container_env")
        if freeze_nccl_qps:
            env_tokens = _freeze_nccl_qps(env_tokens, nodes)
    except (ReleaseSpecError, TypeError) as exc:
        blocking.append(blocking_gap(
            field="container_env", source="conf:CONTAINER_ENV", reason=str(exc)))
        env_tokens = None

    manifest: dict[str, Any] | None = None
    if (files is not None and snapshot_revision is not None
            and not any(item["field"] == "model_id" for item in blocking)):
        try:
            manifest = build_snapshot_manifest(
                model_id=model_id, snapshot_revision=snapshot_revision, files=files)
        except (ReleaseSpecError, TypeError) as exc:
            blocking.append(blocking_gap(
                field="snapshot_manifest", source="snapshot manifest", reason=str(exc)))

    if blocking:
        return None, blocking
    if (remaining is None or env_tokens is None or digest is None
            or manifest is None or snapshot_revision is None):
        return None, blocking
    identity = {
        "model_id": model_id,
        "snapshot_revision": snapshot_revision,
        "snapshot_manifest": manifest,
        "engine_args": remaining,
        "container_env": env_tokens,
        "image": {"digest": digest},
        "geometry": {
            "platform_id": platform_id,
            "nodes": nodes,
            "tp": tensor_parallel,
            "pp": pipeline_parallel,
            "fabric": FABRIC_LOCAL if nodes == 1 else FABRIC_ROCE_V2,
        },
    }
    try:
        return identity_block(identity), []
    except ReleaseSpecError as exc:
        return None, [blocking_gap(
            field="identity", source="generator", reason=str(exc))]
