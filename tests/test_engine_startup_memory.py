"""The memory check judges a node as the engine does at its startup.

vLLM v0.32.0 on a unified-memory GPU reads MemAvailable after Start's own
processes on that rank have started, and refuses below ceil(MemTotal x
--gpu-memory-utilization). Only that certain refusal fails the check. The
platform's startup margin depends on the recipe, so a node inside it passes
with an information line. The figures are those of real starts on this
platform: utilization 0.75 of 121.69 GiB needs 91.27 GiB; a node that read
116.5 GiB before Start was refused when its engine read 90.74 GiB.
"""
import copy
import json
from pathlib import Path
import subprocess
import unittest

from release_spec import serving
from scripts import platform_reference, start_blockers
from tests import test_diagnostics as diagnostics
from tests.test_memory_estimate import document

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


class EngineStartup(unittest.TestCase):
    def setUp(self):
        self.base = diagnostics.Diagnostics()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.root = self.base.root
        self.spec = self.recipe("0.75")
        with Path(self.base.env["BASH_ENV"]).open("a") as out:
            out.write("""
mem_available_gib_local() { echo "${TEST_MEM_0:-118}"; }
mem_available_gib_remote() {
  case "$1" in alias-1) echo "${TEST_MEM_1:-118}";; *) echo "${TEST_MEM_2:-118}";; esac
}
mem_total_gib_local() { echo 121.69; }
mem_total_gib_remote() { echo 121.69; }
""")

    def recipe(self, utilization):
        """The base three-node spec at UTILIZATION, with a small weight estimate so
        that only the engine's startup check decides."""
        spec = copy.deepcopy(self.base.spec)
        args = spec["recipe"]["engine_args"]
        args[args.index("--gpu-memory-utilization") + 1] = utilization
        spec["spec_id"] = serving.spec_id(spec["recipe"])
        spec = serving.verify_spec(spec)
        self.base.path.write_text(json.dumps(spec))
        (self.root / "estimate.json").write_text(json.dumps(document(spec, [10, 10, 10])))
        return spec

    def check(self, **memory):
        result = self.base.run_tool("check-memory.sh", [
            self.spec["spec_id"], "--memory-estimate-file", str(self.root / "estimate.json"), "--json"],
            extra={f"TEST_MEM_{rank}": str(value) for rank, value in memory.items()})
        return result, json.loads(result.stdout)

    def test_below_the_engines_need_is_a_failure(self):
        result, value = self.check(**{"0": 90.74})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(value["result"], "fail")
        self.assertIn("available 90.74 GiB < 91.27 GiB that the engine needs free at its startup "
                      "(gpu-memory-utilization 0.75 of 121.69 GiB), so the engine would refuse", value["reason"])

    def test_inside_the_first_ranks_margin_passes_with_information(self):
        # The engine on a node that read 116.5 GiB before Start later read 90.74 GiB.
        result, value = self.check(**{"0": 116.5, "1": 116.5, "2": 116.5})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value["result"], "pass")
        self.assertEqual(value["reason"], "")
        self.assertIn("rank 0): 116.5 GiB free is 25.23 GiB more than the 91.27 GiB the engine needs at its "
                      "startup; in measured starts on this platform, Start's own processes took up to 26 GiB "
                      "on this rank before the engine read memory, so the engine may refuse",
                      value["engine_startup_info"])
        # 116.5 is more than 91.27 + 6: the other ranks are not named.
        self.assertNotIn("rank 1)", value["engine_startup_info"])
        rows = value["rank_available_gib"]
        self.assertEqual([row["engine_startup_below_margin"] for row in rows], [True, False, False])
        self.assertEqual([row["engine_startup_need_gib"] for row in rows], [91.27] * 3)
        self.assertEqual([row["engine_startup_margin_gib"] for row in rows], [26, 6, 6])
        self.assertEqual([row["total_gib"] for row in rows], [121.69] * 3)
        self.assertEqual(value["gpu_memory_utilization"], 0.75)

    def test_another_rank_inside_its_margin_is_named_too(self):
        result, value = self.check(**{"0": 118, "1": 96, "2": 118})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rank 1): 96 GiB free is 4.73 GiB more than the 91.27 GiB", value["engine_startup_info"])
        self.assertNotIn("rank 0)", value["engine_startup_info"])

    def test_the_certain_refusal_still_fails_when_another_rank_is_inside_its_margin(self):
        result, value = self.check(**{"0": 116.5, "1": 90})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("rank 1): available 90 GiB < 91.27 GiB", value["reason"])
        self.assertNotIn("rank 0)", value["reason"])
        self.assertIn("rank 0): 116.5 GiB free", value["engine_startup_info"])

    def test_enough_for_the_engine_and_its_margin_passes(self):
        result, value = self.check(**{"0": 118, "1": 98, "2": 98})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value["result"], "pass")
        self.assertIsNone(value["engine_startup_info"])
        self.assertEqual([row["engine_startup_below_margin"] for row in value["rank_available_gib"]], [False] * 3)

    def test_an_unreadable_remote_total_skips_only_that_nodes_engine_check(self):
        with Path(self.base.env["BASH_ENV"]).open("a") as out:
            out.write("""
mem_total_gib_remote() { [ "$1" != alias-1 ] && echo 121.69; }
""")
        result, value = self.check(**{"0": 118, "1": 50, "2": 118})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value["result"], "pass")
        rows = value["rank_available_gib"]
        self.assertEqual([row["total_gib"] for row in rows], [121.69, None, 121.69])
        self.assertEqual([row["engine_startup_need_gib"] for row in rows], [91.27, None, 91.27])
        self.assertIsNone(value["engine_startup_info"])

    def test_a_lower_utilization_needs_less(self):
        self.spec = self.recipe("0.6")
        result, value = self.check(**{"0": 100})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value["rank_available_gib"][0]["engine_startup_need_gib"], 73.02)

    def test_the_human_lines_name_the_engines_need_and_the_margin(self):
        for quiet in ("0", "1"):
            with self.subTest(quiet=quiet):
                result = self.base.run_tool("check-memory.sh", [
                    self.spec["spec_id"], "--memory-estimate-file", str(self.root / "estimate.json")],
                    extra={"TEST_MEM_0": "116.5", "QUIET": quiet})
                self.assertEqual(result.returncode, 0, result.stderr)
                text = " ".join((result.stdout + result.stderr).split())
                self.assertIn("needs 91.27 GiB free", text)
                self.assertIn("116.5 GiB free is 25.23 GiB more than the 91.27 GiB", text)
                self.assertNotIn("warning", text.lower())
        self.assertIn("INFO engine", text)
        self.assertIn("PASS memory", text)

    def test_a_dry_run_start_is_not_blocked_inside_the_margin(self):
        with Path(self.base.env["BASH_ENV"]).open("a") as out:
            out.write("""
container_ownership_inspect_remote() { return 3; }
container_ownership_inspect_local() { return 3; }
port_free() { return 0; }
""")
        blockers = self.root / "blockers.jsonl"
        result = subprocess.run(
            [str(ROOT / "pulsar"), "start", self.spec["spec_id"], "--spec-file", str(self.base.path),
             "--dry-run"],
            env={**self.base.env, "DIAG_MODE": "ok", "TEST_MEM_0": "116.5",
                 "PULSAR_START_BLOCKERS_FILE": str(blockers),
                 "PULSAR_MODEL_LIBRARY_DIR": str(self.root / "library")},
            cwd=ROOT, text=True, capture_output=True)
        text = " ".join((result.stdout + result.stderr).split())
        self.assertIn("PASS memory", text)
        self.assertIn("INFO engine", text)
        self.assertNotIn("WARN accepted for dry-run", text)
        blocked = [row["blocker"] for row in start_blockers.read(blockers)] if blockers.exists() else []
        self.assertFalse([code for code in blocked if code.startswith("memory")], text)
        self.assertFalse((self.root / "mutations").exists())


class PlatformMargins(unittest.TestCase):
    def test_the_platform_states_both_margins_and_exports_them(self):
        platform = platform_reference.load_platform_file(ROOT / "platforms" / "dgx-spark-gb10.json")
        self.assertEqual(platform["memory"]["engine_startup_margin_first_rank_gib"], 26)
        self.assertEqual(platform["memory"]["engine_startup_margin_other_rank_gib"], 6)
        shell = platform_reference.export_shell(platform)
        self.assertIn("export PULSAR_ENGINE_STARTUP_MARGIN_FIRST_RANK_GIB=26", shell)
        self.assertIn("export PULSAR_ENGINE_STARTUP_MARGIN_OTHER_RANK_GIB=6", shell)

    def test_a_platform_without_them_loads_and_states_no_margin(self):
        document = json.loads((ROOT / "platforms" / "dgx-spark-gb10.json").read_text())
        for field in platform_reference.OPTIONAL_MEMORY_FIELDS:
            del document["memory"][field]
        platform = platform_reference.validate_platform_reference(document)
        self.assertIsNone(platform["memory"]["engine_startup_margin_first_rank_gib"])
        shell = platform_reference.export_shell(platform)
        self.assertIn("export PULSAR_ENGINE_STARTUP_MARGIN_FIRST_RANK_GIB=''", shell)
        self.assertIn("export PULSAR_ENGINE_STARTUP_MARGIN_OTHER_RANK_GIB=''", shell)

    def test_a_negative_null_or_unknown_margin_is_refused(self):
        document = json.loads((ROOT / "platforms" / "dgx-spark-gb10.json").read_text())
        negative = copy.deepcopy(document)
        negative["memory"]["engine_startup_margin_first_rank_gib"] = -1
        null = copy.deepcopy(document)
        null["memory"]["engine_startup_margin_other_rank_gib"] = None
        unknown = copy.deepcopy(document)
        unknown["memory"]["engine_startup_margin_third_rank_gib"] = 1
        for broken in (negative, null, unknown):
            with self.assertRaises(platform_reference.PlatformReferenceError):
                platform_reference.validate_platform_reference(broken)


if __name__ == "__main__":
    unittest.main()
