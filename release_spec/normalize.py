"""Canonical form for engine args, container env, and snapshot files.

This module is standard-library-only and imports nothing from ``scripts/``.
``canonical_json_digest`` (used for ``spec_id``) encodes with
``ensure_ascii=False``. ``snapshot_manifest_id`` copies the model-library
algorithm and omits ``ensure_ascii=False``. ASCII snapshot paths make the
two encodings agree.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from typing import Any

from .schema import (
    ENV_NAME_RE,
    FILE_ENTRY_KEYS,
    FLAG_ASSIGNMENT_RE,
    SNAPSHOT_MANIFEST_KIND,
    SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    fail,
    require_commit,
    require_model_id,
    require_nonempty_string,
    require_nonnegative_int,
    require_object,
    require_relative_posix_ascii_path,
    require_sha256_hex,
)


def pretty_json_bytes(value: Any) -> bytes:
    """Return deterministic pretty JSON bytes for a verified spec.

    Identity digests stay on compact ``canonical_json_digest``.
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
    """SHA-256 of compact JSON with ``ensure_ascii=False`` (spec_id algorithm)."""
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def snapshot_manifest_id(manifest: dict[str, Any]) -> str:
    """Hash a snapshot manifest the way the model library does.

    ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` with the
    default ``ensure_ascii=True``. Do not pass ``ensure_ascii=False``.
    """
    payload = {key: value for key, value in manifest.items() if key != "manifest_id"}
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _split_flag_assignments(pieces: list[str]) -> list[str]:
    tokens: list[str] = []
    for piece in pieces:
        match = FLAG_ASSIGNMENT_RE.match(piece)
        if match is None:
            tokens.append(piece)
            continue
        tokens.append(match.group(1))
        tokens.append(match.group(2))
    return tokens


def _reject_empty_tokens(tokens: list[str], *, path: str) -> list[str]:
    for index, token in enumerate(tokens):
        if not isinstance(token, str):
            fail(f"{path}[{index}] must be a string")
        if token == "":
            fail(f"{path}[{index}] must not be empty")
        if "\x00" in token:
            fail(f"{path}[{index}] contains a NUL byte")
    return tokens


def normalize_engine_args(
    value: Any,
    *,
    path: str = "identity.engine_args",
) -> list[str]:
    """Return the canonical token list; order is identity and is preserved.

    A single string is shell-split once with ``shlex.split``, so quoting
    works the way it does in a profile conf. A list is taken as literal
    tokens: elements are never re-split, because a value such as a JSON
    ``--speculative-config`` legitimately contains spaces and quotes. In both
    forms ``--flag=value`` is rewritten into two tokens.
    """
    if isinstance(value, str):
        if "\x00" in value:
            fail(f"{path} contains a NUL byte")
        try:
            pieces = shlex.split(value)
        except ValueError as exc:
            fail(f"{path} is not a valid shell token string: {exc}")
    elif isinstance(value, list):
        pieces = list(value)
    else:
        fail(f"{path} must be a list of strings or a single string")
    _reject_empty_tokens(pieces, path=path)
    return _reject_empty_tokens(_split_flag_assignments(pieces), path=path)


def normalize_container_env(
    value: Any,
    *,
    path: str = "identity.container_env",
) -> list[str]:
    """Return sorted unique ``KEY=VALUE`` assignments; duplicate KEY fails."""
    if not isinstance(value, list):
        fail(f"{path} must be a list of KEY=VALUE strings")
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        text = require_nonempty_string(item, path=item_path)
        name, separator, _env_value = text.partition("=")
        if not separator or ENV_NAME_RE.fullmatch(name) is None:
            fail(f"{item_path} must be KEY=VALUE with a valid environment KEY")
        if name in seen:
            fail(f"{item_path} assigns KEY {name!r} more than once")
        seen.add(name)
        items.append(text)
    return sorted(items)


def normalize_snapshot_files(
    value: Any,
    *,
    path: str = "identity.snapshot_manifest.files",
) -> list[dict[str, Any]]:
    """Validate file entries and return them sorted by path.

    Duplicate paths fail. The verifier separately rejects unsorted input;
    this builder is allowed to sort.
    """
    if not isinstance(value, list) or not value:
        fail(f"{path} must be a non-empty list")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_object(item, FILE_ENTRY_KEYS, path=item_path)
        relative = require_relative_posix_ascii_path(
            item["path"],
            path=f"{item_path}.path",
        )
        if relative in seen:
            fail(f"{item_path}.path duplicates {relative!r}")
        seen.add(relative)
        size = require_nonnegative_int(item["size"], path=f"{item_path}.size")
        checksum = require_sha256_hex(item["sha256"], path=f"{item_path}.sha256")
        entries.append({"path": relative, "size": size, "sha256": checksum})
    return sorted(entries, key=lambda entry: entry["path"])


def build_snapshot_manifest(
    *,
    model_id: str,
    snapshot_revision: str,
    files: Any,
) -> dict[str, Any]:
    """Build a closed snapshot manifest, sorting files and filling ``manifest_id``."""
    model_id = require_model_id(model_id, path="identity.snapshot_manifest.model_id")
    snapshot_revision = require_commit(
        snapshot_revision,
        path="identity.snapshot_manifest.snapshot_revision",
    )
    canonical_files = normalize_snapshot_files(
        files,
        path="identity.snapshot_manifest.files",
    )
    manifest: dict[str, Any] = {
        "schema_version": SNAPSHOT_MANIFEST_SCHEMA_VERSION,
        "kind": SNAPSHOT_MANIFEST_KIND,
        "model_id": model_id,
        "snapshot_revision": snapshot_revision,
        "files": canonical_files,
        "file_count": len(canonical_files),
        "total_bytes": sum(item["size"] for item in canonical_files),
        "manifest_id": "",
    }
    manifest["manifest_id"] = snapshot_manifest_id(manifest)
    return manifest
