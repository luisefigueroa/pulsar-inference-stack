"""Explicit estimates of resident weights, bound to an effective serving spec.

An estimate is admission guidance, not a memory measurement or qualification.
Model bytes and execution settings remain in the unchanged serving spec.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re

from . import serving
from .immutable_io import parse_strict_json, read_absolute_file
from .normalize import canonical_json_digest

SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 64 * 1024
GIB = 1024 ** 3


def validate(document, spec):
    spec = serving.verify_spec(spec)
    d = copy.deepcopy(serving.closed(document, {
        "schema_version", "kind", "spec_id", "basis", "ranks",
    }, "memory estimate"))
    if type(d["schema_version"]) is not int or d["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported memory-estimate schema")
    if d["kind"] != "pulsar-memory-estimate":
        raise ValueError("unsupported memory-estimate kind")
    if d["spec_id"] != spec["spec_id"]:
        raise ValueError("memory estimate selects a different effective spec")
    basis = d["basis"]
    if (not isinstance(basis, str) or not basis.strip() or len(basis) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in basis)):
        raise ValueError("memory estimate basis must be 1-1024 characters of single-line text")
    ranks = d["ranks"]
    count = spec["recipe"]["geometry"]["nodes"]
    if not isinstance(ranks, list) or len(ranks) != count:
        raise ValueError("memory estimate must cover every serving rank")
    for index, row in enumerate(ranks):
        serving.closed(row, {"rank", "resident_weights_bytes"}, "memory estimate rank")
        if type(row["rank"]) is not int or row["rank"] != index:
            raise ValueError("memory estimate ranks must be ordered, unique and complete")
        value = row["resident_weights_bytes"]
        if type(value) is not int or not 0 < value <= 2**63 - 1:
            raise ValueError("resident_weights_bytes must be a positive 64-bit integer")
    return d


def freeze(document, spec, *, expected_id=None):
    estimate = validate(document, spec)
    estimate_id = canonical_json_digest(estimate)
    if expected_id is not None:
        if not isinstance(expected_id, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_id):
            raise ValueError("memory estimate ID must be a content digest")
        if expected_id != estimate_id:
            raise ValueError("memory estimate changed after selection")
    return {"estimate_id": estimate_id, "estimate": estimate}


def validate_frozen(value, spec, *, expected_id=None):
    serving.closed(value, {"estimate_id", "estimate"}, "frozen memory estimate")
    frozen = freeze(value["estimate"], spec, expected_id=value["estimate_id"])
    if expected_id is not None and expected_id != frozen["estimate_id"]:
        raise ValueError("memory estimate changed after selection")
    return frozen


def parse(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("memory estimate input exceeds 64 KiB")
    return parse_strict_json(raw, label="memory estimate")


def load(path, spec, *, expected_id=None):
    raw = read_absolute_file(Path(path).absolute(), label="memory estimate")
    return freeze(parse(raw), spec, expected_id=expected_id)


def weights_gib(frozen):
    return [row["resident_weights_bytes"] / GIB for row in frozen["estimate"]["ranks"]]
