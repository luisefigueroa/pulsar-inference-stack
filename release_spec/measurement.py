#!/usr/bin/env python3
"""Closed validator-measurement documents for qualification and diagnostics.

This module owns the versioned measurement contract emitted by
``validate/compare_captures.py``, ``validate/bench_serve.py``,
``validate/gsm8k_eval.py``, ``validate/soak.py``, the experiment-only
resource monitor, ``validate/verify_snapshot_manifest.py``, and
``validate/serve_smoke.py`` (the baseline-v1 identity and serving-smoke
operations). Documents record measured facts and completion/reason only. They
do not evaluate ADR 0004 status, issue a decision, or grant serving
permission.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from . import immutable_io as immutable_descriptor_dir
from . import schema
from .normalize import pretty_json_bytes
import re


PRIVATE_FIELD_NAMES = {
    "address",
    "addresses",
    "cache_path",
    "home_node_id",
    "host",
    "host_id",
    "hostname",
    "hostnames",
    "interface",
    "interfaces",
    "ip",
    "ip_address",
    "ip_addresses",
    "mount_path",
    "node_id",
    "node_ids",
    "path",
    "paths",
    "serial",
    "serial_number",
    "ssh_alias",
    "ssh_host",
    "topology_id",
    "topology_ids",
}
CREDENTIAL_FIELD_NAME_SUFFIXES = {
    "ACCESSKEY",
    "ACCESSTOKEN",
    "APIKEY",
    "AUTHORIZATION",
    "AUTHTOKEN",
    "BEARER",
    "BEARERTOKEN",
    "CLIENTSECRET",
    "CREDENTIAL",
    "CREDENTIALS",
    "GITHUBTOKEN",
    "HFTOKEN",
    "PASSWORD",
    "PASSPHRASE",
    "PASSWD",
    "PRIVATEKEY",
    "SECRET",
}

def _is_credential_field_name(value: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return any(
        normalized == marker or normalized.endswith(marker)
        for marker in CREDENTIAL_FIELD_NAME_SUFFIXES
    )


MEASUREMENT_SCHEMA_VERSION = 1
MEASUREMENT_KIND = "pulsar-validator-measurement"

PROGRAM_OPERATIONS = {
    "validate/compare_captures.py": "compare-captures",
    "validate/bench_serve.py": "benchmark-serving",
    "validate/gsm8k_eval.py": "evaluate-gsm8k",
    "validate/soak.py": "validate-soak",
    "scripts/model-serving-experiment-monitor.sh": "observe-resources",
    "validate/verify_snapshot_manifest.py": "verify-snapshot-manifest",
    "validate/serve_smoke.py": "serve-smoke",
}
OPERATION_PROGRAMS = {value: key for key, value in PROGRAM_OPERATIONS.items()}

COMPLETIONS = {"complete", "incomplete"}
COMPARE_REASONS = {
    "completed",
    "unusable-input",
    "prompt-mismatch",
    "sample-count-mismatch",
    "corrupt-document",
    "interrupted",
    "missing-measurement",
}
BENCHMARK_REASONS = {
    "completed",
    "warmup-failed",
    "measured-incomplete",
    "interrupted",
    "corrupt-document",
    "missing-measurement",
}
RESOURCE_REASONS = {
    "completed",
    "no-samples",
    "missing-ranks",
    "workload-unobserved",
    "interrupted",
    "corrupt-document",
    "missing-measurement",
}
ACCURACY_REASONS = {
    "completed",
    "dataset-invalid",
    "request-failed",
    "measured-incomplete",
    "interrupted",
    "corrupt-document",
    "missing-measurement",
}
SOAK_REASONS = {
    "completed",
    "zero-completions",
    "request-errors",
    "interrupted",
    "corrupt-document",
    "missing-measurement",
}
IDENTITY_REASONS = {
    "completed",
    "mismatch",
    "interrupted",
    "corrupt-document",
    "missing-measurement",
}
SERVE_SMOKE_REASONS = {
    "completed",
    "health-failed",
    "warmup-failed",
    "completion-failed",
    "interrupted",
    "corrupt-document",
    "missing-measurement",
}
SERVE_SMOKE_PHASE_REASONS = {
    "completed",
    "failed",
    "interrupted",
    "measured-incomplete",
}
LEVEL_REASONS = {
    "completed",
    "warmup-failed",
    "measured-incomplete",
    "interrupted",
}
DIAGNOSTIC_VERDICTS = {
    "identical",
    "fp-equivalent",
    "divergent",
    "unusable",
}
PROMPT_STYLES = {"synthetic", "natural"}

COMMON_FIELDS = {
    "schema_version",
    "kind",
    "program",
    "operation",
    "completion",
    "reason",
}
COMPARE_FIELDS = COMMON_FIELDS | {"compare-captures"}
BENCHMARK_FIELDS = COMMON_FIELDS | {"benchmark-serving"}
RESOURCE_FIELDS = COMMON_FIELDS | {"observe-resources"}
ACCURACY_FIELDS = COMMON_FIELDS | {"evaluate-gsm8k"}
SOAK_FIELDS = COMMON_FIELDS | {"validate-soak"}
IDENTITY_FIELDS = COMMON_FIELDS | {"verify-snapshot-manifest"}
SERVE_SMOKE_FIELDS = COMMON_FIELDS | {"serve-smoke"}
COMPARE_PAYLOAD_FIELDS = {
    "sample_count",
    "identical_record_count",
    "exact_text_count",
    "mean_prefix_match",
    "min_prefix_match",
    "max_matched_prefix_logprob_delta",
    "hard_disagreement_count",
    "diagnostic_verdict",
    "source_digests",
}
SOURCE_DIGEST_FIELDS = {"a", "b"}
BENCHMARK_PAYLOAD_FIELDS = {
    "input_tokens",
    "output_tokens",
    "prompt_style",
    "explicit_request_count",
    "levels",
}
LEVEL_FIELDS = {
    "concurrency",
    "requested_request_count",
    "measured_request_count",
    "completion",
    "reason",
    "ttft_p50_ms",
    "ttft_p95_ms",
    "decode_tps_p50",
    "aggregate_tps",
    "wall_s",
}
RESOURCE_PAYLOAD_FIELDS = {
    "started_at",
    "ended_at",
    "duration_seconds",
    "qualification_scope",
    "sample_interval_seconds",
    "expected_rank_count",
    "observed_rank_count",
    "sample_count",
    "ranks",
}
RESOURCE_RANK_FIELDS = {
    "rank",
    "collection_status",
    "sample_count",
    "workload_sample_count",
    "mem_available_min_bytes",
    "swap_used_max_bytes",
    "node_memory_pressure_some_total_delta_us",
    "workload_memory_current_max_bytes",
    "workload_memory_peak_start_bytes",
    "workload_memory_peak_end_bytes",
    "workload_swap_current_max_bytes",
    "oom_delta",
    "oom_kill_delta",
}
RESOURCE_COLLECTION_STATUSES = {"complete", "pool-only", "unavailable"}
RESOURCE_QUALIFICATION_SCOPES = {
    "model-qualification",
    "serving-integration",
    "release-promotion",
}
ACCURACY_PAYLOAD_FIELDS = {
    "dataset_id",
    "dataset_revision",
    "dataset_file_sha256",
    "subset",
    "split",
    "selection",
    "answer_normalization",
    "max_completion_tokens",
    "reasoning_mode",
    "temperature",
    "requested_sample_count",
    "measured_sample_count",
    "correct_count",
    "request_error_count",
    "accuracy",
}
SOAK_PAYLOAD_FIELDS = {
    "started_at",
    "ended_at",
    "duration_seconds",
    "concurrency",
    "completed_requests",
    "request_error_count",
}
IDENTITY_PAYLOAD_FIELDS = {
    "spec_id",
    "manifest_id",
    "expected_file_count",
    "matched_file_count",
    "mismatched_file_count",
    "missing_file_count",
    "extra_file_count",
}
SERVE_SMOKE_PAYLOAD_FIELDS = {
    "health",
    "warmup",
    "completion",
}
SERVE_SMOKE_PHASE_FIELDS = {
    "completion",
    "reason",
}
FORBIDDEN_FIELDS = {
    "adapter",
    "authority",
    "base_status",
    "bundle_id",
    "candidate_id",
    "contract_id",
    "decision",
    "decision_id",
    "decisions",
    "disposition",
    "effective_status",
    "exit_code",
    "exit_status",
    "privacy_review",
    "program_version",
    "promotion_authorized",
    "release_id",
    "return_code",
    "returncode",
    "review",
    "reviewer",
    "serving_authorization",
    "serving_permission",
    "status",
    "validation_status",
    "validator_output",
}

HEX64_RE = schema.SHA256_HEX_RE


class ValidatorMeasurementError(ValueError):
    """A validator measurement document is missing, unsafe, or invalid."""


class ValidatorMeasurementMissing(ValidatorMeasurementError):
    """An expected measurement file is absent."""


def fail(message: str) -> None:
    raise ValidatorMeasurementError(message)


def _require_fields(value: Any, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{label} must be an object")
    extra = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if extra or missing:
        fail(f"{label} fields differ (missing={missing}, extra={extra})")
    return value


def parse_strict_json(data: bytes | str, *, label: str) -> Any:
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        return immutable_descriptor_dir.parse_strict_json(data, label=label)
    except immutable_descriptor_dir.ImmutableDescriptorDirectoryError as exc:
        fail(str(exc))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_stable_bytes(path: str | Path, *, label: str) -> bytes:
    target = Path(path)
    if not target.is_absolute():
        target = Path.cwd() / target
    try:
        return immutable_descriptor_dir.read_absolute_file(target, label=label)
    except immutable_descriptor_dir.ImmutableDescriptorDirectoryError as exc:
        message = str(exc)
        if "missing" in message:
            raise ValidatorMeasurementMissing(message) from exc
        fail(message)


def file_digest(path: str | Path) -> str | None:
    try:
        return sha256_bytes(read_stable_bytes(path, label="digest source"))
    except (OSError, ValidatorMeasurementError):
        return None


def canonical_decimal(
    value: Any, *, label: str, require_canonical: bool = True
) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        fail(f"{label} must be a canonical decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        fail(f"{label} must be numeric")
    if not parsed.is_finite():
        fail(f"{label} must be finite")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"", "-0"}:
        normalized = "0"
    if require_canonical and isinstance(value, str) and value != normalized:
        fail(f"{label} is not canonical (expected {normalized!r})")
    return normalized


def decimal_from_number(value: int | float | Decimal, *, places: int = 6) -> str:
    if isinstance(value, bool):
        fail("decimal value must be numeric")
    if isinstance(value, Decimal):
        parsed = value
    else:
        parsed = Decimal(str(value))
    if not parsed.is_finite():
        fail("decimal value must be finite")
    if places >= 0 and parsed != parsed.to_integral_value():
        quantize = Decimal("1").scaleb(-places)
        parsed = parsed.quantize(quantize)
    return canonical_decimal(
        format(parsed, "f"), label="decimal", require_canonical=False
    )


def _scan_forbidden_keys(value: Any, *, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_FIELDS:
                fail(f"{label} contains forbidden field {key}")
            _scan_forbidden_keys(item, label=f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_forbidden_keys(item, label=f"{label}[{index}]")


def _screen_measurement_json(value: Any, *, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or "\x00" in key:
                fail(f"{label} has an invalid object key")
            if key.lower() in PRIVATE_FIELD_NAMES:
                fail(f"{label} contains private field {key!r}")
            if _is_credential_field_name(key):
                fail(f"{label} contains credential-bearing field {key!r}")
            _screen_measurement_json(item, label=f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _screen_measurement_json(item, label=f"{label}[{index}]")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        fail(f"{label} must encode decimals as canonical strings, not floats")
    if isinstance(value, str):
        schema.screen_public_string(value, path=label)
        return
    fail(f"{label} contains unsupported JSON value {type(value).__name__}")


def _optional_digest(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or HEX64_RE.fullmatch(value) is None:
        fail(f"{label} must be a SHA-256 digest")
    return value


def _optional_decimal(
    value: Any,
    *,
    label: str,
    nonnegative: bool = False,
    positive: bool = False,
    maximum: Decimal | None = None,
) -> str | None:
    if value is None:
        return None
    text = canonical_decimal(value, label=label)
    parsed = Decimal(text)
    if nonnegative and parsed < 0:
        fail(f"{label} must be non-negative")
    if positive and parsed <= 0:
        fail(f"{label} must be positive")
    if maximum is not None and parsed > maximum:
        fail(f"{label} must be at most {maximum}")
    return text


def _optional_ratio(value: Any, *, label: str) -> str | None:
    return _optional_decimal(
        value, label=label, nonnegative=True, maximum=Decimal(1)
    )


def _nonnegative_int(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        fail(f"{label} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, label=label)


def _positive_int(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        fail(f"{label} must be a positive integer")
    return value


def _parse_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        fail(f"{label} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        fail(f"{label} must be RFC3339 UTC")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        fail(f"{label} must be RFC3339 UTC")
    return parsed


def _validate_source_digests(value: Any) -> dict[str, str | None]:
    document = _require_fields(
        value, SOURCE_DIGEST_FIELDS, label="compare-captures.source_digests"
    )
    return {
        "a": _optional_digest(
            document.get("a"), label="compare-captures.source_digests.a"
        ),
        "b": _optional_digest(
            document.get("b"), label="compare-captures.source_digests.b"
        ),
    }


def _validate_compare_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(
        value, COMPARE_PAYLOAD_FIELDS, label="compare-captures"
    )
    verdict = payload.get("diagnostic_verdict")
    if verdict not in DIAGNOSTIC_VERDICTS:
        fail("compare-captures.diagnostic_verdict is unsupported")
    sample_count = _nonnegative_int(
        payload.get("sample_count"), label="compare-captures.sample_count"
    )
    identical = _nonnegative_int(
        payload.get("identical_record_count"),
        label="compare-captures.identical_record_count",
    )
    exact = _nonnegative_int(
        payload.get("exact_text_count"),
        label="compare-captures.exact_text_count",
    )
    hard = _nonnegative_int(
        payload.get("hard_disagreement_count"),
        label="compare-captures.hard_disagreement_count",
    )
    if identical > sample_count:
        fail("compare-captures.identical_record_count exceeds sample_count")
    if exact > sample_count:
        fail("compare-captures.exact_text_count exceeds sample_count")
    if identical > exact:
        fail("compare-captures.identical_record_count exceeds exact_text_count")
    if hard > sample_count:
        fail("compare-captures.hard_disagreement_count exceeds sample_count")
    digests = _validate_source_digests(payload.get("source_digests"))
    if completion == "incomplete" and verdict != "unusable":
        fail("incomplete compare-captures diagnostic must be unusable")
    if verdict == "unusable" and completion == "complete":
        fail("unusable compare-captures diagnostic is only valid when incomplete")
    if verdict == "identical":
        if completion != "complete":
            fail("identical compare-captures diagnostic requires a complete measurement")
        if identical != sample_count or hard != 0:
            fail(
                "identical compare-captures diagnostic requires every record "
                "identical and no hard disagreements"
            )
    mean = _optional_ratio(
        payload.get("mean_prefix_match"),
        label="compare-captures.mean_prefix_match",
    )
    minimum = _optional_ratio(
        payload.get("min_prefix_match"),
        label="compare-captures.min_prefix_match",
    )
    max_delta = _optional_decimal(
        payload.get("max_matched_prefix_logprob_delta"),
        label="compare-captures.max_matched_prefix_logprob_delta",
        nonnegative=True,
    )
    if mean is not None and minimum is not None and Decimal(minimum) > Decimal(mean):
        fail("compare-captures.min_prefix_match exceeds mean_prefix_match")
    if completion == "complete":
        if sample_count < 1:
            fail("complete compare-captures.sample_count must be positive")
        if digests["a"] is None or digests["b"] is None:
            fail("complete compare-captures.source_digests are required")
        if mean is None or minimum is None or max_delta is None:
            fail("complete compare-captures statistics are required")
        if identical == sample_count and verdict != "identical":
            fail("fully identical compare-captures data requires identical diagnostic")
        if verdict == "fp-equivalent" and hard != 0:
            fail("fp-equivalent compare-captures data cannot have hard disagreements")
    return {
        "sample_count": sample_count,
        "identical_record_count": identical,
        "exact_text_count": exact,
        "mean_prefix_match": mean,
        "min_prefix_match": minimum,
        "max_matched_prefix_logprob_delta": max_delta,
        "hard_disagreement_count": hard,
        "diagnostic_verdict": verdict,
        "source_digests": digests,
    }


def _validate_level(value: Any, *, index: int) -> dict[str, Any]:
    label = f"benchmark-serving.levels[{index}]"
    level = _require_fields(value, LEVEL_FIELDS, label=label)
    concurrency = _positive_int(
        level.get("concurrency"), label=f"{label}.concurrency"
    )
    completion = level.get("completion")
    if completion not in COMPLETIONS:
        fail(f"{label}.completion is unsupported")
    reason = level.get("reason")
    if reason not in LEVEL_REASONS:
        fail(f"{label}.reason is unsupported")
    if completion == "complete" and reason != "completed":
        fail(f"{label} complete reason must be completed")
    if completion == "incomplete" and reason == "completed":
        fail(f"{label} incomplete reason must explain the gap")
    requested = _positive_int(
        level.get("requested_request_count"),
        label=f"{label}.requested_request_count",
    )
    measured = _nonnegative_int(
        level.get("measured_request_count"),
        label=f"{label}.measured_request_count",
    )
    if requested < concurrency:
        fail(f"{label}.requested_request_count must be at least concurrency")
    if measured > requested:
        fail(f"{label}.measured_request_count exceeds requested_request_count")
    if completion == "complete" and measured != requested:
        fail(f"{label} complete measured_request_count must equal requested_request_count")
    decimals = {
        "ttft_p50_ms": _optional_decimal(
            level.get("ttft_p50_ms"),
            label=f"{label}.ttft_p50_ms",
            nonnegative=True,
            positive=completion == "complete",
        ),
        "ttft_p95_ms": _optional_decimal(
            level.get("ttft_p95_ms"),
            label=f"{label}.ttft_p95_ms",
            nonnegative=True,
            positive=completion == "complete",
        ),
        "decode_tps_p50": _optional_decimal(
            level.get("decode_tps_p50"),
            label=f"{label}.decode_tps_p50",
            nonnegative=True,
            positive=completion == "complete",
        ),
        "aggregate_tps": _optional_decimal(
            level.get("aggregate_tps"),
            label=f"{label}.aggregate_tps",
            nonnegative=True,
            positive=completion == "complete",
        ),
        "wall_s": _optional_decimal(
            level.get("wall_s"),
            label=f"{label}.wall_s",
            nonnegative=True,
            positive=completion == "complete",
        ),
    }
    if completion == "complete" and any(item is None for item in decimals.values()):
        fail(f"{label} complete level is missing a measured statistic")
    if (
        decimals["ttft_p50_ms"] is not None
        and decimals["ttft_p95_ms"] is not None
        and Decimal(decimals["ttft_p95_ms"]) < Decimal(decimals["ttft_p50_ms"])
    ):
        fail(f"{label}.ttft_p95_ms must be at least ttft_p50_ms")
    return {
        "concurrency": concurrency,
        "requested_request_count": requested,
        "measured_request_count": measured,
        "completion": completion,
        "reason": reason,
        **decimals,
    }


def _validate_benchmark_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(
        value, BENCHMARK_PAYLOAD_FIELDS, label="benchmark-serving"
    )
    style = payload.get("prompt_style")
    if style not in PROMPT_STYLES:
        fail("benchmark-serving.prompt_style is unsupported")
    explicit = payload.get("explicit_request_count")
    if explicit is not None:
        explicit = _positive_int(
            explicit, label="benchmark-serving.explicit_request_count"
        )
    levels = payload.get("levels")
    if not isinstance(levels, list) or not levels:
        fail("benchmark-serving.levels must be a non-empty list")
    validated_levels = [
        _validate_level(item, index=index) for index, item in enumerate(levels)
    ]
    concurrencies = [item["concurrency"] for item in validated_levels]
    if len(concurrencies) != len(set(concurrencies)):
        fail("benchmark-serving concurrency levels must be unique")
    all_complete = all(
        item["completion"] == "complete" for item in validated_levels
    )
    if completion == "complete" and not all_complete:
        fail("complete benchmark-serving requires every level to be complete")
    if completion == "incomplete" and all_complete:
        fail("incomplete benchmark-serving cannot contain only complete levels")
    if explicit is not None and any(
        item["requested_request_count"] != explicit for item in validated_levels
    ):
        fail(
            "benchmark-serving explicit_request_count must match every level's "
            "requested_request_count"
        )
    return {
        "input_tokens": _positive_int(
            payload.get("input_tokens"), label="benchmark-serving.input_tokens"
        ),
        "output_tokens": _positive_int(
            payload.get("output_tokens"),
            label="benchmark-serving.output_tokens",
        ),
        "prompt_style": style,
        "explicit_request_count": explicit,
        "levels": validated_levels,
    }


def _validate_resource_rank(value: Any, *, index: int) -> dict[str, Any]:
    label = f"observe-resources.ranks[{index}]"
    document = _require_fields(value, RESOURCE_RANK_FIELDS, label=label)
    rank = document.get("rank")
    if not isinstance(rank, str) or not (
        rank == "single" or rank.isdigit() and str(int(rank)) == rank
    ):
        fail(f"{label}.rank must be 'single' or a canonical non-negative integer")
    status = document.get("collection_status")
    if status not in RESOURCE_COLLECTION_STATUSES:
        fail(f"{label}.collection_status is unsupported")
    sample_count = _nonnegative_int(
        document.get("sample_count"), label=f"{label}.sample_count"
    )
    workload_sample_count = _nonnegative_int(
        document.get("workload_sample_count"),
        label=f"{label}.workload_sample_count",
    )
    if workload_sample_count > sample_count:
        fail(f"{label}.workload_sample_count exceeds sample_count")
    optional = {
        field: _optional_nonnegative_int(
            document.get(field), label=f"{label}.{field}"
        )
        for field in RESOURCE_RANK_FIELDS
        - {"rank", "collection_status", "sample_count", "workload_sample_count"}
    }
    if status == "complete" and (sample_count < 1 or workload_sample_count < 1):
        fail(f"{label} complete collection requires pool and workload samples")
    if status == "pool-only" and (
        sample_count < 1 or workload_sample_count != 0
    ):
        fail(f"{label} pool-only collection has invalid sample counts")
    if status == "unavailable" and sample_count != 0:
        fail(f"{label} unavailable collection must have zero samples")
    return {
        "rank": rank,
        "collection_status": status,
        "sample_count": sample_count,
        "workload_sample_count": workload_sample_count,
        **optional,
    }


def _validate_resource_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(value, RESOURCE_PAYLOAD_FIELDS, label="observe-resources")
    started_at = payload.get("started_at")
    ended_at = payload.get("ended_at")
    started = _parse_utc(started_at, label="observe-resources.started_at")
    ended = _parse_utc(ended_at, label="observe-resources.ended_at")
    if ended < started:
        fail("observe-resources.ended_at precedes started_at")
    delta = ended - started
    elapsed = Decimal(delta.days * 86400 + delta.seconds) + (
        Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    duration = canonical_decimal(
        payload.get("duration_seconds"), label="observe-resources.duration_seconds"
    )
    if duration != canonical_decimal(
        elapsed, label="observe-resources elapsed", require_canonical=False
    ):
        fail("observe-resources.duration_seconds differs from its timestamp interval")
    scope = payload.get("qualification_scope")
    if scope not in RESOURCE_QUALIFICATION_SCOPES:
        fail("observe-resources.qualification_scope is unsupported")
    interval = canonical_decimal(
        payload.get("sample_interval_seconds"),
        label="observe-resources.sample_interval_seconds",
    )
    if Decimal(interval) < Decimal("0.1") or Decimal(interval) > Decimal(60):
        fail("observe-resources.sample_interval_seconds is outside the supported range")
    expected_rank_count = _positive_int(
        payload.get("expected_rank_count"),
        label="observe-resources.expected_rank_count",
    )
    observed_rank_count = _nonnegative_int(
        payload.get("observed_rank_count"),
        label="observe-resources.observed_rank_count",
    )
    if observed_rank_count > expected_rank_count:
        fail("observe-resources.observed_rank_count exceeds expected_rank_count")
    sample_count = _nonnegative_int(
        payload.get("sample_count"), label="observe-resources.sample_count"
    )
    ranks = payload.get("ranks")
    if not isinstance(ranks, list) or len(ranks) != expected_rank_count:
        fail("observe-resources.ranks must cover every expected rank")
    validated_ranks = [
        _validate_resource_rank(item, index=index) for index, item in enumerate(ranks)
    ]
    rank_labels = [item["rank"] for item in validated_ranks]
    if rank_labels != sorted(
        rank_labels,
        key=lambda item: (item != "single", int(item) if item.isdigit() else -1),
    ) or len(rank_labels) != len(set(rank_labels)):
        fail("observe-resources.ranks must be sorted and unique")
    if sum(item["sample_count"] for item in validated_ranks) != sample_count:
        fail("observe-resources.sample_count differs from rank sample counts")
    observed = sum(item["sample_count"] > 0 for item in validated_ranks)
    if observed != observed_rank_count:
        fail("observe-resources.observed_rank_count differs from rank observations")
    if completion == "complete" and any(
        item["collection_status"] != "complete" for item in validated_ranks
    ):
        fail("complete observe-resources requires complete collection on every rank")
    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration,
        "qualification_scope": scope,
        "sample_interval_seconds": interval,
        "expected_rank_count": expected_rank_count,
        "observed_rank_count": observed_rank_count,
        "sample_count": sample_count,
        "ranks": validated_ranks,
    }


def _validate_accuracy_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(value, ACCURACY_PAYLOAD_FIELDS, label="evaluate-gsm8k")
    requested = _positive_int(
        payload.get("requested_sample_count"),
        label="evaluate-gsm8k.requested_sample_count",
    )
    measured = _nonnegative_int(
        payload.get("measured_sample_count"),
        label="evaluate-gsm8k.measured_sample_count",
    )
    correct = _nonnegative_int(
        payload.get("correct_count"), label="evaluate-gsm8k.correct_count"
    )
    errors = _nonnegative_int(
        payload.get("request_error_count"),
        label="evaluate-gsm8k.request_error_count",
    )
    if measured > requested:
        fail("evaluate-gsm8k.measured_sample_count exceeds requested_sample_count")
    if correct > measured:
        fail("evaluate-gsm8k.correct_count exceeds measured_sample_count")
    accuracy = _optional_ratio(payload.get("accuracy"), label="evaluate-gsm8k.accuracy")
    digest = _optional_digest(
        payload.get("dataset_file_sha256"),
        label="evaluate-gsm8k.dataset_file_sha256",
    )
    if payload.get("reasoning_mode") not in {"enabled", "disabled"}:
        fail("evaluate-gsm8k.reasoning_mode is unsupported")
    temperature = canonical_decimal(
        payload.get("temperature"), label="evaluate-gsm8k.temperature"
    )
    if Decimal(temperature) < 0:
        fail("evaluate-gsm8k.temperature must be non-negative")
    if completion == "complete":
        if measured != requested or errors != 0:
            fail("complete evaluate-gsm8k requires every request to succeed")
        if digest is None or accuracy is None:
            fail("complete evaluate-gsm8k requires dataset and accuracy digests")
        expected = canonical_decimal(
            Decimal(correct) / Decimal(measured),
            label="evaluate-gsm8k expected accuracy",
            require_canonical=False,
        )
        if accuracy != expected:
            fail("evaluate-gsm8k accuracy differs from correct/sample count")
    return {
        "dataset_id": payload.get("dataset_id"),
        "dataset_revision": payload.get("dataset_revision"),
        "dataset_file_sha256": digest,
        "subset": payload.get("subset"),
        "split": payload.get("split"),
        "selection": payload.get("selection"),
        "answer_normalization": payload.get("answer_normalization"),
        "max_completion_tokens": _positive_int(
            payload.get("max_completion_tokens"),
            label="evaluate-gsm8k.max_completion_tokens",
        ),
        "reasoning_mode": payload.get("reasoning_mode"),
        "temperature": temperature,
        "requested_sample_count": requested,
        "measured_sample_count": measured,
        "correct_count": correct,
        "request_error_count": errors,
        "accuracy": accuracy,
    }


def _validate_soak_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(value, SOAK_PAYLOAD_FIELDS, label="validate-soak")
    started_at = payload.get("started_at")
    ended_at = payload.get("ended_at")
    started = _parse_utc(started_at, label="validate-soak.started_at")
    ended = _parse_utc(ended_at, label="validate-soak.ended_at")
    if ended < started:
        fail("validate-soak.ended_at precedes started_at")
    delta = ended - started
    elapsed = Decimal(delta.days * 86400 + delta.seconds) + (
        Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    duration = canonical_decimal(
        payload.get("duration_seconds"), label="validate-soak.duration_seconds"
    )
    if duration != canonical_decimal(
        elapsed, label="validate-soak elapsed", require_canonical=False
    ):
        fail("validate-soak.duration_seconds differs from its timestamp interval")
    concurrency = _positive_int(
        payload.get("concurrency"), label="validate-soak.concurrency"
    )
    completed = _nonnegative_int(
        payload.get("completed_requests"),
        label="validate-soak.completed_requests",
    )
    errors = _nonnegative_int(
        payload.get("request_error_count"),
        label="validate-soak.request_error_count",
    )
    if completion == "complete" and completed < 1:
        fail("complete validate-soak requires at least one completed request")
    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration,
        "concurrency": concurrency,
        "completed_requests": completed,
        "request_error_count": errors,
    }


def _validate_identity_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(
        value, IDENTITY_PAYLOAD_FIELDS, label="verify-snapshot-manifest"
    )
    spec_id = payload.get("spec_id")
    if not isinstance(spec_id, str) or HEX64_RE.fullmatch(spec_id) is None:
        fail("verify-snapshot-manifest.spec_id must be a SHA-256 digest")
    manifest_id = payload.get("manifest_id")
    if not isinstance(manifest_id, str) or HEX64_RE.fullmatch(manifest_id) is None:
        fail("verify-snapshot-manifest.manifest_id must be a SHA-256 digest")
    expected = _nonnegative_int(
        payload.get("expected_file_count"),
        label="verify-snapshot-manifest.expected_file_count",
    )
    matched = _nonnegative_int(
        payload.get("matched_file_count"),
        label="verify-snapshot-manifest.matched_file_count",
    )
    mismatched = _nonnegative_int(
        payload.get("mismatched_file_count"),
        label="verify-snapshot-manifest.mismatched_file_count",
    )
    missing = _nonnegative_int(
        payload.get("missing_file_count"),
        label="verify-snapshot-manifest.missing_file_count",
    )
    extra = _nonnegative_int(
        payload.get("extra_file_count"),
        label="verify-snapshot-manifest.extra_file_count",
    )
    accounted = matched + mismatched + missing
    if accounted > expected:
        fail(
            "verify-snapshot-manifest matched, mismatched, and missing "
            "counts exceed expected_file_count"
        )
    if completion == "complete" and accounted != expected:
        fail(
            "complete verify-snapshot-manifest requires matched, mismatched, "
            "and missing counts to equal expected_file_count"
        )
    return {
        "spec_id": spec_id,
        "manifest_id": manifest_id,
        "expected_file_count": expected,
        "matched_file_count": matched,
        "mismatched_file_count": mismatched,
        "missing_file_count": missing,
        "extra_file_count": extra,
    }


def _validate_serve_smoke_phase(value: Any, *, label: str) -> dict[str, str]:
    phase = _require_fields(value, SERVE_SMOKE_PHASE_FIELDS, label=label)
    completion = phase.get("completion")
    if completion not in COMPLETIONS:
        fail(f"{label}.completion is unsupported")
    reason = phase.get("reason")
    if reason not in SERVE_SMOKE_PHASE_REASONS:
        fail(f"{label}.reason is unsupported")
    if completion == "complete" and reason != "completed":
        fail(f"{label} complete reason must be completed")
    if completion == "incomplete" and reason == "completed":
        fail(f"{label} incomplete reason must explain the gap")
    return {"completion": completion, "reason": reason}


def _validate_serve_smoke_payload(value: Any, *, completion: str) -> dict[str, Any]:
    payload = _require_fields(value, SERVE_SMOKE_PAYLOAD_FIELDS, label="serve-smoke")
    health = _validate_serve_smoke_phase(
        payload.get("health"), label="serve-smoke.health"
    )
    warmup = _validate_serve_smoke_phase(
        payload.get("warmup"), label="serve-smoke.warmup"
    )
    smoke = _validate_serve_smoke_phase(
        payload.get("completion"), label="serve-smoke.completion"
    )
    phases = (health, warmup, smoke)
    all_complete = all(item["completion"] == "complete" for item in phases)
    if completion == "complete" and not all_complete:
        fail("complete serve-smoke requires health, warmup, and completion to be complete")
    if completion == "incomplete" and all_complete:
        fail("incomplete serve-smoke cannot contain only complete phases")
    return {
        "health": health,
        "warmup": warmup,
        "completion": smoke,
    }


def _validate_snapshot_set_payload(value, *, completion):
    payload = _require_fields(value, {'spec_id','snapshots'}, label='snapshot identity set')
    snapshots = payload['snapshots']
    if not isinstance(snapshots,dict) or not snapshots or 'target' not in snapshots:
        fail('snapshot identity set requires target and named snapshots')
    checked = {}
    for name, item in snapshots.items():
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',name):
            fail('invalid snapshot identity name')
        if not isinstance(item,dict) or 'spec_id' in item:
            fail('invalid snapshot identity entry')
        row = _validate_identity_payload({'spec_id':payload['spec_id'],**item},completion=completion)
        row.pop('spec_id')
        checked[name] = row
    return {'spec_id':payload['spec_id'],'snapshots':dict(sorted(checked.items()))}


def validate_measurement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("validator measurement must be an object")
    _scan_forbidden_keys(value, label="validator measurement")
    operation = value.get("operation")
    field_sets = {
        "compare-captures": COMPARE_FIELDS,
        "benchmark-serving": BENCHMARK_FIELDS,
        "observe-resources": RESOURCE_FIELDS,
        "evaluate-gsm8k": ACCURACY_FIELDS,
        "validate-soak": SOAK_FIELDS,
        "verify-snapshot-manifest": IDENTITY_FIELDS,
        "serve-smoke": SERVE_SMOKE_FIELDS,
    }
    if operation not in field_sets:
        fail("validator measurement operation is unsupported")
    document = _require_fields(
        value, field_sets[operation], label="validator measurement"
    )
    if type(document.get("schema_version")) is not int or document.get("schema_version") not in ((1,2) if operation=="verify-snapshot-manifest" else (1,)):
        fail("validator measurement schema_version is unsupported")
    if document.get("kind") != MEASUREMENT_KIND:
        fail("validator measurement kind is invalid")
    program = document.get("program")
    if program not in PROGRAM_OPERATIONS:
        fail("validator measurement program is unsupported")
    if PROGRAM_OPERATIONS[program] != operation:
        fail("validator measurement program/operation pair is invalid")
    completion = document.get("completion")
    if completion not in COMPLETIONS:
        fail("validator measurement completion is unsupported")
    reason = document.get("reason")
    allowed_reasons = {
        "compare-captures": COMPARE_REASONS,
        "benchmark-serving": BENCHMARK_REASONS,
        "observe-resources": RESOURCE_REASONS,
        "evaluate-gsm8k": ACCURACY_REASONS,
        "validate-soak": SOAK_REASONS,
        "verify-snapshot-manifest": IDENTITY_REASONS,
        "serve-smoke": SERVE_SMOKE_REASONS,
    }[operation]
    if reason not in allowed_reasons:
        fail("validator measurement reason is unsupported")
    if completion == "complete" and reason != "completed":
        fail("complete validator measurement reason must be completed")
    if completion == "incomplete" and reason == "completed":
        fail("incomplete validator measurement requires an explanatory reason")
    payload_validators = {
        "compare-captures": _validate_compare_payload,
        "benchmark-serving": _validate_benchmark_payload,
        "observe-resources": _validate_resource_payload,
        "evaluate-gsm8k": _validate_accuracy_payload,
        "validate-soak": _validate_soak_payload,
        "verify-snapshot-manifest": _validate_identity_payload,
        "serve-smoke": _validate_serve_smoke_payload,
    }
    if document["schema_version"] == 2:
        payload_validators[operation] = _validate_snapshot_set_payload
    payload = payload_validators[operation](
        document.get(operation), completion=completion
    )
    validated = {
        "schema_version": document["schema_version"],
        "kind": MEASUREMENT_KIND,
        "program": program,
        "operation": operation,
        "completion": completion,
        "reason": reason,
        operation: payload,
    }
    _screen_measurement_json(validated, label="validator measurement")
    return validated


def load_measurement_bytes(data: bytes) -> dict[str, Any]:
    payload = parse_strict_json(data, label="validator measurement")
    return validate_measurement(payload)


def load_measurement_file(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        data = read_stable_bytes(target, label="validator measurement")
    except FileNotFoundError as exc:
        raise ValidatorMeasurementMissing(
            f"validator measurement is missing: {target.name}"
        ) from exc
    except ValidatorMeasurementMissing:
        raise
    except OSError as exc:
        fail(f"validator measurement cannot be read: {exc}")
    return load_measurement_bytes(data)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Publish bytes with no-follow parents and exclusive no-replace link."""
    destination = Path(path)
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    try:
        absolute = immutable_descriptor_dir.safe_absolute(
            destination, label="output file"
        )
        parts = immutable_descriptor_dir.lexical_parts(
            absolute, label="output file"
        )
    except immutable_descriptor_dir.ImmutableDescriptorDirectoryError as exc:
        fail(str(exc))
    if not parts:
        fail("output file is too broad")
    dest_name = parts[-1]
    staging_name = (
        f".{dest_name}.tmp.{os.getpid()}.{os.urandom(8).hex()}"
    )
    try:
        parent_fd = immutable_descriptor_dir.open_directory_from_root(
            parts[:-1],
            label="output parent",
            create=True,
        )
    except immutable_descriptor_dir.ImmutableDescriptorDirectoryError as exc:
        fail(str(exc))
    staging_fd = None
    try:
        try:
            staging_fd = os.open(
                staging_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            fail(f"cannot create private output staging: {exc.strerror}")
        immutable_descriptor_dir.write_fd(staging_fd, data)
        os.fsync(staging_fd)
        os.close(staging_fd)
        staging_fd = None
        try:
            os.link(
                staging_name,
                dest_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ELOOP}:
                fail(f"refusing to overwrite existing file: {dest_name}")
            fail("exclusive output publish failed")
        os.unlink(staging_name, dir_fd=parent_fd)
        immutable_descriptor_dir.fsync_dir_fd(parent_fd)
    except Exception:
        if staging_fd is not None:
            immutable_descriptor_dir.close_quietly(staging_fd)
        try:
            os.unlink(staging_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        immutable_descriptor_dir.close_quietly(parent_fd)


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, pretty_json_bytes(value))


def write_measurement(path: str | Path, value: Any) -> dict[str, Any]:
    document = validate_measurement(value)
    atomic_write_json(path, document)
    return document


def empty_compare_payload(
    *,
    diagnostic_verdict: str = "unusable",
    source_digests: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    digests = source_digests or {"a": None, "b": None}
    return {
        "sample_count": 0,
        "identical_record_count": 0,
        "exact_text_count": 0,
        "mean_prefix_match": None,
        "min_prefix_match": None,
        "max_matched_prefix_logprob_delta": None,
        "hard_disagreement_count": 0,
        "diagnostic_verdict": diagnostic_verdict,
        "source_digests": {
            "a": digests.get("a"),
            "b": digests.get("b"),
        },
    }


def build_compare_measurement(
    *,
    completion: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    source_digests: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    document = {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "kind": MEASUREMENT_KIND,
        "program": "validate/compare_captures.py",
        "operation": "compare-captures",
        "completion": completion,
        "reason": reason,
        "compare-captures": payload
        if payload is not None
        else empty_compare_payload(source_digests=source_digests),
    }
    return validate_measurement(document)


def empty_benchmark_level(
    *,
    concurrency: int,
    requested_request_count: int,
    measured_request_count: int = 0,
    completion: str = "incomplete",
    reason: str = "measured-incomplete",
) -> dict[str, Any]:
    return {
        "concurrency": concurrency,
        "requested_request_count": requested_request_count,
        "measured_request_count": measured_request_count,
        "completion": completion,
        "reason": reason,
        "ttft_p50_ms": None,
        "ttft_p95_ms": None,
        "decode_tps_p50": None,
        "aggregate_tps": None,
        "wall_s": None,
    }


def build_benchmark_measurement(
    *,
    completion: str,
    reason: str,
    input_tokens: int,
    output_tokens: int,
    prompt_style: str,
    explicit_request_count: int | None,
    levels: list[dict[str, Any]],
) -> dict[str, Any]:
    document = {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "kind": MEASUREMENT_KIND,
        "program": "validate/bench_serve.py",
        "operation": "benchmark-serving",
        "completion": completion,
        "reason": reason,
        "benchmark-serving": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "prompt_style": prompt_style,
            "explicit_request_count": explicit_request_count,
            "levels": levels,
        },
    }
    return validate_measurement(document)


def build_resource_measurement(
    *, completion: str, reason: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return validate_measurement(
        {
            "schema_version": MEASUREMENT_SCHEMA_VERSION,
            "kind": MEASUREMENT_KIND,
            "program": "scripts/model-serving-experiment-monitor.sh",
            "operation": "observe-resources",
            "completion": completion,
            "reason": reason,
            "observe-resources": payload,
        }
    )


def build_accuracy_measurement(
    *, completion: str, reason: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return validate_measurement(
        {
            "schema_version": MEASUREMENT_SCHEMA_VERSION,
            "kind": MEASUREMENT_KIND,
            "program": "validate/gsm8k_eval.py",
            "operation": "evaluate-gsm8k",
            "completion": completion,
            "reason": reason,
            "evaluate-gsm8k": payload,
        }
    )


def build_soak_measurement(
    *, completion: str, reason: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return validate_measurement(
        {
            "schema_version": MEASUREMENT_SCHEMA_VERSION,
            "kind": MEASUREMENT_KIND,
            "program": "validate/soak.py",
            "operation": "validate-soak",
            "completion": completion,
            "reason": reason,
            "validate-soak": payload,
        }
    )


def build_identity_measurement(
    *, completion: str, reason: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return validate_measurement(
        {
            "schema_version": 2 if "snapshots" in payload else 1,
            "kind": MEASUREMENT_KIND,
            "program": "validate/verify_snapshot_manifest.py",
            "operation": "verify-snapshot-manifest",
            "completion": completion,
            "reason": reason,
            "verify-snapshot-manifest": payload,
        }
    )


def empty_serve_smoke_phase(
    *, completion: str = "incomplete", reason: str = "measured-incomplete"
) -> dict[str, str]:
    return {"completion": completion, "reason": reason}


def build_serve_smoke_measurement(
    *, completion: str, reason: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return validate_measurement(
        {
            "schema_version": MEASUREMENT_SCHEMA_VERSION,
            "kind": MEASUREMENT_KIND,
            "program": "validate/serve_smoke.py",
            "operation": "serve-smoke",
            "completion": completion,
            "reason": reason,
            "serve-smoke": payload,
        }
    )
