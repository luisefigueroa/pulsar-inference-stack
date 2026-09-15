"""Model-free checks for explicit memory estimates and unchanged admission gates."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from release_spec import memory_estimate as estimates, serving
from release_spec.normalize import canonical_json_digest, snapshot_manifest_id
from scripts import container_runtime as runtime
from tests import test_container_runtime as fixtures
from tests import test_diagnostics as diagnostics

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


def document(spec, weights=None):
    weights = weights or [99] * spec["recipe"]["geometry"]["nodes"]
    return {"schema_version": 1, "kind": "pulsar-memory-estimate",
            "spec_id": spec["spec_id"], "basis": "Synthetic resident-weight estimate; not a measurement.",
            "ranks": [{"rank": rank, "resident_weights_bytes": int(value * GIB)}
                      for rank, value in enumerate(weights)]}


class MemoryEstimateDocuments(unittest.TestCase):
    def test_complete_identity_and_rank_binding(self):
        spec, *_ = fixtures.fixture(3)
        original = document(spec)
        for case in ("spec", "missing", "duplicate", "order", "extra", "negative", "zero", "float", "bool", "huge", "schema", "basis"):
            value = copy.deepcopy(original)
            if case == "spec": value["spec_id"] = "f" * 64
            elif case == "missing": value["ranks"].pop()
            elif case == "duplicate": value["ranks"][1]["rank"] = 0
            elif case == "order": value["ranks"].reverse()
            elif case == "extra": value["ranks"][0]["unused"] = 1
            elif case == "schema": value["schema_version"] = True
            elif case == "basis": value["basis"] = "bad\nbasis"
            else:
                value["ranks"][0]["resident_weights_bytes"] = {
                    "negative": -1, "zero": 0, "float": 1.5, "bool": True, "huge": 2**63,
                }[case]
            with self.subTest(case=case), self.assertRaises(ValueError):
                estimates.freeze(value, spec)
        frozen = estimates.freeze(original, spec)
        self.assertEqual(estimates.weights_gib(frozen), [99, 99, 99])
        self.assertEqual(frozen, estimates.validate_frozen(frozen, spec))

    def test_strict_file_and_frozen_mutation_checks(self):
        spec, *_ = fixtures.fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "estimate.json"
            value = document(spec)
            path.write_text(json.dumps(value))
            frozen = estimates.load(path, spec)
            # The selected source is snapshotted; later edits cannot change it.
            value["ranks"][0]["resident_weights_bytes"] = 90 * GIB
            path.write_text(json.dumps(value))
            self.assertEqual(estimates.weights_gib(frozen), [99])
            with self.assertRaisesRegex(ValueError, "changed"):
                estimates.load(path, spec, expected_id=frozen["estimate_id"])
            changed = copy.deepcopy(frozen)
            changed["estimate"]["ranks"][0]["resident_weights_bytes"] = 1
            with self.assertRaisesRegex(ValueError, "changed"):
                estimates.validate_frozen(changed, spec)
            link = Path(temp) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError): estimates.load(link, spec)
            for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
                        b" " * (estimates.MAX_INPUT_BYTES + 1)):
                with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                    estimates.parse(raw)

    def test_plans_keep_old_formats_and_bind_estimate_content(self):
        for count, speculative in ((1, False), (3, False), (3, True)):
            with self.subTest(nodes=count, speculative=speculative):
                spec, facts, prepared, old, containers, images = fixtures.fixture(count, speculative)
                self.assertEqual(old["schema_version"], spec["schema_version"] + 1)
                self.assertNotIn("memory_estimate", old)
                frozen = estimates.freeze(document(spec), spec)
                facts["memory_estimate"] = frozen
                plan = runtime.build_plan(spec, spec["spec_id"], facts, prepared)
                self.assertEqual(plan["schema_version"], 5)
                self.assertEqual(plan["spec_id"], old["spec_id"])
                self.assertEqual(plan["service_id"], old["service_id"])
                self.assertNotEqual(plan["plan_id"], old["plan_id"])
                self.assertEqual(plan["memory_estimate"], frozen)
                runtime.validate_plan(plan)
                for rank in range(count):
                    containers[rank]["Config"]["Labels"] = runtime.rank_spec(plan, rank)["labels"]
                    runtime.observe_rank(plan, rank, containers[rank], images[rank])
                changed = copy.deepcopy(plan)
                changed["memory_estimate"]["estimate"]["basis"] = "mutated"
                changed["plan_id"] = canonical_json_digest({k: v for k, v in changed.items() if k != "plan_id"})
                with self.assertRaises(ValueError): runtime.validate_plan(changed)
                changed = copy.deepcopy(plan); changed["schema_version"] = old["schema_version"]
                changed["plan_id"] = canonical_json_digest({k: v for k, v in changed.items() if k != "plan_id"})
                with self.assertRaises(ValueError): runtime.validate_plan(changed)
                changed = copy.deepcopy(old); changed["schema_version"] = 5
                changed["plan_id"] = canonical_json_digest({k: v for k, v in changed.items() if k != "plan_id"})
                with self.assertRaises(ValueError): runtime.validate_plan(changed)


class MemoryEstimateAdmission(unittest.TestCase):
    def setUp(self):
        self.base = diagnostics.Diagnostics()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.root, self.env = self.base.root, self.base.env
        spec = copy.deepcopy(self.base.spec)
        manifest = spec["recipe"]["model"]["snapshot_manifest"]
        manifest["files"] = [{"path": "weights.safetensors", "size": 510313353565, "sha256": "c" * 64}]
        manifest.update(file_count=1, total_bytes=510313353565)
        manifest["manifest_id"] = snapshot_manifest_id(manifest)
        args = spec["recipe"]["engine_args"]
        if "--kv-cache-memory-bytes" in args:
            args[args.index("--kv-cache-memory-bytes") + 1] = str(2 * GIB)
        else:
            args += ["--kv-cache-memory-bytes", str(2 * GIB)]
        spec["spec_id"] = serving.spec_id(spec["recipe"])
        self.spec = serving.verify_spec(spec)
        self.base.path.write_text(json.dumps(self.spec))
        self.path = self.root / "estimate.json"
        self.path.write_text(json.dumps(document(self.spec)))
        with Path(self.env["BASH_ENV"]).open("a") as out:
            out.write("""
mem_available_gib_local() { echo "${TEST_MEM_0:-120}"; }
mem_available_gib_remote() {
  case "$1" in alias-1) echo "${TEST_MEM_1:-120}";; *) echo "${TEST_MEM_2:-120}";; esac
}
""")

    def check(self, weights=None, extra=None, flags=()):
        if weights is not None:
            self.path.write_text(json.dumps(document(self.spec, weights)))
        return self.base.run_tool("check-memory.sh", [
            self.spec["spec_id"], "--memory-estimate-file", str(self.path), "--json", *flags,
        ], extra=extra)

    def test_default_remains_snapshot_size_and_ambient_input_is_ignored(self):
        frozen = estimates.freeze(document(self.spec), self.spec)
        result = self.base.run_tool("check-memory.sh", [self.spec["spec_id"], "--json"],
                                   extra={"PULSAR_MEMORY_ESTIMATE_JSON": json.dumps(frozen)})
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["weights_gib_total"], 476)
        self.assertEqual(value["footprint_gib"], 170.67)
        self.assertNotIn("memory_estimate_id", value)

    def test_all_thresholds_and_fixed_kv_are_preserved(self):
        for available, code, grade in ((120, 0, "pass"), (119, 0, "pass"),
                                       (118.99, 2, "warn"), (114.12, 2, "warn"),
                                       (102.11, 1, "fail"), (3.99, 1, "fail")):
            result = self.check(extra={"TEST_MEM_0": str(available)})
            with self.subTest(available=available):
                self.assertEqual(result.returncode, code, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual(value["result"], grade)
                self.assertEqual(value["footprint_gib"], 111)
                self.assertEqual(value["need_start_gib"], 114)
                self.assertEqual(value["buffer_gib"], 8)
                self.assertEqual(value["hard_floor_gib"], 4)
                self.assertTrue(value["kv_fixed"])
                self.assertEqual(value["kv_gib"], 2)
        value = json.loads(self.check(flags=("--max-model-len", "1")).stdout)
        self.assertEqual(value["footprint_gib"], 111)
        self.assertFalse((self.root / "mutations").exists())

    def test_decisions_use_each_rank_not_the_maximum(self):
        result = self.check([99, 98, 120], extra={"TEST_MEM_2": "110"})
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual([r["footprint_gib"] for r in value["rank_available_gib"]], [111, 110, 132])
        self.assertIn("rank 2", value["reason"])
        self.assertNotIn("rank 0", value["reason"])

    def test_invalid_estimate_is_not_a_warning(self):
        value = document(self.spec); value["spec_id"] = "f" * 64
        self.path.write_text(json.dumps(value))
        result = self.check()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn('"result": "warn"', result.stdout)
        for entry in ("scripts/up.sh", "serve.sh", "cluster/start-cluster.sh"):
            result = subprocess.run(["bash", str(ROOT / entry), self.spec["spec_id"],
                                     "--memory-estimate-file", str(self.path), "--accept-memory-warn"],
                                    env=self.env, cwd=ROOT, capture_output=True, text=True)
            with self.subTest(entry=entry):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("memory estimate", result.stderr)
                self.assertFalse((self.root / "mutations").exists())

    def test_launcher_reentry_keeps_estimate_and_warning_authority(self):
        script = """
. "$BASH_ENV"
load_conf "$TEST_SPEC_ID"
select_memory_estimate "$TEST_ESTIMATE" "" ""
resolve_library_hot_for_profile() { echo PREPARED_RECHECK; }
require_launch_operational_checks
"""
        for available, accepted, success in ((120, "0", True), (114.12, "0", False),
                                              (114.12, "1", True), (3.99, "1", False)):
            env = {**self.env, "TEST_SPEC_ID": self.spec["spec_id"], "TEST_ESTIMATE": str(self.path),
                   "TEST_MEM_0": str(available), "PULSAR_ACCEPT_MEMORY_WARN": accepted}
            result = subprocess.run(["bash", "-c", script], env=env, cwd=ROOT, text=True, capture_output=True)
            with self.subTest(available=available, accepted=accepted):
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual("PREPARED_RECHECK" in result.stdout, success)
                self.assertFalse((self.root / "mutations").exists())

    def test_public_verification_and_changed_id(self):
        command = [str(ROOT / "pulsar"), "memory", "verify", "--file", str(self.path),
                   "--spec-file", str(self.base.path), "--json"]
        result = subprocess.run(command, cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        frozen = json.loads(result.stdout)["result"]
        self.assertEqual(frozen["estimate"]["spec_id"], self.spec["spec_id"])
        value = document(self.spec); value["ranks"][0]["resident_weights_bytes"] = 90 * GIB
        self.path.write_text(json.dumps(value))
        result = subprocess.run(command + ["--estimate-id", frozen["estimate_id"]],
                                cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
