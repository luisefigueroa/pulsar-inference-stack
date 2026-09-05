"""Closed ADR 0017 release-spec constants and privacy screen.

This module is standard-library-only and imports nothing from ``scripts/``.
``spec_id`` hashes identity with ``ensure_ascii=False``. Nested snapshot
``manifest_id`` uses ``json.dumps(..., sort_keys=True, separators=(",", ":"))``
without ``ensure_ascii=False``. ASCII snapshot paths make the two encodings
agree.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any


SCHEMA_VERSION = 1
KIND = "pulsar-release-spec"
SNAPSHOT_MANIFEST_SCHEMA_VERSION = 1
SNAPSHOT_MANIFEST_KIND = "model-library-snapshot-manifest"

STATES = frozenset({"measured", "released"})
REVIEW_STATUSES = frozenset({"stable", "validated", "failed", "withdrawn"})
MEASUREMENT_SUITES = frozenset({"baseline-v1", "deep"})
MEASUREMENT_OUTCOMES = frozenset(
    {"pass", "fail", "inconclusive", "incomplete"}
)
THRESHOLD_OPERATORS = frozenset({"<", "<=", ">", ">=", "=="})
FABRIC_LOCAL = "local"
FABRIC_ROCE_V2 = "roce-v2"

# Copied from scripts/model_identity.py (do not import it).
HF_MODEL_ID_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
HF_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# Copied from scripts/platform_reference.py SAFE_PLATFORM_ID (do not import it).
PLATFORM_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FLAG_ASSIGNMENT_RE = re.compile(r"^(--?[A-Za-z][A-Za-z0-9-]*)=(.*)$")

# Closed subset of scripts/model_serving_release.validate_public_string_value
# regexes (do not import the ADR 0004 module).
HIGH_RISK_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:bearer|basic)\s+\S+|"
    r"(?:^|[^A-Za-z0-9])(?:password|passwd|passphrase|secret|token|"
    r"credential|authorization|api[_-]?key|access[_-]?key|"
    r"private[_-]?key)\s*[:=]|"
    r"[:=]\s*[\"']?(?:password|passwd|passphrase|secret|token|"
    r"credential|authorization|api[_-]?key|access[_-]?key|"
    r"private[_-]?key)(?:$|[^A-Za-z0-9])|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"hf_[A-Za-z0-9]{30,64}(?![A-Za-z0-9_-])|"
    r"(?:gh[opusr]_|github_pat_)[A-Za-z0-9_]{20,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"AIza[A-Za-z0-9_-]{20,}|"
    r"(?:^|[^A-Za-z0-9])eyJ[A-Za-z0-9_-]+\."
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:$|[^A-Za-z0-9])"
    r")"
)
ABSOLUTE_SITE_PATH_RE = re.compile(
    r"(?:^|[\s=,:;\"'(\[{])(?:~[/\\]|/(?!/)|[A-Za-z]:[/\\]|\\\\)"
)
DEPLOYMENT_REFERENCE_RE = re.compile(
    r"(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|"
    r"%[A-Za-z_][A-Za-z0-9_]*%|"
    r"(?:^|[\s=,:;\"'(\[{])\.\.[/\\])"
)
URI_ENDPOINT_RE = re.compile(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://")
IPV4_VALUE_RE = re.compile(
    r"(?:^|[^0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:$|[^0-9])"
)
IPV6_VALUE_RE = re.compile(
    r"(?i)(?:^|[\s=\[('\"])(?:[0-9a-f]{0,4}:){2,}"
    r"[0-9a-f]{0,4}(?:$|[\s\])'\",}])"
)
PRIVATE_ENDPOINT_VALUE_RE = re.compile(
    r"(?i)(?:^|[\s=,:;\"'(\[])"
    r"(?:localhost|(?:node|host|topology|site|lab)[-_.][A-Za-z0-9-]+|"
    r"[A-Za-z][A-Za-z0-9_-]*:[0-9]{1,5}|"
    r"[A-Za-z0-9-]+\.(?:local|lan|internal))"
    r"(?:$|[\s,;\"')\]}])"
)
PRIVATE_VALUE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:address|cache[_ -]?path|endpoint|"
    r"home[_ -]?node[_ -]?id|host(?:name|[_ -]?id)?|interface|"
    r"ip(?:[_ -]?address)?|mount[_ -]?path|node[_ -]?id|"
    r"serial(?:[_ -]?number)?|ssh[_ -]?(?:alias|host)|"
    r"topology[_ -]?id)\s*[:=]"
)

FORBIDDEN_ENGINE_FLAGS = (
    "--tensor-parallel-size",
    "-tp",
    "--pipeline-parallel-size",
    "-pp",
    "--port",
    "--host",
    "--served-model-name",
    "--model",
    "--api-key",
    "--download-dir",
)

TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "spec_id",
        "state",
        "identity",
        "launch_contract",
        "measurements",
        "baselines",
        "evidence",
        "review",
    }
)
IDENTITY_KEYS = frozenset(
    {
        "model_id",
        "snapshot_revision",
        "snapshot_manifest",
        "engine_args",
        "container_env",
        "image",
        "geometry",
    }
)
SNAPSHOT_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "model_id",
        "snapshot_revision",
        "files",
        "file_count",
        "total_bytes",
        "manifest_id",
    }
)
FILE_ENTRY_KEYS = frozenset({"path", "size", "sha256"})
IMAGE_KEYS = frozenset({"digest"})
GEOMETRY_KEYS = frozenset({"platform_id", "nodes", "tp", "pp", "fabric"})
LAUNCH_CONTRACT_KEYS = frozenset({"stack_version", "argv"})
MEASUREMENT_KEYS = frozenset(
    {
        "criterion_id",
        "suite",
        "policy_digest",
        "thresholds",
        "outcome",
        "evidence_ids",
    }
)
THRESHOLD_KEYS = frozenset({"metric", "operator", "value", "unit"})
BASELINE_KEYS = frozenset(
    {"claim", "source", "metric", "claimed", "measured", "unit"}
)
EVIDENCE_KEYS = frozenset({"id", "lab_commit", "path", "sha256"})
REVIEW_KEYS = frozenset({"status", "reviewer", "reviewed_at"})


class ReleaseSpecError(ValueError):
    """A release spec document is invalid."""


def fail(message: str) -> None:
    raise ReleaseSpecError(message)


def require_object(value: Any, keys: frozenset[str], *, path: str) -> dict[str, Any]:
    label = path or "document"
    if not isinstance(value, dict):
        fail(f"{label} must be an object")
    actual = set(value)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        fail(f"{label} fields differ (missing={missing}, extra={extra})")
    return value


def require_nonempty_string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        fail(f"{path} must be a non-empty string")
    return value


def screen_public_string(value: str, *, path: str) -> str:
    """Reject recognized private, secret, or deployment-only string data.

    ``VLLM_MARLIN_USE_ATOMIC_ADD=1`` is public and must pass this screen.
    """
    if "\x00" in value:
        fail(f"{path} contains a NUL byte")
    if (
        HIGH_RISK_SECRET_VALUE_RE.search(value)
        or ABSOLUTE_SITE_PATH_RE.search(value)
        or DEPLOYMENT_REFERENCE_RE.search(value)
        or URI_ENDPOINT_RE.search(value)
        or IPV4_VALUE_RE.search(value)
        or IPV6_VALUE_RE.search(value)
        or PRIVATE_ENDPOINT_VALUE_RE.search(value)
        or PRIVATE_VALUE_ASSIGNMENT_RE.search(value)
    ):
        fail(f"{path} contains private, secret, or deployment-only data")
    return value


def require_public_string(value: Any, *, path: str) -> str:
    return screen_public_string(
        require_nonempty_string(value, path=path),
        path=path,
    )


def require_sha256_hex(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or SHA256_HEX_RE.fullmatch(value) is None:
        fail(f"{path} must be a 64-character lowercase hex digest")
    return value


def require_commit(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        fail(f"{path} must be a 40-character lowercase hex commit")
    return value


def require_model_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or HF_MODEL_ID_RE.fullmatch(value) is None:
        fail(f"{path} must be a Hugging Face org/name model id")
    return value


def require_positive_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(f"{path} must be a positive integer")
    return value


def require_nonnegative_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(f"{path} must be a non-negative integer")
    return value


def require_relative_posix_ascii_path(value: Any, *, path: str) -> str:
    text = require_nonempty_string(value, path=path)
    if not text.isascii():
        fail(f"{path} must be a relative POSIX ASCII path")
    posix = pathlib.PurePosixPath(text)
    if text.startswith("/") or posix.is_absolute():
        fail(f"{path} must not be an absolute path")
    if ".." in posix.parts:
        fail(f"{path} must not contain '..'")
    return text
