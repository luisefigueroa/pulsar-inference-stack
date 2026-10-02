"""Saved catalog views remain distinct from live verification and serving."""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.catalog import archive_fact, argument_groups, entries, render, age_seconds
from model_library.node_names import NodeNames
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

    def add_spec(self, guarded=False, historical=False):
        from release_spec.tests.test_serving_guard import FIXTURES, guarded as with_guard
        if historical:
            # A schema-1 record: readable and stoppable, never started.
            helper = runpy.run_path(str(ROOT / "tests/test_release_contribution.py"))["make_contribution"]
            spec, _, _, _ = helper(self.root / "fixture")
        else:
            # A schema-2 spec; guarded adds a serving guard to recipe.container.
            spec = json.loads((FIXTURES / "spec.json").read_text())
            if guarded:
                spec = with_guard(spec)
        (self.repo / "releases").mkdir(exist_ok=True)
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        return spec

    def observe(self, spec, **fields):
        self.store.put("observations", spec["spec_id"], {"schema_version": 1,
            "kind": "pulsar-saved-observation", "spec_id": spec["spec_id"],
            "checked_at": "2026-09-05T00:00:00Z", **fields})

    def show(self, rows, *, details=False, width=80, location="configured", names=None):
        output = io.StringIO()
        render(rows, details=details, writer=TerminalWriter(width=width, stream=output),
               names=names or NodeNames({"node-a": "spark-1"}), location=location, now=self.now)
        return output.getvalue()

    def test_empty_catalog_does_not_create_state(self):
        self.assertEqual(entries(self.repo, self.store), [])
        self.assertFalse(self.store.root.exists())
        output = io.StringIO(); render([], writer=TerminalWriter(stream=output))
        self.assertIn("catalog is empty", output.getvalue())
        self.assertIn("Specs appear when the maintainer publishes them under releases/.", output.getvalue())

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
        # Unset metadata adds no rows; set metadata is shown as written.
        for details in (False, True):
            text = self.show([row], details=details)
            self.assertNotIn("not specified", text)
            self.assertNotRegex(text, r"(?m)^  (State|Review) ")
        spec["state"] = "released"
        spec["review"] = {"status": "validated", "reviewer": "example-reviewer",
                          "reviewed_at": "2026-09-03T00:00:00Z"}
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        text = self.show(entries(self.repo, self.store, now=self.now))
        self.assertRegex(text, r"(?m)^  State +released$")
        self.assertRegex(text, r"(?m)^  Review +validated$")

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
        text = self.show([row])
        self.assertRegex(text, r"(?m)^  Files +prepared on every rank \(checked 1 hour ago\)$")
        self.assertTrue(" ".join(text.split()).endswith("start rechecks everything."), text)
        self.assertNotIn("running", text)
        self.assertNotIn("running", row)

    def test_known_home_and_archive_do_not_imply_current_health(self):
        spec = self.add_spec(historical=True); manifest = spec["identity"]["snapshot_manifest"]["manifest_id"]
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
        spec["review"] = {"status": "withdrawn", "reviewer": "example-reviewer",
                          "reviewed_at": "2026-09-03T00:00:00Z", "reason": "Later testing found inconsistent answers."}
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        rows = entries(self.repo, self.store)
        self.assertEqual(len(rows), 1)
        output = io.StringIO(); render(rows, writer=TerminalWriter(stream=output))
        self.assertIn("Later testing found inconsistent answers", output.getvalue())
        self.assertIn("Exact serving remains possible", output.getvalue())

    def test_historical_spec_stays_listed_and_says_start_is_unsupported(self):
        spec = self.add_spec(historical=True)
        row = entries(self.repo, self.store)[0]
        self.assertEqual((row["start_supported"], row["start_unsupported_reason"]), (False, "historical_spec"))
        self.assertRegex(self.show([row]), r"(?m)^  Start +not supported by this Stack \(historical schema-1 spec\)$")
        self.assertEqual(row["spec_id"], spec["spec_id"])

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
            text = self.show([rows[guarded["spec_id"]]], details=details)
            self.assertRegex(text, r"(?m)^  Start +not supported by this Stack \(serving guard\)$")
            text = self.show([rows[ordinary["spec_id"]]], details=details)
            self.assertNotIn("not supported by this Stack", text)

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
        text = self.show(entries(self.repo, self.store, now=self.now))
        self.assertRegex(text, r"(?m)^  Archive +present at last check, not verified$")
        self.assertNotIn("Last archive verification", text)

    def test_one_archive_line_reconciles_the_check_with_the_verification_record(self):
        # The contradictions this replaces: "Archive unknown" beside a saved
        # verification, and "present; verify before restore" beside a newer one.
        spec = self.add_spec(); manifest = spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        self.store.put("archives", manifest, {"snapshot_manifest_id": manifest, "verified": True,
                                              "verified_at": "2026-08-17T01:00:00Z"})
        text = self.show(entries(self.repo, self.store, now=self.now))
        self.assertRegex(text, r"(?m)^  Archive +verified 19 days ago$")
        self.assertEqual(text.count("Archive"), 1)
        self.observe(spec, local_state="ready", archive_state="missing")
        text = self.show(entries(self.repo, self.store, now=self.now))
        self.assertRegex(text, r"(?m)^  Archive +not found at last check \(verified 19 days ago before that\)$")
        self.store.put("archives", manifest, {"snapshot_manifest_id": manifest, "verified": True,
                                              "verified_at": "2026-09-04T12:00:00Z"})
        self.observe(spec, local_state="ready", archive_state="present")
        text = self.show(entries(self.repo, self.store, now=self.now))
        self.assertRegex(text, r"(?m)^  Archive +verified 13 hours ago$")

    def test_archive_fact_table(self):
        hour, day = 3600, 86400
        record = {"verified_at": "fixture"}
        # (check state, checked, check age, record, record age) -> wording
        table = [
            (("unknown", False, None, None, None), "unknown: never checked"),
            (("unknown", False, None, record, 19 * day), "verified 19 days ago"),
            (("unknown", True, 2 * hour, None, None), "unknown (checked 2 hours ago)"),
            (("unknown", True, 2 * hour, record, 19 * day), "verified 19 days ago"),
            (("verified", True, 13 * hour, record, 13 * hour), "verified 13 hours ago"),
            (("verified", True, 13 * hour, record, 3 * day), "verified 13 hours ago"),
            (("verified", True, 13 * hour, None, None), "verified 13 hours ago"),
            (("present", True, 2 * hour, None, None), "present at last check, not verified"),
            (("present", True, 2 * hour, record, 13 * hour), "verified 13 hours ago"),
            (("present", True, 2 * day, record, 13 * hour), "verified 13 hours ago"),
            (("missing", True, 2 * hour, None, None), "not found at last check"),
            (("missing", True, 2 * hour, record, 19 * day), "not found at last check (verified 19 days ago before that)"),
            (("missing", True, 2 * day, record, 13 * hour), "verified 13 hours ago"),
            (("unavailable", True, 2 * hour, None, None), "unavailable at last check"),
            (("unavailable", True, 2 * hour, record, 19 * day),
             "unavailable at last check (verified 19 days ago before that)"),
            (("unavailable", True, 2 * day, record, 13 * hour), "verified 13 hours ago"),
            (("not-configured", True, 2 * hour, None, None), "archive location not configured at last check"),
            (("not-configured", True, 2 * hour, record, 19 * day),
             "archive location not configured at last check (verified 19 days ago before that)"),
            (("not-configured", True, 2 * day, record, 13 * hour), "verified 13 hours ago"),
        ]
        for (state, checked, check_age, saved, saved_age), wording in table:
            with self.subTest(state=state, checked=checked, record=saved is not None, check_age=check_age):
                self.assertEqual(archive_fact(state, check_age, saved, saved_age, checked=checked)[2], wording)

    def test_future_observation_age_is_unknown(self):
        self.assertIsNone(age_seconds("2027-01-01T00:00:00Z", self.now))

    def test_unknown_or_equal_archive_times_do_not_claim_an_order(self):
        for check_age, record_age in ((None, 3600), (3600, None), (None, None), (3600, 3600)):
            with self.subTest(check_age=check_age, record_age=record_age):
                kind, _, wording = archive_fact("missing", check_age, {"verified": True}, record_age)
                self.assertEqual(kind, "missing")
                self.assertIn("order relative to check unknown", wording)
                self.assertNotIn("before that", wording)

    def put_home(self, spec, node="node-a"):
        manifest = spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        self.store.put("homes", manifest, {"schema_version": 1, "kind": "pulsar-home",
            "snapshot_manifest_id": manifest, "node_id": node, "hub_path": "/nonexistent/home",
            "path": "/nonexistent/home/snapshot", "verification": {"snapshot_manifest_id": manifest},
            "verified_at": "2026-09-05T00:00:00Z"})
        return manifest

    def test_list_leads_with_state_and_fits_80_and_44_columns(self):
        spec = self.add_spec()
        self.put_home(spec)
        for width in (80, 44):
            text = self.show(entries(self.repo, self.store, now=self.now), width=width)
            lines = text.splitlines()
            self.assertTrue(all(len(line) <= width for line in lines), text)
            # The block leads with the model and spec, then saved state; one caveat ends the view.
            self.assertTrue(lines[0].startswith("example/model"), text)
            self.assertIn(f"spec {spec['spec_id'][:12]}", text)
            self.assertRegex(text, r"(?m)^  Recipe +1 node · tensor parallel 1$")
            self.assertRegex(text, r"(?m)^  Files +unknown: never checked$")
            self.assertRegex(text, r"(?m)^  Archive +unknown: never checked$")
            self.assertEqual(" ".join(text.split("\n\n")[-1].split()),
                             "Saved records. ./pulsar models check SPEC refreshes them; start rechecks everything.")
            for retired in ("not specified", "choose Check now", "node(s)", "Home record", "Copy records",
                            "Last archive verification", "Files / archive"):
                self.assertNotIn(retired, text)

    def test_details_keep_flags_with_values_and_the_digest_whole(self):
        spec = self.add_spec()
        self.put_home(spec)
        pairs = [(flag, value) for flag, value in zip(spec["recipe"]["engine_args"], spec["recipe"]["engine_args"][1:])
                 if flag.startswith("--") and not value.startswith("--")]
        self.assertTrue(pairs)
        for width in (80, 44):
            text = self.show(entries(self.repo, self.store, now=self.now), details=True, width=width)
            lines = text.splitlines()
            self.assertIn("    " + spec["recipe"]["image_digest"], lines)
            for flag, value in pairs:
                line = next(line for line in lines if re.search(rf"(^| ){re.escape(flag)}( |$)", line))
                # The value starts on the flag's line; only an overlong value breaks later.
                self.assertIn(f"{flag} {shlex.quote(value)[:10]}", line, text)
            self.assertRegex(text, r"(?m)^  Home +spark-1$")
            self.assertIn("/nonexistent/home/snapshot", text)
            self.assertRegex(text, r"(?m)^  Prepared copies +none recorded$")

    def test_argument_groups_pair_flags_with_values(self):
        self.assertEqual(argument_groups(["--max-model-len", "8192", "--enforce-eager", "--seed", "-1",
                                          "--speculative-config", '{"method": "mtp"}', "--x=y", "plain"]),
                         ["--max-model-len 8192", "--enforce-eager", "--seed -1",
                          "--speculative-config '{\"method\": \"mtp\"}'", "--x=y", "plain"])

    @classmethod
    def help_text(cls, *command):
        cache = cls.__dict__.get("_help", None)
        if cache is None:
            cache = cls._help = {}
        if command not in cache:
            env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_"))}
            result = subprocess.run([str(ROOT / "pulsar"), *command, "--help"], env={**env, "COLUMNS": "100"},
                                    stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60)
            assert result.returncode == 0, result.stderr
            cache[command] = result.stdout
        return cache[command]

    def suggested(self, text):
        lines = text.splitlines()
        start = next((index for index, line in enumerate(lines) if line.startswith("  Suggested ")), None)
        if start is None:
            return None
        command = [lines[start][len("  Suggested"):].strip()]
        while command[-1].endswith(" \\"):
            start += 1
            command.append(lines[start].strip())
        return "\n".join(command)

    def assert_pasteable(self, command):
        syntax = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        tokens = shlex.split(command.replace("\\\n", " "))
        self.assertEqual(tokens[0], "./pulsar")
        # models check forwards to the model check operation and its options.
        help_command = ("model",) if tokens[1] in ("model", "models") else (tokens[1],)
        for flag in (token for token in tokens if token.startswith("--")):
            self.assertIn(flag, self.help_text(*help_command), (command, help_command))
        return tokens

    def assert_suggestion(self, expected, location="configured"):
        for details in (False, True):
            for width in (80, 44):
                text = self.show(entries(self.repo, self.store, now=self.now), details=details,
                                 width=width, location=location)
                command = self.suggested(text)
                if expected is None:
                    self.assertIsNone(command, text)
                    continue
                self.assertIsNotNone(command, text)
                self.assertEqual(self.assert_pasteable(command), expected, text)

    def test_suggested_command_is_the_menu_step_as_a_pasteable_command(self):
        spec = self.add_spec(); prefix = spec["spec_id"][:12]
        manifest = self.put_home(spec)
        # Never checked, or checked too long ago: check the node the saved home names.
        self.assert_suggestion(["./pulsar", "models", "check", prefix, "--node", "spark-1"])
        self.observe(spec, local_state="ready", archive_state="present", checked_at="2026-09-01T00:00:00Z")
        self.assert_suggestion(["./pulsar", "models", "check", prefix, "--node", "spark-1"])
        self.observe(spec, local_state="changed", archive_state="present")
        self.assert_suggestion(["./pulsar", "model", "prepare", prefix, "--node", "spark-1", "--yes"])
        self.observe(spec, local_state="ready", archive_state="present")
        self.assert_suggestion(["./pulsar", "start", prefix, "--node", "spark-1"])
        # No home and no record naming a node: the command's default destination.
        self.store.remove("homes", manifest)
        self.observe(spec, local_state="missing", archive_state="not-configured")
        acquire = ["./pulsar", "model", "acquire", prefix, "--yes"]
        self.assert_suggestion(acquire, location="not-configured")
        self.store.put("archives", manifest, {"snapshot_manifest_id": manifest, "verified": True,
                                              "verified_at": "2026-09-05T00:30:00Z"})
        self.assert_suggestion(["./pulsar", "model", "restore", prefix, "--yes"])
        self.assert_suggestion(acquire, location="disabled")

    def test_archive_check_and_suggested_command_agree(self):
        spec = self.add_spec()
        manifest = spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        self.store.put("archives", manifest, {"snapshot_manifest_id": manifest, "verified": True,
                                              "verified_at": "2026-08-17T01:00:00Z"})
        for state in ("missing", "unavailable"):
            with self.subTest(state=state):
                self.observe(spec, local_state="missing", archive_state=state)
                self.assert_suggestion(["./pulsar", "model", "acquire", spec["spec_id"][:12], "--yes"])
        self.store.put("archives", manifest, {"snapshot_manifest_id": manifest, "verified": True,
                                              "verified_at": "2026-09-05T00:30:00Z"})
        self.assert_suggestion(["./pulsar", "model", "restore", spec["spec_id"][:12], "--yes"])

    def test_a_guarded_spec_gets_no_suggestion_toward_start(self):
        spec = self.add_spec(guarded=True)
        self.put_home(spec)
        self.assert_suggestion(["./pulsar", "models", "check", spec["spec_id"][:12], "--node", "spark-1"])
        for state in ("ready", "changed", "missing"):
            self.observe(spec, local_state=state, archive_state="present")
            self.assert_suggestion(None)

    def test_narrow_details_and_help_do_not_overflow(self):
        spec = self.add_spec()
        self.observe(spec, local_state="ready", archive_state="verified", blockers=["A long explanatory message is wrapped so an operator can read it at a narrow terminal width."])
        digest = spec["recipe"]["image_digest"]
        for width in (80, 44):
            text = self.show(entries(self.repo, self.store, now=self.now), details=True, width=width)
            # The image digest is the one identifier printed whole, past the width.
            lines = [line for line in text.splitlines() if line != "    " + digest]
            self.assertTrue(all(len(line) <= width for line in lines), text)
        result = subprocess.run([str(ROOT / "pulsar"), "help"], env={**os.environ, "COLUMNS": "44"},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(len(line) <= 44 for line in result.stdout.splitlines()), result.stdout)

    # Scripted operator shell: parameterized doubles for the UI, topology and
    # every operation. No Docker, SSH, topology discovery or model files.
    UI = r'''
require_gum() { :; }
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
rc = int(os.environ.get("ACTION_RC", "0"))
if "--plan" in sys.argv: print(open(os.environ["PLAN_FILE"]).read())
elif sys.argv[1] == "check":
    rc = int(os.environ.get("CHECK_RC", str(rc)))
    observation = json.loads(os.environ["CHECK_OBSERVATION"])
    if observation is not None:
        from model_library.state import Store, now
        spec = sys.argv[2]
        Store(os.environ["PULSAR_MODEL_LIBRARY_DIR"]).put("observations", spec, {
            "schema_version": 1, "kind": "pulsar-saved-observation", "spec_id": spec,
            "checked_at": now(), **observation})
        print(json.dumps({"spec_id": spec, "observation": observation}))
raise SystemExit(rc)
"""

    def run_menu(self, answers, confirms=(), plan=None, action_rc=0, archive_root="/fixture/archive", guarded=False,
                 with_home=False, observation=None, check_observation=None, check_rc=None):
        spec = self.add_spec(guarded=guarded)
        if with_home:
            self.put_home(spec)
        if observation is not None:
            self.observe(spec, **{"checked_at": datetime.now(timezone.utc).isoformat(), **observation})
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
        env = dict(os.environ, PYTHONPATH=str(ROOT), PULSAR_MODEL_LIBRARY_DIR=str(self.store.root),
                   CLUSTER_TOPOLOGY_FILE=str(self.root / "no-topology.json"),
                   MENU_ANSWERS=str(files["answers"]), MENU_CONFIRMS=str(files["confirms"]),
                   CHOICES_LOG=str(files["choices.log"]), CONFIRM_LOG=str(files["confirm.log"]),
                   ACTION_LOG=str(files["action.log"]), PLAN_FILE=str(files["plan.json"]), ACTION_RC=str(action_rc),
                   CHECK_OBSERVATION=json.dumps(check_observation),
                   CHECK_RC=str(action_rc if check_rc is None else check_rc))
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
        spec, result, actions, choices, _ = self.run_menu(
            ["#0", "Check now (suggested)", "fixture-host", "Back", "Back"], with_home=True,
            check_observation={"local_state": "ready", "archive_state": "unknown", "blockers": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["model-library.sh", "check", spec, "--node", "fixture-node", "--json"]])
        self.assertIn("✓ Check now finished for", result.stdout)
        self.assertEqual(choices.count("Choose one operation\n"), 2)
        self.assertEqual(choices.count("Select a catalog spec\n"), 2)
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

    def test_recorded_home_does_not_prevent_a_restore_preview(self):
        spec, result, actions, _, questions = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], with_home=True,
            observation={"local_state": "changed", "archive_state": "verified", "blockers": ["home files changed"]},
            confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["model-library.sh", "restore", spec, "--node", "fixture-node", "--plan", "--json"]])
        self.assertIn("Restore", questions)
        self.assertIn("Nothing changed.", result.stdout)

    def test_recorded_home_recovery_still_stops_at_a_blocked_plan(self):
        plan = {"kind": "pulsar-restore-plan", "eligible": False, "blockers": ["existing files must not be replaced"]}
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], with_home=True, plan=plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][-2:], ["--plan", "--json"])
        self.assertEqual(questions, "")
        self.assertIn("existing files must not be replaced", result.stdout)

    def test_recorded_missing_files_clear_the_previous_mutation_check(self):
        plan = {"kind": "pulsar-preparation-plan", "eligible": True,
                "actions": [{"rank": 0, "node_id": "fixture-node", "action": "home-view"}]}
        spec, result, actions, choices, _ = self.run_menu(
            ["#0", "Prepare", "fixture-host", "Check now (suggested)", "fixture-host", "Back", "Back"],
            with_home=True, plan=plan, confirms=["yes"], check_rc=1,
            check_observation={"local_state": "missing", "archive_state": "unknown",
                               "blockers": ["required prepared copy is missing"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[-1], ["model-library.sh", "check", spec, "--node", "fixture-node", "--json"])
        last_options = choices.rsplit("Choose one operation\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Prepare (suggested)", last_options)
        self.assertNotIn("Check now (suggested)", last_options)

    def test_recorded_missing_home_guides_restore_after_a_nonzero_check(self):
        spec, result, actions, _, _ = self.run_menu(
            ["#0", "Check now (suggested)", "fixture-host", "Restore (suggested)", "fixture-host", "Back", "Back"],
            confirms=["no"], check_rc=1,
            check_observation={"local_state": "missing", "archive_state": "verified",
                               "blockers": ["no home is registered"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [
            ["model-library.sh", "check", spec, "--node", "fixture-node", "--json"],
            ["model-library.sh", "restore", spec, "--node", "fixture-node", "--plan", "--json"]])

    def test_unrecorded_check_does_not_recommend_start_from_old_readiness(self):
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Check now", "fixture-host", "Back", "Back"], with_home=True, check_rc=1,
            observation={"local_state": "ready", "archive_state": "unknown", "blockers": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        last_options = choices.rsplit("Choose one operation\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Check now (suggested)", last_options)
        self.assertNotIn("Start (suggested)", last_options)

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
        self.assertIn("Not shown: Start (ordinary start cannot enforce the spec's serving guard)", shown)


if __name__ == "__main__": unittest.main()
