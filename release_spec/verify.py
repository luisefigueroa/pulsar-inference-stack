"""Verifier for the closed ADR 0017 release spec document.

This module is standard-library-only and imports nothing from ``scripts/``.
It recomputes ``spec_id`` with ``ensure_ascii=False`` and nested snapshot
``manifest_id`` without ``ensure_ascii=False``. ASCII snapshot paths make
the two encodings agree. It does not judge thresholds and does not read
policy or platform files.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta
from typing import Any

from .identity import argv_from_identity,  canonical_identity, spec_id_for
from .schema import (
    BASELINE_KEYS,
    EVIDENCE_KEYS,
    KIND,
    LAUNCH_CONTRACT_KEYS,
    MEASUREMENT_KEYS,
    MEASUREMENT_OUTCOMES,
    MEASUREMENT_SUITES,
    REVIEW_KEYS,
    REVIEW_STATUSES,
    SCHEMA_VERSION,
    THRESHOLD_KEYS,
    THRESHOLD_OPERATORS,
    TOP_LEVEL_KEYS,
    fail,
    require_commit,
    require_nonempty_string,
    require_object,
    require_public_string,
    require_relative_posix_ascii_path,
    require_sha256_hex,
    screen_public_string,
)


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


def _reject_json_float(value: str) -> None:
    fail("document must not contain JSON floats")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            fail(f"document contains duplicate key {key!r}")
        document[key] = value
    return document


def _require_iso8601_utc_z(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or "T" not in value:
        fail(f"{path} must be ISO-8601 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        fail(f"{path} must be ISO-8601 UTC ending in Z")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        fail(f"{path} must be ISO-8601 UTC ending in Z")
    return value


def _verify_thresholds(value: Any, *, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        fail(f"{path} must be a list")
    thresholds: list[dict[str, str]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_object(item, THRESHOLD_KEYS, path=item_path)
        operator = item.get("operator")
        if operator not in THRESHOLD_OPERATORS:
            fail(f"{item_path}.operator is not a supported comparison operator")
        thresholds.append(
            {
                "metric": require_nonempty_string(
                    item.get("metric"),
                    path=f"{item_path}.metric",
                ),
                "operator": operator,
                "value": require_nonempty_string(
                    item.get("value"),
                    path=f"{item_path}.value",
                ),
                "unit": require_nonempty_string(
                    item.get("unit"),
                    path=f"{item_path}.unit",
                ),
            }
        )
    return thresholds


def _verify_evidence(value: Any, *, path: str) -> tuple[list[dict[str, str]], set[str]]:
    if not isinstance(value, list):
        fail(f"{path} must be a list")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_object(item, EVIDENCE_KEYS, path=item_path)
        evidence_id = require_nonempty_string(item.get("id"), path=f"{item_path}.id")
        if evidence_id in seen:
            fail(f"{item_path}.id duplicates {evidence_id!r}")
        seen.add(evidence_id)
        evidence_path = require_relative_posix_ascii_path(
            item.get("path"),
            path=f"{item_path}.path",
        )
        screen_public_string(evidence_path, path=f"{item_path}.path")
        entries.append(
            {
                "id": evidence_id,
                "lab_commit": require_commit(
                    item.get("lab_commit"),
                    path=f"{item_path}.lab_commit",
                ),
                "path": evidence_path,
                "sha256": require_sha256_hex(
                    item.get("sha256"),
                    path=f"{item_path}.sha256",
                ),
            }
        )
    return entries, seen


def _verify_measurements(
    value: Any,
    *,
    evidence_ids: set[str],
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        fail(f"{path} must be a list")
    measurements: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_object(item, MEASUREMENT_KEYS, path=item_path)
        suite = item.get("suite")
        if suite not in MEASUREMENT_SUITES:
            fail(f"{item_path}.suite is not a supported suite")
        policy_digest = item.get("policy_digest")
        if suite == "baseline-v1":
            policy_digest = require_sha256_hex(
                policy_digest,
                path=f"{item_path}.policy_digest",
            )
        elif policy_digest is not None:
            fail(f"{item_path}.policy_digest must be null when suite is {suite!r}")
        thresholds = _verify_thresholds(
            item.get("thresholds"),
            path=f"{item_path}.thresholds",
        )
        if suite == "deep" and not thresholds:
            fail(f"{item_path}.thresholds must be non-empty when suite is 'deep'")
        outcome = item.get("outcome")
        if outcome not in MEASUREMENT_OUTCOMES:
            fail(f"{item_path}.outcome is not a supported outcome")
        raw_ids = item.get("evidence_ids")
        if not isinstance(raw_ids, list):
            fail(f"{item_path}.evidence_ids must be a list")
        named: list[str] = []
        for id_index, evidence_id in enumerate(raw_ids):
            id_path = f"{item_path}.evidence_ids[{id_index}]"
            named_id = require_nonempty_string(evidence_id, path=id_path)
            if named_id not in evidence_ids:
                fail(f"{id_path} does not name an evidence entry")
            named.append(named_id)
        measurements.append(
            {
                "criterion_id": require_nonempty_string(
                    item.get("criterion_id"),
                    path=f"{item_path}.criterion_id",
                ),
                "suite": suite,
                "policy_digest": policy_digest,
                "thresholds": thresholds,
                "outcome": outcome,
                "evidence_ids": named,
            }
        )
    return measurements


def _verify_baselines(value: Any, *, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        fail(f"{path} must be a list")
    baselines: list[dict[str, str]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_object(item, BASELINE_KEYS, path=item_path)
        baselines.append(
            {
                field: require_public_string(
                    item.get(field),
                    path=f"{item_path}.{field}",
                )
                for field in (
                    "claim",
                    "source",
                    "metric",
                    "claimed",
                    "measured",
                    "unit",
                )
            }
        )
    return baselines


def _verify_launch_contract(value: Any, *, path: str) -> dict[str, Any]:
    require_object(value, LAUNCH_CONTRACT_KEYS, path=path)
    argv_raw = value.get("argv")
    if not isinstance(argv_raw, list):
        fail(f"{path}.argv must be a list of public strings")
    argv: list[str] = []
    for index, item in enumerate(argv_raw):
        argv.append(
            require_public_string(item, path=f"{path}.argv[{index}]")
        )
    return {
        "stack_version": require_public_string(
            value.get("stack_version"),
            path=f"{path}.stack_version",
        ),
        "argv": argv,
    }


def _verify_review(value: Any, *, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        fail(f"{path} must be null or an object")
    if value == {}:
        return {}
    status = value.get("status")
    keys = REVIEW_KEYS | {"reason"} if status == "withdrawn" else REVIEW_KEYS
    require_object(value, keys, path=path)
    if status not in REVIEW_STATUSES:
        fail(f"{path}.status is not a supported review status")
    review = {
        "status": status,
        "reviewer": require_public_string(value.get("reviewer"), path=f"{path}.reviewer"),
        "reviewed_at": _require_iso8601_utc_z(value.get("reviewed_at"), path=f"{path}.reviewed_at"),
    }
    if status == "withdrawn":
        review["reason"] = require_public_string(value.get("reason"), path=f"{path}.reason")
    return review


def _require_status_evidence(
    review: dict[str, Any] | None,
    measurements: list[dict[str, Any]],
) -> None:
    """Couple passing review statuses to passing measurement suites.

    ADR 0017 decision 4: ``stable`` means the spec passed baseline-v1 and
    ``validated`` means it also passed the deep suite. This checks recorded
    outcomes only; it does not judge thresholds or read the policy file.
    """
    status = (review or {}).get("status")
    if status not in {"stable", "validated"}:
        return
    baseline = [item for item in measurements if item["suite"] == "baseline-v1"]
    if not baseline:
        fail(f"review.status {status!r} requires baseline-v1 measurements")
    if any(item["outcome"] != "pass" for item in baseline):
        fail(f"review.status {status!r} requires every baseline-v1 outcome to be 'pass'")
    if len({item["policy_digest"] for item in baseline}) != 1:
        fail(f"review.status {status!r} requires one baseline-v1 policy_digest")
    if status == "validated":
        deep = [item for item in measurements if item["suite"] == "deep"]
        if not deep:
            fail("review.status 'validated' requires deep measurements")
        if any(item["outcome"] != "pass" for item in deep):
            fail("review.status 'validated' requires every deep outcome to be 'pass'")


def verify_spec(document: Any) -> dict[str, Any]:
    """Return the canonical document or raise ``ReleaseSpecError``."""
    if not isinstance(document, dict):
        fail("document must be an object")
    _reject_floats(document, path="")
    require_object(document, TOP_LEVEL_KEYS, path="document")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        fail("document.schema_version must be 1")
    if document.get("kind") != KIND:
        fail(f"document.kind must be {KIND!r}")
    state = document.get("state")
    if state not in {None, "measured", "released"}:
        fail("document.state must be null, 'measured', or 'released'")
    spec_id = require_sha256_hex(document.get("spec_id"), path="spec_id")
    identity = canonical_identity(document.get("identity"), path="identity")
    evidence, evidence_ids = _verify_evidence(
        document.get("evidence"),
        path="evidence",
    )
    measurements = _verify_measurements(
        document.get("measurements"),
        evidence_ids=evidence_ids,
        path="measurements",
    )
    baselines = _verify_baselines(document.get("baselines"), path="baselines")
    launch_contract = _verify_launch_contract(
        document.get("launch_contract"),
        path="launch_contract",
    )
    review = _verify_review(document.get("review"), path="review")
    _require_status_evidence(review, measurements)
    computed = spec_id_for(identity)
    if spec_id != computed:
        fail("spec_id does not match the identity block")
    if launch_contract["argv"] != argv_from_identity(identity):
        fail("launch_contract.argv does not match the identity's engine_args and geometry")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "spec_id": computed,
        "state": state,
        "identity": identity,
        "launch_contract": launch_contract,
        "measurements": measurements,
        "baselines": baselines,
        "evidence": evidence,
        "review": review,
    }


def load_spec(path: str | pathlib.Path) -> dict[str, Any]:
    """Load a JSON file, verify it, and return the canonical document."""
    spec_path = pathlib.Path(path)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read {spec_path}: {exc}")
    try:
        document = json.loads(
            text,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON: {exc}")
    except TypeError as exc:
        fail(f"invalid JSON: {exc}")
    return verify_spec(document)
