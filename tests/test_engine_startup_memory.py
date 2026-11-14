"""The memory check judges a node as the engine does at its startup.

vLLM on a unified-memory GPU reads MemAvailable after Start's own processes on
that rank have started, and refuses below ceil(MemTotal x
--gpu-memory-utilization). The figures are those of real starts on this
platform: utilization 0.75 of 121.69 GiB needs 91.27 GiB; a node that read
116.5 GiB before Start passed the old check and was refused at 90.74 GiB.
"""
import copy
import json
from pathlib import Path
import unittest

from release_spec import serving
from scripts import platform_reference
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

    def test_the_first_rank_needs_the_margin_of_its_api_server_and_engine_core(self):
        # The old check passed this node at 116.5 GiB; its engine then read 90.74 GiB.
        result, value = self.check(**{"0": 116.5, "1": 116.5, "2": 116.5})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(value["result"], "warn")
        self.assertIn("rank 0): available 116.5 GiB < 91.27 GiB the engine needs at its startup + 26 GiB",
                      value["reason"])
        # 116.5 is more than 91.27 + 6: the other ranks are fine.
        self.assertNotIn("rank 1)", value["reason"])
        self.assertNotIn("rank 2)", value["reason"])
        rows = value["rank_available_gib"]
        self.assertEqual([row["engine_startup_need_gib"] for row in rows], [91.27] * 3)
        self.assertEqual([row["engine_startup_margin_gib"] for row in rows], [26, 6, 6])
        self.assertEqual([row["total_gib"] for row in rows], [121.69] * 3)
        self.assertEqual(value["gpu_memory_utilization"], 0.75)

    def test_another_rank_below_its_margin_warns_too(self):
        result, value = self.check(**{"0": 118, "1": 96, "2": 118})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("rank 1): available 96 GiB < 91.27 GiB the engine needs at its startup + 6 GiB",
                      value["reason"])

    def test_enough_for_the_engine_and_its_margin_passes(self):
        result, value = self.check(**{"0": 118, "1": 98, "2": 98})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value["result"], "pass")

    def test_a_lower_utilization_needs_less(self):
        self.spec = self.recipe("0.6")
        result, value = self.check(**{"0": 100})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value["rank_available_gib"][0]["engine_startup_need_gib"], 73.02)

    def test_the_human_lines_name_the_engines_need(self):
        result = self.base.run_tool("check-memory.sh", [
            self.spec["spec_id"], "--memory-estimate-file", str(self.root / "estimate.json")],
            extra={"TEST_MEM_0": "116.5"})
        self.assertEqual(result.returncode, 2, result.stderr)
        text = " ".join((result.stdout + result.stderr).split())
        self.assertIn("engine startup: needs 91.27 GiB free (gpu-memory-utilization 0.75 of 121.69 GiB) "
                      "+ margin 26 GiB on rank 0, 6 GiB on the others", text)
        self.assertIn("the engine may refuse", text)


class PlatformMargins(unittest.TestCase):
    def test_the_platform_states_both_margins_and_exports_them(self):
        platform = platform_reference.load_platform_file(ROOT / "platforms" / "dgx-spark-gb10.json")
        self.assertEqual(platform["memory"]["engine_startup_margin_first_rank_gib"], 26)
        self.assertEqual(platform["memory"]["engine_startup_margin_other_rank_gib"], 6)
        shell = platform_reference.export_shell(platform)
        self.assertIn("export PULSAR_ENGINE_STARTUP_MARGIN_FIRST_RANK_GIB=26", shell)
        self.assertIn("export PULSAR_ENGINE_STARTUP_MARGIN_OTHER_RANK_GIB=6", shell)

    def test_a_platform_without_them_or_with_a_negative_one_is_refused(self):
        document = json.loads((ROOT / "platforms" / "dgx-spark-gb10.json").read_text())
        missing = copy.deepcopy(document)
        del missing["memory"]["engine_startup_margin_other_rank_gib"]
        negative = copy.deepcopy(document)
        negative["memory"]["engine_startup_margin_first_rank_gib"] = -1
        for broken in (missing, negative):
            with self.assertRaises(platform_reference.PlatformReferenceError):
                platform_reference.validate_platform_reference(broken)


if __name__ == "__main__":
    unittest.main()
