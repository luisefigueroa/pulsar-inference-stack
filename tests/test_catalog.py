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

    def add_spec(self, guarded=False):
        if guarded:
            # A schema-2 spec whose recipe.container holds a serving guard.
            from release_spec.tests.test_serving_guard import FIXTURES, guarded as with_guard
            spec = with_guard(json.loads((FIXTURES / "spec.json").read_text()))
        else:
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
        self.assertIn("confirms cluster membership first", output.getvalue())

    def test_catalog_spec_without_files_stays_unknown(self):
        self.add_spec()
        with patch("subprocess.run", side_effect=AssertionError("catalog must not probe")):
            row = entries(self.repo, self.store, now=self.now)[0]
        self.assertEqual(row["local_state"], "unknown")
        self.assertEqual(row["archive_state"], "unknown")
        self.assertIsNone(row["checked_at"])
        self.assertFalse(self.store.root.exists())

    def test_nullable_state_and_review_are_visible_without_gating(self):
        spec = self.add_spec()
        spec["state"] = None
        spec["review"] = None
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(
            pretty_json_bytes(spec))
        row = entries(self.repo, self.store, now=self.now)[0]
        self.assertIsNone(row["state"])
        self.assertIsNone(row["review"])
        output = io.StringIO()
        render([row], writer=TerminalWriter(stream=output))
        self.assertGreaterEqual(output.getvalue().count("not specified"), 2)

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

    def test_guarded_spec_stays_listed_and_says_start_is_unsupported(self):
        ordinary = self.add_spec()
        guarded = self.add_spec(guarded=True)
        with patch("subprocess.run", side_effect=AssertionError("catalog must not probe")):
            rows = {row["spec_id"]: row for row in entries(self.repo, self.store, now=self.now)}
        self.assertEqual(set(rows), {ordinary["spec_id"], guarded["spec_id"]})
        self.assertIs(rows[ordinary["spec_id"]]["start_supported"], True)
        self.assertIsNone(rows[ordinary["spec_id"]]["start_unsupported_reason"])
        self.assertIs(rows[guarded["spec_id"]]["start_supported"], False)
        self.assertEqual(rows[guarded["spec_id"]]["start_unsupported_reason"], "guard_unsupported")
        for details in (False, True):
            output = io.StringIO()
            render([rows[guarded["spec_id"]]], details=details, writer=TerminalWriter(stream=output))
            self.assertIn("Start     not supported by this Stack (serving guard)", output.getvalue())
            output = io.StringIO()
            render([rows[ordinary["spec_id"]]], details=details, writer=TerminalWriter(stream=output))
            self.assertNotIn("not supported by this Stack", output.getvalue())

    def test_withdrawn_guarded_spec_does_not_claim_serving_remains_possible(self):
        spec = self.add_spec(guarded=True)
        spec["review"] = {"status": "withdrawn", "reviewer": "example-reviewer",
                          "reviewed_at": "2026-09-03T00:00:00Z", "reason": "Superseded."}
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        output = io.StringIO()
        render(entries(self.repo, self.store), writer=TerminalWriter(stream=output))
        self.assertIn("Withdrawn recipes are not recommended.", output.getvalue())
        self.assertNotIn("Exact serving remains possible", output.getvalue())

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

    # Scripted operator shell: parameterized doubles for the UI, topology and
    # every operation. No Docker, SSH, topology discovery or model files.
    UI = r'''
emit_frame() { cat; }
spin() { shift; "$@"; }
_pop() { local n; n=$(cat "$1.n" 2>/dev/null || echo 0); echo $((n + 1)) >"$1.n"; sed -n "$((n + 1))p" "$1"; }
choose_index() {
  local header="$1" answer i=0 option; shift
  { printf '%s\n' "$header" "$@"; printf '\n'; } >>"$CHOICES_LOG"
  answer=$(_pop "$MENU_ANSWERS")
  case "$answer" in "") return 99 ;; "<esc>") return 1 ;; "<ctrl-c>") return 130 ;; "#"*) echo "${answer#\#}"; return 0 ;; esac
  for option in "$@"; do [ "$option" != "$answer" ] || { echo "$i"; return 0; }; i=$((i + 1)); done
  echo "MISSING OPTION: $answer" >>"$CHOICES_LOG"; return 99
}
confirm() {
  printf '%s\n' "$1" >>"$CONFIRM_LOG"
  case "$(_pop "$MENU_CONFIRMS")" in yes) return 0 ;; "<ctrl-c>") return 130 ;; *) return 1 ;; esac
}
'''
    ACTION = """#!/usr/bin/env python3
import json,os,sys
open(os.environ["ACTION_LOG"],"a").write(json.dumps([os.path.basename(sys.argv[0])]+sys.argv[1:])+"\\n")
if "--plan" in sys.argv: print(open(os.environ["PLAN_FILE"]).read())
raise SystemExit(int(os.environ.get("ACTION_RC","0")))
"""

    def run_menu(self, answers, confirms=(), plan=None, action_rc=0, archive_root="/fixture/archive", guarded=False):
        spec = self.add_spec(guarded=guarded)
        shell_root = self.root / "shell"; scripts = shell_root / "scripts"; scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/model-storage.sh", scripts / "model-storage.sh")
        (scripts / "lib.sh").write_text('PULSAR_MODEL_LIBRARY_DIR="'+str(self.store.root)+'"\nrequire_cluster_nodes() { CLUSTER_NODE_IDS=(fixture-node); CLUSTER_NODE_HOSTNAMES=(fixture-host); }\nhuman_node_name() { printf "%s\\n" "${CLUSTER_NODE_HOSTNAMES[$1]}"; }\n')
        (scripts / "ui.sh").write_text(self.UI)
        for name in ("model-library.sh", "status.sh", "up.sh", "down.sh"):
            (scripts / name).write_text(self.ACTION); (scripts / name).chmod(0o700)
        files = {name: self.root / name for name in ("answers", "confirms", "choices.log", "confirm.log", "action.log", "plan.json")}
        files["answers"].write_text("\n".join(answers) + "\n")
        files["confirms"].write_text("\n".join(confirms) + "\n")
        files["plan.json"].write_text(json.dumps(plan or {"kind": "pulsar-restore-plan", "selected_node": "fixture-node"}))
        env = dict(os.environ, PYTHONPATH=str(ROOT), GUM="0", PULSAR_MODEL_LIBRARY_DIR=str(self.store.root),
                   CLUSTER_TOPOLOGY_FILE=str(self.root / "no-topology.json"),
                   MENU_ANSWERS=str(files["answers"]), MENU_CONFIRMS=str(files["confirms"]),
                   CHOICES_LOG=str(files["choices.log"]), CONFIRM_LOG=str(files["confirm.log"]),
                   ACTION_LOG=str(files["action.log"]), PLAN_FILE=str(files["plan.json"]), ACTION_RC=str(action_rc))
        env.pop("PULSAR_COLD_ROOT", None)
        if archive_root is not None:
            env["PULSAR_COLD_ROOT"] = archive_root
        shutil.copytree(self.repo / "releases", shell_root / "releases")
        # The catalog reads the fixture releases root through a tiny python3 launcher.
        binary = self.root / "bin"; binary.mkdir()
        python = binary / "python3"
        python.write_text('#!/usr/bin/env bash\nif [ "${1:-}" = -m ] && [ "${2:-}" = model_library.catalog ]; then shift 2; exec '+sys.executable+' -m model_library.catalog --repo-root '+str(shell_root)+' "$@"; fi\nexec '+sys.executable+' "$@"\n')
        python.chmod(0o700); env["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
        result = subprocess.run(["bash", str(scripts / "model-storage.sh"), "menu"], env=env, text=True, capture_output=True)
        log = files["action.log"]
        actions = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        choices = files["choices.log"].read_text()
        self.assertNotIn("MISSING OPTION", choices)
        confirm_log = files["confirm.log"]
        return spec["spec_id"], result, actions, choices, confirm_log.read_text() if confirm_log.exists() else ""

    def test_menu_returns_to_the_recipe_after_an_action(self):
        spec, result, actions, choices, _ = self.run_menu(["#0", "Check now (suggested)", "fixture-host", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["model-library.sh", "check", spec, "--node", "fixture-node"]])
        self.assertIn("✓ Check now finished for", result.stdout)
        self.assertEqual(choices.count("Choose one operation\n"), 2)
        self.assertEqual(choices.count("Select a catalog recipe\n"), 2)
        # The node picker names machines by hostname and passes the stable node_id.
        self.assertIn("Select a confirmed physical node\nfixture-host\n", choices)
        self.assertNotIn("fixture-node", choices)

    def test_failed_action_keeps_the_session(self):
        _, result, actions, choices, _ = self.run_menu(["#0", "Check now (suggested)", "fixture-host", "Back", "Back"], action_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertIn("✗ Check now failed for", result.stdout)
        self.assertIn("(exit 1)", result.stdout)
        self.assertEqual(choices.count("Choose one operation\n"), 2)

    def test_restore_previews_the_plan_before_a_specific_confirmation(self):
        spec, result, actions, _, questions = self.run_menu(["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["model-library.sh", "restore", spec, "--node", "fixture-node", "--plan", "--json"],
                                   ["model-library.sh", "restore", spec, "--node", "fixture-node", "--yes"]])
        self.assertIn("Restoration preview", result.stdout)
        self.assertRegex(questions, r"Restore \S+ @ \w{8} from the archive to ")

    def test_declined_restore_changes_nothing(self):
        _, result, actions, _, _ = self.run_menu(["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([a[-1] for a in actions], ["--json"])
        self.assertIn("Nothing changed.", result.stdout)

    def test_ctrl_c_at_a_confirmation_leaves_the_menu(self):
        _, result, actions, _, questions = self.run_menu(["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["<ctrl-c>"])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual([a[-1] for a in actions], ["--json"])
        self.assertIn("Nothing changed.", result.stdout)
        self.assertIn("Restore", questions)

    def test_blocked_plan_is_shown_without_a_confirmation(self):
        plan = {"plan": {"kind": "pulsar-purge-plan", "eligible": False, "blockers": ["prepared copy is pinned"],
                         "views": [], "actions": []}, "incomplete_preparations": []}
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Storage and archive…", "Purge prepared copies", "Back", "Back"], plan=plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([a[-2:] for a in actions], [["--plan", "--json"]])
        self.assertIn("prepared copy is pinned", result.stdout)
        self.assertIn("The plan is blocked; nothing changed.", result.stdout)
        self.assertEqual(questions, "")

    def test_escape_steps_back_one_level(self):
        _, result, actions, choices, _ = self.run_menu(["#0", "Check now (suggested)", "<esc>", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(choices.count("Choose one operation\n"), 2)

    def test_ctrl_c_at_a_prompt_leaves_the_menu(self):
        _, result, actions, _, _ = self.run_menu(["#0", "<ctrl-c>"])
        self.assertEqual(result.returncode, 130)
        self.assertEqual(actions, [])

    def test_operations_follow_saved_state_and_explain_what_is_hidden(self):
        _, result, _, choices, _ = self.run_menu(["#0", "Back", "Back"], archive_root=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        block = choices.split("Choose one operation\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(block, ["Check now (suggested)", "Download", "Start", "Stop", "Live status",
                                 "Storage and archive…", "Show details", "Back"])
        shown = " ".join(result.stdout.split())
        self.assertIn("Suggested: Check now — no saved check", shown)
        self.assertIn("Not shown: Restore, Verify archive (no archive location is configured)", shown)
        self.assertIn("Not shown: Prepare, Move home, Create archive, Remove home (no home is recorded)", shown)

    def test_guarded_spec_menu_leaves_out_start_and_explains_why(self):
        _, result, actions, choices, _ = self.run_menu(["#0", "Back", "Back"], archive_root=None, guarded=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        block = choices.split("Choose one operation\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(block, ["Check now (suggested)", "Download", "Stop", "Live status",
                                 "Storage and archive…", "Show details", "Back"])
        shown = " ".join(result.stdout.split())
        self.assertIn("Not shown: Start (this Stack cannot run the spec's serving guard)", shown)


if __name__ == "__main__": unittest.main()
