"""Independent public checks for compact evidence and catalog membership."""
import copy
from pathlib import Path, PurePosixPath
from typing import Any
from . import load_spec
from .baseline_policy import load_policy, applied_accuracy_floor
from .baseline_evaluate import evaluate, OPERATION_FILES
from .measurement import read_stable_bytes, sha256_bytes, load_measurement_bytes, parse_strict_json
from .run_record import verify_run_record, _time
from .schema import fail

APPROVED_POLICY_DIGEST = "0b79190daf6e03b81c4b847adf0895b6102daec575ac8d6b0fac712381085539"
POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "baseline-v1.json"
CLAIM_STATUSES = frozenset({"stable", "validated"})


def _policy():
    policy, policy_digest = load_policy(POLICY_PATH)
    if policy_digest != APPROVED_POLICY_DIGEST:
        fail("public baseline-v1 policy differs from the approved fixed policy")
    return policy, policy_digest


def _bind_declared_evidence(spec: dict[str, Any], evidence_root: str | Path) -> dict[str, bytes]:
    evidence = {row["id"]: row for row in spec["evidence"]}
    root = Path(evidence_root).absolute()
    data: dict[str, bytes] = {}
    paths: set[str] = set()
    for key, row in evidence.items():
        relative = PurePosixPath(row["path"])
        if relative.parts[0] != "results" or len(relative.parts) < 3:
            fail("public evidence must be below results/<attempt>/")
        if row["path"] in paths:
            fail("evidence paths must be unique")
        paths.add(row["path"])
        raw = read_stable_bytes(root / row["path"], label="contribution evidence")
        if sha256_bytes(raw) != row["sha256"]:
            fail(f"evidence digest mismatch: {key}")
        data[key] = raw
    return data


def verify_compact_evidence(spec_path: str | Path, evidence_root: str | Path,
                            run_path: str | Path, *, require_pass: bool = True) -> dict[str, Any]:
    """Recompute baseline-v1 judgements and bind the six operation documents.

    Does not inspect spec state or review.status. ``require_pass`` is the
    six-gate success check used when a spec claims ``stable`` or ``validated``.
    """
    spec = load_spec(spec_path)
    if spec['schema_version'] == 2:
        from .evidence_v2 import verify_evidence
        result=verify_evidence(spec_path,run_path,evidence_root)
        if require_pass and result['outcome']!='pass':
            fail('all six baseline-v1 criteria and unchanged observations must pass')
        return result
    policy, policy_digest = _policy()
    required_ids = set(OPERATION_FILES) | {"baseline-run"}
    evidence = {row["id"]: row for row in spec["evidence"]}
    if set(evidence) != required_ids:
        fail("contribution requires exactly six operation documents and baseline-run evidence")
    data = _bind_declared_evidence(spec, evidence_root)
    expected_run_path = Path(evidence_root).absolute() / evidence["baseline-run"]["path"]
    if Path(run_path).absolute() != expected_run_path:
        fail("--run must name the run file bound in spec evidence")
    run = verify_run_record(
        parse_strict_json(data["baseline-run"], label="baseline run"),
        spec, policy_digest, require_success=require_pass)
    if any(row["lab_commit"] != run["lab_commit"] for row in spec["evidence"]):
        fail("evidence lab revision differs from the qualification run")
    for operation in OPERATION_FILES:
        if run["measurement_sha256"].get(operation) != sha256_bytes(data[operation]):
            fail(f"run measurement digest differs from exported evidence: {operation}")
    documents = {operation: load_measurement_bytes(data[operation]) for operation in OPERATION_FILES}
    for operation, document in documents.items():
        if document["operation"] != operation:
            fail(f"evidence {operation} contains a different operation")
    soak = documents["validate-soak"]["validate-soak"]
    soak_gate = next(gate for gate in run["gates"] if gate["name"] == "validate-soak")
    if not (_time(soak_gate["started_at"], "soak invocation start") <= _time(soak["started_at"], "soak start")
            <= _time(soak["ended_at"], "soak end") <= _time(soak_gate["ended_at"], "soak invocation end")):
        fail("soak measurement lies outside its run window")
    evaluation_input = copy.deepcopy(spec)
    evaluation_input["review"] = None
    recomputed, outcomes, proposed = evaluate(
        spec=evaluation_input, policy=policy, policy_digest=policy_digest, documents=documents,
        evidence_rows=spec["evidence"],
        accuracy_floor=applied_accuracy_floor(policy, spec["identity"]["model_id"]))
    recorded_baseline = [
        row for row in spec["measurements"] if row["suite"] == "baseline-v1"
    ]
    if recorded_baseline != recomputed["measurements"]:
        fail("recorded outcomes, thresholds or evidence references differ from independent evaluation")
    if require_pass and (proposed != "stable" or any(outcome != "pass" for outcome in outcomes.values())):
        fail("all six baseline-v1 criteria must pass")
    other_suites = sorted({
        row["suite"] for row in spec["measurements"] if row["suite"] != "baseline-v1"
    })
    return {"kind": "pulsar-contribution-verification", "schema_version": 1,
            "spec_id": spec["spec_id"], "policy_digest": policy_digest,
            "lab_commit": run["lab_commit"], "stack_commit": run["stack_commit"],
            "gates": outcomes, "verified_suite": "baseline-v1",
            "unverified_suites": other_suites, "verified": True}


def verify_contribution(spec_path: str | Path, evidence_root: str | Path | None = None,
                        run_path: str | Path | None = None) -> dict[str, Any]:
    """Validate the spec document selected for maintainer publication.

    Catalog membership is established by the maintainer publishing a
    schema-valid document under ``releases/``. State, review, evidence, and
    launch compatibility are independent metadata or diagnostics; they never
    authorize or block catalog membership here. ``evidence_root`` and
    ``run_path`` remain accepted for callers using the former interface.
    """
    spec = load_spec(spec_path)
    return {"kind": "pulsar-contribution-verification", "schema_version": 1,
            "spec_id": spec["spec_id"], "state": spec["state"],
            "review": spec["review"], "schema_valid": True,
            "evidence_verified": False, "launch_compatible": None,
            "verified": True}
