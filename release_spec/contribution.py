"""Independent public qualification checks for a compact catalog contribution."""
from pathlib import Path, PurePosixPath
from typing import Any
from . import load_spec, verify_spec
from .baseline_policy import load_policy, applied_accuracy_floor
from .baseline_evaluate import evaluate, OPERATION_FILES
from .measurement import read_stable_bytes, sha256_bytes, load_measurement_bytes, parse_strict_json
from .run_record import verify_run_record, _time
from .schema import fail

APPROVED_POLICY_DIGEST = "0b79190daf6e03b81c4b847adf0895b6102daec575ac8d6b0fac712381085539"
POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "baseline-v1.json"


def verify_contribution(spec_path: str | Path, evidence_root: str | Path,
                        run_path: str | Path) -> dict[str, Any]:
    """Recompute every baseline judgement and verify the referenced public bytes.

    A successful check establishes document consistency, not physical execution
    or maintainer approval. Archive verification is an additional export gate.
    """
    spec = load_spec(spec_path)
    if spec["state"] != "released" or spec["review"]["status"] not in {"stable", "withdrawn"}:
        fail("catalog contribution must be stable or explicitly withdrawn; deep qualification is deferred")
    policy, policy_digest = load_policy(POLICY_PATH)
    if policy_digest != APPROVED_POLICY_DIGEST:
        fail("public baseline-v1 policy differs from the approved fixed policy")
    required_ids = set(OPERATION_FILES) | {"baseline-run"}
    evidence = {row["id"]: row for row in spec["evidence"]}
    if set(evidence) != required_ids:
        fail("contribution requires exactly six operation documents and baseline-run evidence")
    root = Path(evidence_root).absolute()
    data = {}
    paths = set()
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
    expected_run_path = root / evidence["baseline-run"]["path"]
    if Path(run_path).absolute() != expected_run_path:
        fail("--run must name the run file bound in spec evidence")
    run = verify_run_record(parse_strict_json(data["baseline-run"], label="baseline run"),
                            spec, policy_digest)
    if any(row["lab_commit"] != run["lab_commit"] for row in evidence.values()):
        fail("evidence lab revision differs from the qualification run")
    for operation in OPERATION_FILES:
        if run["measurement_sha256"][operation] != sha256_bytes(data[operation]):
            fail(f"run measurement digest differs from exported evidence: {operation}")
    documents = {operation: load_measurement_bytes(data[operation]) for operation in OPERATION_FILES}
    for operation, document in documents.items():
        if document["operation"] != operation:
            fail(f"evidence {operation} contains a different operation")
    # Soak is the only compact producer with a timestamped measurement window.
    # It must lie within its recorded invocation, rather than being borrowed
    # from another attempt that passed at a different time.
    soak = documents["validate-soak"]["validate-soak"]
    soak_gate = next(gate for gate in run["gates"] if gate["name"] == "validate-soak")
    if not (_time(soak_gate["started_at"], "soak invocation start") <= _time(soak["started_at"], "soak start")
            <= _time(soak["ended_at"], "soak end") <= _time(soak_gate["ended_at"], "soak invocation end")):
        fail("soak measurement lies outside its run window")
    # Re-evaluate facts with public policy, then compare the entire claims list:
    # this catches shortened suites, weakened thresholds, swapped evidence and
    # fabricated pass strings even when all documents are self-consistent JSON.
    recomputed, outcomes, proposed = evaluate(
        spec=spec, policy=policy, policy_digest=policy_digest, documents=documents,
        evidence_rows=spec["evidence"],
        accuracy_floor=applied_accuracy_floor(policy, spec["identity"]["model_id"]))
    if proposed != "stable" or any(outcome != "pass" for outcome in outcomes.values()):
        fail("all six baseline-v1 criteria must pass")
    if spec["measurements"] != recomputed["measurements"]:
        fail("recorded outcomes, thresholds or evidence references differ from independent evaluation")
    return {"kind": "pulsar-contribution-verification", "schema_version": 1,
            "spec_id": spec["spec_id"], "policy_digest": policy_digest,
            "lab_commit": run["lab_commit"], "stack_commit": run["stack_commit"],
            "gates": outcomes, "verified": True}
