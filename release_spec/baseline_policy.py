#!/usr/bin/env python3
"""Closed fixed baseline-v1 and baseline-v2 policy schema.

This module owns ``policy/baseline-v1.json`` and ``policy/baseline-v2.json``.
It verifies the document,
requires on-disk bytes to match ``release_spec.pretty_json_bytes``, and
returns the SHA-256 of those canonical bytes as ``policy_digest``. It does
not judge measurements, write a spec, or assign review status.
"""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from . import pretty_json_bytes  # noqa: E402
from .schema import (  # noqa: E402
    HF_MODEL_ID_RE,
    SHA256_HEX_RE,
    THRESHOLD_KEYS,
    THRESHOLD_OPERATORS,
    require_commit,
    require_nonempty_string,
    require_object,
    require_public_string,
    require_sha256_hex,
    screen_public_string,
)

KIND = "pulsar-baseline-policy"
SCHEMA_VERSION = 1
SUITE = "baseline-v1"  # Historical schema-1 evidence.
SUPPORTED_POLICY_DIGESTS = {
    "baseline-v1": "0b79190daf6e03b81c4b847adf0895b6102daec575ac8d6b0fac712381085539",
    "baseline-v2": "a4b3095c26e9b2636b7406d73b1ed4a310673fe75e3a862d2f5a41c0b9ecbdb0",
}
POLICY_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "suite",
        "accuracy_floor_overrides",
        "gates",
    }
)
GATE_BASE_KEYS = frozenset({"criterion_id", "operation", "thresholds"})
GSM8K_PIN_KEYS = (
    "dataset_id",
    "dataset_revision",
    "dataset_file_sha256",
    "subset",
    "split",
    "selection",
    "answer_normalization",
    "temperature",
    "reasoning_mode",
    "max_completion_tokens",
)
EXPECTED_GATES = (
    ("identity-snapshot-manifest", "verify-snapshot-manifest"),
    ("serving-integration-smoke", "serve-smoke"),
    ("strict-same-boot-captures", "compare-captures"),
    ("gsm8k-subset", "evaluate-gsm8k"),
    ("soak-60", "validate-soak"),
    ("perf-snapshot", "benchmark-serving"),
)
class BaselinePolicyError(ValueError):
    """A baseline-v1 policy document is missing, unsafe, or invalid."""


def fail(message: str) -> None:
    raise BaselinePolicyError(message)


def _reject_floats(value: Any, *, path: str) -> None:
    if isinstance(value, float):
        fail(f"{path} must not be a JSON float")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _reject_floats(item, path=child)


def _reject_json_float(_value: str) -> None:
    fail("document must not contain JSON floats")


def _canonical_decimal(value: Any, *, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        fail(f"{path} must be a canonical decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        fail(f"{path} must be numeric")
    if not parsed.is_finite():
        fail(f"{path} must be finite")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"", "-0"}:
        normalized = "0"
    if isinstance(value, str) and value != normalized:
        fail(f"{path} is not canonical (expected {normalized!r})")
    return normalized


def _ratio(value: Any, *, path: str) -> str:
    text = _canonical_decimal(value, path=path)
    parsed = Decimal(text)
    if parsed < 0 or parsed > 1:
        fail(f"{path} must be a ratio in [0, 1]")
    return text


def _positive_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(f"{path} must be a positive integer")
    return value


def _verify_threshold(value: Any, *, path: str) -> dict[str, str]:
    require_object(value, THRESHOLD_KEYS, path=path)
    operator = value.get("operator")
    if operator not in THRESHOLD_OPERATORS:
        fail(f"{path}.operator is not a supported comparison operator")
    metric = require_nonempty_string(value.get("metric"), path=f"{path}.metric")
    unit = require_nonempty_string(value.get("unit"), path=f"{path}.unit")
    raw = value.get("value")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        fail(f"{path}.value must be a non-empty string")
    screen_public_string(raw, path=f"{path}.value")
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        text = raw
    else:
        if parsed.is_finite():
            text = _canonical_decimal(raw, path=f"{path}.value")
        else:
            fail(f"{path}.value must be finite")
    return {
        "metric": metric,
        "operator": operator,
        "value": text,
        "unit": unit,
    }


def _require_threshold(
    thresholds: list[dict[str, str]],
    *,
    metric: str,
    operator: str,
    value: str | None = None,
    unit: str,
    path: str,
) -> dict[str, str]:
    matches = [item for item in thresholds if item["metric"] == metric]
    if len(matches) != 1:
        fail(f"{path} must contain exactly one {metric!r} threshold")
    item = matches[0]
    if item["operator"] != operator:
        fail(f"{path} threshold {metric!r} operator must be {operator!r}")
    if item["unit"] != unit:
        fail(f"{path} threshold {metric!r} unit must be {unit!r}")
    if value is not None and item["value"] != value:
        fail(f"{path} threshold {metric!r} value must be {value!r}")
    return item


def _verify_pins(value: Any, *, path: str) -> dict[str, Any]:
    require_object(value, frozenset(GSM8K_PIN_KEYS), path=path)
    pins: dict[str, Any] = {}
    pins["dataset_id"] = require_public_string(
        value.get("dataset_id"), path=f"{path}.dataset_id"
    )
    pins["dataset_revision"] = require_commit(
        value.get("dataset_revision"), path=f"{path}.dataset_revision"
    )
    pins["dataset_file_sha256"] = require_sha256_hex(
        value.get("dataset_file_sha256"), path=f"{path}.dataset_file_sha256"
    )
    for field in ("subset", "split", "selection", "answer_normalization"):
        pins[field] = require_public_string(value.get(field), path=f"{path}.{field}")
    pins["temperature"] = _canonical_decimal(
        value.get("temperature"), path=f"{path}.temperature"
    )
    mode = value.get("reasoning_mode")
    if mode not in {"enabled", "disabled"}:
        fail(f"{path}.reasoning_mode is unsupported")
    pins["reasoning_mode"] = mode
    pins["max_completion_tokens"] = _positive_int(
        value.get("max_completion_tokens"),
        path=f"{path}.max_completion_tokens",
    )
    return pins


def _verify_overrides(value: Any, *, path: str) -> dict[str, str]:
    if not isinstance(value, dict):
        fail(f"{path} must be an object")
    overrides: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or HF_MODEL_ID_RE.fullmatch(key) is None:
            fail(f"{path} keys must be Hugging Face org/name model ids")
        screen_public_string(key, path=f"{path}[{key!r}]")
        overrides[key] = _ratio(item, path=f"{path}[{key!r}]")
    return overrides


def _verify_gate(value: Any, *, index: int, expected: tuple[str, str]) -> dict[str, Any]:
    path = f"gates[{index}]"
    if not isinstance(value, dict):
        fail(f"{path} must be an object")
    criterion_id, operation = expected
    extra_keys: set[str] = set()
    if operation == "evaluate-gsm8k":
        extra_keys.add("pins")
    elif operation == "benchmark-serving":
        extra_keys.add("required_concurrencies")
    require_object(value, GATE_BASE_KEYS | extra_keys, path=path)
    if value.get("criterion_id") != criterion_id:
        fail(f"{path}.criterion_id must be {criterion_id!r}")
    if value.get("operation") != operation:
        fail(f"{path}.operation must be {operation!r}")
    raw_thresholds = value.get("thresholds")
    if not isinstance(raw_thresholds, list) or not raw_thresholds:
        fail(f"{path}.thresholds must be a non-empty list")
    thresholds = [
        _verify_threshold(item, path=f"{path}.thresholds[{index}]")
        for index, item in enumerate(raw_thresholds)
    ]
    metrics = [item["metric"] for item in thresholds]
    if len(metrics) != len(set(metrics)):
        fail(f"{path}.thresholds metrics must be unique")
    gate: dict[str, Any] = {
        "criterion_id": criterion_id,
        "operation": operation,
        "thresholds": thresholds,
    }
    if operation == "verify-snapshot-manifest":
        _require_threshold(
            thresholds,
            metric="unmatched_file_count",
            operator="==",
            value="0",
            unit="count",
            path=path,
        )
        if len(thresholds) != 1:
            fail(f"{path}.thresholds must contain only unmatched_file_count")
    elif operation == "serve-smoke":
        for metric in (
            "health_complete",
            "warmup_complete",
            "completion_smoke_complete",
        ):
            _require_threshold(
                thresholds,
                metric=metric,
                operator="==",
                value="1",
                unit="boolean",
                path=path,
            )
        if len(thresholds) != 3:
            fail(f"{path}.thresholds must contain the three smoke completeness metrics")
    elif operation == "compare-captures":
        _require_threshold(
            thresholds,
            metric="exact_match_rate",
            operator="==",
            value="1",
            unit="ratio",
            path=path,
        )
        if len(thresholds) != 1:
            fail(f"{path}.thresholds must contain only exact_match_rate")
    elif operation == "evaluate-gsm8k":
        pins = _verify_pins(value.get("pins"), path=f"{path}.pins")
        accuracy = _require_threshold(
            thresholds,
            metric="accuracy",
            operator=">=",
            unit="ratio",
            path=path,
        )
        _ratio(accuracy["value"], path=f"{path} accuracy value")
        _require_threshold(
            thresholds,
            metric="measured_sample_count",
            operator=">=",
            value="100",
            unit="count",
            path=path,
        )
        for metric, pin_key in (
            ("dataset_id", "dataset_id"),
            ("dataset_revision", "dataset_revision"),
            ("dataset_file_sha256", "dataset_file_sha256"),
        ):
            _require_threshold(
                thresholds,
                metric=metric,
                operator="==",
                value=str(pins[pin_key]),
                unit=(
                    "id"
                    if metric == "dataset_id"
                    else "commit"
                    if metric == "dataset_revision"
                    else "digest"
                ),
                path=path,
            )
        if len(thresholds) != 5:
            fail(f"{path}.thresholds must contain accuracy, sample size, and dataset pins")
        gate["pins"] = pins
    elif operation == "validate-soak":
        _require_threshold(
            thresholds,
            metric="duration_seconds",
            operator=">=",
            value="3600",
            unit="seconds",
            path=path,
        )
        _require_threshold(
            thresholds,
            metric="request_error_count",
            operator="==",
            value="0",
            unit="count",
            path=path,
        )
        _require_threshold(
            thresholds,
            metric="completed_requests",
            operator=">",
            value="0",
            unit="count",
            path=path,
        )
        if len(thresholds) != 3:
            fail(f"{path}.thresholds must contain duration, errors, and completions")
    else:
        _require_threshold(
            thresholds,
            metric="required_levels_complete",
            operator="==",
            value="1",
            unit="boolean",
            path=path,
        )
        if len(thresholds) != 1:
            fail(f"{path}.thresholds must contain only required_levels_complete")
        raw_levels = value.get("required_concurrencies")
        if not isinstance(raw_levels, list) or not raw_levels:
            fail(f"{path}.required_concurrencies must be a non-empty list")
        levels: list[int] = []
        for level_index, item in enumerate(raw_levels):
            levels.append(
                _positive_int(
                    item, path=f"{path}.required_concurrencies[{level_index}]"
                )
            )
        if len(levels) != len(set(levels)):
            fail(f"{path}.required_concurrencies must be unique")
        gate["required_concurrencies"] = levels
    return gate


def verify_policy(document: Any) -> dict[str, Any]:
    """Return the canonical policy object or raise ``BaselinePolicyError``."""
    if not isinstance(document, dict):
        fail("document must be an object")
    _reject_floats(document, path="")
    require_object(document, POLICY_KEYS, path="document")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        fail("document.schema_version must be 1")
    if document.get("kind") != KIND:
        fail(f"document.kind must be {KIND!r}")
    suite = document.get("suite")
    if not isinstance(suite, str) or suite not in SUPPORTED_POLICY_DIGESTS:
        fail("document.suite must be baseline-v1 or baseline-v2")
    expected_gates = tuple(gate for gate in EXPECTED_GATES
                           if suite == "baseline-v1" or gate[1] != "compare-captures")
    overrides = _verify_overrides(
        document.get("accuracy_floor_overrides"),
        path="accuracy_floor_overrides",
    )
    raw_gates = document.get("gates")
    if not isinstance(raw_gates, list) or len(raw_gates) != len(expected_gates):
        fail(f"gates must list the {len(expected_gates)} {suite} gates in order")
    gates = [
        _verify_gate(item, index=index, expected=expected)
        for index, (item, expected) in enumerate(zip(raw_gates, expected_gates))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "suite": suite,
        "accuracy_floor_overrides": overrides,
        "gates": gates,
    }


def canonical_policy_bytes(policy: dict[str, Any]) -> bytes:
    return pretty_json_bytes(verify_policy(policy))


def load_supported_policy(path: str | Path) -> tuple[dict[str, Any], str]:
    """Accept only the exact published policy for the recorded suite."""
    policy, digest = load_policy(path)
    if digest != SUPPORTED_POLICY_DIGESTS[policy['suite']]:
        fail(f"{policy['suite']} policy differs from the supported fixed policy")
    return policy, digest


def policy_digest_for(policy: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_policy_bytes(policy)).hexdigest()


def load_policy(path: str | Path) -> tuple[dict[str, Any], str]:
    """Load, verify, and digest a canonical policy file."""
    policy_path = Path(path)
    try:
        data = policy_path.read_bytes()
    except OSError as exc:
        fail(f"cannot read {policy_path}: {exc}")
    try:
        document = json.loads(
            data.decode("utf-8"),
            parse_float=_reject_json_float,
            parse_constant=_reject_json_float,
        )
    except UnicodeDecodeError as exc:
        fail(f"invalid UTF-8: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON: {exc}")
    except TypeError as exc:
        fail(f"invalid JSON: {exc}")
    policy = verify_policy(document)
    canonical = pretty_json_bytes(policy)
    if data != canonical:
        fail("policy file bytes differ from the canonical encoding")
    digest = hashlib.sha256(canonical).hexdigest()
    if SHA256_HEX_RE.fullmatch(digest) is None:
        fail("policy digest is invalid")
    return policy, digest


def applied_accuracy_floor(policy: dict[str, Any], model_id: str) -> str:
    """Return the override for ``model_id`` or the policy default floor."""
    default = None
    for gate in policy["gates"]:
        if gate["criterion_id"] != "gsm8k-subset":
            continue
        for item in gate["thresholds"]:
            if item["metric"] == "accuracy":
                default = item["value"]
                break
    if default is None:
        fail("policy is missing the gsm8k-subset accuracy threshold")
    override = policy["accuracy_floor_overrides"].get(model_id)
    if override is None:
        return default
    return override


def copied_thresholds(gate: dict[str, Any], *, accuracy_floor: str | None = None) -> list[dict[str, str]]:
    """Return a verbatim copy of gate thresholds, applying the GSM8K floor."""
    copied: list[dict[str, str]] = []
    for item in gate["thresholds"]:
        threshold = dict(item)
        if accuracy_floor is not None and threshold["metric"] == "accuracy":
            threshold["value"] = accuracy_floor
        copied.append(threshold)
    return copied
