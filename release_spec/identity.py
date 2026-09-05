"""Identity block, spec_id, and snapshot file-list comparison.

This module is standard-library-only and imports nothing from ``scripts/``.
``spec_id`` hashes the identity block with ``ensure_ascii=False``. Nested
snapshot ``manifest_id`` uses the model-library encoding without
``ensure_ascii=False``. ASCII snapshot paths make the two encodings agree.
"""

from __future__ import annotations

from typing import Any

from .normalize import (
    canonical_json_digest,
    normalize_container_env,
    normalize_engine_args,
    normalize_snapshot_files,
    snapshot_manifest_id,
)
from .schema import (
    FABRIC_LOCAL,
    FABRIC_ROCE_V2,
    FORBIDDEN_ENGINE_FLAGS,
    GEOMETRY_KEYS,
    IDENTITY_KEYS,
    IMAGE_DIGEST_RE,
    IMAGE_KEYS,
    PLATFORM_ID_RE,
    SNAPSHOT_MANIFEST_KIND,
    SNAPSHOT_MANIFEST_KEYS,
    SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    fail,
    require_commit,
    require_model_id,
    require_nonempty_string,
    require_nonnegative_int,
    require_object,
    require_positive_int,
    require_public_string,
    require_sha256_hex,
    screen_public_string,
)


def snapshot_file_lists_equal(left: Any, right: Any) -> bool:
    """Compare two file lists as canonical ``(path, size, sha256)`` tuples."""
    left_files = normalize_snapshot_files(left, path="left")
    right_files = normalize_snapshot_files(right, path="right")
    left_tuples = tuple(
        (item["path"], item["size"], item["sha256"]) for item in left_files
    )
    right_tuples = tuple(
        (item["path"], item["size"], item["sha256"]) for item in right_files
    )
    return left_tuples == right_tuples


def spec_id_for(identity: Any) -> str:
    """SHA-256 hex of the canonical identity object (``ensure_ascii=False``)."""
    if not isinstance(identity, dict):
        fail("identity must be an object")
    return canonical_json_digest(identity)


def _canonical_engine_args(value: Any, *, path: str) -> list[str]:
    canonical = normalize_engine_args(value, path=path)
    for index, token in enumerate(canonical):
        for flag in FORBIDDEN_ENGINE_FLAGS:
            if token == flag or token.startswith(flag + "="):
                fail(
                    f"{path}[{index}] must not include {flag} "
                    "(geometry or deployment overlay owns this flag)"
                )
        screen_public_string(token, path=f"{path}[{index}]")
    if not isinstance(value, list) or value != canonical:
        fail(f"{path} must be in canonical token-list form")
    return canonical


def _canonical_container_env(value: Any, *, path: str) -> list[str]:
    canonical = normalize_container_env(value, path=path)
    for index, item in enumerate(canonical):
        screen_public_string(item, path=f"{path}[{index}]")
        _name, _separator, env_value = item.partition("=")
        screen_public_string(env_value, path=f"{path}[{index}] value")
    if not isinstance(value, list) or value != canonical:
        fail(f"{path} must be a sorted unique KEY=VALUE list")
    return canonical


def _canonical_snapshot_files(value: Any, *, path: str) -> list[dict[str, Any]]:
    canonical = normalize_snapshot_files(value, path=path)
    if not isinstance(value, list) or value != canonical:
        fail(f"{path} must be sorted by path and unique")
    return canonical


def _canonical_snapshot_manifest(
    value: Any,
    *,
    model_id: str,
    snapshot_revision: str,
    path: str,
) -> dict[str, Any]:
    if isinstance(value, str) or (
        isinstance(value, dict)
        and "files" not in value
        and ("digest" in value or "manifest_id" in value)
    ):
        fail(
            f"{path} must be a full snapshot manifest, not a digest-only value"
        )
    require_object(value, SNAPSHOT_MANIFEST_KEYS, path=path)
    if type(value.get("schema_version")) is not int or value.get("schema_version") != SNAPSHOT_MANIFEST_SCHEMA_VERSION:
        fail(f"{path}.schema_version must be {SNAPSHOT_MANIFEST_SCHEMA_VERSION}")
    if value.get("kind") != SNAPSHOT_MANIFEST_KIND:
        fail(f"{path}.kind must be {SNAPSHOT_MANIFEST_KIND!r}")
    manifest_model_id = require_model_id(
        value.get("model_id"),
        path=f"{path}.model_id",
    )
    if manifest_model_id != model_id:
        fail(f"{path}.model_id must equal identity.model_id")
    manifest_revision = require_commit(
        value.get("snapshot_revision"),
        path=f"{path}.snapshot_revision",
    )
    if manifest_revision != snapshot_revision:
        fail(f"{path}.snapshot_revision must equal identity.snapshot_revision")
    files = _canonical_snapshot_files(value.get("files"), path=f"{path}.files")
    file_count = require_nonnegative_int(
        value.get("file_count"),
        path=f"{path}.file_count",
    )
    if file_count != len(files):
        fail(f"{path}.file_count must equal len(files)")
    total_bytes = require_nonnegative_int(
        value.get("total_bytes"),
        path=f"{path}.total_bytes",
    )
    expected_total = sum(item["size"] for item in files)
    if total_bytes != expected_total:
        fail(f"{path}.total_bytes must equal the sum of file sizes")
    manifest = {
        "schema_version": SNAPSHOT_MANIFEST_SCHEMA_VERSION,
        "kind": SNAPSHOT_MANIFEST_KIND,
        "model_id": manifest_model_id,
        "snapshot_revision": manifest_revision,
        "files": files,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "manifest_id": require_sha256_hex(
            value.get("manifest_id"),
            path=f"{path}.manifest_id",
        ),
    }
    computed = snapshot_manifest_id(manifest)
    if manifest["manifest_id"] != computed:
        fail(f"{path}.manifest_id does not match the nested manifest")
    return manifest


def _canonical_image(value: Any, *, path: str) -> dict[str, str]:
    require_object(value, IMAGE_KEYS, path=path)
    digest = value.get("digest")
    if not isinstance(digest, str) or IMAGE_DIGEST_RE.fullmatch(digest) is None:
        fail(f"{path}.digest must be sha256:<64 lowercase hex> with no registry name")
    return {"digest": digest}


def _canonical_geometry(value: Any, *, path: str) -> dict[str, Any]:
    # v1: tp * pp == nodes on single-accelerator platforms.
    # A multi-GPU platform is a schema bump.
    require_object(value, GEOMETRY_KEYS, path=path)
    platform_id = require_nonempty_string(
        value.get("platform_id"),
        path=f"{path}.platform_id",
    )
    if PLATFORM_ID_RE.fullmatch(platform_id) is None:
        fail(f"{path}.platform_id is invalid")
    nodes = require_positive_int(value.get("nodes"), path=f"{path}.nodes")
    tp = require_positive_int(value.get("tp"), path=f"{path}.tp")
    pp = require_positive_int(value.get("pp"), path=f"{path}.pp")
    if tp * pp != nodes:
        fail(f"{path}: tp * pp must equal nodes")
    fabric = require_nonempty_string(value.get("fabric"), path=f"{path}.fabric")
    if nodes == 1:
        if fabric != FABRIC_LOCAL:
            fail(f"{path}.fabric must be {FABRIC_LOCAL!r} when nodes is 1")
    elif fabric != FABRIC_ROCE_V2:
        fail(
            f"{path}.fabric must be {FABRIC_ROCE_V2!r} when nodes is greater than 1"
        )
    return {
        "platform_id": platform_id,
        "nodes": nodes,
        "tp": tp,
        "pp": pp,
        "fabric": fabric,
    }


def canonical_identity(value: Any, *, path: str = "identity") -> dict[str, Any]:
    """Validate and return the canonical identity object."""
    require_object(value, IDENTITY_KEYS, path=path)
    model_id = require_model_id(value.get("model_id"), path=f"{path}.model_id")
    snapshot_revision = require_commit(
        value.get("snapshot_revision"),
        path=f"{path}.snapshot_revision",
    )
    snapshot_manifest = _canonical_snapshot_manifest(
        value.get("snapshot_manifest"),
        model_id=model_id,
        snapshot_revision=snapshot_revision,
        path=f"{path}.snapshot_manifest",
    )
    engine_args = _canonical_engine_args(
        value.get("engine_args"),
        path=f"{path}.engine_args",
    )
    container_env = _canonical_container_env(
        value.get("container_env"),
        path=f"{path}.container_env",
    )
    image = _canonical_image(value.get("image"), path=f"{path}.image")
    geometry = _canonical_geometry(value.get("geometry"), path=f"{path}.geometry")
    return {
        "model_id": model_id,
        "snapshot_revision": snapshot_revision,
        "snapshot_manifest": snapshot_manifest,
        "engine_args": engine_args,
        "container_env": container_env,
        "image": image,
        "geometry": geometry,
    }


def argv_from_identity(identity: dict[str, Any]) -> list[str]:
    """Recipe argv the identity implies: engine_args, then the tp and pp tokens.

    ``launch_contract.argv`` must equal this projection; the stack appends its
    own launcher arguments at serve time and never writes them into a spec.
    """
    return [
        *list(identity["engine_args"]),
        "--tensor-parallel-size",
        str(identity["geometry"]["tp"]),
        "--pipeline-parallel-size",
        str(identity["geometry"]["pp"]),
    ]


def identity_block(spec: Any) -> dict[str, Any]:
    """Return the canonical identity object from a spec or identity dict."""
    if not isinstance(spec, dict):
        fail("document must be an object")
    if "identity" in spec:
        return canonical_identity(spec["identity"], path="identity")
    return canonical_identity(spec, path="identity")
