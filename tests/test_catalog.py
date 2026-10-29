"""Saved catalog views remain distinct from live verification and serving."""
import copy
from datetime import datetime, timedelta, timezone
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
from model_library.catalog import archive_fact, argument_groups, combined_observation, entries, render, age_seconds
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

    def add_spec(self, guarded=False, historical=False, speculative=False):
        from release_spec.tests.test_serving_guard import FIXTURES, guarded as with_guard
        if historical:
            # A schema-1 record: readable and stoppable, never started.
            helper = runpy.run_path(str(ROOT / "tests/test_release_contribution.py"))["make_contribution"]
            spec, _, _, _ = helper(self.root / "fixture")
        elif speculative:
            # A schema-3 spec requiring the target and a draft snapshot.
            from tests.test_container_runtime import fixture
            spec = fixture(speculative=True)[0]
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

    def test_blocker_text_names_the_snapshot_once_and_each_rank_with_its_node(self):
        from model_library.catalog import blocker_text
        from model_library.node_names import NodeNames
        names = NodeNames({"node-a": "spark-1", "node-b": "spark-2"})
        row = {"snapshots": {"target": {"snapshot_manifest_id": "m-target"},
                             "draft": {"snapshot_manifest_id": "m-draft"}},
               "prepared_copies": [{"rank": 0, "node_id": "node-a", "snapshot_manifest_id": "m-target"},
                                   {"rank": 0, "node_id": "node-b", "snapshot_manifest_id": "m-draft"}]}
        message = "required prepared copy is missing or placement changed"
        self.assertEqual(blocker_text(f"target: required snapshot target: rank 0: {message}", row, names),
                         f"target snapshot · rank 0 (copy recorded on spark-1): {message}")
        self.assertEqual(blocker_text(f"draft: required snapshot draft: rank 0: {message}", row, names),
                         f"draft snapshot · rank 0 (copy recorded on spark-2): {message}")
        # A rank without a copy record is not given a guessed node.
        self.assertEqual(blocker_text(f"target: required snapshot target: rank 1: {message}", row, names),
                         f"target snapshot · rank 1 (no recorded copy): {message}")
        # Schema-2 blockers name the snapshot once, without the aggregate label.
        schema2 = {"prepared_copies": [{"rank": 0, "node_id": "node-a"}]}
        self.assertEqual(blocker_text(f"required snapshot target: rank 0: {message}", schema2, names),
                         f"target snapshot · rank 0 (copy recorded on spark-1): {message}")
        self.assertEqual(blocker_text("node-a: files missing", row, names), "spark-1: files missing")
        self.assertEqual(blocker_text("files missing", row, names), "files missing")

    def test_models_and_bare_status_show_the_same_list(self):
        import pty
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_", "GUM"))}
        env.update(COLUMNS="80", PULSAR_MODEL_LIBRARY_DIR=str(self.store.root), TERM="dumb",
                   PYTHONDONTWRITEBYTECODE="1")
        listed = subprocess.run([str(ROOT / "pulsar"), "models", "list"], env=env, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        # status without a spec: the same list, then how to check one spec live.
        status = subprocess.run([str(ROOT / "pulsar"), "status"], env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(status.stdout, listed.stdout + "\nSelect a spec for a live check: ./pulsar status <spec_id>\n")
        # On a terminal without Gum, pulsar models shows the list instead of refusing.
        primary, secondary = pty.openpty()
        try:
            browse = subprocess.run([str(ROOT / "pulsar"), "models"], env=env, stdin=secondary, text=True,
                                    capture_output=True, timeout=60)
        finally:
            os.close(primary); os.close(secondary)
        self.assertEqual(browse.returncode, 0, browse.stderr)
        self.assertEqual(browse.stdout, listed.stdout)
        self.assertEqual(browse.stderr, "warning: the catalog menu needs Gum on this terminal; showing pulsar models list\n")

    def test_copy_records_show_when_they_were_verified(self):
        from model_library.catalog import blocker_text, _details
        from model_library.node_names import NodeNames
        from scripts.terminal_format import TerminalWriter
        names = NodeNames({"node-a": "spark-1"})
        now = datetime(2026, 10, 12, tzinfo=timezone.utc)
        copy = {"rank": 0, "node_id": "node-a", "path": "/copies/rank-0", "pinned": False,
                "verified_at": "2026-10-10T00:00:00Z"}
        row = {"prepared_copies": [copy]}
        message = "required prepared copy is missing or placement changed"
        # A copy's age can be weighed against the check's age.
        self.assertEqual(blocker_text(f"required snapshot target: rank 0: {message}", row, names, now),
                         f"target snapshot · rank 0 (copy recorded on spark-1, verified 2 days ago): {message}")
        # An unreadable time is left out rather than guessed.
        row = {"prepared_copies": [{**copy, "verified_at": "not a time"}]}
        self.assertEqual(blocker_text(f"required snapshot target: rank 0: {message}", row, names, now),
                         f"target snapshot · rank 0 (copy recorded on spark-1): {message}")
        out = io.StringIO()
        detail = {"spec_id": "a" * 64, "snapshot_revision": "b" * 40, "snapshot_manifest_id": "c" * 64,
                  "image": {"digest": "sha256:" + "d" * 64}, "engine_args": [],
                  "home": {"node_id": "node-a", "path": "/homes/model", "verified_at": "2026-10-11T00:00:00Z"},
                  "prepared_copies": [copy]}
        _details(TerminalWriter(width=80, stream=out), detail, names, 13, now)
        self.assertIn("Home         spark-1 · verified 1 day ago", out.getvalue())
        self.assertIn("rank 0  spark-1 · not pinned · verified 2 days ago", out.getvalue())

    def test_snapshot_blockers_survive_json_key_reordering(self):
        from tests.test_container_runtime import fixture
        spec = fixture(speculative=True)[0]
        (self.repo / 'releases').mkdir()
        (self.repo / 'releases' / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        members = {name: {'local_state': 'missing', 'archive_state': 'unknown',
                          'prepared': {'verified': 0, 'required': 1}, 'blockers': ['files missing']}
                   for name in ('target', 'draft')}
        aggregate = combined_observation(members)
        reordered = json.loads(json.dumps(members, sort_keys=True))
        self.assertEqual(list(reordered), ['draft', 'target'])
        self.assertEqual(combined_observation(reordered), aggregate)
        # Accept either historical order; persistence sorts only object keys.
        for blockers in (aggregate['blockers'], list(reversed(aggregate['blockers']))):
            with self.subTest(blockers=blockers):
                self.observe(spec, **{**aggregate, 'blockers': blockers})
                path = self.store.root / 'observations' / f"{spec['spec_id']}.json"
                original = path.read_bytes()
                row, = entries(self.repo, self.store, now=self.now)
                self.assertEqual(row['local_state'], 'missing')
                self.assertEqual(row['blockers'], blockers)
                self.assertEqual(path.read_bytes(), original)
        for changes in ({'local_state': 'ready'}, {'prepared': {'verified': 2, 'required': 2}},
                        {'blockers': aggregate['blockers'][:-1]},
                        {'blockers': aggregate['blockers'] + [aggregate['blockers'][0]]},
                        {'blockers': ['different blocker']}, {'blockers': [None]}):
            with self.subTest(changes=changes):
                self.observe(spec, **{**aggregate, **changes})
                with self.assertRaisesRegex(StorageError, 'saved aggregate disagrees'):
                    entries(self.repo, self.store, now=self.now)

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
            self.assertRegex(text, r"(?m)^  Home +spark-1 · verified \d+ hours? ago$")
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
ui_keep_styles() { :; }
emit_frame() { cat; }
# FIXTURE_ROWS and FIXTURE_COLUMNS set the terminal's size; unset, it is unknown.
_ui_terminal_rows() { [ -n "${FIXTURE_ROWS:-}" ] && echo "$FIXTURE_ROWS"; }
_ui_terminal_columns() { [ -n "${FIXTURE_COLUMNS:-}" ] && echo "$FIXTURE_COLUMNS"; }
spin() { shift; "$@"; }
# The pager and the Continue hold are recorded where menus are; CONTINUE_RC=130
# answers every hold with Ctrl-C.
page_output() { printf '[page] %s | %s\n\n' "$1" "${*:2}" >>"$CHOICES_LOG"; shift; "$@"; }
wait_continue() { printf '[continue] %s\n\n' "${1:-}" >>"$CHOICES_LOG"; [ "${CONTINUE_RC:-0}" != 130 ] || return 130; }
_pop() { local n; n=$(cat "$1.n" 2>/dev/null || echo 0); echo $((n + 1)) >"$1.n"; sed -n "$((n + 1))p" "$1"; }
choose_index() {
  local header="$1" answer i=0 option; shift
  { printf '%s\n' "$header" "$@"; printf '\n'; } >>"$CHOICES_LOG"
  answer=$(_pop "$MENU_ANSWERS")
  case "$answer" in "") return 99 ;; "<esc>") return 1 ;; "<ctrl-c>") return 130 ;; "#"*) echo "${answer#\#}"; return 0 ;; esac
  # An answer names a choice with or without the effect after " — ".
  for option in "$@"; do
    [ "$option" != "$answer" ] && [ "${option%% — *}" != "$answer" ] || { echo "$i"; return 0; }
    i=$((i + 1))
  done
  echo "MISSING OPTION: $answer" >>"$CHOICES_LOG"; return 99
}
confirm() {
  printf '%s\n' "$1" >>"$CONFIRM_LOG"
  case "$(_pop "$MENU_CONFIRMS")" in yes) return 0 ;; "<ctrl-c>") return 130 ;; *) return 1 ;; esac
}
confirm_or_review() { confirm "$1"; }
''' + "".join(re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", (ROOT / "scripts/ui.sh").read_text(), re.S | re.M).group(0)
             for name in ("elapsed_text", "report_outcome"))
    ACTION = """#!/usr/bin/env python3
import json,os,sys
open(os.environ["ACTION_LOG"],"a").write(json.dumps([os.path.basename(sys.argv[0])]+sys.argv[1:])+"\\n")
rc = int(os.environ.get("ACTION_RC", "0"))
if os.path.basename(sys.argv[0]) == "pulsar":
    fixture = json.loads(os.environ["LAUNCH_FIXTURE"])
    if sys.argv[1] == "start":
        mode = "dry" if "--dry-run" in sys.argv else ("accept" if "--accept-memory-warn" in sys.argv else "start")
        rc = fixture.get(mode + "_rc", 0)
        if os.environ.get("PULSAR_START_BLOCKERS_FILE"):
            with open(os.environ["PULSAR_START_BLOCKERS_FILE"], "w") as stream:
                # A blocker is a code, or a record such as {"blocker": ..., "stage": "launch"}.
                for code in fixture.get("blockers", []):
                    record = code if isinstance(code, dict) else {"blocker": code}
                    stream.write(json.dumps({"field": "blocker", "stage": "check", **record}) + "\\n")
    elif sys.argv[1:3] == ["image", "stage"] and "--plan" in sys.argv:
        state = fixture.get("image_state", "missing")
        plan = {"schema_version": 1, "kind": "pulsar-image-check", "operation": "stage-image",
                "model": sys.argv[3], "image": fixture["image"], "nodes": 1,
                "mode": "pull-exact-digest" if "--pull" in sys.argv else "stream-from-controller",
                "ranks": [{"rank": 0, "topology_index": 0, "state": state}]}
        print(json.dumps({"schema_version": 1, "ok": True, "result": plan}))
        rc = fixture.get("plan_rc", 0)
    elif sys.argv[1:3] == ["image", "stage"]:
        rc = fixture.get("stage_rc", 0)
    elif sys.argv[1] == "inventory":
        print(os.environ.get("INVENTORY_FIXTURE", ""))
        rc = int(os.environ.get("INVENTORY_RC", "0"))
elif os.environ.get("ACTION_RC_SEQUENCE") and "--yes" in sys.argv:
    # Successive confirmed storage operations exit with these statuses in turn.
    counter = os.environ["ACTION_LOG"] + ".n"
    index = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(index + 1))
    rc = int(os.environ["ACTION_RC_SEQUENCE"].split(",")[index])
elif sys.argv[1] in ("acquire", "restore") and "--yes" in sys.argv and os.environ.get("RECORD_HOME") and rc == 0:
    # As the real operations do after verifying the files: record the home.
    from model_library.state import Store
    manifest = os.environ["RECORD_HOME"]
    Store(os.environ["PULSAR_MODEL_LIBRARY_DIR"]).put("homes", manifest, {
        "schema_version": 1, "kind": "pulsar-home", "snapshot_manifest_id": manifest, "node_id": "fixture-node",
        "hub_path": "/nonexistent/home", "path": "/nonexistent/home/snapshot",
        "verification": {"snapshot_manifest_id": manifest}, "verified_at": "2026-09-05T00:00:00Z"})
elif "--plan" in sys.argv:
    print(open(os.environ["PLAN_FILE"]).read())
    rc = int(os.environ.get("PLAN_RC", str(rc)))
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
if os.environ.get("ACTION_SIGNAL_PARENT") == "1" and "--plan" not in sys.argv:
    # Only the fixture's waiting Bash process receives this test signal.
    import signal
    os.kill(os.getppid(), signal.SIGINT)
raise SystemExit(rc)
"""

    def run_menu(self, answers, confirms=(), plan=None, action_rc=0, archive_root="/fixture/archive", guarded=False,
                 with_home=False, observation=None, check_observation=None, check_rc=None, launch=None, review=None,
                 menu_args=(), additional_specs=(), empty_catalog=False, plan_rc=None, interrupt_parent=False,
                 inventory=None, inventory_rc=0, env_extra=None, speculative=False, home_node="node-a"):
        spec = self.add_spec(guarded=guarded, speculative=speculative)
        if review is not None:
            spec["review"] = review
            (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        if with_home:
            self.put_home(spec, node=home_node)
        if observation is not None:
            self.observe(spec, **{"checked_at": datetime.now(timezone.utc).isoformat(), **observation})
        for extra in additional_specs:
            (self.repo / "releases" / f"{extra['spec_id']}.json").write_bytes(pretty_json_bytes(extra))
        shell_root = self.root / "shell"; scripts = shell_root / "scripts"; scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/model-storage.sh", scripts / "model-storage.sh")
        for name in ("with-connect-card.sh", "connect_card.py"):
            shutil.copy(ROOT / "scripts" / name, scripts / name)
        (scripts / "lib.sh").write_text('PULSAR_MODEL_LIBRARY_DIR="'+str(self.store.root)+'"\nrequire_cluster_nodes() { CLUSTER_NODE_IDS=(fixture-node); CLUSTER_NODE_HOSTNAMES=(fixture-host); }\nhuman_node_name() { printf "%s\\n" "${CLUSTER_NODE_HOSTNAMES[$1]}"; }\n')
        (scripts / "ui.sh").write_text(self.UI)
        for name in ("model-library.sh", "status.sh", "up.sh", "down.sh"):
            (scripts / name).write_text(self.ACTION); (scripts / name).chmod(0o700)
        (shell_root / "pulsar").write_text(self.ACTION)
        (shell_root / "pulsar").chmod(0o700)
        files = {name: self.root / name for name in ("answers", "confirms", "choices.log", "confirm.log", "action.log", "plan.json")}
        files["answers"].write_text("\n".join(answers) + "\n")
        files["confirms"].write_text("\n".join(confirms) + "\n")
        files["plan.json"].write_text(json.dumps(plan or {"kind": "pulsar-restore-plan", "selected_node": "fixture-node"}))
        env = dict(os.environ, PYTHONPATH=str(ROOT), PULSAR_MODEL_LIBRARY_DIR=str(self.store.root),
                   CLUSTER_TOPOLOGY_FILE=str(self.root / "no-topology.json"),
                   MENU_ANSWERS=str(files["answers"]), MENU_CONFIRMS=str(files["confirms"]),
                   CHOICES_LOG=str(files["choices.log"]), CONFIRM_LOG=str(files["confirm.log"]),
                   ACTION_LOG=str(files["action.log"]), PLAN_FILE=str(files["plan.json"]), ACTION_RC=str(action_rc),
                   PLAN_RC=str(action_rc if plan_rc is None else plan_rc),
                   ACTION_SIGNAL_PARENT="1" if interrupt_parent else "0",
                   CHECK_OBSERVATION=json.dumps(check_observation),
                   CHECK_RC=str(action_rc if check_rc is None else check_rc),
                   LAUNCH_FIXTURE=json.dumps({"image": spec["recipe"]["image_digest"], **(launch or {})}),
                   INVENTORY_RC=str(inventory_rc),
                   INVENTORY_FIXTURE=inventory(spec["spec_id"]) if callable(inventory) else (inventory or ""))
        if callable(env_extra):
            env.update(env_extra(spec["spec_id"]))
        env.pop("PULSAR_COLD_ROOT", None)
        if archive_root is not None:
            env["PULSAR_COLD_ROOT"] = archive_root
        if empty_catalog:
            (shell_root / "releases").mkdir()
        else:
            shutil.copytree(self.repo / "releases", shell_root / "releases")
        # The catalog reads the fixture releases root through a tiny python3 launcher.
        binary = self.root / "bin"; binary.mkdir()
        python = binary / "python3"
        python.write_text('#!/usr/bin/env bash\nif [ "${1:-}" = -m ] && [ "${2:-}" = model_library.catalog ]; then shift 2; exec '+sys.executable+' -m model_library.catalog --repo-root '+str(shell_root)+' "$@"; fi\nexec '+sys.executable+' "$@"\n')
        python.chmod(0o700); env["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
        result = subprocess.run(["bash", str(scripts / "model-storage.sh"), "menu", *menu_args], env=env, text=True, capture_output=True)
        log = files["action.log"]
        actions = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        choices = files["choices.log"].read_text()
        self.assertNotIn("MISSING OPTION", choices)
        confirm_log = files["confirm.log"]
        return spec["spec_id"], result, actions, choices, confirm_log.read_text() if confirm_log.exists() else ""

    def test_read_only_catalog_opens_saved_details_without_operations(self):
        spec, result, actions, choices, questions = self.run_menu(
            ["#0", "Show details", "Published results", "Back", "Back"], archive_root=None, menu_args=("--read-only",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(questions, "")
        self.assertIn(spec, result.stdout)
        self.assertIn("Image", result.stdout)
        self.assertIn("Arguments", result.stdout)
        self.assertIn("Saved records:", result.stdout)
        self.assertIn("Catalog spec (read-only)\nShow details\nPublished results\nCompare catalog specs\nBack\n", choices)
        self.assertIn("None supplied.", result.stdout)
        self.assertEqual(choices.count("Select a catalog spec (read-only)\n"), 2)
        self.assertNotIn("Choose one operation", choices)
        self.assertFalse(self.store.root.exists())
        self.assertIn(f"[page]  | python3 -m model_library.catalog show {spec}\n", choices)
        self.assertIn(f"[page]  | python3 -m model_library.catalog results {spec}\n", choices)
        self.assertNotIn("[continue]", choices)

    def test_catalog_comparison_uses_only_the_two_selected_published_specs(self):
        from release_spec.tests.test_serving_guard import FIXTURES, guarded
        extra = guarded(json.loads((FIXTURES / "spec.json").read_text()))
        spec, result, actions, choices, questions = self.run_menu(
            ["#0", "Compare catalog specs", "#0", "Back", "Back"],
            menu_args=("--read-only",), additional_specs=(extra,))
        self.assertEqual(result.returncode, 0, result.stderr)
        before, after = sorted([spec, extra["spec_id"]])
        releases = self.root / "shell/releases"
        self.assertEqual(actions, [["pulsar", "spec", "compare", "--before", str(releases / f"{before}.json"),
                                   "--after", str(releases / f"{after}.json")]])
        self.assertEqual(questions, "")
        self.assertEqual(choices.count("Catalog spec (read-only)\n"), 2)
        self.assertIn(f"Selected spec: {before}", result.stdout)

    def test_single_catalog_spec_has_a_clear_comparison_return(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Compare catalog specs", "Back", "Back"], menu_args=("--read-only",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(questions, "")
        self.assertIn("No other catalog spec supports comparison", result.stdout)
        self.assertEqual(choices.count("Catalog spec (read-only)\n"), 2)

    def test_storage_budget_inspects_all_nodes_without_confirming_or_changing_suggestion(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Storage and archive…", "Storage budget (all nodes)", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "model", "budget"]])
        self.assertEqual(questions, "")
        self.assertEqual(choices.count("Check now (suggested) — "), 2)
        self.assertNotIn('Which machine', choices)
        self.assertIn('Inspect storage budget finished for all confirmed nodes', result.stdout)
        self.assertEqual(choices.count("[continue]"), 1)
        self.assertLess(choices.index("[continue]"), choices.rindex("Choose one operation"))

    def test_empty_catalog_still_offers_storage_budget_inspection(self):
        _, result, actions, choices, questions = self.run_menu(
            ["Storage budget (all nodes)", "Back"], empty_catalog=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "model", "budget"]])
        self.assertEqual(questions, "")
        self.assertIn('The catalog is empty.', result.stdout)
        self.assertLess(choices.index("[continue]"), choices.rindex("Select a catalog spec"))

    def test_esc_from_read_only_details_returns_to_catalog(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "<esc>", "<esc>"], menu_args=("--read-only",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(questions, "")
        self.assertEqual(choices.count("Select a catalog spec (read-only)\n"), 2)

    def test_ctrl_c_in_read_only_details_exits_the_menu(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "<ctrl-c>"], menu_args=("--read-only",))
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(questions, "")
        self.assertEqual(choices.count("Select a catalog spec (read-only)\n"), 1)

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
        self.assertIn("Which machine's files should be checked?\nfixture-host\n", choices)
        self.assertNotIn("fixture-node", choices)

    def test_failed_action_keeps_the_session(self):
        _, result, actions, choices, _ = self.run_menu(["#0", "Check now (suggested)", "fixture-host", "Back", "Back"], action_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertIn("✗ Check now failed for", result.stdout)
        self.assertIn("exit 1 after", result.stdout)
        self.assertEqual(choices.count("Choose one operation\n"), 2)

    def test_check_without_a_readable_observation_leaves_the_result_unknown(self):
        _, result, _, _, _ = self.run_menu(["#0", "Check now (suggested)", "fixture-host", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("did not return a readable saved observation", result.stderr)
        outcome = " ".join(result.stdout.split())
        self.assertRegex(outcome, r"\? Check now could not finish for \S+, so its result is unknown\. exit 3 after")
        self.assertNotIn("rejected", outcome)

    def test_live_status_not_running_reads_as_safe_to_start(self):
        _, result, actions, _, _ = self.run_menu(["#0", "Live status", "fixture-host", "Back", "Back"], action_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[0][0], "status.sh")
        outcome = " ".join(result.stdout.split())
        self.assertRegex(outcome, r"▲ \S+ is not running\. exit 1 after \d+s · no node runs it, so starting it won't conflict")
        self.assertNotIn("✗", outcome)

    def test_live_status_that_could_not_observe_a_node_says_not_to_start(self):
        _, result, _, _, _ = self.run_menu(["#0", "Live status", "fixture-host", "Back", "Back"], action_rc=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = " ".join(result.stdout.split())
        self.assertRegex(outcome, r"\? Whether \S+ is running is unknown: a node could not be observed\.")
        self.assertIn("don't start it until Live status observes every node", outcome)

    @staticmethod
    def live_inventory(state="running", *, age_minutes=0, unobserved=False):
        """A service inventory double for one spec, observed age_minutes ago."""
        from tests.test_inventory_menu import snapshot
        def build(spec_id):
            document = snapshot(spec_id, state=state)
            document["generated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
            if unobserved:
                document["services"] = []
                document["nodes"]["worker"]["probe_status"] = "unreachable"
            return json.dumps(document)
        return build

    def test_running_services_are_checked_only_when_asked(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Back", "Check running services (service inventory)", "#0", "Back", "Back"],
            inventory=self.live_inventory())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "inventory", "--json"]])
        self.assertEqual(questions, "")
        text = " ".join(result.stdout.split())
        first, _, second = text.partition("Running services: service inventory less than a minute ago.")
        self.assertTrue(second, text)
        self.assertIn("Running? not checked here; choose Live status", first)
        self.assertIn("Next Check now", first)
        self.assertIn("Running? running on fixture-remote (service inventory, less than a minute ago)", second)
        self.assertIn("Next Live status — running on fixture-remote", second)
        menus = choices.split("\n\n")
        # The list, a recipe, the list again, then the list and recipe after the check.
        self.assertNotIn("running on", menus[0] + menus[2])
        self.assertIn("running on 1 node", menus[3])
        self.assertIn("Live status (suggested)", menus[4])

    def test_an_old_inventory_reads_as_last_seen(self):
        _, result, _, choices, _ = self.run_menu(
            ["Check running services (service inventory)", "#0", "Back", "Back"],
            inventory=self.live_inventory(age_minutes=30))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Running? last seen running on fixture-remote (service inventory, 30 minutes ago)", text)
        self.assertIn("last seen running 30 minutes ago", choices)
        self.assertIn("Live status (suggested)", choices)

    def test_an_unobserved_node_is_unknown_not_absent(self):
        _, result, _, choices, _ = self.run_menu(
            ["Check running services (service inventory)", "#0", "Back", "Back"],
            inventory=self.live_inventory(unobserved=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("not observed: fixture-remote", text)
        self.assertIn("Running? unknown; fixture-remote was not observed", text)
        self.assertNotIn("not running", text)

    def test_a_failed_or_unreadable_inventory_leaves_the_menu_usable(self):
        for rc, fixture, expected in ((3, "", "✗ Running services could not be checked; the reason is above. exit 3"),
                                      (0, "not json", "✗ The service inventory could not be read")):
            with self.subTest(rc=rc):
                self.setUp()
                _, result, actions, choices, _ = self.run_menu(
                    ["Check running services (service inventory)", "#0", "Back", "Back"],
                    inventory=fixture, inventory_rc=rc)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, " ".join(result.stdout.split()))
                self.assertIn("Running? not checked here", " ".join(result.stdout.split()))
                self.assertIn("[continue]", choices)

    def test_ctrl_c_while_checking_running_services_exits_the_menu(self):
        _, result, _, choices, _ = self.run_menu(
            ["Check running services (service inventory)", "Back"], inventory="", inventory_rc=130)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(choices.count("Select a catalog spec\n"), 1)

    def test_read_only_catalog_never_offers_a_service_probe(self):
        _, result, _, choices, _ = self.run_menu(["Back"], menu_args=("--read-only",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Check running services", choices)

    def test_live_status_and_stop_outcomes_date_the_frame(self):
        _, result, _, _, _ = self.run_menu(["#0", "Live status", "fixture-host", "Back", "Back"], action_rc=1)
        text = " ".join(result.stdout.split())
        self.assertIn("Running? not running on any node (Live status, less than a minute ago)", text)
        self.setUp()
        _, result, _, _, _ = self.run_menu(["#0", "Live status", "fixture-host", "Back", "Back"], action_rc=0)
        text = " ".join(result.stdout.split())
        self.assertIn("Running? a service exists (Live status, less than a minute ago)", text)
        self.assertIn("Live status (suggested)", text + self.root.joinpath("choices.log").read_text())

    def test_live_status_after_start_replaces_the_started_suggestion(self):
        _, result, actions, _, _ = self.run_menu(
            ["#0", "Start", "fixture-host", "Live status (suggested)", "fixture-host", "Back", "Back"],
            confirms=["yes"], action_rc=1, launch={"start_rc": 0})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([action[:2] for action in actions], [["pulsar", "start"], ["status.sh", actions[1][1]]])
        text = " ".join(result.stdout.split())
        last = text.rpartition("Running?")[2]
        self.assertTrue(last.startswith(" not running on any node (Live status"), last)
        self.assertNotIn("started from this menu", last)

    def test_a_one_node_stop_does_not_claim_absence_everywhere(self):
        _, result, _, _, _ = self.run_menu(["#0", "Stop", "fixture-host", "Back", "Back"], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Running? stopped on fixture-node; other nodes not observed since (Stop,", text)
        self.assertNotIn("not running on any node", text)

    def test_open_in_catalog_opens_one_recipe_with_the_captured_inventory(self):
        captured = self.root / "captured-inventory.json"
        def opened(spec_id):
            captured.write_text(self.live_inventory()(spec_id))
            return {"PULSAR_CATALOG_OPEN_SPEC": spec_id, "PULSAR_CATALOG_INVENTORY": str(captured)}
        _, result, actions, choices, _ = self.run_menu(["Back"], env_extra=opened)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertTrue(choices.startswith("Choose one operation\n"), choices)
        self.assertNotIn("Select a catalog spec", choices)
        self.assertIn("Running? running on fixture-remote", " ".join(result.stdout.split()))
        self.assertTrue(captured.exists())
        self.setUp()
        _, result, _, _, _ = self.run_menu(["<ctrl-c>"], env_extra=opened)
        self.assertEqual(result.returncode, 130, result.stderr)

    def test_refused_preview_says_the_operation_is_not_possible(self):
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["yes"], plan_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(questions, "")
        self.assertEqual([action for action in actions if "--yes" in action], [])
        outcome = " ".join(result.stdout.split())
        self.assertRegex(outcome, r"✗ Restore is not possible for \S+ now; nothing changed\. exit 1 · the reason is above")

    def test_an_operation_outcome_waits_for_continue_and_keeps_ctrl_c(self):
        observation = {"local_state": "ready", "archive_state": "unknown", "blockers": []}
        _, result, _, choices, _ = self.run_menu(
            ["#0", "Check now (suggested)", "fixture-host", "Back", "Back"], check_observation=observation)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✓ Check now finished", result.stdout)
        # The outcome stays on screen until Continue; only then do the frame and menu return.
        blocks = [block.splitlines()[0].strip() for block in choices.strip().split("\n\n")]
        self.assertEqual(blocks, ["Select a catalog spec", "Choose one operation", "Which machine's files should be checked?",
                                  "[continue]", "Choose one operation", "Select a catalog spec"])
        self.setUp()
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Check now (suggested)", "fixture-host"], check_observation=observation,
            env_extra=lambda spec: {"CONTINUE_RC": "130"})
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertTrue(choices.rstrip().endswith("[continue]"), choices)

    def test_interrupted_storage_keeps_partial_effects_unknown_and_suggests_recheck(self):
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["yes"],
            action_rc=130, plan_rc=0, with_home=True,
            observation={"local_state": "ready", "archive_state": "unknown", "blockers": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Interruption requested: Restore", text)
        self.assertIn("not confirmed cleanup or partial effects", text)
        self.assertIn("Retained staging may be recoverable", text)
        self.assertNotIn("Restore finished", text)
        last = choices.rsplit("Choose one operation\n", 1)[1]
        self.assertIn("Check now (suggested)", last)
        self.assertNotIn("Start (suggested)", last)
        self.assertEqual(len(actions), 2)  # preview and requested action only

    def test_interrupted_stop_suggests_live_status_before_any_retry(self):
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Stop", "fixture-host", "Back", "Back"], confirms=["yes"], action_rc=130)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Live status (suggested)", choices.rsplit("Choose one operation\n", 1)[1])
        self.assertIn("Use Live status", " ".join(result.stdout.split()))
        self.assertEqual(len(actions), 1)

    def test_interruption_arriving_with_zero_exit_does_not_claim_completion(self):
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Check now (suggested)", "fixture-host", "Back", "Back"], interrupt_parent=True,
            check_observation={"local_state": "ready", "archive_state": "unknown", "blockers": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Interruption requested: Check now", text)
        self.assertIn("exit 0 · this menu has not confirmed cleanup", text)
        self.assertNotIn("✓ Check now finished", text)
        self.assertNotIn("the last check did not record an observation", text)
        self.assertIn("rerun before relying on saved observations", text)
        self.assertIn("Check now (suggested)", choices.rsplit("Choose one operation\n", 1)[1])
        self.assertEqual(len(actions), 1)

    def test_restore_previews_the_plan_before_a_specific_confirmation(self):
        spec, result, actions, _, questions = self.run_menu(["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["model-library.sh", "restore", spec, "--node", "fixture-node", "--plan", "--json"],
                                   ["model-library.sh", "restore", spec, "--node", "fixture-node", "--yes"]])
        # A summary leads; the full preview opens only on request.
        self.assertIn("Copy the snapshot (size not observed) from the archive to fixture-node", result.stdout)
        self.assertNotIn("Restoration preview", result.stdout)
        self.assertRegex(questions, r"Restore \S+ @ \w{8} from the archive to ")

    # TUI-24: the choice says what each snapshot is for; the operation gets its name.
    def test_restore_asks_for_a_required_snapshot_by_what_it_is_for(self):
        spec, result, actions, choices, questions = self.run_menu(
            ["#0", "Restore", "draft", "fixture-host", "Back", "Back"], confirms=["yes"], with_home=True,
            speculative=True, env_extra=lambda _: {"COLUMNS": "100"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Select a required snapshot\n"
                      "draft — speculative-decoding checkpoint (example/model @ eeeeeeee) · no home recorded\n"
                      "target — served model (example/model @ aaaaaaaa) · home on node-a (not in saved topology)\n\n",
                      choices)
        placement = ["--snapshot", "draft", "--node", "fixture-node"]
        self.assertEqual(actions, [["model-library.sh", "restore", spec, *placement, "--plan", "--json"],
                                   ["model-library.sh", "restore", spec, *placement, "--yes"]])
        self.assertIn("Restore example/model @ eeeeeeee from the archive to ", questions)

    def test_menu_choices_fit_the_terminal_it_draws_on(self):
        # Python runs with its output captured, so the menu passes the width it
        # read from the terminal; an inherited COLUMNS does not decide it.
        spec, result, actions, choices, _ = self.run_menu(
            ["#0", "Restore", "#1", "fixture-host", "Back", "Back"], confirms=["no"], speculative=True,
            env_extra=lambda _: {"FIXTURE_COLUMNS": "44", "COLUMNS": "120"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Select a required snapshot\ntarget — served model · no home recorded\n"
                      "draft · no home recorded\n\n", choices)
        self.assertEqual(actions, [["model-library.sh", "restore", spec, "--snapshot", "draft", "--node",
                                    "fixture-node", "--plan", "--json"]])

    # TUI-25: the node question says what the operation does with the machine,
    # and each machine what saved records place there.
    def test_node_choices_name_what_saved_records_place_on_each_machine(self):
        spec, result, actions, choices, _ = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], with_home=True, home_node="fixture-node",
            confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Which machine should hold the model files?\nfixture-host — home recorded here\n\n", choices)
        # The operation still receives the stable node_id.
        self.assertEqual(actions[0][-4:], ["--node", "fixture-node", "--plan", "--json"])

    def test_recorded_home_does_not_prevent_a_restore_preview(self):
        spec, result, actions, choices, questions = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], with_home=True,
            observation={"local_state": "changed", "archive_state": "verified", "blockers": ["home files changed"]},
            confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["model-library.sh", "restore", spec, "--node", "fixture-node", "--plan", "--json"]])
        self.assertIn("Restore", questions)
        self.assertIn("Nothing changed.", result.stdout)
        self.assertNotIn("[continue]", choices)

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
            ["#0", "Prepare", "fixture-host", "Check now", "fixture-host", "Back", "Back"],
            with_home=True, plan=plan, confirms=["yes"], check_rc=1,
            check_observation={"local_state": "missing", "archive_state": "unknown",
                               "blockers": ["required prepared copy is missing"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[-1], ["model-library.sh", "check", spec, "--node", "fixture-node", "--json"])
        last_options = choices.rsplit("Choose one operation\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Prepare (suggested)", last_options)
        self.assertNotIn("Check now (suggested)", last_options)
        self.assertIn("saved an observation with findings", result.stdout)
        self.assertNotIn("Check now failed", result.stdout)

    def test_a_successful_prepare_suggests_start_and_keeps_the_saved_check_visible(self):
        plan = {"kind": "pulsar-preparation-plan", "eligible": True,
                "actions": [{"rank": 0, "node_id": "fixture-node", "action": "home-view"}]}
        spec, result, actions, choices, _ = self.run_menu(
            ["#0", "Prepare (suggested)", "fixture-host", "Back", "Back"], with_home=True, plan=plan,
            confirms=["yes"], observation={"local_state": "missing", "archive_state": "unknown", "blockers": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[-1], ["model-library.sh", "prepare", spec, "--node", "fixture-node", "--yes"])
        last_options = choices.rsplit("Choose one operation\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Start (suggested)", last_options)
        text = " ".join(result.stdout.split())
        last_frame = text.rpartition("Settings ")[2]
        self.assertIn("Files prepared in this session (less than a minute ago); saved check:", last_frame)
        self.assertIn("Next Start — prepared on every serving rank just now", last_frame)

    def test_operations_run_under_their_name_without_the_effect(self):
        _, result, _, choices, _ = self.run_menu(["#0", "Live status", "fixture-host", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Live status — check whether it runs now", choices)
        self.assertIn("→ Live status example/model", " ".join(result.stdout.split()))
        self.assertNotIn("check whether it runs now", result.stdout)

    def test_a_successful_restore_moves_on_to_prepare(self):
        def record(spec_id):
            spec = json.loads((self.repo / "releases" / f"{spec_id}.json").read_text())
            return {"RECORD_HOME": spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]}
        spec, result, actions, choices, _ = self.run_menu(
            ["#0", "Restore", "fixture-host", "Back", "Back"], confirms=["yes"], env_extra=record)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[-1], ["model-library.sh", "restore", spec, "--node", "fixture-node", "--yes"])
        last_options = choices.rsplit("Choose one operation\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Prepare (suggested)", last_options)
        self.assertNotIn("Check now (suggested)", last_options)
        text = " ".join(result.stdout.split())
        self.assertIn("Next Prepare — restore finished; every required snapshot has a verified home", text)

    def test_a_failed_storage_change_replaces_an_earlier_prepare(self):
        plan = {"kind": "pulsar-preparation-plan", "eligible": True,
                "actions": [{"rank": 0, "node_id": "fixture-node", "action": "home-view"}]}
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Prepare (suggested)", "fixture-host", "Prepare", "fixture-host", "Back", "Back"],
            with_home=True, plan=plan, confirms=["yes", "yes"],
            observation={"local_state": "missing", "archive_state": "unknown", "blockers": []},
            env_extra=lambda spec_id: {"ACTION_RC_SEQUENCE": "0,1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum("--yes" in action for action in actions), 2)
        last_options = choices.rsplit("Choose one operation\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Check now (suggested)", last_options)
        self.assertNotIn("Start (suggested)", last_options)
        last_frame = " ".join(result.stdout.split()).rpartition("Settings ")[2]
        self.assertNotIn("prepared in this session", last_frame)
        self.assertIn("Next Check now — the last storage change did not succeed", last_frame)

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
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Storage and archive…", "Purge prepared copies", "Back", "Back", "Back", "Back"], plan=plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([a[-2:] for a in actions], [["--plan", "--json"]])
        self.assertIn("Blocked: prepared copy is pinned.", result.stdout)
        self.assertIn("The plan is blocked; nothing changed.", result.stdout)
        self.assertEqual(questions, "")
        # The reason stays on screen at the blocked plan's own choices; Back
        # returns to the submenu.
        held = choices.index("Blocked plan\nShow full preview\nBack\n")
        self.assertLess(choices.index("Storage and archive\n"), held)
        self.assertLess(held, choices.rindex("Storage and archive\n"))
        self.assertNotIn("[continue]", choices)

    def container_plan(self, *containers):
        """A blocked purge whose prepared copy is mounted by CONTAINERS (labels as the planner reads them)."""
        details = [{"code": "container-reference", "message": "a container references the prepared copy",
                    "node_id": "fixture-node", "container": container, "mount": "/fixture/hub",
                    "path": "/fixture/hub/snapshot", "fix": "fix"} for container in containers]
        return {"plan": {"kind": "pulsar-purge-plan", "eligible": False, "views": [], "actions": [],
                         "blockers": ["a container references the prepared copy"], "blocker_details": details},
                "incomplete_preparations": []}

    @staticmethod
    def blocked_menu(choices):
        """The options of the first Blocked plan menu, through its Back."""
        lines = choices[choices.index("Blocked plan\n"):].splitlines()
        return lines[1:lines.index("Back") + 1]

    def test_blocked_plan_offers_stop_for_the_catalog_specs_pulsar_container(self):
        from release_spec.tests.test_serving_guard import FIXTURES
        fixture = json.loads((FIXTURES / "spec.json").read_text())
        spec_id, model = fixture["spec_id"], fixture["recipe"]["model"]["model_id"]
        plan = self.container_plan({"id": "c" * 64, "name": "pulsar-fixture", "state": "running", "spec_id": spec_id},
                                   {"id": "d" * 64, "name": "someone-else", "state": "stopped"})
        stop = f"Stop {model} ({spec_id[:12]})"
        spec, result, actions, choices, questions = self.run_menu(
            ["#0", "Storage and archive…", "Purge prepared copies", "Show full preview", stop, "Back", "Back", "Back"],
            plan=plan, confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(spec, spec_id)
        self.assertEqual(self.blocked_menu(choices),
                         ["Show full preview", stop, "Open Live service inventory", "Back"])
        text = " ".join(result.stdout.split())
        self.assertIn(f"Blocked: running Pulsar container pulsar-fixture (spec {spec_id[:12]})", text)
        self.assertIn("Blocked: stopped container someone-else", text)
        # Show full preview pages the whole plan, then the choices return.
        self.assertIn("[page]  | bash -c", choices)
        self.assertIn("Prepared-copy removal preview", result.stdout)
        # Stop asks first, then stops only the catalog spec's service; nothing
        # removes the other container.
        self.assertEqual(questions, f"Stop {model}? Model files and pins are kept.\n")
        self.assertEqual([action for action in actions if action[-2:] != ["--plan", "--json"]],
                         [["down.sh", spec_id]])
        self.assertNotIn("someone-else", json.dumps(actions))

    def test_blocked_plan_never_offers_to_remove_a_container_pulsar_did_not_start(self):
        plan = self.container_plan({"id": "d" * 64, "name": "someone-else", "state": "running"},
                                   {"id": "e" * 64, "name": "other-spec", "state": "running", "spec_id": "ef" * 32})
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Storage and archive…", "Purge prepared copies", "Open Live service inventory", "Back",
             "Back", "Back", "Back"], plan=plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        # A spec outside the catalog has no Stop; only the inventory opens.
        options = self.blocked_menu(choices)
        self.assertEqual((len(options), options[0], options[-1]), (3, "Show full preview", "Back"))
        self.assertIn("Blocked: running container someone-else on fixture-node (not in saved topology) mounts "
                      "/fixture/hub, which contains these files", " ".join(result.stdout.split()))
        self.assertEqual(questions, "")
        self.assertEqual([action[:3] for action in actions if action[-2:] != ["--plan", "--json"]],
                         [["pulsar", "inventory", "menu"]])

    def test_blocked_plan_names_the_inventory_choice(self):
        # Its spec and model fields are empty; splitting on every tab keeps
        # the label in place (IFS=$'\t' read would merge them).
        plan = self.container_plan({"id": "d" * 64, "name": "other-spec", "state": "running", "spec_id": "ef" * 32})
        _, result, _, choices, _ = self.run_menu(
            ["#0", "Storage and archive…", "Purge prepared copies", "Back", "Back", "Back", "Back"], plan=plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.blocked_menu(choices), ["Show full preview", "Open Live service inventory", "Back"])

    def test_a_plan_the_summary_cannot_read_is_shown_whole(self):
        plan = {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "blockers": [], "views": [],
                         "actions": [{"rank": 0, "node_id": "fixture-node", "action": "remove-copy",
                                      "retained_bindings": 0}]}, "incomplete_preparations": []}
        answers = ["#0", "Storage and archive…", "Purge prepared copies", "Back", "Back", "Back"]
        _, result, _, _, questions = self.run_menu(answers, plan=plan, confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Delete 1 working copy on fixture-node", result.stdout)
        self.assertNotIn("Prepared-copy removal preview", result.stdout)
        self.assertIn("Purge", questions)
        # The summary cannot count these incomplete preparations; the whole
        # rendered plan stands in for it, and nothing is asked or run.
        self.setUp()
        plan["incomplete_preparations"] = 5
        _, result, actions, _, questions = self.run_menu(answers, plan=plan, confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prepared-copy removal preview", result.stdout)
        self.assertNotIn("Delete 1 working copy", result.stdout)
        self.assertEqual(questions, "")
        self.assertEqual([action[-2:] for action in actions], [["--plan", "--json"]])

    def test_escape_steps_back_one_level(self):
        _, result, actions, choices, _ = self.run_menu(["#0", "Check now (suggested)", "<esc>", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(choices.count("Choose one operation\n"), 2)

    def test_escape_from_a_storage_action_returns_to_its_submenu(self):
        _, result, actions, choices, _ = self.run_menu(
            ["#0", "Storage and archive…", "Move home", "<esc>", "Back", "Back", "Back"], with_home=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(choices.count("Storage and archive\n"), 2)

    def test_ctrl_c_from_a_submenu_action_exits_without_mutation(self):
        _, result, actions, _, _ = self.run_menu(
            ["#0", "Storage and archive…", "Move home", "<ctrl-c>"], with_home=True)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(actions, [])

    def test_ctrl_c_at_a_prompt_leaves_the_menu(self):
        _, result, actions, _, _ = self.run_menu(["#0", "<ctrl-c>"])
        self.assertEqual(result.returncode, 130)
        self.assertEqual(actions, [])

    def test_operations_follow_saved_state_and_explain_what_is_hidden(self):
        _, result, _, choices, _ = self.run_menu(["#0", "Back", "Back"], archive_root=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        block = choices.split("Choose one operation\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual([label.split(" — ")[0] for label in block],
                         ["Check now (suggested)", "Download", "Start", "Live status", "Service logs", "Stop",
                          "Launch options…", "Storage and archive…", "Show details", "Published results",
                          "Compare catalog specs", "Back"])
        shown = " ".join(result.stdout.split())
        self.assertIn("Next Check now — no saved check", shown)
        self.assertIn("Not shown Restore, Verify archive (no archive location is configured). Set one in "
                      "Archive storage configuration on the home menu.", shown)
        self.assertIn("Not shown Prepare, Move home, Create archive, Remove home (no home is recorded). "
                      "Download records a home.", shown)
        self.assertNotIn("Check now refreshes", shown)

    # TUI-41: Service logs is read-only, so it opens in the pager with nothing to confirm.
    def test_service_logs_reads_every_rank_without_a_question(self):
        spec, result, actions, choices, questions = self.run_menu(["#0", "Service logs", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "logs", spec]])
        self.assertRegex(choices, rf"\[page\] Reading service logs… \| \S+/pulsar logs {spec}\n")
        self.assertEqual(questions, "")
        self.assertNotIn("Which machine", choices)
        self.assertNotIn("[continue]", choices)
        # A failed read stays on screen until Continue.
        self.setUp()
        _, result, _, choices, _ = self.run_menu(["#0", "Service logs", "Back", "Back"], action_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[continue]", choices)
        self.assertEqual(choices.count("Choose one operation"), 2)

    def test_a_short_terminal_keeps_only_the_first_frame_line_and_next(self):
        for rows, full in (("24", False), ("60", True)):
            with self.subTest(rows=rows):
                self.setUp()
                _, result, _, _, _ = self.run_menu(["#0", "Back", "Back"], archive_root=None,
                                                   env_extra=lambda spec_id: {"FIXTURE_ROWS": rows})
                self.assertEqual(result.returncode, 0, result.stderr)
                shown = " ".join(result.stdout.split())
                self.assertIn("example/model [", shown)
                self.assertIn("Next Check now — no saved check", shown)
                self.assertEqual("Files unknown" in shown, full)
                self.assertEqual("Not shown " in shown, full)

    def test_guarded_spec_menu_leaves_out_start_and_explains_why(self):
        _, result, actions, choices, _ = self.run_menu(["#0", "Back", "Back"], archive_root=None, guarded=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        block = choices.split("Choose one operation\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual([label.split(" — ")[0] for label in block],
                         ["Check now (suggested)", "Download", "Live status", "Service logs", "Stop",
                          "Launch options…", "Storage and archive…", "Show details", "Published results",
                          "Compare catalog specs", "Back"])
        shown = " ".join(result.stdout.split())
        self.assertIn("Not shown Start (ordinary start cannot enforce the spec's serving guard).", shown)


if __name__ == "__main__": unittest.main()
