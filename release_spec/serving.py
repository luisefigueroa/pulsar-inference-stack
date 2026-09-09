"""Portable serving specs, independent of checkouts and deployment locations.

Schema 1 is read only through ``load_spec(..., historical=True)``. Snapshot
manifests retain their original schema and digest so storage remains reusable.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .identity import _canonical_container_env, _canonical_engine_args, _canonical_geometry
from .immutable_io import ImmutableDescriptorDirectoryError, parse_strict_json, read_absolute_file
from .manifest import verify_snapshot_manifest
from .normalize import canonical_json_digest, normalize_container_env, normalize_engine_args
from .schema import ReleaseSpecError, IMAGE_DIGEST_RE, require_commit, require_model_id, require_public_string
from .verify import _verify_review, verify_spec as verify_historical_spec

SPEC_SCHEMA_VERSION = 2
SPEC_KIND = "pulsar-serving-spec"
DRAFT_SCHEMA_VERSION = 1
DRAFT_KIND = "pulsar-recipe-draft"
SPEC_FIELDS = {"schema_version", "kind", "spec_id", "recipe", "source", "state", "review"}
RECIPE_FIELDS = {"model", "image_digest", "engine_args", "container_env", "geometry", "container"}
CONTAINER_FIELDS = {"network_mode", "ipc_mode", "shm_size_bytes", "ulimits",
                    "memory_limit_bytes", "cpu_limit_nanos", "accelerator_access",
                    "devices", "restart_policy", "restart_max_retries", "healthcheck"}
# These values are supplied by the documented rank/site binding, never by a
# recipe or arbitrary environment inherited by the launcher.
RESERVED_ENV = {"HF_TOKEN", "VLLM_API_KEY", "API_KEY", "VLLM_HOST_IP", "NCCL_IB_HCA",
                "NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "TP_SOCKET_IFNAME"}


class SpecValidationError(ReleaseSpecError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.reason = message


def invalid(field: str, message: str):
    raise SpecValidationError(field, message)


def closed(value: Any, fields: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        invalid(path, "expected an object")
    if set(value) != fields:
        missing, extra = sorted(fields - value.keys()), sorted(value.keys() - fields)
        invalid(path, f"missing fields {missing}; unknown fields {extra}")
    return value


def integer(value: Any, path: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        invalid(path, f"expected an integer >= {minimum}")
    return value


def choice(value: Any, choices: tuple[str, ...], path: str) -> str:
    if not isinstance(value, str) or value not in choices:
        invalid(path, f"expected one of {', '.join(choices)}")
    return value


def canonical_container(value: Any) -> dict:
    c = copy.deepcopy(closed(value, CONTAINER_FIELDS, "recipe.container"))
    choice(c["network_mode"], ("bridge", "host"), "recipe.container.network_mode")
    choice(c["ipc_mode"], ("host", "private"), "recipe.container.ipc_mode")
    if c["ipc_mode"] == "host":
        if c["shm_size_bytes"] is not None:
            invalid("recipe.container.shm_size_bytes", "must be null with host IPC")
    else:
        integer(c["shm_size_bytes"], "recipe.container.shm_size_bytes", 1)
    for name in ("memory_limit_bytes", "cpu_limit_nanos", "restart_max_retries"):
        integer(c[name], "recipe.container." + name)
    if 0 < c['memory_limit_bytes'] < 6 * 1024 * 1024:
        invalid('recipe.container.memory_limit_bytes','Docker memory limits must be zero or at least 6 MiB')
    choice(c["accelerator_access"], ("all",), "recipe.container.accelerator_access")
    if not isinstance(c["devices"], list) or any(x != "infiniband" for x in c["devices"]):
        invalid("recipe.container.devices", "expected a list of supported device identifiers")
    if len(c["devices"]) != len(set(c["devices"])):
        invalid("recipe.container.devices", "duplicate device requirement")
    c["devices"] = sorted(c["devices"])
    choice(c["restart_policy"], ("no", "always", "unless-stopped", "on-failure"),
           "recipe.container.restart_policy")
    if c["restart_policy"] != "on-failure" and c["restart_max_retries"] != 0:
        invalid("recipe.container.restart_max_retries", "requires on-failure restart policy")
    limits = c["ulimits"]
    if not isinstance(limits, dict) or set(limits) - {"memlock", "stack", "nofile"}:
        invalid("recipe.container.ulimits", "supported limits are memlock, stack, nofile")
    if not {"memlock", "stack"}.issubset(limits):
        invalid("recipe.container.ulimits", "memlock and stack must be explicit")
    for name, limit in limits.items():
        path = f"recipe.container.ulimits.{name}"
        closed(limit, {"soft", "hard"}, path)
        soft, hard = (integer(limit[k], path + "." + k, -1) for k in ("soft", "hard"))
        if hard != -1 and (soft == -1 or soft > hard):
            invalid(path, "soft limit exceeds hard limit")
    health = c["healthcheck"]
    if health is not None:
        closed(health, {"path", "interval_seconds", "timeout_seconds", "retries",
                        "start_period_seconds"}, "recipe.container.healthcheck")
        # A path is data used by the fixed HTTP health probe, never shell code.
        if not isinstance(health["path"], str) or not re.fullmatch(r"/[A-Za-z0-9_./-]*", health["path"]):
            invalid("recipe.container.healthcheck.path", "expected an HTTP path without query or shell syntax")
        for name in ("interval_seconds", "timeout_seconds", "retries"):
            integer(health[name], "recipe.container.healthcheck." + name, 1)
        integer(health["start_period_seconds"], "recipe.container.healthcheck.start_period_seconds")
    return c


def canonical_recipe(value: Any) -> dict:
    r = copy.deepcopy(closed(value, RECIPE_FIELDS, "recipe"))
    model = closed(r["model"], {"model_id", "model_commit", "snapshot_manifest"}, "recipe.model")
    require_public_string(require_model_id(model["model_id"], path="recipe.model.model_id"),
                          path="recipe.model.model_id")
    require_commit(model["model_commit"], path="recipe.model.model_commit")
    manifest = verify_snapshot_manifest(model["snapshot_manifest"])
    if manifest["model_id"] != model["model_id"] or manifest["snapshot_revision"] != model["model_commit"]:
        invalid("recipe.model.snapshot_manifest", "does not identify the selected model commit")
    model["snapshot_manifest"] = manifest
    if not isinstance(r["image_digest"], str) or not IMAGE_DIGEST_RE.fullmatch(r["image_digest"]):
        invalid("recipe.image_digest", "expected sha256:<64 lowercase hex digits>")
    if not isinstance(r["engine_args"], list):
        invalid("recipe.engine_args", "expected a token array, not executable shell text")
    args = normalize_engine_args(r["engine_args"], path="recipe.engine_args")
    r["engine_args"] = _canonical_engine_args(args, path="recipe.engine_args")
    flags = [x for x in args if x.startswith("--")]
    if len(flags) != len(set(flags)):
        invalid("recipe.engine_args", "duplicate engine flags are not supported")
    try:
        index = args.index("--gpu-memory-utilization") + 1
        memory_fraction = Decimal(args[index])
        if not memory_fraction.is_finite() or not 0 < memory_fraction <= 1:
            raise InvalidOperation
    except (ValueError, IndexError, InvalidOperation):
        invalid("recipe.engine_args", "explicit --gpu-memory-utilization must be a decimal in (0, 1]")
    r["engine_args"][index] = format(memory_fraction.normalize(), "f")
    r["container_env"] = _canonical_container_env(
        normalize_container_env(r["container_env"], path="recipe.container_env"), path="recipe.container_env")
    env = dict(x.split("=", 1) for x in r["container_env"])
    if RESERVED_ENV.intersection(env):
        invalid("recipe.container_env", "cannot override site bindings or credentials")
    r["geometry"] = _canonical_geometry(r["geometry"], path="recipe.geometry")
    r["container"] = canonical_container(r["container"])
    if r["geometry"]["nodes"] > 1:
        try:
            backend = args[args.index('--distributed-executor-backend') + 1]
        except (ValueError, IndexError):
            backend = None
        if backend != 'mp':
            invalid('recipe.engine_args', 'multi-node serving requires --distributed-executor-backend mp')
        if r["container"]["network_mode"] != "host" or "infiniband" not in r["container"]["devices"]:
            invalid("recipe.container", "multi-node serving requires host networking and infiniband access")
        if not re.fullmatch(r"[1-9][0-9]*", env.get("NCCL_IB_QPS_PER_CONNECTION", "")):
            invalid("recipe.container_env", "multi-node NCCL_IB_QPS_PER_CONNECTION must be explicit and positive")
    return r


def spec_id(recipe: dict) -> str:
    return canonical_json_digest({"schema_version": SPEC_SCHEMA_VERSION,
                                  "recipe": canonical_recipe(recipe)})


def source_location(value: Any) -> dict:
    source = closed(value, {"image_repository"}, "source")
    repository = require_public_string(source["image_repository"], path="source.image_repository")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", repository) or ".." in repository.split("/"):
        invalid("source.image_repository", "expected a public repository without tag, digest, or endpoint")
    return {"image_repository": repository}


def verify_spec(document: Any) -> dict:
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document.get("schema_version") != SPEC_SCHEMA_VERSION:
        invalid("schema_version", "unsupported spec; new operations require schema 2 (use historical show for old records)")
    closed(document, SPEC_FIELDS, "spec")
    if document["kind"] != SPEC_KIND:
        invalid("kind", f"expected {SPEC_KIND}")
    recipe = canonical_recipe(document["recipe"])
    if recipe != document["recipe"]:
        invalid("recipe", "must use canonical ordering and token spelling; freeze the draft first")
    if document["spec_id"] != spec_id(recipe):
        invalid("spec_id", "does not match the canonical recipe")
    if document["state"] not in (None, "measured", "released"):
        invalid("state", "expected null, measured, or released")
    return {**copy.deepcopy(document), "source": source_location(document["source"]),
            "review": _verify_review(document["review"], path="review")}


def load_json(path: str | Path) -> Any:
    try:
        return parse_strict_json(read_absolute_file(Path(path).absolute(), label="spec input"), label="spec input")
    except ImmutableDescriptorDirectoryError as exc:
        raise SpecValidationError("input", str(exc)) from exc


def load_spec(path: str | Path, *, historical: bool = False) -> dict:
    document = load_json(path)
    if historical and isinstance(document, dict) and document.get("schema_version") == 1:
        return verify_historical_spec(document)
    return verify_spec(document)


def freeze(draft: Any, manifest: Any) -> dict:
    closed(draft, {"schema_version", "kind", "recipe", "source"}, "draft")
    if type(draft["schema_version"]) is not int or draft["schema_version"] != DRAFT_SCHEMA_VERSION or draft["kind"] != DRAFT_KIND:
        invalid("draft", "unsupported draft format")
    recipe = copy.deepcopy(draft["recipe"])
    if not isinstance(recipe, dict):
        invalid("draft.recipe", "expected an object")
    model = closed(recipe.get("model"), {"model_id", "model_commit"}, "draft.recipe.model")
    recipe["model"] = {**model, "snapshot_manifest": verify_snapshot_manifest(manifest)}
    recipe = canonical_recipe(recipe)
    return verify_spec({"schema_version": SPEC_SCHEMA_VERSION, "kind": SPEC_KIND,
                        "spec_id": spec_id(recipe), "recipe": recipe,
                        "source": source_location(draft["source"]), "state": None, "review": None})


def apply_overrides(selected: dict, overrides: Any) -> dict:
    selected = verify_spec(selected)
    if not isinstance(overrides, dict) or set(overrides) - {"engine_args", "container_env", "container"}:
        invalid("overrides", "only engine_args, container_env and container may be overridden")
    recipe = copy.deepcopy(selected["recipe"])

    def merge(target, patch, path):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value, path + "." + key)
            else:
                target[key] = copy.deepcopy(value)

    merge(recipe, overrides, "recipe")
    recipe = canonical_recipe(recipe)
    effective = {**selected, "recipe": recipe, "spec_id": spec_id(recipe)}
    if effective["spec_id"] != selected["spec_id"]:
        effective.update(state=None, review=None)
    return verify_spec(effective)


def compare(before: dict, after: dict) -> dict:
    before, after = verify_spec(before), verify_spec(after)
    changes = []

    def visit(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                visit(a.get(key), b.get(key), path + "." + key)
        elif a != b:
            changes.append({"field": path, "before": a, "after": b})

    visit(before["recipe"], after["recipe"], "recipe")
    return {"before_spec_id": before["spec_id"], "after_spec_id": after["spec_id"],
            "recipe_changed": before["spec_id"] != after["spec_id"], "changes": changes}


def example(nodes: int = 1) -> dict:
    integer(nodes, "nodes", 1)
    env = ["HF_HUB_OFFLINE=1"]
    if nodes == 1:
        env.append("VLLM_LOGGING_LEVEL=INFO")
    else:
        env += ["NCCL_DEBUG=WARN", "NCCL_IB_DISABLE=0", "NCCL_IB_QPS_PER_CONNECTION=4", "NCCL_NET=IB"]
    return {"schema_version": DRAFT_SCHEMA_VERSION, "kind": DRAFT_KIND,
            "source": {"image_repository": "vllm/vllm-openai"},
            "recipe": {"model": {"model_id": None, "model_commit": None},
                       "image_digest": None, "engine_args": ["--gpu-memory-utilization", "0.80"] +
                        (["--distributed-executor-backend", "mp"] if nodes > 1 else []),
                       "container_env": sorted(env),
                       "geometry": {"platform_id": "dgx-spark-gb10", "nodes": nodes,
                                    "tp": nodes, "pp": 1, "fabric": "local" if nodes == 1 else "roce-v2"},
                       "container": {"network_mode": "bridge" if nodes == 1 else "host", "ipc_mode": "host",
                                     "shm_size_bytes": None, "ulimits": {"memlock": {"soft": -1, "hard": -1},
                                     "stack": {"soft": 67108864, "hard": 67108864}},
                                     "memory_limit_bytes": 0, "cpu_limit_nanos": 0, "accelerator_access": "all",
                                     "devices": [] if nodes == 1 else ["infiniband"], "restart_policy": "no",
                                     "restart_max_retries": 0,
                                     "healthcheck": {"path": "/health", "interval_seconds": 30, "timeout_seconds": 5,
                                                     "retries": 3, "start_period_seconds": 900} if nodes == 1 else None}}}


def identity_fields(spec: dict) -> dict:
    """Model/geometry fields consumed by unchanged storage and display schemas.

This is a projection only. It is never hashed or used to rebuild a launch recipe.
Storage manifests and records deliberately keep their original field names.
"""
    if spec.get("schema_version") == 1:
        return spec["identity"]
    r = spec["recipe"]
    return {"model_id": r["model"]["model_id"], "snapshot_revision": r["model"]["model_commit"],
            "snapshot_manifest": r["model"]["snapshot_manifest"], "image": {"digest": r["image_digest"]},
            "geometry": r["geometry"], "engine_args": r["engine_args"], "container_env": r["container_env"]}
