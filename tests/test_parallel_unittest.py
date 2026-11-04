import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "parallel_unittest.py"

# A synthetic checkout: each test records its process ID and start order in LOG.
TESTS = {
    "release_spec/tests/__init__.py": "",
    "release_spec/tests/test_spec.py": """
        import unittest
        from record import note
        class Spec(unittest.TestCase):
            def test_spec(self):
                note("spec")
    """,
    "release_spec/tests/record.py": """
        import os
        def note(name):
            with open(os.environ["LOG"], "a") as log:
                log.write(f"{name} {os.getpid()}\\n")
    """,
    "tests/__init__.py": "",
    "tests/record.py": """
        import os
        def note(name):
            with open(os.environ["LOG"], "a") as log:
                log.write(f"{name} {os.getpid()}\\n")
    """,
    "tests/test_outcomes.py": """
        import os
        import unittest
        from tests.record import note
        class Separate(unittest.TestCase):
            def test_first(self):
                note("first")
            def test_second(self):
                note("second")
            def test_fails(self):
                print("shown with the failure")
                os.write(1, b"written past the buffer\\n")
                self.assertEqual(1, 2)
            def test_errors(self):
                raise RuntimeError("broken")
            def test_subtest(self):
                for value in (1, 2):
                    with self.subTest(value=value):
                        self.assertEqual(value, 1)
            @unittest.skip("not here")
            def test_skipped(self):
                pass
        class Shared(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                note("setUpClass")
            def test_one(self):
                note("shared")
            def test_two(self):
                note("shared")
    """,
    "tests/test_broken.py": "import a_module_that_does_not_exist\n",
}


class ParallelRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, text in TESTS.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(textwrap.dedent(text))
        self.log = self.root / "log"
        self.durations = self.root / "durations.json"

    def run_tests(self, *args, jobs="2"):
        env = {**os.environ, "LOG": str(self.log), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(RUNNER), "--root", str(self.root), "--jobs", jobs,
                               "--durations-file", str(self.durations), *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def notes(self):
        return [line.split() for line in self.log.read_text().splitlines()]

    def test_reports_every_outcome_from_both_passes(self):
        result = self.run_tests()
        self.assertEqual(result.returncode, 1, result.stderr)
        out = result.stdout
        self.assertIn("FAIL: test_fails (tests.test_outcomes.Separate.test_fails)", out)
        self.assertIn("shown with the failure", out)
        self.assertIn("Output of the test process:\nwritten past the buffer", out)
        self.assertIn("ERROR: test_errors (tests.test_outcomes.Separate.test_errors)", out)
        self.assertIn("RuntimeError: broken", out)
        self.assertIn("test_subtest (tests.test_outcomes.Separate.test_subtest) (value=2)", out)
        self.assertIn("a_module_that_does_not_exist", out)
        self.assertIn("Ran 10 tests", out)
        self.assertIn("FAILED (failures=2, errors=2, skipped=1)", out)
        self.assertEqual(sorted(name for name, _ in self.notes()),
                         ["first", "second", "setUpClass", "shared", "shared", "spec"])

    def test_each_test_has_its_own_process_unless_its_class_shares_fixtures(self):
        self.run_tests()
        pids = {}
        for name, pid in self.notes():
            pids.setdefault(name, set()).add(pid)
        self.assertNotEqual(pids["first"], pids["second"])
        self.assertEqual(pids["setUpClass"], pids["shared"])
        self.assertEqual(len(pids["shared"]), 1)

    def test_unmeasured_then_the_slowest_saved_tests_start_first(self):
        first, second = "tests.test_outcomes.Separate.test_first", "tests.test_outcomes.Separate.test_second"
        self.durations.write_text(json.dumps({first: 1.0, second: 50.0}))
        self.run_tests(first, second, "tests.test_outcomes.Shared.test_one", jobs="1")
        self.assertEqual([note for note, _ in self.notes()], ["setUpClass", "shared", "second", "first"])
        saved = json.loads(self.durations.read_text())
        self.assertLess(saved[second], 50.0)
        self.assertIn("tests.test_outcomes.Shared.test_one", saved)

    def test_names_run_only_those_tests_and_pass(self):
        result = self.run_tests("tests.test_outcomes.Shared")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 2 tests", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("OK"))

    def test_shards_partition_the_tests_and_keep_shared_fixtures_together(self):
        counts, shards = [], []
        for index in (1, 2, 3):
            result = self.run_tests("--shard", f"{index}/3")
            self.assertIn(f"shard {index} of 3", result.stdout)
            counts.append(int(re.search(r"Ran (\d+) tests", result.stdout).group(1)))
            shards.append(sorted(name for name, _ in self.notes()) if self.log.exists() else [])
            self.log.unlink(missing_ok=True)
        self.assertEqual(sum(counts), 10)
        self.assertTrue(all(counts), counts)
        self.assertEqual(sorted(name for names in shards for name in names),
                         ["first", "second", "setUpClass", "shared", "shared", "spec"])
        with_fixture = [names for names in shards if "shared" in names]
        self.assertEqual(len(with_fixture), 1)
        self.assertEqual(with_fixture[0].count("shared"), 2)
        self.assertIn("setUpClass", with_fixture[0])

    def test_a_shard_outside_its_count_is_rejected(self):
        for value in ("0/2", "3/2", "two", "1/2/3"):
            result = self.run_tests("--shard", value)
            self.assertEqual(result.returncode, 2, value)
            self.assertIn("--shard", result.stderr)

    def test_selftest_rejects_a_job_count_that_is_not_a_positive_number(self):
        for args in (["--jobs"], ["--jobs", "0"], ["-j", "two"]):
            # The public CLI sets PULSAR_USAGE_EXIT for the scripts it runs; this checks the default.
            env = {key: value for key, value in os.environ.items() if not key.startswith("PULSAR_")}
            result = subprocess.run(["bash", str(ROOT / "scripts" / "selftest.sh"), *args],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("usage: scripts/selftest.sh [--checks-only] [--jobs N]", result.stderr)


if __name__ == "__main__":
    unittest.main()
