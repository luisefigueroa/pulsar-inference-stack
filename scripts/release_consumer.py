#!/usr/bin/env python3
"""Catalog reading, deployment overlays, and advisory shell values.

Serving compiles the actual recipe directly from the canonical spec. No profile
reconstruction or private Workbench projector is supported by this module.
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
DEFAULT_IMAGE_REPO = "vllm/vllm-openai"
SPEC_ID_RE = re.compile(r"[0-9a-f]{64}")


from release_spec.serving import identity_fields


class SpecCatalogError(ValueError):
    """A releases index, overlay, or profile projection is invalid."""

def fail(message: str) -> None:
    raise SpecCatalogError(message)

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
    except SpecCatalogError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"{path}: {exc}")

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

def spec_shell_values(
    spec: dict[str, Any],
    overlay_entry: dict[str, Any],
    image_repo: str,
    *,
    active_platform_id: str | None = None,
) -> dict[str, Any]:
    """Extract site and advisory values; the launch compiler reads the spec directly.

    ``SPEC_PLATFORM_ID`` is always exported so the start path can refuse a
    spec frozen for another platform. Pass ``active_platform_id`` only from
    a launch-admission caller: stop and status must still load a spec after
    the platform setting changed, so the shared loader never gates on it.
    """
    spec_id = spec_lifecycle_key(str(spec.get("spec_id") or ""))
    identity = identity_fields(spec)
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

def _review_fields(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    review = spec.get("review") or {}
    status = review.get("status")
    reviewed_at = review.get("reviewed_at")
    return (
        str(status) if status else None,
        str(reviewed_at) if reviewed_at else None,
    )

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
                "model_id": identity_fields(spec)["model_id"],
                "nodes": int(identity_fields(spec)["geometry"]["nodes"]),
                "image_digest": identity_fields(spec)["image"]["digest"],
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
        served = identity_fields(spec)["model_id"]
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

def cmd_shell_values(
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
    selected_spec_id = spec['spec_id']
    selected_document = spec
    if spec['schema_version'] == 2:
        from release_spec.serving import apply_overrides, load_json as load_contract_json
        override = os.environ.get('PULSAR_OVERRIDE_FILE')
        if override:
            spec = apply_overrides(spec, load_contract_json(override))
        previous = os.environ.get('PULSAR_EFFECTIVE_SPEC_ID')
        if previous and previous != spec['spec_id']:
            fail('effective recipe changed after selection; review the override again')
    overlay_file = (
        pathlib.Path(overlay_path)
        if overlay_path not in (None, "")
        else pathlib.Path(repo_root) / OVERLAY_FILENAME
    )
    overlay = load_overlay(overlay_file)
    entry = overlay_for_spec(overlay, selected_document)
    repo = image_repo_from_reference(
        image_repo or os.environ.get("VLLM_IMAGE_MAINLINE")
    )
    variables = spec_shell_values(
        spec, entry, repo, active_platform_id=active_platform_id or None
    )
    variables['CONF_NAME'] = selected_spec_id
    variables['SPEC_REVIEW_STATUS'] = (selected_document.get('review') or {}).get('status') or 'not specified'
    variables['PULSAR_EFFECTIVE_SPEC_ID'] = spec['spec_id'] if spec['schema_version'] == 2 else ''
    if spec['schema_version'] == 2:
        variables['IMAGE'] = spec['source']['image_repository'] + '@' + spec['recipe']['image_digest']
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


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['list','verify','show','shell-values'])
    parser.add_argument('spec_id',nargs='?')
    parser.add_argument('--repo-root',default=str(_REPO_ROOT))
    parser.add_argument('--releases-root')
    parser.add_argument('--spec-file')
    parser.add_argument('--overlay')
    parser.add_argument('--image-repo')
    parser.add_argument('--platform-id')
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--markdown',action='store_true')
    args=parser.parse_args(argv)
    try:
        root=pathlib.Path(args.repo_root)
        if args.command=='list':
            return cmd_list(root,as_json=args.json,as_markdown=args.markdown,releases_root=args.releases_root)
        if not args.spec_id: fail('select an exact spec id')
        if args.command=='shell-values':
            return cmd_shell_values(root,args.spec_id,overlay_path=args.overlay,releases_root=args.releases_root,
                image_repo=args.image_repo,active_platform_id=args.platform_id,spec_file=args.spec_file)
        if args.command=='verify': return cmd_verify(root,args.spec_id,as_json=args.json,releases_root=args.releases_root)
        return cmd_show(root,args.spec_id,as_json=args.json,releases_root=args.releases_root,spec_file=args.spec_file)
    except (ValueError,OSError) as exc:
        print(f'error: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__': raise SystemExit(main())
