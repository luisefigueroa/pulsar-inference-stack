"""Portable serving specs, independent of checkouts and deployment locations.

Schema 1 is read only through ``load_spec(..., historical=True)``. Snapshot
manifests retain their original schema and digest so storage remains reusable.
"""
from __future__ import annotations

import copy
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .identity import _canonical_container_env, _canonical_engine_args, _canonical_geometry
from .immutable_io import ImmutableDescriptorDirectoryError, parse_strict_json, read_absolute_file
from .manifest import verify_snapshot_manifest
from .normalize import canonical_json_digest, normalize_container_env, normalize_engine_args
from .schema import ReleaseSpecError, IMAGE_DIGEST_RE, STATES, require_commit, require_model_id, require_public_string
from .verify import _verify_review, verify_spec as verify_historical_spec

SUPPORTED_SPEC_SCHEMAS = (2, 3)
# Latest supported schema, not the only accepted schema or the default output.
SPEC_SCHEMA_VERSION = max(SUPPORTED_SPEC_SCHEMAS)
SPEC_KIND = "pulsar-serving-spec"
# Latest draft format; the default schema-1 draft still freezes to spec schema 2.
DRAFT_SCHEMA_VERSION = 2
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


class InputFileError(SpecValidationError):
    """A named input file is missing, unreadable or unsafe to read."""


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
    fields = CONTAINER_FIELDS | ({"guard"} if isinstance(value, dict) and "guard" in value else set())
    c = copy.deepcopy(closed(value, fields, "recipe.container"))
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
    if "guard" in c:
        from .serving_guard import validate
        try:
            c["guard"] = validate(c["guard"], c)
        except ValueError as exc:
            invalid("recipe.container.guard", str(exc))
    return c


def canonical_recipe(value: Any) -> dict:
    fields = RECIPE_FIELDS | ({"required_snapshots"} if isinstance(value, dict) and "required_snapshots" in value else set())
    r = copy.deepcopy(closed(value, fields, "recipe"))
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
    if "required_snapshots" in r:
        extra = r["required_snapshots"]
        if not isinstance(extra, dict):
            invalid("recipe.required_snapshots", "expected a named snapshot map")
        for name, snapshot in extra.items():
            if not isinstance(name, str) or name == "target" or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                invalid("recipe.required_snapshots", "invalid or reserved snapshot name")
            closed(snapshot, {"model_id", "model_commit", "snapshot_manifest"}, "recipe.required_snapshots." + name)
            require_public_string(require_model_id(snapshot["model_id"], path="snapshot.model_id"), path="snapshot.model_id")
            require_commit(snapshot["model_commit"], path="snapshot.model_commit")
            checked = verify_snapshot_manifest(snapshot["snapshot_manifest"])
            if checked["model_id"] != snapshot["model_id"] or checked["snapshot_revision"] != snapshot["model_commit"]:
                invalid("recipe.required_snapshots." + name, "manifest differs from selected model commit")
            snapshot["snapshot_manifest"] = checked
        r["required_snapshots"] = dict(sorted(extra.items()))
        snapshot_engine_args(r)
    return r


def required_snapshots(spec: dict) -> dict:
    """Complete named file dependencies; the target remains the serving model."""
    if spec.get("schema_version") == 1:
        model = spec["identity"]
        return {"target": {"model_id": model["model_id"], "model_commit": model["snapshot_revision"],
                           "snapshot_manifest": model["snapshot_manifest"]}}
    return {"target": spec["recipe"]["model"], **spec["recipe"].get("required_snapshots", {})}


def snapshot_engine_args(recipe: dict, paths: dict | None = None) -> list[str]:
    """Resolve only the supported speculative model slot; never template arbitrary argv."""
    declared = {"target": recipe["model"], **recipe.get("required_snapshots", {})}
    args = list(recipe["engine_args"])
    seen = set()
    forms = set()
    consumed = set()

    def resolve(value):
        if not isinstance(value, str) or not value.startswith("pulsar-snapshot:"):
            invalid("recipe.engine_args", "speculative model must reference a declared pulsar-snapshot:NAME")
        name, separator, subdirectory = value.removeprefix("pulsar-snapshot:").partition("/")
        if name not in declared:
            invalid("recipe.engine_args", "unknown required snapshot: " + name)
        if separator:
            if (not subdirectory.isascii() or "\\" in subdirectory
                    or any(ord(char) < 32 or ord(char) == 127 for char in subdirectory)
                    or any(part in ("", ".", "..") for part in subdirectory.split("/"))):
                invalid("recipe.engine_args", "snapshot subdirectory must be a canonical relative POSIX directory")
            prefix = subdirectory + "/"
            if not any(item["path"].startswith(prefix) for item in declared[name]["snapshot_manifest"]["files"]):
                invalid("recipe.engine_args", "snapshot subdirectory has no manifest files: " + name + "/" + subdirectory)
        consumed.add(name)
        return paths[name] + ("/" + subdirectory if separator else "") if paths is not None else value

    for i, token in enumerate(args):
        flag = token.replace("--speculative_config", "--speculative-config", 1)
        if flag != "--speculative-config" and not flag.startswith("--speculative-config."):
            continue
        if flag in seen:
            invalid("recipe.engine_args", "duplicate speculative configuration field")
        seen.add(flag)
        forms.add("json" if flag == "--speculative-config" else "dotted")
        if len(forms) > 1 or i + 1 == len(args) or args[i + 1].startswith("--"):
            invalid("recipe.engine_args", "conflicting or incomplete speculative configuration")
        if flag == "--speculative-config":
            value = parse_strict_json(args[i + 1].encode(), label="speculative configuration")
            if not isinstance(value, dict):
                invalid("recipe.engine_args", "speculative configuration must be an object")
            if {"revision", "model_revision"}.intersection(value):
                invalid("recipe.engine_args", "speculative revision belongs in the snapshot declaration")
            if "model" in value:
                value["model"] = resolve(value["model"])
            if paths is not None and "model" in value:
                args[i + 1] = json.dumps(value, sort_keys=True, separators=(",", ":"))
        elif flag == "--speculative-config.model":
            args[i + 1] = resolve(args[i + 1])
        # The pinned local path is the checkpoint authority, not a second revision selector.
        if flag in ("--speculative-config.revision", "--speculative-config.model_revision"):
            invalid("recipe.engine_args", "speculative revision belongs in the snapshot declaration")
    for name in recipe.get("required_snapshots", {}):
        if name not in consumed:
            invalid("recipe.required_snapshots." + name, "snapshot has no supported engine reference")
    if paths is not None and any("pulsar-snapshot:" in token for token in args):
        invalid("recipe.engine_args", "snapshot reference outside a supported model field")
    if paths is None:
        snapshot_engine_args(recipe, {name: "/pulsar-check/" + name for name in declared})
    return args


def spec_id(recipe: dict) -> str:
    return canonical_json_digest({"schema_version": 3 if "required_snapshots" in recipe else 2,
                                  "recipe": canonical_recipe(recipe)})


def source_location(value: Any) -> dict:
    source = closed(value, {"image_repository"}, "source")
    repository = require_public_string(source["image_repository"], path="source.image_repository")
    # Distribution's repository-component grammar, limited to the contract's
    # existing public registry/name form (no port, tag, or digest here).
    parts=repository.split('/')
    domain='docker.io'
    if len(parts)>1 and ('.' in parts[0] or ':' in parts[0]
                        or parts[0]=='localhost' or parts[0].lower()!=parts[0]):
        domain=parts.pop(0)
        domain_component=r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
        if not re.fullmatch(domain_component+r'(?:\.'+domain_component+r')*',domain):
            invalid('source.image_repository','invalid public registry hostname')
        if domain=='index.docker.io': domain='docker.io'
    component=r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
    if any(not re.fullmatch(component,part) for part in parts):
        invalid('source.image_repository','expected Docker repository components without a tag, digest, or port')
    normalized='/'.join(parts)
    if domain=='docker.io' and len(parts)==1: normalized='library/'+normalized
    if len(domain+'/'+normalized)>255:
        invalid('source.image_repository','normalized Docker repository name exceeds 255 characters')
    return {"image_repository": repository}


def verify_spec(document: Any) -> dict:
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document.get("schema_version") not in SUPPORTED_SPEC_SCHEMAS:
        invalid("schema_version", "unsupported spec; supported specs use schema 2 or 3 (use historical show for old records)")
    closed(document, SPEC_FIELDS, "spec")
    if document["kind"] != SPEC_KIND:
        invalid("kind", f"expected {SPEC_KIND}")
    recipe = canonical_recipe(document["recipe"])
    if (document["schema_version"] == 3) != ("required_snapshots" in recipe):
        invalid("recipe.required_snapshots", "field is required only in spec schema 3")
    if recipe != document["recipe"]:
        invalid("recipe", "must use canonical ordering and token spelling; freeze the draft first")
    if document["spec_id"] != spec_id(recipe):
        invalid("spec_id", "does not match the canonical recipe")
    state = document["state"]
    if state is not None and (not isinstance(state, str) or state not in STATES):
        invalid("state", "expected null, candidate, measured, or released")
    return {**copy.deepcopy(document), "source": source_location(document["source"]),
            "review": _verify_review(document["review"], path="review")}


def load_json(path: str | Path) -> Any:
    # Only reading is a file problem; malformed content is a document error.
    try:
        raw = read_absolute_file(Path(path).absolute(), label="spec input")
    except ImmutableDescriptorDirectoryError as exc:
        raise InputFileError("input", str(exc)) from exc
    try:
        return parse_strict_json(raw, label="spec input")
    except ImmutableDescriptorDirectoryError as exc:
        raise SpecValidationError("input", str(exc)) from exc


def load_spec(path: str | Path, *, historical: bool = False) -> dict:
    document = load_json(path)
    if historical and isinstance(document, dict) and document.get("schema_version") == 1:
        return verify_historical_spec(document)
    return verify_spec(document)


def freeze(draft: Any, manifest: Any) -> dict:
    closed(draft, {"schema_version", "kind", "recipe", "source"}, "draft")
    version = draft["schema_version"]
    if type(version) is not int or version not in (1, 2) or draft["kind"] != DRAFT_KIND:
        invalid("draft", "unsupported draft format")
    recipe = copy.deepcopy(draft["recipe"])
    if not isinstance(recipe, dict):
        invalid("draft.recipe", "expected an object")
    closed(recipe, RECIPE_FIELDS | ({"required_snapshots"} if version == 2 else set()), "draft.recipe")
    extra = recipe.get("required_snapshots", {})
    if not isinstance(extra, dict) or "target" in extra:
        invalid("draft.recipe.required_snapshots", "expected named snapshots; target is reserved")
    models = {"target": recipe["model"], **extra}
    manifests = {"target": manifest} if version == 1 else manifest
    closed(manifests, set(models), "manifests")
    for name, model in models.items():
        closed(model, {"model_id", "model_commit"}, "draft snapshot " + name)
        model["snapshot_manifest"] = verify_snapshot_manifest(manifests[name])
    recipe = canonical_recipe(recipe)
    snapshot_engine_args(recipe)
    return verify_spec({"schema_version": version + 1, "kind": SPEC_KIND,
                        "spec_id": spec_id(recipe), "recipe": recipe,
                        "source": source_location(draft["source"]), "state": "candidate", "review": None})


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
        effective.update(state="candidate", review=None)
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


def example(nodes: int = 1, schema_version: int = 1) -> dict:
    integer(nodes, "nodes", 1)
    env = ["HF_HUB_OFFLINE=1"]
    if nodes == 1:
        env.append("VLLM_LOGGING_LEVEL=INFO")
    else:
        env += ["NCCL_DEBUG=WARN", "NCCL_IB_DISABLE=0", "NCCL_IB_QPS_PER_CONNECTION=4", "NCCL_NET=IB"]
    result = {"schema_version": schema_version, "kind": DRAFT_KIND,
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

    if type(schema_version) is not int or schema_version not in (1, 2):
        invalid("schema_version", "unsupported draft schema")
    if schema_version == 2:
        result["recipe"]["required_snapshots"] = {}
    return result


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
