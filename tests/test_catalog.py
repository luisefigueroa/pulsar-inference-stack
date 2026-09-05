"""Saved catalog views remain distinct from live verification and serving."""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.catalog import entries, render, age_seconds
from model_library.state import Store, view_key
from model_library.integrity import StorageError
from release_spec import pretty_json_bytes
from scripts.terminal_format import TerminalWriter


class Catalog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.store = Store(self.root / "state")
        self.now = datetime(2026, 9, 5, 1, tzinfo=timezone.utc)

    def add_spec(self):
        helper = runpy.run_path(str(ROOT / "tests/test_release_contribution.py"))["make_contribution"]
        spec, _, _, _ = helper(self.root / "fixture")
        (self.repo / "releases").mkdir(exist_ok=True)
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        return spec

    def observe(self, spec, **fields):
        self.store.put("observations", spec["spec_id"], {"schema_version": 1,
            "kind": "pulsar-saved-observation", "spec_id": spec["spec_id"],
            "checked_at": "2026-09-05T00:00:00Z", **fields})

    def test_empty_catalog_does_not_create_state(self):
        self.assertEqual(entries(self.repo, self.store), [])
        self.assertFalse(self.store.root.exists())
        output = io.StringIO(); render([], writer=TerminalWriter(stream=output))
        self.assertIn("catalog is empty", output.getvalue())

    def test_catalog_spec_without_files_stays_unknown(self):
        self.add_spec()
        with patch("subprocess.run", side_effect=AssertionError("catalog must not probe")):
            row = entries(self.repo, self.store, now=self.now)[0]
        self.assertEqual(row["local_state"], "unknown")
        self.assertEqual(row["archive_state"], "unknown")
        self.assertIsNone(row["checked_at"])
        self.assertFalse(self.store.root.exists())

    def test_explicit_missing_differs_from_unknown(self):
        spec = self.add_spec()
        self.observe(spec, local_state="missing", archive_state="missing", blockers=["No home was found."])
        row = entries(self.repo, self.store, now=self.now)[0]
        self.assertEqual(row["local_state"], "missing")
        self.assertEqual(row["archive_state"], "missing")
        self.assertEqual(row["observation_age_seconds"], 3600)
        self.assertEqual(row["blockers"], ["No home was found."])

    def test_prepared_observation_is_not_live_service_status(self):
        spec = self.add_spec()
        self.observe(spec, local_state="ready", archive_state="verified")
        row = entries(self.repo, self.store, now=self.now)[0]
        output = io.StringIO(); render([row], writer=TerminalWriter(stream=output))
        self.assertIn("files prepared at last check", output.getvalue())
        self.assertIn("Start rechecks", output.getvalue())
        self.assertNotIn("running", row)

    def test_known_home_and_archive_do_not_imply_current_health(self):
        spec = self.add_spec(); manifest = spec["identity"]["snapshot_manifest"]["manifest_id"]
        home = {"schema_version": 1, "kind": "pulsar-home", "snapshot_manifest_id": manifest,
            "node_id": "node-a", "hub_path": "/nonexistent/home", "path": "/nonexistent/home/snapshot",
            "verification": {"snapshot_manifest_id": manifest}, "verified_at": "2026-09-05T00:00:00Z"}
        self.store.put("homes", manifest, home)
        view = {**home, "kind": "pulsar-prepared-view", "spec_id": spec["spec_id"],
            "topology_id": "c" * 64, "rank": 0, "pinned": True, "is_home_view": True}
        self.store.put("views", view_key(spec["spec_id"], "node-a"), view)
        self.store.put("archives", manifest, {"snapshot_manifest_id": manifest,
            "verified": True, "verified_at": "2026-09-04T01:00:00Z"})
        row = entries(self.repo, self.store, now=self.now)[0]
        self.assertEqual(row["local_state"], "unknown")
        self.assertEqual(row["archive_state"], "unknown")
        self.assertTrue(row["prepared_copies"][0]["pinned"])
        self.assertEqual(row["archive_age_seconds"], 86400)

    def test_withdrawal_reason_visible_without_hiding_recipe(self):
        spec = self.add_spec()
        spec["review"].update(status="withdrawn", reason="Later testing found inconsistent answers.")
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        rows = entries(self.repo, self.store)
        self.assertEqual(len(rows), 1)
        output = io.StringIO(); render(rows, writer=TerminalWriter(stream=output))
        self.assertIn("Later testing found inconsistent answers", output.getvalue())
        self.assertIn("Exact serving remains possible", output.getvalue())

    def test_corrupt_observation_does_not_look_ready(self):
        spec = self.add_spec()
        self.observe(spec, local_state="magic-ready")
        with self.assertRaisesRegex(StorageError, "preparation state"):
            entries(self.repo, self.store)

    def test_archive_presence_does_not_claim_full_verification(self):
        spec = self.add_spec()
        self.observe(spec, local_state="unknown", archive_state="present")
        output = io.StringIO()
        render(entries(self.repo, self.store), writer=TerminalWriter(stream=output))
        self.assertIn("present; verify before restore", output.getvalue())
        self.assertNotIn("verified at last check", output.getvalue())
        self.assertNotIn("Last archive verification", output.getvalue())

    def test_future_observation_age_is_unknown(self):
        self.assertIsNone(age_seconds("2027-01-01T00:00:00Z", self.now))

    def test_narrow_details_and_help_do_not_overflow(self):
        spec = self.add_spec()
        self.observe(spec, local_state="ready", archive_state="verified", blockers=["A long explanatory message is wrapped so an operator can read it at a narrow terminal width."])
        output = io.StringIO()
        render(entries(self.repo, self.store), details=True, writer=TerminalWriter(width=44, stream=output))
        self.assertTrue(all(len(line) <= 44 for line in output.getvalue().splitlines()), output.getvalue())
        result = subprocess.run([str(ROOT / "pulsar"), "help"], env={**os.environ, "COLUMNS": "44"},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(len(line) <= 44 for line in result.stdout.splitlines()), result.stdout)

    def menu_action(self, action_index=0, confirm_status=1):
        spec = self.add_spec()
        # Build a minimal operator shell with parameterized doubles. No Docker,
        # SSH, topology discovery or model files are touched.
        shell_root = self.root / "shell"; scripts = shell_root / "scripts"; scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/model-storage.sh", scripts / "model-storage.sh")
        (scripts / "lib.sh").write_text('PULSAR_MODEL_LIBRARY_DIR="'+str(self.store.root)+'"\nrequire_cluster_nodes() { CLUSTER_NODE_IDS=(fixture-node); }\n')
        (scripts / "ui.sh").write_text('choose_index() { if [ "$1" = "Choose one operation" ]; then printf "%s\\n" "$MENU_ACTION"; else printf "0\\n"; fi; }; confirm() { return "$MENU_CONFIRM_STATUS"; }\n')
        log = self.root / "action.json"
        action = scripts / "model-library.sh"
        action.write_text('#!/usr/bin/env python3\nimport json,sys\nopen('+repr(str(log))+',"w").write(json.dumps(sys.argv[1:]))\n')
        action.chmod(0o700)
        # Catalog Python still uses the real shared package with a temporary releases root.
        env = dict(os.environ, PYTHONPATH=str(ROOT), GUM="0", PULSAR_MODEL_LIBRARY_DIR=str(self.store.root),
                   MENU_ACTION=str(action_index), MENU_CONFIRM_STATUS=str(confirm_status))
        # The wrapper supplies a shell-root catalog path, so provide its released spec.
        shutil.copytree(self.repo / "releases", shell_root / "releases")
        # module ROOT remains the canonical stack path; force a catalog function override
        # through a tiny Python launcher that injects the fixture repo into module argv.
        binary = self.root / "bin"; binary.mkdir()
        python = binary / "python3"
        python.write_text('#!/usr/bin/env bash\nif [ "${1:-}" = -m ] && [ "${2:-}" = model_library.catalog ]; then shift 2; exec '+sys.executable+' -m model_library.catalog --repo-root '+str(shell_root)+' "$@"; fi\nexec '+sys.executable+' "$@"\n')
        python.chmod(0o700); env["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
        result = subprocess.run(["bash", str(scripts / "model-storage.sh"), "menu"], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return spec, json.loads(log.read_text()) if log.exists() else None

    def test_menu_check_executes_one_explicit_action(self):
        spec, action = self.menu_action()
        self.assertEqual(action, ["check", spec["spec_id"], "--node", "fixture-node"])

    def test_menu_restore_requires_confirmation_and_never_starts(self):
        spec, action = self.menu_action(action_index=2, confirm_status=0)
        self.assertEqual(action, ["restore", spec["spec_id"], "--node", "fixture-node", "--yes"])

    def test_declined_menu_restore_has_no_action(self):
        _, action = self.menu_action(action_index=2, confirm_status=1)
        self.assertIsNone(action)


if __name__ == "__main__": unittest.main()
