#!/usr/bin/env python3
"""Evaluate closed measurements against the lab-wide baseline-v1 policy.

Fills a measured ADR 0017 spec's ``measurements[]`` and ``evidence[]`` and
prints a proposed ``stable`` or ``failed`` status. It does not write
``review``, start a server, or claim physical behavior.
"""

from __future__ import annotations

import argparse
import copy
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_VALIDATE_DIR = Path(__file__).resolve().parent
if str(_VALIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATE_DIR))

from . import ReleaseSpecError, load_spec, pretty_json_bytes, verify_spec  # noqa: E402
from .schema import (  # noqa: E402
    COMMIT_RE,
    require_relative_posix_ascii_path,
    screen_public_string,
)

from .baseline_policy import (  # noqa: E402
    BaselinePolicyError,
    applied_accuracy_floor,
    copied_thresholds,
    load_policy,
)
from .measurement import (  # noqa: E402
    ValidatorMeasurementError,
    ValidatorMeasurementMissing,
    atomic_write_bytes,
    canonical_decimal,
    load_measurement_bytes,
    read_stable_bytes,
    sha256_bytes,
)

OPERATION_FILES = {
    "verify-snapshot-manifest": "verify-snapshot-manifest.json",
    "serve-smoke": "serve-smoke.json",
    "compare-captures": "compare-captures.json",
    "evaluate-gsm8k": "evaluate-gsm8k.json",
    "validate-soak": "validate-soak.json",
    "benchmark-serving": "benchmark-serving.json",
}


class BaselineEvaluatorError(ValueError):
    """The evaluator cannot produce a measured spec from the given inputs."""


def fail(message: str) -> None:
    raise BaselineEvaluatorError(message)


def _evidence_path(prefix: str, filename: str) -> str:
    if prefix:
        if not prefix.endswith("/"):
            prefix = f"{prefix}/"
        text = f"{prefix}{filename}"
    else:
        text = filename
    require_relative_posix_ascii_path(text, path="evidence.path")
    screen_public_string(text, path="evidence.path")
    return text


def _require_lab_commit(value: str) -> str:
    if COMMIT_RE.fullmatch(value) is None:
        fail("--lab-commit must be a 40-character lowercase hex commit")
    return value


def _parse_measurement_override(value: str) -> tuple[str, str]:
    if "=" not in value:
        fail("--measurement must be <operation>=PATH")
    operation, path = value.split("=", 1)
    if operation not in OPERATION_FILES:
        fail(f"unsupported measurement operation {operation!r}")
    if not path:
        fail(f"--measurement {operation} path is empty")
    return operation, path


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return False
    try:
        parsed = Decimal(str(value))
    except Exception:
        return False
    return parsed.is_finite()


def _threshold_holds(observed: Any, operator: str, expected: str) -> bool:
    if operator == "==" and not _is_numeric(observed):
        return str(observed) == expected
    if not _is_numeric(observed):
        fail(f"observed metric {observed!r} is not numeric")
    left = Decimal(str(observed))
    right = Decimal(expected)
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "==":
        return left == right
    fail(f"unsupported operator {operator!r}")
    return False


def _all_thresholds_hold(
    observed: dict[str, Any], thresholds: list[dict[str, str]]
) -> bool:
    for item in thresholds:
        metric = item["metric"]
        if metric not in observed:
            fail(f"missing observed metric {metric!r}")
        if not _threshold_holds(observed[metric], item["operator"], item["value"]):
            return False
    return True


def _identity_observed(
    payload: dict[str, Any], spec: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if spec.get('schema_version') == 3:
        from .serving import required_snapshots
        expected = required_snapshots(spec)
        rows = payload.get('snapshots')
        if not isinstance(rows,dict) or set(rows) != set(expected):
            return {'unmatched_file_count':0}, False
        bound = payload.get('spec_id') == spec['spec_id']
        unmatched = 0
        for name, model in expected.items():
            row = rows[name]; manifest = model['snapshot_manifest']
            bound = bound and row['manifest_id'] == manifest['manifest_id'] and row['expected_file_count'] == manifest['file_count']
            unmatched += row['mismatched_file_count'] + row['missing_file_count'] + row['extra_file_count']
        return {'unmatched_file_count':unmatched}, bound
    if 'snapshots' in payload:
        return {'unmatched_file_count':0}, False
    unmatched = (
        payload["mismatched_file_count"]
        + payload["missing_file_count"]
        + payload["extra_file_count"]
    )
    from .serving import identity_fields
    manifest = identity_fields(spec)["snapshot_manifest"]
    bound = (
        payload["spec_id"] == spec["spec_id"]
        and payload["manifest_id"] == manifest["manifest_id"]
        and payload["expected_file_count"] == manifest["file_count"]
    )
    return {"unmatched_file_count": unmatched}, bound


def _smoke_observed(payload: dict[str, Any]) -> dict[str, Any]:
    def flag(phase: dict[str, Any]) -> int:
        return 1 if phase["completion"] == "complete" else 0

    return {
        "health_complete": flag(payload["health"]),
        "warmup_complete": flag(payload["warmup"]),
        "completion_smoke_complete": flag(payload["completion"]),
    }


def _compare_observed(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sample = payload["sample_count"]
    identical = payload["identical_record_count"]
    if sample < 1:
        rate = "0"
    else:
        rate = canonical_decimal(
            Decimal(identical) / Decimal(sample),
            label="exact_match_rate",
            require_canonical=False,
        )
    extra_ok = payload["diagnostic_verdict"] == "identical"
    return {"exact_match_rate": rate}, extra_ok


def _gsm8k_observed(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "accuracy": payload["accuracy"],
        "measured_sample_count": payload["measured_sample_count"],
        "dataset_id": payload["dataset_id"],
        "dataset_revision": payload["dataset_revision"],
        "dataset_file_sha256": payload["dataset_file_sha256"],
    }


def _gsm8k_pins_match(payload: dict[str, Any], pins: dict[str, Any]) -> bool:
    for key, expected in pins.items():
        if payload.get(key) != expected:
            return False
    return True


def _soak_observed(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration_seconds": payload["duration_seconds"],
        "request_error_count": payload["request_error_count"],
        "completed_requests": payload["completed_requests"],
    }


def _perf_observed(payload: dict[str, Any], required: list[int]) -> dict[str, Any]:
    complete = {
        item["concurrency"]: item["completion"] == "complete"
        for item in payload["levels"]
    }
    ok = all(complete.get(concurrency) is True for concurrency in required)
    return {"required_levels_complete": 1 if ok else 0}


def _judge_gate(
    gate: dict[str, Any],
    document: dict[str, Any] | None,
    *,
    spec: dict[str, Any],
    accuracy_floor: str,
) -> str:
    if document is None:
        return "incomplete"
    if document["operation"] != gate["operation"]:
        fail(
            f"measurement operation {document['operation']!r} does not match "
            f"gate {gate['operation']!r}"
        )
    if document["completion"] != "complete":
        return "incomplete"
    thresholds = copied_thresholds(
        gate,
        accuracy_floor=accuracy_floor
        if gate["criterion_id"] == "gsm8k-subset"
        else None,
    )
    operation = gate["operation"]
    payload = document[operation]
    extra_ok = True
    if operation == "verify-snapshot-manifest":
        observed, extra_ok = _identity_observed(payload, spec)
    elif operation == "serve-smoke":
        observed = _smoke_observed(payload)
    elif operation == "compare-captures":
        observed, extra_ok = _compare_observed(payload)
    elif operation == "evaluate-gsm8k":
        observed = _gsm8k_observed(payload)
        extra_ok = _gsm8k_pins_match(payload, gate["pins"])
    elif operation == "validate-soak":
        observed = _soak_observed(payload)
    else:
        observed = _perf_observed(payload, gate["required_concurrencies"])
    if extra_ok and _all_thresholds_hold(observed, thresholds):
        return "pass"
    return "fail"


def evaluate(
    *,
    spec: dict[str, Any],
    policy: dict[str, Any],
    policy_digest: str,
    documents: dict[str, dict[str, Any] | None],
    evidence_rows: list[dict[str, str]],
    accuracy_floor: str,
) -> tuple[dict[str, Any], dict[str, str], str]:
    measurements: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    for gate in policy["gates"]:
        operation = gate["operation"]
        document = documents.get(operation)
        outcome = _judge_gate(
            gate,
            document,
            spec=spec,
            accuracy_floor=accuracy_floor,
        )
        outcomes[gate["criterion_id"]] = outcome
        evidence_ids = [operation] if document is not None else []
        measurements.append(
            {
                "criterion_id": gate["criterion_id"],
                "suite": "baseline-v1",
                "policy_digest": policy_digest,
                "thresholds": copied_thresholds(
                    gate,
                    accuracy_floor=accuracy_floor
                    if gate["criterion_id"] == "gsm8k-subset"
                    else None,
                ),
                "outcome": outcome,
                "evidence_ids": evidence_ids,
            }
        )
    measurements.sort(key=lambda item: item["criterion_id"])
    evidence = sorted(evidence_rows, key=lambda item: item["id"])
    filled = copy.deepcopy(spec)
    filled["measurements"] = measurements
    filled["evidence"] = evidence
    verified = verify_spec(filled)
    proposed = (
        "stable"
        if all(outcome == "pass" for outcome in outcomes.values())
        else "failed"
    )
    return verified, outcomes, proposed


def _load_input_spec(path: str) -> dict[str, Any]:
    spec = load_spec(path)
    if spec["state"] != "measured":
        fail("input spec state must be measured")
    if spec["review"] != {}:
        fail("input spec review must be an empty object")
    if spec["measurements"] != []:
        fail("input spec measurements must be empty")
    if spec["evidence"] != []:
        fail("input spec evidence must be empty")
    return spec


def _resolve_measurement_path(
    *,
    measurements_dir: Path,
    operation: str,
    overrides: dict[str, str],
) -> Path:
    if operation in overrides:
        return Path(overrides[operation])
    return measurements_dir / OPERATION_FILES[operation]


def _load_documents(
    *,
    measurements_dir: Path,
    overrides: dict[str, str],
    lab_commit: str,
    evidence_prefix: str,
) -> tuple[dict[str, dict[str, Any] | None], list[dict[str, str]]]:
    if not measurements_dir.is_dir():
        fail(f"measurements directory is not a directory: {measurements_dir}")
    documents: dict[str, dict[str, Any] | None] = {}
    evidence_rows: list[dict[str, str]] = []
    for operation, filename in OPERATION_FILES.items():
        path = _resolve_measurement_path(
            measurements_dir=measurements_dir,
            operation=operation,
            overrides=overrides,
        )
        if not path.exists():
            documents[operation] = None
            continue
        try:
            raw = read_stable_bytes(path, label="validator measurement")
        except ValidatorMeasurementMissing as exc:
            fail(str(exc))
        except ValidatorMeasurementError as exc:
            fail(str(exc))
        document = load_measurement_bytes(raw)
        if document["operation"] != operation:
            fail(
                f"{filename} operation is {document['operation']!r}, "
                f"expected {operation!r}"
            )
        documents[operation] = document
        evidence_rows.append(
            {
                "id": operation,
                "lab_commit": lab_commit,
                "path": _evidence_path(evidence_prefix, filename),
                "sha256": sha256_bytes(raw),
            }
        )
    return documents, evidence_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--measurements-dir", required=True)
    parser.add_argument("--lab-commit", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--evidence-path-prefix", default="")
    parser.add_argument(
        "--measurement",
        action="append",
        default=[],
        help="override one operation file as <operation>=PATH",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        lab_commit = _require_lab_commit(args.lab_commit)
        spec = _load_input_spec(args.spec)
        policy, policy_digest = load_policy(args.policy)
        overrides = dict(
            _parse_measurement_override(item) for item in args.measurement
        )
        documents, evidence_rows = _load_documents(
            measurements_dir=Path(args.measurements_dir),
            overrides=overrides,
            lab_commit=lab_commit,
            evidence_prefix=args.evidence_path_prefix,
        )
        accuracy_floor = applied_accuracy_floor(
            policy, spec["identity"]["model_id"]
        )
        filled, outcomes, proposed = evaluate(
            spec=spec,
            policy=policy,
            policy_digest=policy_digest,
            documents=documents,
            evidence_rows=evidence_rows,
            accuracy_floor=accuracy_floor,
        )
        atomic_write_bytes(args.out, pretty_json_bytes(filled))
    except (
        BaselineEvaluatorError,
        BaselinePolicyError,
        ReleaseSpecError,
        ValidatorMeasurementError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"policy_digest={policy_digest}")
    for gate in policy["gates"]:
        print(f"{gate['criterion_id']} {outcomes[gate['criterion_id']]}")
    print(f"proposed_status={proposed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
