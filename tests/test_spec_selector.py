"""Human-mode commands resolve a unique shortened spec ID; machine callers stay exact."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import spec_selector

ROOT = Path(__file__).resolve().parents[1]
FIRST = "0a60" + "1" * 60
SIBLING = "0a60" + "1" * 20 + "2" * 40
OTHER = "b" * 64


class SpecSelector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        releases = self.root / "releases"
        releases.mkdir()
        for spec_id in (FIRST, SIBLING, OTHER):
            (releases / f"{spec_id}.json").write_text("{}")
        (releases / "README.md").write_text("not a spec")

    def test_unique_prefix_resolves(self):
        self.assertEqual(spec_selector.resolve(self.root, OTHER[:12]), OTHER)
        self.assertEqual(spec_selector.resolve(self.root, FIRST[:30]), FIRST)

    def test_errors_name_complete_ids(self):
        with self.assertRaisesRegex(spec_selector.SelectionError, "at least 12 characters.*" + OTHER):
            spec_selector.resolve(self.root, "bbbb")
        with self.assertRaisesRegex(spec_selector.SelectionError, "more than one spec"):
            spec_selector.resolve(self.root, FIRST[:12])
        with self.assertRaisesRegex(spec_selector.SelectionError, "no catalog spec ID starts with"):
            spec_selector.resolve(self.root, "c" * 12)

    def test_values_that_are_not_prefixes_pass_through(self):
        for value in (OTHER, "model-name", "--all", "0A60" + "1" * 8):
            self.assertEqual(spec_selector.resolve(self.root, value), value)

    def test_destructive_operations_require_complete_id(self):
        with self.assertRaisesRegex(spec_selector.SelectionError, "model purge requires the complete.*" + OTHER):
            spec_selector.resolve(self.root, OTHER[:12], require_full="model purge")
        self.assertEqual(spec_selector.resolve(self.root, OTHER, require_full="model purge"), OTHER)

    def test_spec_position(self):
        cases = {
            ("start", (OTHER[:12], "--dry-run")): (0, None),
            ("stop", ("--all",)): (None, None),
            ("models", ("show", OTHER[:12])): (1, None),
            ("models", ("results", OTHER[:12])): (1, None),
            ("models", ("list",)): (None, None),
            ("models", ("check", "--node", "n1", OTHER[:12])): (3, None),
            ("models", ("show", "--json")): (None, None),
            ("model", ("prepare", "--node", "n1", OTHER[:12], "--plan")): (3, None),
            ("model", ("archive", "verify", OTHER[:12])): (2, None),
            ("model", ("purge", OTHER[:12], "--yes")): (1, "model purge"),
        }
        for (command, args), expected in cases.items():
            self.assertEqual(spec_selector.spec_position(command, list(args)), expected, (command, args))

    def run_helper(self, *args):
        return subprocess.run(["python3", str(ROOT / "scripts/spec_selector.py"), "--repo-root", str(self.root), *args],
                              text=True, capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT)})

    def test_machine_modes_keep_exact_contract(self):
        for extra in (["--json"], ["--spec-file", "candidate.json"], ["--help"]):
            result = self.run_helper("start", OTHER[:12], *extra)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), extra)

    def test_helper_prints_replacement_and_notice(self):
        result = self.run_helper("model", "prepare", "--node", "n1", OTHER[:12], "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"3 {OTHER}\n")
        self.assertIn(f"Using spec {OTHER}", result.stderr)

    def test_dispatcher_replaces_only_the_spec_argument(self):
        shell = self.root / "shell"
        (shell / "scripts").mkdir(parents=True)
        shutil.copytree(self.root / "releases", shell / "releases")
        shutil.copyfile(ROOT / "pulsar", shell / "pulsar")
        (shell / "pulsar").chmod(0o755)
        for name in ("spec_selector.py", "terminal_format.py", "__init__.py"):
            if (ROOT / "scripts" / name).exists():
                shutil.copyfile(ROOT / "scripts" / name, shell / "scripts" / name)
        # The stubbed public CLI records its arguments; no Stack action runs.
        (shell / "scripts/public_cli.py").write_text(
            "import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        env = {**os.environ, "PYTHONPATH": str(shell)}
        result = subprocess.run([str(shell / "pulsar"), "start", OTHER[:12], "--node", "has space"],
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["start", OTHER, "--node", "has space"])
        self.assertIn(f"Using spec {OTHER}", result.stderr)
        result = subprocess.run([str(shell / "pulsar"), "stop", "0a60"], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        result = subprocess.run([str(shell / "pulsar"), "status", OTHER[:12], "--json"],
                                text=True, capture_output=True, env=env)
        self.assertEqual(json.loads(result.stdout), ["status", OTHER[:12], "--json"])


if __name__ == "__main__":
    unittest.main()
