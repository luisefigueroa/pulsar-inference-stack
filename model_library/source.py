"""Verify a complete upstream Git/LFS inventory before deriving a snapshot manifest.

Source inventory is acquisition input, not another receipt or approval graph.
The controller owns all-rank discovery, absence barriers, publication and records.
These node-local primitives do not download, attach a home, or grant serving access.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any

from release_spec import build_snapshot_manifest
from release_spec.schema import require_commit, require_model_id, require_relative_posix_ascii_path
from .integrity import StorageError, directory, file_at, file_names, metadata, verify_manifest
from .state import ensure_directory

SOURCE_KEYS = {"schema_version", "kind", "model_id", "snapshot_revision", "files"}
SOURCE_KIND = "pulsar-download-source"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _path(value: Any) -> str:
    try:
        path = require_relative_posix_ascii_path(value, path="source file")
    except ValueError as exc:
        raise StorageError(str(exc)) from exc
    parts = PurePosixPath(path).parts
    # hf --local-dir owns this metadata namespace. Never discard a repository's
    # real files to make its inventory fit the downloader's bookkeeping.
    if path == ".cache" or parts[:2] == (".cache", "huggingface"):
        raise StorageError("upstream inventory collides with the downloader's .cache/huggingface metadata")
    return path


def _hex(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value) or set(value) == {"0"}:
        raise StorageError(f"{label} must be a nonzero lowercase object digest")
    return value


def validate_source(source: Any) -> dict:
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        raise StorageError("download source fields differ")
    if type(source["schema_version"]) is not int or source["schema_version"] != 1 or source["kind"] != SOURCE_KIND:
        raise StorageError("unsupported download source document")
    try:
        require_model_id(source["model_id"], path="source.model_id")
        require_commit(source["snapshot_revision"], path="source.snapshot_revision")
    except ValueError as exc:
        raise StorageError(str(exc)) from exc
    rows = source["files"]
    if not isinstance(rows, list) or not rows:
        raise StorageError("source inventory must contain all repository files")
    paths = []
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (
            {"path", "size", "git_oid"}, {"path", "size", "sha256"}):
            raise StorageError("source row must describe one Git blob or LFS object")
        paths.append(_path(row["path"]))
        if type(row["size"]) is not int or row["size"] < 0:
            raise StorageError("upstream file size must be a nonnegative integer")
        if "git_oid" in row:
            _hex(row["git_oid"], HEX40, "Git object ID")
        else:
            _hex(row["sha256"], HEX64, "LFS SHA-256")
    if paths != sorted(set(paths)):
        raise StorageError("source file paths must be sorted and unique")
    names = set(paths)
    if any(parent.as_posix() in names for path in paths for parent in PurePosixPath(path).parents if parent.as_posix() != "."):
        raise StorageError("upstream files conflict with directory paths")
    return source


def normalize_inventory(raw: Any, expected_model: str, expected_revision: str | None = None) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"id", "sha", "siblings"}:
        raise StorageError("upstream inventory is incomplete or has unexpected fields")
    if raw["id"] != expected_model:
        raise StorageError("upstream model differs from the selected model")
    if expected_revision is not None and raw["sha"] != expected_revision:
        raise StorageError("upstream resolved commit differs from the requested exact commit")
    if not isinstance(raw["siblings"], list) or not raw["siblings"]:
        raise StorageError("upstream repository has no files")
    rows = []
    for item in raw["siblings"]:
        if not isinstance(item, dict) or set(item) not in (
            {"type", "path", "size", "blob_id"}, {"type", "path", "size", "blob_id", "lfs"}):
            raise StorageError("upstream inventory row has unexpected fields")
        if item["type"] != "file":
            raise StorageError("upstream inventory contains a non-file entry")
        _hex(item["blob_id"], HEX40, "Git object ID")
        row = {"path": _path(item["path"]), "size": item["size"]}
        if "lfs" in item:
            lfs = item["lfs"]
            if not isinstance(lfs, dict) or set(lfs) != {"size", "sha256"} or type(lfs["size"]) is not int or lfs["size"] != item["size"]:
                raise StorageError("upstream LFS content size differs from file size")
            row["sha256"] = _hex(lfs["sha256"], HEX64, "LFS SHA-256")
        else:
            row["git_oid"] = item["blob_id"]
        rows.append(row)
    return validate_source({"schema_version": 1, "kind": SOURCE_KIND,
        "model_id": expected_model, "snapshot_revision": raw["sha"],
        "files": sorted(rows, key=lambda row: row["path"])})


def compare_inventory_to_manifest(source: dict, expected_manifest: dict) -> None:
    source = validate_source(source)
    manifest = verify_manifest(expected_manifest)
    if source["model_id"] != manifest["model_id"] or source["snapshot_revision"] != manifest["snapshot_revision"]:
        raise StorageError("upstream model or commit differs from the selected spec")
    if [(x["path"], x["size"]) for x in source["files"]] != [(x["path"], x["size"]) for x in manifest["files"]]:
        raise StorageError("complete upstream inventory differs from the selected spec")
    for upstream, expected in zip(source["files"], manifest["files"]):
        if "sha256" in upstream and upstream["sha256"] != expected["sha256"]:
            raise StorageError("upstream LFS digest differs from the selected spec")


def verify_download(path: str | Path, source: dict, expected_manifest: dict | None = None) -> dict:
    source = validate_source(source)
    if expected_manifest is not None:
        compare_inventory_to_manifest(source, expected_manifest)
    path = Path(path)
    expected_paths = [row["path"] for row in source["files"]]
    observed, fingerprints = [], {}
    with directory(path) as root:
        root_before = metadata(os.fstat(root))
        if file_names(root) != expected_paths:
            raise StorageError("downloaded file set differs from complete upstream inventory")
        for item in source["files"]:
            with file_at(root, item["path"]) as fd:
                before = metadata(os.fstat(fd))
                if before[3] != item["size"]:
                    raise StorageError(f"downloaded file size differs: {item['path']}")
                sha256 = hashlib.sha256()
                git = hashlib.sha1(f"blob {item['size']}\0".encode("ascii"))
                while chunk := os.read(fd, 4 * 1024 * 1024):
                    sha256.update(chunk)
                    git.update(chunk)
                if metadata(os.fstat(fd)) != before:
                    raise StorageError("downloaded file changed while verifying")
                digest = sha256.hexdigest()
                if ("git_oid" in item and git.hexdigest() != item["git_oid"]):
                    raise StorageError(f"downloaded Git blob differs from upstream object: {item['path']}")
                if "sha256" in item and digest != item["sha256"]:
                    raise StorageError(f"downloaded LFS content differs from upstream object: {item['path']}")
                fingerprints[item["path"]] = before
                observed.append({"path": item["path"], "size": item["size"], "sha256": digest})
        for relative, fingerprint in fingerprints.items():
            with file_at(root, relative) as fd:
                if metadata(os.fstat(fd)) != fingerprint:
                    raise StorageError("downloaded file was replaced during verification")
        if file_names(root) != expected_paths or metadata(os.fstat(root)) != root_before:
            raise StorageError("download directory changed while verifying")
        with directory(path) as current:
            if metadata(os.fstat(current)) != root_before:
                raise StorageError("download directory was replaced during verification")
    manifest = build_snapshot_manifest(model_id=source["model_id"],
        snapshot_revision=source["snapshot_revision"], files=observed)
    if expected_manifest is not None and manifest != verify_manifest(expected_manifest):
        raise StorageError("downloaded SHA-256 manifest differs from the selected spec")
    return manifest


def _stage(stage):
    stage = Path(stage)
    if not stage.is_absolute() or not re.fullmatch(r"\.pending-[0-9a-f]{32}", stage.name):
        raise StorageError("download requires an owned acquisition staging directory")
    with directory(stage) as fd:
        observed = os.fstat(fd)
        if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
            raise StorageError("download staging must be private and owned by the selected user")
    return stage


def prepare_download(stage: str | Path, source: dict) -> dict:
    source = validate_source(source)
    stage = _stage(stage)
    snapshot = stage / "snapshots" / source["snapshot_revision"]
    ensure_directory(snapshot)
    with directory(snapshot) as fd:
        if os.listdir(fd):
            raise StorageError("acquisition payload is not empty; use an explicit recovery operation")
    cache = stage / ".download-cache"
    for name in ("hub", "xet", "assets", "tmp"):
        ensure_directory(cache / name)
    return {"snapshot_path": str(snapshot), "cache_root": str(cache)}


def _remove_cache(path: Path) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise StorageError("platform cannot safely prune acquisition metadata")
    with directory(path.parent) as parent:
        try:
            observed = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(observed.st_mode):
            raise StorageError("download cache was replaced by a non-directory")
        shutil.rmtree(path.name, dir_fd=parent)
        os.fsync(parent)


def clean_download_metadata(stage: str | Path, source: dict) -> None:
    source = validate_source(source)
    stage = _stage(stage)
    snapshot = stage / "snapshots" / source["snapshot_revision"]
    cache_parent = snapshot / ".cache"
    try:
        with directory(cache_parent):
            pass
    except StorageError:
        if cache_parent.exists() or cache_parent.is_symlink():
            raise
    else:
        _remove_cache(cache_parent / "huggingface")
        with directory(snapshot) as fd:
            with directory(cache_parent) as cache:
                empty = not os.listdir(cache)
            if empty:
                os.rmdir(".cache", dir_fd=fd)
                os.fsync(fd)
    _remove_cache(stage / ".download-cache")
