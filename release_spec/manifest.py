"""Standalone snapshot manifests used before the first candidate spec exists."""
from pathlib import Path
from typing import Any
from .identity import _canonical_snapshot_manifest
from .schema import fail
from .immutable_io import parse_strict_json, read_absolute_file


def verify_snapshot_manifest(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        fail("snapshot manifest must be an object")
    return _canonical_snapshot_manifest(document, model_id=document.get("model_id"),
                                        snapshot_revision=document.get("snapshot_revision"),
                                        path="snapshot_manifest")


def load_snapshot_manifest(path: str | Path) -> dict[str, Any]:
    raw = read_absolute_file(Path(path).absolute(), label="snapshot manifest")
    return verify_snapshot_manifest(parse_strict_json(raw, label="snapshot manifest"))
