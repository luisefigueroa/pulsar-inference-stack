#!/usr/bin/env python3
"""Stack-side spec consumer: releases index, overlay, launch-contract comparison.

This module owns the ``releases/`` index, the deployment overlay schema, the
projector from a loaded profile to a comparable launch contract, and the
display-only catalog ``project`` projection. It imports ``release_spec`` and
does not write ``releases/`` or start a server. Lab generator code imports the
projector from here.

``PULSAR_RELEASES_ROOT`` is a test override naming the ``releases/`` directory.
An explicit ``--releases-root`` flag wins over the environment variable; both
win over ``<repo-root>/releases``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from typing import Any, Iterable, Sequence

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from release_spec import (  # noqa: E402
    ReleaseSpecError,
    load_spec,
    pretty_json_bytes,
    spec_id_for,
)
from release_spec.recipe import (  # noqa: E402
    build_profile_identity as canonical_build_profile_identity,
    profile_image_digest as canonical_profile_image_digest,
)
from release_spec.identity import argv_from_identity as spec_argv_from_identity  # noqa: E402
from release_spec.schema import (  # noqa: E402
    FABRIC_LOCAL,
    FABRIC_ROCE_V2,
    SHA256_HEX_RE,
    require_public_string,
)

try:
    from scripts.terminal_format import TerminalWriter
except ModuleNotFoundError:  # pragma: no cover - direct script invocation
    from terminal_format import TerminalWriter  # type: ignore[no-redef]

RELEASES_DIR = "releases"
OVERLAY_KIND = "pulsar-deployment-overlay"
OVERLAY_SCHEMA_VERSION = 1
OVERLAY_FILENAME = ".pulsar-overlay.json"
OVERLAY_TOP_KEYS = frozenset({"schema_version", "kind", "defaults", "specs"})
OVERLAY_ENTRY_KEYS = frozenset(
    {"port", "served_name", "cache_root", "placement"}
)
OVERLAY_PLACEMENT_KEYS = frozenset({"node_id"})
RECIPE_OVERLAY_KEYS = frozenset(
    {
        "engine_args",
        "image",
        "geometry",
        "container_env",
        "extra_env",
        "vllm_extra_args",
        "gpu_mem_util",
        "tp",
        "pp",
        "nodes",
        "fabric",
        "platform_id",
    }
)
COMPARABLE_FIELDS = ("argv", "container_env", "geometry", "image_digest")
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

USAGE = (
    "usage: python3 scripts/release_consumer.py list|verify|show [spec_id] "
    "[--repo-root R] [--releases-root D] [--json] [--markdown]\n"
    "       python3 scripts/release_consumer.py export-profile <spec_id> "
    "[--overlay F] [--releases-root D] [--repo-root R] [--image-repo NAME]\n"
    "       python3 scripts/release_consumer.py project --repo-root R "
    "--library-dir L --profile NAME [profile fields] "
    "[--releases-root D] [--extra-arg A] [--extra-env E]\n"
    "       python3 scripts/release_consumer.py project-batch --records FILE"
)
DEFAULT_IMAGE_REPO = "vllm/vllm-openai"
SPEC_ID_RE = re.compile(r"[0-9a-f]{64}")
EMPTY_PROJECTION = {"manifest": "missing", "identities": []}
UNREADABLE_PROJECTION = {"manifest": "unreadable", "identities": []}


class ReleaseConsumerError(ValueError):
    """A releases index, overlay, or profile projection is invalid."""


def fail(message: str) -> None:
    raise ReleaseConsumerError(message)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"JSON object contains duplicate key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    fail(f"JSON contains unsupported constant {value}")


def _reject_floats(value: Any, *, path: str) -> None:
    if isinstance(value, float):
        fail(f"{path} must not be a JSON float")
    if isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}[{index}]" if path else f"[{index}]"
            _reject_floats(item, path=child)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _reject_floats(item, path=child)


def load_json(path: str | pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_json_constant,
            )
    except ReleaseConsumerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"{path}: {exc}")


def profile_image_digest(image: str) -> str:
    """Return ``sha256:<hex>`` from a conf IMAGE pin; fail if unpinned."""
    try:
        return canonical_profile_image_digest(image)
    except ReleaseSpecError as exc:
        fail(str(exc))


def _flag_and_value(item: str, index: int, tokens: list[str]) -> tuple[str, str, int] | None:
    for flag in STRUCTURED_PROFILE_FLAGS:
        if item == flag:
            if index + 1 >= len(tokens):
                fail(f"profile {item} requires a value")
            return flag, tokens[index + 1], index + 2
        prefix = flag + "="
        if item.startswith(prefix):
            return flag, item[len(prefix) :], index + 1
    return None


build_profile_identity = canonical_build_profile_identity


def profile_identity(
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
) -> dict[str, Any]:
    """Return the canonical identity or raise ``ReleaseConsumerError``."""
    identity, gaps = build_profile_identity(
        model_id=model_id,
        image=image,
        nodes=nodes,
        gpu_mem_util=gpu_mem_util,
        engine_args=engine_args,
        container_env=container_env,
        spec_decode_args=spec_decode_args,
        spec_decode=spec_decode,
        platform_id=platform_id,
        snapshot_revision=snapshot_revision,
        files=files,
        source_model_id=source_model_id,
    )
    if identity is None:
        reason = next(
            (item["reason"] for item in gaps if item["class"] == "blocking"),
            "profile identity failed",
        )
        fail(reason)
    return identity


def argv_from_identity(
    identity: dict[str, Any],
    *,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Recipe argv: the shared projection, then live extra args."""
    return [*spec_argv_from_identity(identity), *list(extra_args)]


def comparable_contract_from_identity(
    identity: dict[str, Any],
    *,
    extra_args: Sequence[str] = (),
    extra_env: Sequence[str] = (),
) -> dict[str, Any]:
    """Comparable launch contract for ADR 0017 decision 5 (plus container env)."""
    return {
        "argv": argv_from_identity(identity, extra_args=extra_args),
        "container_env": [*list(identity["container_env"]), *list(extra_env)],
        "image_digest": identity["image"]["digest"],
        "geometry": dict(identity["geometry"]),
    }


def comparable_contract_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    identity = spec["identity"]
    return {
        "argv": list(spec["launch_contract"]["argv"]),
        "container_env": list(identity["container_env"]),
        "image_digest": identity["image"]["digest"],
        "geometry": dict(identity["geometry"]),
    }


def compare_contracts(
    computed: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Return ``{"result": "equal"|"differs", "fields": [...]}``."""
    fields: list[str] = []
    for name in COMPARABLE_FIELDS:
        if computed.get(name) != expected.get(name):
            fields.append(name)
    if fields:
        return {"result": "differs", "fields": fields}
    return {"result": "equal", "fields": []}


def require_exact_contract(computed: dict[str, Any], expected: dict[str, Any]) -> None:
    """Reject recipe drift before launch, independent of catalog review status."""
    comparison = compare_contracts(computed, expected)
    if comparison["result"] != "equal":
        fail("launch contract differs from selected spec: " + ", ".join(comparison["fields"]))


def spec_lifecycle_key(spec_id: str) -> str:
    """Return the 64-hex key used for CONF_NAME, labels, and containers."""
    return _require_spec_id(spec_id)


def image_repo_from_reference(reference: str | None) -> str:
    """Repository part of a pullable image; default ``vllm/vllm-openai``.

    Strips an ``@digest`` and a ``:tag`` that follows the final slash, so a
    registry port such as ``registry.example:5000/team/vllm:tag`` keeps its
    port and yields ``registry.example:5000/team/vllm``.
    """
    text = (reference or "").strip()
    if not text:
        return DEFAULT_IMAGE_REPO
    at = text.find("@")
    if at != -1:
        text = text[:at]
    last_slash = text.rfind("/")
    colon = text.rfind(":")
    if colon != -1 and colon > last_slash:
        text = text[:colon]
    repo = text.strip()
    return repo or DEFAULT_IMAGE_REPO


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def format_shell_assignments(variables: dict[str, Any]) -> str:
    """Emit ``NAME=value`` / ``NAME=(...)`` lines for Bash ``eval``."""
    lines: list[str] = []
    for key, value in variables.items():
        if not isinstance(key, str) or not key.isidentifier():
            fail(f"export-profile: invalid variable name {key!r}")
        if isinstance(value, list):
            quoted = " ".join(_shell_single_quote(str(item)) for item in value)
            lines.append(f"{key}=({quoted})" if quoted else f"{key}=()")
            continue
        if value is None:
            continue
        lines.append(f"{key}={_shell_single_quote(str(value))}")
    return "\n".join(lines) + "\n"


def _gpu_mem_util_from_engine_args(engine_args: list[str]) -> tuple[str, list[str]]:
    """Return GPU_MEM_UTIL and engine_args without that flag pair."""
    remaining: list[str] = []
    gpu_value: str | None = None
    index = 0
    while index < len(engine_args):
        item = engine_args[index]
        if item == "--gpu-memory-utilization":
            if index + 1 >= len(engine_args):
                fail("identity.engine_args: --gpu-memory-utilization requires a value")
            if gpu_value is not None:
                fail("identity.engine_args repeats --gpu-memory-utilization")
            gpu_value = engine_args[index + 1]
            index += 2
            continue
        if item.startswith("--gpu-memory-utilization="):
            if gpu_value is not None:
                fail("identity.engine_args repeats --gpu-memory-utilization")
            gpu_value = item.split("=", 1)[1]
            index += 1
            continue
        remaining.append(item)
        index += 1
    if gpu_value is None or not gpu_value:
        fail("identity.engine_args must include --gpu-memory-utilization")
    return gpu_value, remaining


def spec_profile_variables(
    spec: dict[str, Any],
    overlay_entry: dict[str, Any],
    image_repo: str,
    *,
    active_platform_id: str | None = None,
) -> dict[str, Any]:
    """Map a catalog spec plus overlay into load_conf-shaped variables.

    ``SPEC_PLATFORM_ID`` is always exported so the start path can refuse a
    spec frozen for another platform. Pass ``active_platform_id`` only from
    a launch-admission caller: stop and status must still load a spec after
    the platform setting changed, so the shared loader never gates on it.
    """
    spec_id = spec_lifecycle_key(str(spec.get("spec_id") or ""))
    identity = spec.get("identity")
    if not isinstance(identity, dict):
        fail("spec identity is missing")
    geometry = identity.get("geometry")
    if not isinstance(geometry, dict):
        fail("spec identity.geometry is missing")
    try:
        nodes = int(geometry["nodes"])
        tensor_parallel = int(geometry["tp"])
        pipeline_parallel = int(geometry["pp"])
    except (KeyError, TypeError, ValueError) as exc:
        fail(f"spec identity.geometry is incomplete: {exc}")
    spec_platform = geometry.get("platform_id")
    if not isinstance(spec_platform, str) or not spec_platform:
        fail("spec identity.geometry.platform_id is missing")
    if active_platform_id and spec_platform != active_platform_id:
        fail(
            f"catalog spec {spec_id} targets platform {spec_platform!r}; "
            f"this stack is {active_platform_id!r} (refusing to launch outside "
            "the spec's frozen geometry)"
        )
    fabric = geometry.get("fabric")
    expected_fabric = FABRIC_LOCAL if nodes == 1 else FABRIC_ROCE_V2
    if fabric != expected_fabric:
        fail(
            f"spec geometry.fabric {fabric!r} disagrees with nodes={nodes} "
            f"(expected {expected_fabric})"
        )
    digest = identity.get("image", {}).get("digest") if isinstance(
        identity.get("image"), dict
    ) else None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        fail("spec identity.image.digest is missing")
    repo = image_repo_from_reference(image_repo)
    gpu_mem_util, engine_args = _gpu_mem_util_from_engine_args(
        list(identity.get("engine_args") or [])
    )
    if nodes > 1 or tensor_parallel != 1 or pipeline_parallel != 1:
        engine_args = [
            *engine_args,
            "--tensor-parallel-size",
            str(tensor_parallel),
            "--pipeline-parallel-size",
            str(pipeline_parallel),
        ]
    served_name = overlay_entry.get("served_name")
    if not isinstance(served_name, str) or not served_name:
        fail("overlay served_name is missing")
    port = overlay_entry.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        fail("overlay port must be an integer")
    placement = overlay_entry.get("placement") or {}
    placement_node = ""
    if isinstance(placement, dict):
        node_id = placement.get("node_id")
        if isinstance(node_id, str):
            placement_node = node_id
    cache_root = overlay_entry.get("cache_root")
    if nodes == 1:
        topology_class = "single"
        min_rails = "0"
    else:
        topology_class = "roce-full-mesh"
        min_rails = "2"
    snapshot_revision = identity.get("snapshot_revision")
    if not isinstance(snapshot_revision, str) or not snapshot_revision:
        fail("spec identity.snapshot_revision is missing")
    manifest = identity.get("snapshot_manifest")
    if not isinstance(manifest, dict):
        fail("spec identity.snapshot_manifest is missing")
    manifest_id = manifest.get("manifest_id")
    if not isinstance(manifest_id, str) or SHA256_HEX_RE.fullmatch(manifest_id) is None:
        fail("spec identity.snapshot_manifest.manifest_id is missing")
    total_bytes = manifest.get("total_bytes")
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        fail("spec identity.snapshot_manifest.total_bytes is missing")
    # Disk footprint for the memory gate: whole GiB, rounded up, never below 1.
    weights_gib = str(max(1, -(-total_bytes // (1024 ** 3))))
    variables: dict[str, Any] = {
        "MODEL": identity.get("model_id") or "",
        "IMAGE": f"{repo}@{digest}",
        "NODES": str(nodes),
        "PORT": str(port),
        "SERVED_NAME": served_name,
        "GPU_MEM_UTIL": gpu_mem_util,
        "ENGINE_ARGS": engine_args,
        "CONTAINER_ENV": list(identity.get("container_env") or []),
        "SPEC_DECODE_ARGS": [],
        "PROFILE_PURPOSE": "serving",
        "TOPOLOGY_CLASS": topology_class,
        "MIN_RAILS_PER_PAIR": min_rails,
        "STATUS": "?",
        "NOTES": "",
        "RECOMMENDED_SPEC": "0",
        "FIRST_RUN_CANDIDATE": "0",
        "FAMILY_RECOMMENDED": "0",
        "PROFILE_FAMILY": served_name,
        "VARIANT_LABEL": f"{nodes}-node",
        "WEIGHTS_GIB": weights_gib,
        "WEIGHTS_RAM_GIB": "",
        "KV_GIB": "",
        "OVERHEAD_GIB": "",
        "MEM_MIN_FREE_GIB": "",
        "CONF_NAME": spec_id,
        "CONF_SOURCE": "spec",
        "SNAPSHOT_REVISION": snapshot_revision,
        "SPEC_MANIFEST_ID": manifest_id,
        "SPEC_PLATFORM_ID": spec_platform,
        "OVERLAY_PLACEMENT_NODE_ID": placement_node,
        "OVERLAY_CACHE_ROOT": cache_root if isinstance(cache_root, str) else "",
    }
    if not variables["MODEL"]:
        fail("spec identity.model_id is missing")
    if isinstance(cache_root, str) and cache_root:
        variables["HF_CACHE"] = cache_root
    return variables


def _format_gpu_mem_util(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    number = float(value)
    text = f"{number:.2f}"
    if float(text) == number:
        return text
    return format(number, "g")


def _extract_structured_tokens(
    tokens: list[str],
) -> tuple[int | None, int | None, str | None, list[str]]:
    tensor_parallel: int | None = None
    pipeline_parallel: int | None = None
    gpu_value: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(tokens):
        matched = _flag_and_value(tokens[index], index, tokens)
        if matched is None:
            remaining.append(tokens[index])
            index += 1
            continue
        flag, raw, next_index = matched
        canonical = PARALLELISM_CANONICAL.get(flag, flag)
        if canonical == "--tensor-parallel-size":
            tensor_parallel = int(raw)
        elif canonical == "--pipeline-parallel-size":
            pipeline_parallel = int(raw)
        elif canonical == "--gpu-memory-utilization":
            gpu_value = raw
        else:
            remaining.append(tokens[index])
        index = next_index
    return tensor_parallel, pipeline_parallel, gpu_value, remaining


def plan_to_comparable(plan: dict[str, Any]) -> dict[str, Any]:
    """Project a launch plan onto ADR 0017 comparable contract fields."""
    if not isinstance(plan, dict):
        fail("plan must be an object")
    runtime = plan.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    engine_tp, engine_pp, engine_gpu, remaining_engine = _extract_structured_tokens(
        list(runtime.get("engine_args") or [])
    )
    spec_tp, spec_pp, spec_gpu, remaining_spec = _extract_structured_tokens(
        list(runtime.get("spec_decode_args") or [])
    )
    tensor_parallel = engine_tp if engine_tp is not None else spec_tp
    pipeline_parallel = engine_pp if engine_pp is not None else spec_pp
    if tensor_parallel is None:
        tensor_parallel = 1
    if pipeline_parallel is None:
        pipeline_parallel = 1
    gpu_value = engine_gpu or spec_gpu or _format_gpu_mem_util(plan.get("gpu_mem_util"))
    argv = [
        *remaining_engine,
        "--gpu-memory-utilization",
        gpu_value,
        *remaining_spec,
        "--tensor-parallel-size",
        str(tensor_parallel),
        "--pipeline-parallel-size",
        str(pipeline_parallel),
    ]
    try:
        nodes = int(plan["nodes"])
    except (KeyError, TypeError, ValueError) as exc:
        fail(f"plan.nodes is invalid: {exc}")
    try:
        digest = profile_image_digest(str(plan.get("image") or ""))
    except ReleaseConsumerError as exc:
        fail(f"plan.image: {exc}")
    platform_id = plan.get("platform_id") or "dgx-spark-gb10"
    return {
        "argv": argv,
        "container_env": list(runtime.get("container_env") or []),
        "image_digest": digest,
        "geometry": {
            "platform_id": platform_id,
            "nodes": nodes,
            "tp": tensor_parallel,
            "pp": pipeline_parallel,
            "fabric": FABRIC_LOCAL if nodes == 1 else FABRIC_ROCE_V2,
        },
    }


def profile_identities(
    *,
    model_id: str,
    image: str,
    nodes: int,
    gpu_mem_util: str,
    engine_args: list[str],
    container_env: list[str],
    spec_decode_args: list[str],
    platform_id: str,
    snapshot_revision: str | None,
    files: list[dict[str, Any]] | None,
    source_model_id: str | None = None,
    recommended_spec: bool = False,
) -> list[dict[str, Any]]:
    """Return one entry per spec-decode variant for a profile.

    ``default`` is true for the auto-policy variant: MTP when
    ``recommended_spec`` is true, otherwise the non-MTP identity.
    """
    variants = [False]
    if spec_decode_args:
        variants.append(True)
    rows: list[dict[str, Any]] = []
    for spec_decode in variants:
        identity = profile_identity(
            model_id=model_id,
            image=image,
            nodes=nodes,
            gpu_mem_util=gpu_mem_util,
            engine_args=engine_args,
            container_env=container_env,
            spec_decode_args=spec_decode_args,
            spec_decode=spec_decode,
            platform_id=platform_id,
            snapshot_revision=snapshot_revision,
            files=files,
            source_model_id=source_model_id,
        )
        default = spec_decode if recommended_spec else not spec_decode
        rows.append(
            {
                "spec_decode": spec_decode,
                "spec_id": spec_id_for(identity),
                "contract": comparable_contract_from_identity(identity),
                "default": default,
            }
        )
    return rows


def _releases_root(
    repo_root: str | pathlib.Path,
    releases_root: str | pathlib.Path | None = None,
) -> pathlib.Path:
    if releases_root not in (None, ""):
        return pathlib.Path(releases_root)
    env = os.environ.get("PULSAR_RELEASES_ROOT", "").strip()
    if env:
        return pathlib.Path(env)
    return pathlib.Path(repo_root) / RELEASES_DIR


def _require_spec_id(spec_id: str) -> str:
    if not isinstance(spec_id, str) or SHA256_HEX_RE.fullmatch(spec_id) is None:
        fail("spec_id must be a 64-character lowercase hex digest")
    return spec_id


def _load_released_file(path: pathlib.Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        fail(f"{path}: release file must be a regular file")
    try:
        spec = load_spec(path)
    except ReleaseSpecError as exc:
        fail(f"{path}: {exc}")
    stem = path.stem
    if stem != spec["spec_id"]:
        fail(
            f"{path}: filename stem {stem!r} must equal spec_id "
            f"{spec['spec_id']!r}"
        )
    return spec


def load_release(
    repo_root: str | pathlib.Path,
    spec_id: str,
    *,
    releases_root: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """Load one catalog spec; fail without fallback on any mismatch."""
    digest = _require_spec_id(spec_id)
    path = _releases_root(repo_root, releases_root) / f"{digest}.json"
    if not path.exists():
        fail(f"{path}: catalog spec is missing")
    return _load_released_file(path)


def matching_release_for_profile(
    *,
    repo_root: str | pathlib.Path,
    releases_root: str | pathlib.Path | None,
    model_id: str,
    image: str,
    nodes: int,
    gpu_mem_util: str,
    engine_args: list[str],
    container_env: list[str],
    spec_decode_args: list[str],
    platform_id: str,
    snapshot_revision: str,
    files: list[dict[str, Any]],
    source_model_id: str | None,
    recommended_spec: bool = False,
) -> dict[str, Any]:
    """Load the catalog spec whose id recomputes from these fields plus the snapshot manifest.

    Absent identity or missing file: ``state=absent`` (no new prepare check).
    An invalid file at that id fails without fallback. Never scans by
    ``model_id@revision``. Never uses ``project_profile``.
    """
    spec_decode = bool(recommended_spec) and bool(spec_decode_args)
    identity, _gaps = build_profile_identity(
        model_id=model_id,
        image=image,
        nodes=int(nodes),
        gpu_mem_util=gpu_mem_util,
        engine_args=list(engine_args),
        container_env=list(container_env),
        spec_decode_args=list(spec_decode_args),
        spec_decode=spec_decode,
        platform_id=platform_id or "dgx-spark-gb10",
        snapshot_revision=snapshot_revision,
        files=list(files),
        source_model_id=source_model_id,
    )
    if identity is None:
        return {"state": "absent", "spec_id": None, "snapshot_manifest": None}
    spec_id = spec_id_for(identity)
    root = _releases_root(repo_root, releases_root)
    spec, state = try_load_release(root, spec_id)
    if state == "invalid":
        fail(f"{root / f'{spec_id}.json'}: catalog spec file is invalid")
    if spec is None:
        return {"state": "absent", "spec_id": spec_id, "snapshot_manifest": None}
    manifest = spec["identity"]["snapshot_manifest"]
    if not isinstance(manifest, dict):
        fail(f"{spec_id}: spec identity.snapshot_manifest is missing")
    return {
        "state": "valid",
        "spec_id": spec_id,
        "snapshot_manifest": manifest,
    }


def try_load_release(
    releases_root: str | pathlib.Path,
    spec_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Return ``(spec, state)`` with state ``absent``, ``valid``, or ``invalid``.

    ``invalid`` means the exact ``<spec_id>.json`` exists but fails
    schema or filename verification. The
    catalog reports that explicitly instead of pretending no spec exists.
    """
    try:
        digest = _require_spec_id(spec_id)
    except ReleaseConsumerError:
        return None, "absent"
    path = pathlib.Path(releases_root) / f"{digest}.json"
    if not path.exists() and not path.is_symlink():
        return None, "absent"
    try:
        return _load_released_file(path), "valid"
    except (ReleaseConsumerError, OSError):
        return None, "invalid"


class ProjectionContext:
    """Per-process caches so a batch projection parses shared data once."""

    def __init__(self) -> None:
        self.releases: dict[tuple[str, str], tuple[dict[str, Any] | None, str]] = {}

    def release(self, root: pathlib.Path, spec_id: str) -> tuple[dict[str, Any] | None, str]:
        key = (str(root), spec_id)
        if key not in self.releases:
            self.releases[key] = try_load_release(root, spec_id)
        return self.releases[key]


def _review_fields(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    review = spec.get("review") or {}
    status = review.get("status")
    reviewed_at = review.get("reviewed_at")
    return (
        str(status) if status else None,
        str(reviewed_at) if reviewed_at else None,
    )


def compact_projection_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )


def _empty_projection(manifest: str) -> dict[str, Any]:
    return {"manifest": manifest, "identities": []}


def _blocked_identity_row(*, spec_decode: bool, default: bool) -> dict[str, Any]:
    return {
        "spec_decode": spec_decode,
        "default": default,
        "spec_id": None,
        "released": False,
        "release_file": "absent",
        "comparison": None,
        "differs_fields": [],
        "review_status": None,
        "reviewed_at": None,
    }


def identity_review_cell(
    identity: dict[str, Any] | None,
    *,
    manifest: str,
) -> str:
    """Human cell for one identity (D5)."""
    if not identity:
        return "-"
    if manifest in ("missing", "unreadable") and not identity.get("released"):
        return "-"
    if identity.get("release_file") == "invalid":
        return "invalid release file (verification failed)"
    if not identity.get("spec_id") or not identity.get("released"):
        return "-"
    if identity.get("comparison") == "differs":
        fields = ", ".join(str(item) for item in (identity.get("differs_fields") or []))
        return f"blocked (launch contract differs: {fields})"
    if identity.get("comparison") != "equal":
        return "-"
    status = identity.get("review_status")
    if not status:
        return "-"
    if status == "stable":
        reviewed_at = identity.get("reviewed_at")
        if reviewed_at:
            return f"stable since {reviewed_at}"
        return "stable"
    return str(status)


def human_spec_review_values(payload: dict[str, Any]) -> list[str]:
    manifest = str(payload.get("manifest") or "missing")
    identities = payload.get("identities") or []
    if not isinstance(identities, list) or not identities:
        return ["-"]
    two = len(identities) > 1
    values: list[str] = []
    for item in identities:
        if not isinstance(item, dict):
            cell = "-"
            spec_decode = False
            default = False
        else:
            cell = identity_review_cell(item, manifest=manifest)
            spec_decode = bool(item.get("spec_decode"))
            default = bool(item.get("default"))
        if two:
            prefix = "spec-decode" if spec_decode else "base"
            if default:
                cell = f"{prefix}: {cell} (default)"
            else:
                cell = f"{prefix}: {cell}"
        values.append(cell)
    return values


def picker_spec_marks(payload: dict[str, Any]) -> list[str]:
    manifest = str(payload.get("manifest") or "missing")
    identities = [
        item for item in (payload.get("identities") or []) if isinstance(item, dict)
    ]
    if not identities:
        return ["spec=-"]
    default = next((item for item in identities if item.get("default")), identities[0])
    marks = [f"spec={identity_review_cell(default, manifest=manifest)}"]
    others = [item for item in identities if item is not default]
    if others:
        marks.append(
            f"spec-other={identity_review_cell(others[0], manifest=manifest)}"
        )
    return marks


def enabled_identity_cell(payload: dict[str, Any], *, spec_decode: bool) -> str:
    manifest = str(payload.get("manifest") or "missing")
    identities = [
        item for item in (payload.get("identities") or []) if isinstance(item, dict)
    ]
    if not identities:
        return "-"
    match = next(
        (item for item in identities if bool(item.get("spec_decode")) == spec_decode),
        None,
    )
    if match is None:
        match = next((item for item in identities if item.get("default")), identities[0])
    return identity_review_cell(match, manifest=manifest)


def project_profile(
    *,
    profile: str,
    model_id: str,
    image: str,
    nodes: int,
    gpu_mem_util: str,
    engine_args: Sequence[str],
    container_env: Sequence[str],
    spec_decode_args: Sequence[str],
    platform_id: str,
    recommended_spec: bool,
    library_dir: str | pathlib.Path | None,
    catalog_path: str | pathlib.Path | None = None,
    releases_root: str | pathlib.Path | None = None,
    repo_root: str | pathlib.Path | None = None,
    extra_args: Sequence[str] = (),
    extra_env: Sequence[str] = (),
    context: ProjectionContext | None = None,
    snapshot_revision: str | None = None,
) -> dict[str, Any]:
    """Display-only projection. Never raises for spec absence.

    ``snapshot_revision`` is the exact commit when the caller already knows it
    (a spec start exports it); it replaces the catalog hint so a library with
    several revisions of one model still resolves the selected manifest.
    """
    try:
        return _project_profile(
            profile=profile,
            model_id=model_id,
            image=image,
            nodes=nodes,
            gpu_mem_util=gpu_mem_util,
            engine_args=engine_args,
            container_env=container_env,
            spec_decode_args=spec_decode_args,
            platform_id=platform_id,
            recommended_spec=recommended_spec,
            library_dir=library_dir,
            catalog_path=catalog_path,
            releases_root=releases_root,
            repo_root=repo_root,
            extra_args=extra_args,
            extra_env=extra_env,
            context=context,
            snapshot_revision=snapshot_revision,
        )
    except Exception:
        return dict(UNREADABLE_PROJECTION)


def _project_profile(
    *,
    profile: str,
    model_id: str,
    image: str,
    nodes: int,
    gpu_mem_util: str,
    engine_args: Sequence[str],
    container_env: Sequence[str],
    spec_decode_args: Sequence[str],
    platform_id: str,
    recommended_spec: bool,
    library_dir: str | pathlib.Path | None,
    catalog_path: str | pathlib.Path | None,
    releases_root: str | pathlib.Path | None,
    repo_root: str | pathlib.Path | None,
    extra_args: Sequence[str],
    extra_env: Sequence[str],
    context: ProjectionContext | None = None,
    snapshot_revision: str | None = None,
) -> dict[str, Any]:
    root = _releases_root(
        repo_root if repo_root not in (None, "") else _REPO_ROOT,
        releases_root,
    )
    own = _released_spec_named_by_profile(root, profile, context)
    if own is None:
        return _empty_projection("missing")
    files = list(own["identity"]["snapshot_manifest"]["files"])
    selected_revision = str(own["identity"]["snapshot_revision"])
    if snapshot_revision and snapshot_revision != selected_revision:
        return _empty_projection("unreadable")
    snapshot_revision = selected_revision
    source_model_id = str(own["identity"]["model_id"])
    manifest_label = "spec"

    variants = [False]
    if spec_decode_args:
        variants.append(True)
    identities: list[dict[str, Any]] = []
    for spec_decode in variants:
        default = spec_decode if recommended_spec else not spec_decode
        identity, _gaps = build_profile_identity(
            model_id=model_id,
            image=image,
            nodes=int(nodes),
            gpu_mem_util=gpu_mem_util,
            engine_args=list(engine_args),
            container_env=list(container_env),
            spec_decode_args=list(spec_decode_args),
            spec_decode=spec_decode,
            platform_id=platform_id or "dgx-spark-gb10",
            snapshot_revision=snapshot_revision,
            files=files,
            source_model_id=source_model_id,
            freeze_nccl_qps=any(
                item.startswith("NCCL_IB_QPS_PER_CONNECTION=")
                for item in own["identity"]["container_env"]
            ),
        )
        if identity is None:
            identities.append(
                _blocked_identity_row(spec_decode=spec_decode, default=default)
            )
            continue
        spec_id = spec_id_for(identity)
        if context is not None:
            spec, release_file = context.release(root, spec_id)
        else:
            spec, release_file = try_load_release(root, spec_id)
        if spec is None:
            identities.append(
                {
                    "spec_decode": spec_decode,
                    "default": default,
                    "spec_id": spec_id,
                    "released": False,
                    "release_file": release_file,
                    "comparison": None,
                    "differs_fields": [],
                    "review_status": None,
                    "reviewed_at": None,
                }
            )
            continue
        computed = comparable_contract_from_identity(
            identity, extra_args=extra_args, extra_env=extra_env
        )
        comparison = compare_contracts(
            computed, comparable_contract_from_spec(spec)
        )
        equal = comparison["result"] == "equal"
        status, reviewed_at = _review_fields(spec)
        identities.append(
            {
                "spec_decode": spec_decode,
                "default": default,
                "spec_id": spec_id,
                "released": True,
                "release_file": "valid",
                "comparison": comparison["result"],
                "differs_fields": list(comparison["fields"]),
                "review_status": status if equal else None,
                "reviewed_at": reviewed_at if equal else None,
            }
        )
    return {"manifest": manifest_label, "identities": identities}


def _released_spec_named_by_profile(
    root: pathlib.Path,
    profile: str,
    context: ProjectionContext | None,
) -> dict[str, Any] | None:
    """The catalog spec whose id is ``profile`` (a profile is a spec id)."""
    if not SPEC_ID_RE.fullmatch(profile or ""):
        return None
    if context is not None:
        spec, _release_file = context.release(root, profile)
    else:
        spec, _release_file = try_load_release(root, profile)
    return spec


def list_releases(
    repo_root: str | pathlib.Path,
    *,
    releases_root: str | pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Return sorted released-spec rows. A bad file fails the listing."""
    root = _releases_root(repo_root, releases_root)
    if not root.exists():
        return []
    if not root.is_dir() or root.is_symlink():
        fail(f"{root}: releases must be a directory")
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        spec = _load_released_file(path)
        status, reviewed_at = _review_fields(spec)
        rows.append(
            {
                "spec_id": spec["spec_id"],
                "model_id": spec["identity"]["model_id"],
                "nodes": int(spec["identity"]["geometry"]["nodes"]),
                "image_digest": spec["identity"]["image"]["digest"],
                "state": spec["state"],
                "review_status": status,
                "reviewed_at": reviewed_at,
                "withdrawal_reason": (spec.get("review") or {}).get("reason"),
                "path": f"{RELEASES_DIR}/{path.name}",
            }
        )
    rows.sort(key=lambda item: item["spec_id"])
    return rows


def _reject_recipe_keys(keys: Iterable[str], *, path: str) -> None:
    for key in keys:
        if key in RECIPE_OVERLAY_KEYS:
            fail(f"{path}: overlay must not name recipe field {key}")


def _require_overlay_object(
    value: Any,
    allowed: frozenset[str],
    *,
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{path} must be an object")
    _reject_recipe_keys(value, path=path)
    extra = sorted(set(value) - set(allowed))
    missing = sorted(set(allowed) - set(value))
    if extra:
        fail(f"{path}: unknown key {extra[0]}")
    if missing:
        fail(f"{path} fields differ (missing={missing}, extra={extra})")
    return value


def _optional_public_string(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    try:
        return require_public_string(value, path=path)
    except ReleaseSpecError as exc:
        fail(str(exc))


def _optional_site_string(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        fail(f"{path} must be a non-empty string")
    return value


def _overlay_port(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{path} must be an integer")
    if value < 1 or value > 65535:
        fail(f"{path} must be a TCP port")
    return value


def _overlay_placement(value: Any, *, path: str) -> dict[str, str] | None:
    if value is None:
        return None
    obj = _require_overlay_object(value, OVERLAY_PLACEMENT_KEYS, path=path)
    node_id = _optional_site_string(obj.get("node_id"), path=f"{path}.node_id")
    if node_id is None:
        fail(f"{path}.node_id must be a non-empty string")
    return {"node_id": node_id}


def _overlay_entry(value: Any, *, path: str) -> dict[str, Any]:
    obj = _require_overlay_object(value, OVERLAY_ENTRY_KEYS, path=path)
    return {
        "port": _overlay_port(obj.get("port"), path=f"{path}.port"),
        "served_name": _optional_public_string(
            obj.get("served_name"), path=f"{path}.served_name"
        ),
        "cache_root": _optional_site_string(
            obj.get("cache_root"), path=f"{path}.cache_root"
        ),
        "placement": _overlay_placement(
            obj.get("placement"), path=f"{path}.placement"
        ),
    }


def default_overlay() -> dict[str, Any]:
    """The overlay a site has before it writes one: every default unset, so
    the port is 8000, the served name is the model id, and placement is
    resolved at start. A fresh clone serves a catalog spec with it."""
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "kind": OVERLAY_KIND,
        "defaults": _overlay_entry(
            {"port": 8000, "served_name": None, "cache_root": None, "placement": None},
            path="overlay.defaults",
        ),
        "specs": {},
    }


def load_overlay(path: str | pathlib.Path) -> dict[str, Any]:
    """Load a closed deployment overlay. Fail without fallback on any extra key.

    An absent file is the default overlay; a symlink, directory, or unreadable
    file is an error (never silently ignored).
    """
    overlay_path = pathlib.Path(path)
    if overlay_path.is_symlink():
        fail(f"{overlay_path}: overlay must be a regular file, not a symlink")
    if not overlay_path.exists():
        return default_overlay()
    if not overlay_path.is_file():
        fail(f"{overlay_path}: overlay must be a regular file")
    document = load_json(overlay_path)
    _reject_floats(document, path="overlay")
    obj = _require_overlay_object(document, OVERLAY_TOP_KEYS, path="overlay")
    schema_version = obj.get("schema_version")
    if type(schema_version) is not int or schema_version != OVERLAY_SCHEMA_VERSION:
        fail("overlay.schema_version must be 1")
    if obj.get("kind") != OVERLAY_KIND:
        fail(f"overlay.kind must be {OVERLAY_KIND!r}")
    defaults = _overlay_entry(obj.get("defaults"), path="overlay.defaults")
    specs_raw = obj.get("specs")
    if not isinstance(specs_raw, dict):
        fail("overlay.specs must be an object")
    _reject_recipe_keys(specs_raw, path="overlay.specs")
    specs: dict[str, dict[str, Any]] = {}
    for key, item in specs_raw.items():
        if not isinstance(key, str) or SHA256_HEX_RE.fullmatch(key) is None:
            fail(
                "overlay.specs keys must be 64-character lowercase hex spec ids"
            )
        specs[key] = _overlay_entry(item, path=f"overlay.specs.{key}")
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "kind": OVERLAY_KIND,
        "defaults": defaults,
        "specs": specs,
    }


def overlay_for_spec(
    overlay: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Merge defaults and per-spec overlay. ``served_name`` null → model_id."""
    defaults = overlay["defaults"]
    spec_id = spec["spec_id"]
    entry = dict(defaults)
    if spec_id in overlay.get("specs", {}):
        entry.update(overlay["specs"][spec_id])
    served = entry.get("served_name")
    if served is None:
        served = spec["identity"]["model_id"]
    return {
        "served_name": served,
        "port": entry["port"],
        "cache_root": entry.get("cache_root"),
        "placement": entry.get("placement"),
    }


def load_spec_file_for_id(spec_file: str | pathlib.Path, spec_id: str) -> dict[str, Any]:
    """A spec document whose spec_id is ``spec_id``.

    The lab may start a spec directly before catalog publication:
    ``PULSAR_SPEC_FILE`` names the file and the profile is its spec id.
    """
    digest = _require_spec_id(spec_id)
    path = pathlib.Path(spec_file)
    if path.is_symlink() or not path.is_file():
        fail(f"{path}: spec file must be a regular file")
    try:
        spec = load_spec(path)
    except ReleaseSpecError as exc:
        fail(f"{path}: {exc}")
    if spec["spec_id"] != digest:
        fail(f"{path}: spec_id {spec['spec_id']!r} is not the requested {digest!r}")
    return spec


def cmd_export_profile(
    repo_root: pathlib.Path,
    spec_id: str,
    *,
    overlay_path: str | pathlib.Path | None = None,
    releases_root: str | pathlib.Path | None = None,
    image_repo: str | None = None,
    active_platform_id: str | None = None,
    spec_file: str | pathlib.Path | None = None,
) -> int:
    if spec_file:
        spec = load_spec_file_for_id(spec_file, spec_id)
    else:
        spec = load_release(repo_root, spec_id, releases_root=releases_root)
    overlay_file = (
        pathlib.Path(overlay_path)
        if overlay_path not in (None, "")
        else pathlib.Path(repo_root) / OVERLAY_FILENAME
    )
    overlay = load_overlay(overlay_file)
    entry = overlay_for_spec(overlay, spec)
    repo = image_repo_from_reference(
        image_repo or os.environ.get("VLLM_IMAGE_MAINLINE")
    )
    variables = spec_profile_variables(
        spec, entry, repo, active_platform_id=active_platform_id or None
    )
    variables["OVERLAY_SOURCE"] = (
        str(overlay_file) if overlay_file.is_file() else f"defaults (no {overlay_file})"
    )
    sys.stdout.write(format_shell_assignments(variables))
    return 0


def _review_text(status: str | None, reviewed_at: str | None) -> str:
    """Human review line: ``stable since <date>``, otherwise the bare status."""
    if not status:
        return "-"
    if status == "stable" and reviewed_at:
        return f"{status} since {reviewed_at}"
    return status


def markdown_release_table(rows: list[dict[str, Any]]) -> str:
    """The generated support-matrix block for docs/MODELS.md.

    One row per catalog spec: the spec id is the profile name operators pass
    to ``./pulsar start``; review is display-only (ADR 0017).
    """
    lines = [
        "| Spec id (profile) | Model | Nodes | Image digest | Review |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| `{spec_id}` | {model_id} | {nodes} | `{digest}` | {review} |".format(
                spec_id=row["spec_id"],
                model_id=row["model_id"],
                nodes=row["nodes"],
                digest=str(row["image_digest"]).rsplit(":", 1)[-1][:12],
                review=_review_text(row["review_status"], row["reviewed_at"]),
            )
        )
    if len(lines) == 2:
        lines.append("| (no catalog specs) | | | | |")
    return "\n".join(lines) + "\n"


def cmd_list(
    repo_root: pathlib.Path,
    *,
    as_json: bool,
    as_markdown: bool = False,
    releases_root: str | pathlib.Path | None = None,
) -> int:
    rows = list_releases(repo_root, releases_root=releases_root)
    if as_json:
        sys.stdout.buffer.write(pretty_json_bytes({"releases": rows}))
        return 0
    if as_markdown:
        sys.stdout.write(markdown_release_table(rows))
        return 0
    term = TerminalWriter()
    for index, row in enumerate(rows):
        if index:
            term.blank()
        term.emit(row["spec_id"])
        term.field("model", row["model_id"], indent=2)
        term.field("review", _review_text(row["review_status"], row["reviewed_at"]), indent=2)
    return 0


def cmd_verify(
    repo_root: pathlib.Path,
    spec_id: str,
    *,
    as_json: bool,
    releases_root: str | pathlib.Path | None = None,
) -> int:
    spec = load_release(repo_root, spec_id, releases_root=releases_root)
    status, reviewed_at = _review_fields(spec)
    if as_json:
        sys.stdout.buffer.write(
            pretty_json_bytes(
                {
                    "spec_id": spec["spec_id"],
                    "state": spec["state"],
                    "review": status,
                    "reviewed_at": reviewed_at,
                }
            )
        )
        return 0
    term = TerminalWriter()
    term.field("spec_id", spec["spec_id"])
    term.field("state", spec["state"] or "not specified")
    term.field("review", _review_text(status, reviewed_at))
    return 0


def cmd_show(
    repo_root: pathlib.Path,
    spec_id: str,
    *,
    as_json: bool,
    releases_root: str | pathlib.Path | None = None,
    spec_file: str | pathlib.Path | None = None,
) -> int:
    if spec_file:
        spec = load_spec_file_for_id(spec_file, spec_id)
    else:
        spec = load_release(repo_root, spec_id, releases_root=releases_root)
    sys.stdout.buffer.write(pretty_json_bytes(spec))
    return 0


def project_payload(
    args: argparse.Namespace,
    *,
    context: ProjectionContext | None = None,
) -> dict[str, Any]:
    try:
        recommended = bool(int(args.recommended_spec or 0))
    except (TypeError, ValueError):
        recommended = False
    try:
        nodes = int(args.nodes or 1)
    except (TypeError, ValueError):
        nodes = 1
    payload = project_profile(
        profile=str(args.profile or ""),
        model_id=str(args.model_id or ""),
        image=str(args.image or ""),
        nodes=nodes,
        gpu_mem_util=str(args.gpu_mem_util or ""),
        engine_args=list(args.engine_arg or []),
        container_env=list(args.container_env or []),
        spec_decode_args=list(args.spec_decode_arg or []),
        platform_id=str(args.platform_id or "dgx-spark-gb10"),
        recommended_spec=recommended,
        library_dir=args.library_dir or None,
        catalog_path=args.catalog or None,
        releases_root=args.releases_root,
        repo_root=args.repo_root,
        extra_args=list(args.extra_arg or []),
        extra_env=list(args.extra_env or []),
        context=context,
        snapshot_revision=str(getattr(args, "snapshot_revision", "") or "") or None,
    )
    return payload


def cmd_project(args: argparse.Namespace) -> int:
    sys.stdout.write(compact_projection_json(project_payload(args)) + "\n")
    return 0


def read_projection_records(path: str | pathlib.Path) -> list[list[str]]:
    """Read batch records: one argv token per line, blank line between records."""
    records: list[list[str]] = []
    current: list[str] = []
    for raw in pathlib.Path(path).read_text(encoding="utf-8").split("\n"):
        if raw == "":
            if current:
                records.append(current)
                current = []
            continue
        current.append(raw)
    if current:
        records.append(current)
    return records


def _record_profile(record: list[str]) -> str:
    for index, token in enumerate(record):
        if token == "--profile" and index + 1 < len(record):
            return record[index + 1]
        if token.startswith("--profile="):
            return token[len("--profile="):]
    return ""


def cmd_project_batch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Project every record with one process and shared caches (display only)."""
    context = ProjectionContext()
    projections: dict[str, dict[str, Any]] = {}
    try:
        records = read_projection_records(args.records)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for record in records:
        profile = _record_profile(record)
        try:
            record_args = parser.parse_args(["project", *record])
            payload = project_payload(record_args, context=context)
        except SystemExit:
            payload = dict(UNREADABLE_PROJECTION)
        except Exception:
            payload = dict(UNREADABLE_PROJECTION)
        if profile:
            projections[profile] = payload
    sys.stdout.write(compact_projection_json({"projections": projections}) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read catalog specs under releases/",
        usage=USAGE,
    )
    parser.add_argument(
        "command",
        choices=(
            "list",
            "verify",
            "show",
            "export-profile",
            "project",
            "project-batch",
        ),
    )
    parser.add_argument("spec_id", nargs="?")
    parser.add_argument(
        "--overlay",
        default="",
        help="Deployment overlay path (default: <repo-root>/.pulsar-overlay.json)",
    )
    parser.add_argument(
        "--image-repo",
        default="",
        help="Image repository used with the spec digest (default: VLLM_IMAGE_MAINLINE)",
    )
    parser.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
    )
    parser.add_argument(
        "--releases-root",
        default=None,
        help="Directory that is releases/; overrides PULSAR_RELEASES_ROOT",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="list: print the generated docs/MODELS.md support-matrix table",
    )
    parser.add_argument(
        "--spec-file",
        default="",
        help="export-profile/show: read this spec document (measured or released) instead of releases/",
    )
    parser.add_argument("--library-dir", default="")
    parser.add_argument("--catalog", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--model-id", default="")
    parser.add_argument("--served-name", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--nodes", default="1")
    parser.add_argument("--port", default="8000")
    parser.add_argument("--gpu-mem-util", default="")
    parser.add_argument("--engine-arg", action="append", default=[])
    parser.add_argument("--container-env", action="append", default=[])
    parser.add_argument("--spec-decode-arg", action="append", default=[])
    parser.add_argument("--recommended-spec", default="0")
    parser.add_argument("--profile-purpose", default="")
    parser.add_argument("--topology-class", default="")
    parser.add_argument("--min-rails-per-pair", default="")
    parser.add_argument("--weights-gib", default="")
    parser.add_argument("--weights-ram-gib", default="")
    parser.add_argument("--kv-gib", default="")
    parser.add_argument("--overhead-gib", default="")
    parser.add_argument("--mem-min-free-gib", default="")
    # No parser default: export-profile must only gate on a platform the
    # launch-admission caller passes explicitly; project applies the GB10
    # default itself.
    parser.add_argument("--platform-id", default="")
    parser.add_argument("--extra-arg", action="append", default=[])
    parser.add_argument("--extra-env", action="append", default=[])
    parser.add_argument("--records", default="", help="project-batch: records file")
    parser.add_argument(
        "--snapshot-revision",
        default="",
        help="project: exact commit already known to the caller (spec starts)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    repo_root = pathlib.Path(args.repo_root)
    try:
        if args.command == "project-batch":
            if not args.records:
                fail("project-batch requires --records FILE")
            return cmd_project_batch(args, parser)
        if args.command == "project":
            if args.spec_id:
                fail("project does not take a spec_id")
            return cmd_project(args)
        if args.command == "list":
            if args.spec_id:
                fail("list does not take a spec_id")
            return cmd_list(
                repo_root,
                as_json=args.json,
                as_markdown=args.markdown,
                releases_root=args.releases_root,
            )
        if not args.spec_id:
            fail(f"{args.command} requires a spec_id")
        if args.command == "export-profile":
            return cmd_export_profile(
                repo_root,
                args.spec_id,
                overlay_path=args.overlay or None,
                spec_file=args.spec_file or None,
                releases_root=args.releases_root,
                image_repo=args.image_repo or None,
                active_platform_id=args.platform_id or None,
            )
        if args.command == "verify":
            return cmd_verify(
                repo_root,
                args.spec_id,
                as_json=args.json,
                releases_root=args.releases_root,
            )
        return cmd_show(
            repo_root,
            args.spec_id,
            as_json=args.json,
            releases_root=args.releases_root,
            spec_file=args.spec_file or None,
        )
    except (ReleaseConsumerError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
