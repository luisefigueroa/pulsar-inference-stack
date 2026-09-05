"""Public compact qualification run record, verified without private captures.

This checks consistency of published observations. It cannot independently prove
that a maintainer performed the physical measurements; repository review does.
"""
from datetime import datetime
import re
from typing import Any
from . import runtime_contract_id, verify_spec
from .schema import fail, require_object, require_commit, require_sha256_hex

GATE_NAMES = ("verify-snapshot-manifest", "serve-smoke", "run-gates",
              "evaluate-gsm8k", "validate-soak")
RUN_KEYS = frozenset({"schema_version", "kind", "spec_id", "policy_digest",
                     "lab_commit", "stack_commit", "image_digest", "launch_contract_id",
                     "snapshot_manifest_id", "ranks_before", "ranks_after", "gates",
                     "proposed_status", "observation_complete", "same_boot", "measurement_sha256"})
RANK_KEYS = frozenset({"rank", "running", "owned", "image_digest", "launch_contract_id",
                      "boot_witness", "snapshot_manifest_id", "files_verified"})


def _time(value: Any, label: str) -> datetime:
    """Parse UTC ISO timestamps without truncating producer microseconds."""
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
    ) is None:
        fail(f"{label} must be an ISO timestamp in UTC with at most microsecond precision")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        fail(f"{label} is not a valid UTC timestamp")


def verify_run_record(document: Any, spec: dict, policy_digest: str, *,
                      require_success: bool = True) -> dict:
    """Validate a run against its frozen spec; success requires every rank/gate."""
    spec = verify_spec(spec)
    require_object(document, RUN_KEYS, path="run")
    if type(document["schema_version"]) is not int or document["schema_version"] != 2:
        fail("run.schema_version must be 2")
    if document["kind"] != "pulsar-baseline-run":
        fail("run.kind must be pulsar-baseline-run")
    expected = {"spec_id": spec["spec_id"], "policy_digest": policy_digest,
                "image_digest": spec["identity"]["image"]["digest"],
                "launch_contract_id": runtime_contract_id(spec),
                "snapshot_manifest_id": spec["identity"]["snapshot_manifest"]["manifest_id"]}
    for key, value in expected.items():
        if document[key] != value:
            fail(f"run.{key} does not match the selected spec/policy")
    require_commit(document["lab_commit"], path="run.lab_commit")
    require_commit(document["stack_commit"], path="run.stack_commit")
    if document["stack_commit"] != spec["launch_contract"]["stack_version"]:
        fail("run.stack_commit differs from frozen stack_version")
    for key in ("observation_complete", "same_boot"):
        if type(document[key]) is not bool:
            fail(f"run.{key} must be boolean")
    if document["proposed_status"] not in (None, "stable", "failed"):
        fail("run.proposed_status is invalid")
    hashes = document["measurement_sha256"]
    operations = {"verify-snapshot-manifest", "serve-smoke", "compare-captures",
                  "benchmark-serving", "evaluate-gsm8k", "validate-soak"}
    if not isinstance(hashes, dict) or set(hashes) - operations:
        fail("run.measurement_sha256 must map known operations to hashes")
    for operation, digest in hashes.items():
        require_sha256_hex(digest, path=f"run.measurement_sha256.{operation}")
    if require_success and set(hashes) != operations:
        fail("successful run must record all six measurement hashes")
    nodes = spec["identity"]["geometry"]["nodes"]
    complete = True
    for side in ("ranks_before", "ranks_after"):
        ranks = document[side]
        if not isinstance(ranks, list):
            fail(f"run.{side} must be a list")
        if ranks and len(ranks) != nodes:
            fail(f"run.{side} must name every expected rank")
        if not ranks:
            complete = False
        for index, rank in enumerate(ranks):
            require_object(rank, RANK_KEYS, path=f"run.{side}[{index}]")
            if type(rank["rank"]) is not int or rank["rank"] != index:
                fail(f"run.{side} ranks must be exactly 0 through nodes-1")
            for key in ("running", "owned", "files_verified"):
                if rank[key] is not True:
                    fail(f"run.{side} rank {index} is not {key}")
            require_sha256_hex(rank["boot_witness"], path=f"run.{side}.boot_witness")
            for key in ("image_digest", "launch_contract_id", "snapshot_manifest_id"):
                if rank[key] != expected[key]:
                    fail(f"run.{side} rank {index} {key} differs from spec")
    same_boot = complete and document["ranks_before"] == document["ranks_after"]
    if document["same_boot"] != same_boot or document["observation_complete"] != complete:
        fail("run observation flags disagree with rank observations")
    gates = document["gates"]
    if not isinstance(gates, list):
        fail("run.gates must be a list")
    names = []
    previous_end = None
    for gate in gates:
        require_object(gate, {"name", "started_at", "ended_at", "rc"}, path="run.gate")
        name = gate["name"]
        names.append(name)
        start, end = _time(gate["started_at"], "gate start"), _time(gate["ended_at"], "gate end")
        if end < start or (previous_end and start < previous_end):
            fail("run gate windows overlap or go backwards")
        previous_end = end
        if type(gate["rc"]) is not int or not 0 <= gate["rc"] <= 255:
            fail("run gate rc must be an exit code")
    if names != list(GATE_NAMES[:len(names)]):
        fail("run gates must be unique in producer order")
    successful = (complete and same_boot and names == list(GATE_NAMES)
                  and all(gate["rc"] == 0 for gate in gates))
    if require_success and (not successful or document["proposed_status"] != "stable"):
        fail("qualification requires a successful complete same-boot all-rank run")
    if document["proposed_status"] == "stable" and not successful:
        fail("run cannot propose stable after incomplete or failed producers")
    return document
