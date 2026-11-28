import importlib.util
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
_spec = importlib.util.spec_from_file_location("parallel_unittest", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

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

    def test_committed_durations_give_a_long_test_a_shard_of_its_own(self):
        first = "tests.test_outcomes.Separate.test_first"
        (self.root / "tests" / "shard-durations.json").write_text(json.dumps({first: 100}))
        counts, notes = [], []
        for index in (1, 2):
            result = self.run_tests("--shard", f"{index}/2")
            counts.append(int(re.search(r"Ran (\d+) tests", result.stdout).group(1)))
            notes.append(sorted(name for name, _ in self.notes()))
            self.log.unlink()
        self.assertEqual(counts, [1, 9])
        self.assertEqual(notes[0], ["first"])
        self.assertNotIn("first", notes[1])

    def test_an_unreadable_durations_file_stops_before_any_test(self):
        for text in ("not json", "[1, 2]", '{"a": "slow"}', '{"a": -1}', '{"a": true}'):
            (self.root / "tests" / "shard-durations.json").write_text(text)
            result = self.run_tests("--shard", "1/2")
            self.assertEqual(result.returncode, 1, text)
            self.assertIn("The durations in tests/shard-durations.json could not be read", result.stderr)
            self.assertFalse(self.log.exists(), text)

    def test_written_durations_keep_the_tests_found_now_that_take_a_second_or_more(self):
        first, second = "tests.test_outcomes.Separate.test_first", "tests.test_outcomes.Separate.test_second"
        self.durations.write_text(json.dumps({first: 2.345, second: 0.5, "tests.test_gone.Gone.test_gone": 9.0}))
        result = self.run_tests("--write-shard-durations")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.root / "tests" / "shard-durations.json").read_text()), {first: 2.3})
        self.assertIn("8 of the 10 tests found have no saved duration", result.stdout)
        self.assertFalse(self.log.exists())
        for args in (["--shard", "1/2"], [first]):
            result = self.run_tests("--write-shard-durations", *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("--write-shard-durations needs every test", result.stderr)

    def test_a_split_has_every_job_once_and_depends_only_on_its_input(self):
        jobs = [(top, [f"{top}.test_{n}"]) for top in ("release_spec/tests", ".") for n in range(11)]
        jobs.append((".", ["m.Shared.test_a", "m.Shared.test_b"]))
        durations = {f"..test_{n}": float(n * n) for n in range(11)} | {"m.Shared.test_a": 30.0}
        for count in range(1, 6):
            for known in ({}, durations):
                parts = runner.split(jobs, count, known)
                self.assertEqual(len(parts), count)
                self.assertCountEqual([job for part in parts for job in part], jobs)
                self.assertEqual(runner.split(list(reversed(jobs)), count, dict(known)), parts)
            # Jobs of the same time are split as every COUNT-th job of the sorted list.
            single = [job for job in jobs if len(job[1]) == 1]
            ordered = sorted(single, key=lambda job: (job[0], job[1][0]))
            self.assertEqual(runner.split(single, count, {}), [ordered[i::count] for i in range(count)])
        seconds = [sum(durations.get(i, runner.UNLISTED_SECONDS) for _, ids in part for i in ids)
                   for part in runner.split(jobs, 3, durations)]
        self.assertLess(max(seconds) - min(seconds), 30.0)

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

    def _make_selftest_checkout(self):
        td = tempfile.TemporaryDirectory(prefix="stack-selftest-")
        self.addCleanup(td.cleanup)
        repo = Path(td.name)
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "selftest.sh").write_text((ROOT / "scripts" / "selftest.sh").read_text())
        for stub in ("check-catalog.py", "check_publishable_privacy.py"):
            (scripts / stub).write_text("import sys\nsys.exit(0)\n")
        return repo

    def test_selftest_checks_only_skips_claude_worktrees_and_cache_dirs(self):
        repo = self._make_selftest_checkout()
        for rel in (".claude/worktrees/wip/broken.py", ".ruff_cache/broken.py"):
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def broken(:\n")
        bad_sh = repo / ".claude/worktrees/wip/broken.sh"
        bad_sh.write_text("if then fi\n")
        good_skill = repo / "skills/example/helper.py"
        good_skill.parent.mkdir(parents=True, exist_ok=True)
        good_skill.write_text("VALUE = 1\n")

        result = subprocess.run(["bash", str(repo / "scripts" / "selftest.sh"), "--checks-only"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Python and shell syntax checks passed", result.stdout)

    def test_selftest_checks_only_still_rejects_syntax_errors_in_source_dirs(self):
        for rel, body in (("skills/example/broken.py", "def broken(:\n"),
                          ("scripts/broken.sh", "if then fi\n")):
            repo = self._make_selftest_checkout()
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            result = subprocess.run(["bash", str(repo / "scripts" / "selftest.sh"), "--checks-only"],
                                    capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0, rel)


if __name__ == "__main__":
    unittest.main()
