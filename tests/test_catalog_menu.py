"""Catalog menu decisions come from saved records only and never hide unknown state."""
import copy
from datetime import datetime, timezone
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library import catalog_menu as menu
from model_library.catalog import archive_facts, archive_text, combined_observation, render
from model_library.node_names import NodeNames
from scripts.terminal_format import TerminalWriter

NAMES = NodeNames({"n0": "spark-1", "n1": "spark-2", "n2": "spark-3"})
HOME = {"node_id": "n0", "path": "/fixture/home", "verified_at": "2026-09-05T00:00:00Z"}


def row(**fields):
    base = {
        "spec_id": "ab" * 32, "model_id": "org/model", "snapshot_revision": "0123456789abcdef" * 2 + "01234567",
        "geometry": {"nodes": 3, "tp": 3, "pp": 1}, "local_state": "unknown", "archive_state": "unknown",
        "checked_at": None, "observation_age_seconds": None, "blockers": [], "home": None,
        "archive": None, "prepared_copies": [],
    }
    base.update(fields)
    return base


def frame_text(lines):
    """The recipe frame's text from view lines, whitespace collapsed."""
    return " ".join(" ".join(line.split("\t", 1)[1] for line in lines
                             if line.startswith(("header\t", "next\t"))).split())


def checked(**fields):
    return row(**{"checked_at": "2026-09-05T00:00:00Z", "observation_age_seconds": 7200, **fields})


# Catalog fields for a spec whose recipe.container holds a serving guard.
GUARDED = {"start_supported": False, "start_unsupported_reason": "guard_unsupported"}
GUARD_REASON = "ordinary start cannot enforce the spec's serving guard"


def two_snapshots(target_home, draft_home):
    member = {"model_id": "org/model", "model_commit": "c" * 40, "archive": None}
    return {"target": {**member, "home": target_home}, "draft": {**member, "model_id": "org/draft", "home": draft_home}}


class Operations(unittest.TestCase):
    def test_recovery_available_with_a_recorded_home(self):
        for state in ("unknown", "missing", "changed", "ready"):
            with self.subTest(state=state):
                offered, hidden, _ = menu.operations(checked(home=HOME, local_state=state), "configured")
                self.assertIn("acquire", offered)
                self.assertIn("restore", offered)
                self.assertNotIn("acquire", hidden)
                self.assertNotIn("restore", hidden)

    def test_every_required_snapshot_is_selectable_for_recovery(self):
        for homes in ((HOME, None), (HOME, HOME)):
            with self.subTest(homes=homes):
                _, _, eligible = menu.operations(row(snapshots=two_snapshots(*homes)), "configured")
                self.assertCountEqual(eligible["acquire"], ["target", "draft"])
                self.assertCountEqual(eligible["restore"], ["target", "draft"])

    def test_recorded_home_keeps_every_operation_available(self):
        offered, hidden, _ = menu.operations(checked(home=HOME), "configured")
        self.assertEqual(hidden, {})
        self.assertEqual(offered[:len(menu.MAIN)], list(menu.MAIN))

    def test_missing_home_hides_home_operations(self):
        offered, hidden, _ = menu.operations(row(), "configured")
        for action in ("prepare", "move", "remove", "archive"):
            self.assertIn(action, hidden)
        self.assertIn("acquire", offered)
        self.assertIn("restore", offered)

    def test_unconfigured_archives_hide_archive_operations_with_reason(self):
        _, hidden, _ = menu.operations(checked(home=HOME), "not-configured")
        self.assertEqual(hidden["verify"], "no archive location is configured")
        self.assertEqual(hidden["archive"], "no archive location is configured")
        _, hidden, _ = menu.operations(row(), "disabled")
        self.assertEqual(hidden["restore"], "archives are disabled")

    def test_live_and_node_side_operations_are_always_offered(self):
        for state in (row(), checked(home=HOME, local_state="ready")):
            offered, _, _ = menu.operations(state, "not-configured")
            for action in ("check", "start", "stop", "status", "pin", "unpin", "purge"):
                self.assertIn(action, offered)

    def test_guarded_spec_leaves_out_start_and_keeps_everything_else(self):
        for state in (row(**GUARDED), checked(home=HOME, local_state="ready", **GUARDED)):
            offered, hidden, _ = menu.operations(state, "configured")
            self.assertNotIn("start", offered)
            self.assertEqual(hidden["start"], GUARD_REASON)
            # A guarded service may have been started elsewhere.
            for action in ("check", "stop", "status", "pin", "unpin", "purge"):
                self.assertIn(action, offered)
        offered, hidden, _ = menu.operations(checked(home=HOME, **GUARDED), "configured")
        self.assertEqual(hidden, {"start": GUARD_REASON})
        for action in ("prepare", "move", "archive", "verify", "remove"):
            self.assertIn(action, offered)

    def test_supported_or_unmarked_spec_offers_start(self):
        for fields in ({}, {"start_supported": True, "start_unsupported_reason": None}):
            offered, hidden, _ = menu.operations(checked(home=HOME, **fields), "configured")
            self.assertIn("start", offered)
            self.assertNotIn("start", hidden)

    def test_schema3_names_snapshots_and_prioritizes_missing_homes(self):
        state = row(snapshots=two_snapshots(HOME, None))
        offered, hidden, eligible = menu.operations(state, "configured")
        self.assertEqual(hidden["prepare"], "no home is recorded for snapshot draft")
        self.assertIn("acquire", offered)
        self.assertEqual(eligible["acquire"], ["draft", "target"])
        self.assertEqual(eligible["move"], ["target"])


class Suggestion(unittest.TestCase):
    def suggest(self, state, after=None, location="configured"):
        offered, _, _ = menu.operations(state, location)
        return menu.suggestion(state, offered, location, after, NAMES)

    def test_fresh_missing_files_recommend_a_remedy_despite_blockers(self):
        for state, expected in (
            (checked(local_state="missing", blockers=["no home is registered"]), "acquire"),
            (checked(local_state="missing", archive_state="verified", blockers=["no home is registered"]), "restore"),
            (checked(home=HOME, local_state="missing", blockers=["required prepared copy is missing"]), "prepare"),
            (checked(home=HOME, local_state="changed", blockers=["file hash mismatch"]), "prepare"),
        ):
            with self.subTest(expected=expected, state=state):
                self.assertEqual(self.suggest(state)[0], expected)

    def test_unknown_and_stale_observations_are_checked_before_repair(self):
        for state in (
            checked(local_state="unknown"),
            checked(local_state="unknown", blockers=["node is unreachable"]),
            checked(home=HOME, local_state="unknown", blockers=["node is unreachable"]),
            checked(local_state="missing", observation_age_seconds=None),
            checked(local_state="missing", observation_age_seconds=2 * 86400, blockers=["no home is registered"]),
            checked(snapshots=two_snapshots(HOME, None), local_state="unknown"),
        ):
            with self.subTest(state=state):
                self.assertEqual(self.suggest(state)[0], "check")

    def test_unsaved_check_does_not_reuse_ready_state(self):
        state = checked(home=HOME, local_state="ready")
        self.assertEqual(self.suggest(state, after="check-failed")[0], "check")

    def test_rules_in_order(self):
        self.assertEqual(self.suggest(row())[0], "check")
        self.assertEqual(self.suggest(checked(observation_age_seconds=3 * 86400)), ("check", "last check 3 days ago"))
        self.assertEqual(self.suggest(checked(blockers=["n1: disk full"])), ("check", "saved blocker: spark-2: disk full"))
        self.assertEqual(self.suggest(checked(local_state="missing"))[0], "acquire")
        self.assertEqual(self.suggest(checked(local_state="missing", archive_state="verified"))[0], "restore")
        self.assertEqual(self.suggest(checked(home=HOME, local_state="changed")),
                         ("prepare", "files changed since they were verified (checked 2 hours ago)"))
        self.assertEqual(self.suggest(checked(home=HOME, local_state="ready"))[0], "start")

    def test_restore_needs_a_configured_archive_location(self):
        self.assertEqual(self.suggest(checked(local_state="missing", archive_state="verified"), location="disabled")[0], "acquire")

    def test_session_history_outranks_saved_state(self):
        ready = checked(home=HOME, local_state="ready")
        self.assertEqual(self.suggest(ready, after="move"), ("check", "move home ran after the last check"))
        self.assertEqual(self.suggest(ready, after="start")[0], "status")
        self.assertEqual(self.suggest(ready, after="stop")[0], "start")
        self.assertEqual(self.suggest(ready, after="check")[0], "start")

    def test_guarded_spec_keeps_check_download_restore_and_nothing_toward_start(self):
        self.assertEqual(self.suggest(row(**GUARDED)), ("check", "no saved check"))
        self.assertEqual(self.suggest(checked(local_state="missing", **GUARDED))[0], "acquire")
        self.assertEqual(self.suggest(checked(local_state="missing", archive_state="verified", **GUARDED))[0], "restore")
        self.assertEqual(self.suggest(checked(home=HOME, local_state="ready", **GUARDED), after="prepare"),
                         ("check", "prepare ran after the last check"))
        for local in ("ready", "missing", "changed"):
            for after in (None, "stop", "check"):
                with self.subTest(local=local, after=after):
                    self.assertIsNone(self.suggest(checked(home=HOME, local_state=local, **GUARDED), after))

    def test_suggestion_never_starts_a_guarded_spec(self):
        for state in (row(**GUARDED), checked(**GUARDED), checked(home=HOME, local_state="ready", **GUARDED),
                      checked(home=HOME, local_state="changed", **GUARDED)):
            for after in (None, "stop", "check", "prepare", "restore"):
                result = self.suggest(state, after)
                self.assertNotIn(result and result[0], {"start", "prepare"})

    def test_suggestion_never_stops_or_touches_storage(self):
        for state in (row(), checked(home=HOME, local_state="ready"), checked(home=HOME, local_state="missing")):
            for after in (None, "start", "stop", "purge"):
                result = self.suggest(state, after)
                self.assertNotIn(result and result[0], {"stop", *menu.STORAGE})


class NextLifecycleStep(unittest.TestCase):
    """Download, Restore and Prepare verify what they record, so the menu moves on."""
    NOW = datetime(2026, 9, 5, 2, tzinfo=timezone.utc)

    def suggest(self, state, after, location="configured", live=None):
        offered, _, _ = menu.operations(state, location)
        return menu.suggestion(state, offered, location, after, NAMES, now=self.NOW, live=live)

    def snapshots(self, draft_home=None, *, draft_archive="verified"):
        rows = ArchiveSuggestions.snapshot_row(("present", draft_archive), target_home=HOME,
                                               dates={"draft": "2026-09-05T01:00:00Z"})
        rows["snapshots"]["draft"]["home"] = draft_home
        return rows

    def test_prepare_suggests_start(self):
        for state in (row(home=HOME), checked(home=HOME, local_state="missing"),
                      checked(home=HOME, local_state="ready", observation_age_seconds=9 * 86400)):
            with self.subTest(state=state["local_state"]):
                self.assertEqual(self.suggest(state, "prepare"),
                                 ("start", "prepared on every serving rank just now; start rechecks prerequisites"))

    def test_after_download_or_restore_the_next_snapshot_without_a_home_is_suggested(self):
        for after in ("acquire", "restore"):
            with self.subTest(after=after):
                action, reason = self.suggest(self.snapshots(), after)
                self.assertEqual(action, "restore")
                self.assertTrue(reason.startswith("draft has no home yet; archive: verified"), reason)
                action, reason = self.suggest(self.snapshots(), after, location="disabled")
                self.assertEqual(action, "acquire")
                self.assertTrue(reason.startswith("draft has no home yet; "), reason)

    def test_when_every_snapshot_has_a_home_prepare_is_next(self):
        for state in (self.snapshots(draft_home=HOME), row(home=HOME)):
            for after in ("acquire", "restore"):
                with self.subTest(after=after, schema3="snapshots" in state):
                    self.assertEqual(self.suggest(state, after),
                                     ("prepare", f"{menu.LABELS[after].lower()} finished; "
                                                 "every required snapshot has a verified home"))

    def test_other_mutations_still_suggest_check_now(self):
        ready = checked(home=HOME, local_state="ready")
        for after in sorted(menu.MUTATIONS - {"acquire", "restore", "prepare"}):
            with self.subTest(after=after):
                self.assertEqual(self.suggest(ready, after)[0], "check")

    def test_a_failed_storage_change_needs_check_now(self):
        ready = checked(home=HOME, local_state="ready")
        self.assertEqual(self.suggest(ready, "storage-failed"),
                         ("check", "the last storage change did not succeed; inspect current files before retrying"))
        lines = menu.view_lines(ready, "configured", after="storage-failed", after_age=60, width=200, names=NAMES,
                                now=self.NOW)
        self.assertFalse(any("prepared in this session" in line for line in lines))

    def test_a_guarded_spec_is_never_led_toward_prepare_or_start(self):
        guarded = row(home=HOME, **GUARDED)
        self.assertEqual(self.suggest(guarded, "restore")[0], "check")
        self.assertEqual(self.suggest(guarded, "prepare")[0], "check")

    def test_an_unknown_service_still_comes_before_start(self):
        unknown = {"kind": "unknown", "unobserved": ["spark-2"], "age": 60, "source": "inventory"}
        action, reason = self.suggest(row(home=HOME), "prepare", live=unknown)
        self.assertEqual(action, "status")
        self.assertIn("check before starting", reason)

    def test_the_frame_keeps_the_saved_check_visible_after_prepare(self):
        state = checked(home=HOME, local_state="missing", observation_age_seconds=12 * 86400)
        lines = menu.view_lines(state, "configured", after="prepare", after_age=120, width=200, names=NAMES,
                                now=self.NOW)
        frame = frame_text(lines)
        self.assertIn("Files prepared in this session (2 minutes ago); saved check: ", frame)
        self.assertIn("12 days ago", frame)
        self.assertIn("Next Start — prepared on every serving rank just now", frame)
        unmarked = menu.view_lines(state, "configured", after="move", after_age=120, width=200, names=NAMES,
                                   now=self.NOW)
        self.assertFalse(any("prepared in this session" in line for line in unmarked))


class ArchiveSuggestions(unittest.TestCase):
    NOW = datetime(2026, 9, 5, 2, tzinfo=timezone.utc)

    def suggest(self, state):
        offered, _, _ = menu.operations(state, "configured")
        return menu.suggestion(state, offered, "configured", names=NAMES, now=self.NOW)

    @staticmethod
    def snapshot_row(states, *, dates=None, target_home=None):
        snapshots = two_snapshots(target_home, None)
        for name, state in zip(("target", "draft"), states):
            snapshots[name]["observation"] = {
                "archive_state": state, "local_state": "ready" if snapshots[name]["home"] else "missing",
                "blockers": [],
            }
            if dates and name in dates:
                snapshots[name]["archive"] = {"verified": True, "verified_at": dates[name]}
        combined = combined_observation({name: member["observation"] for name, member in snapshots.items()})
        return checked(**{**combined, "snapshots": snapshots})

    def test_presence_is_not_presented_as_verification(self):
        state = checked(local_state="missing", archive_state="present")
        action, reason = self.suggest(state)
        self.assertEqual(action, "restore")
        self.assertIn(archive_text(state), reason)
        self.assertIn("Restore rechecks contents", reason)
        self.assertNotIn("verified archive is available", reason)

    def test_old_and_unknown_verification_ages_remain_explicit(self):
        for age in (19 * 86400, None):
            with self.subTest(age=age):
                state = checked(local_state="missing", archive={"verified": True}, archive_age_seconds=age)
                action, reason = self.suggest(state)
                self.assertEqual(action, "restore")
                self.assertIn(archive_text(state), reason)
                self.assertIn("Restore rechecks contents", reason)
                self.assertNotIn("available", reason)

    def test_newer_negative_check_overrides_old_verification(self):
        for status in ("missing", "unavailable", "not-configured"):
            with self.subTest(status=status):
                state = checked(local_state="missing", archive_state=status,
                                archive={"verified": True}, archive_age_seconds=19 * 86400)
                action, reason = self.suggest(state)
                self.assertEqual(action, "acquire")
                self.assertIn(archive_text(state), reason)

    def test_newer_verification_overrides_an_older_negative_check(self):
        state = checked(local_state="missing", archive_state="missing",
                        archive={"verified": True}, archive_age_seconds=3600)
        action, reason = self.suggest(state)
        self.assertEqual(action, "restore")
        self.assertIn(archive_text(state), reason)
        self.assertIn("Restore rechecks contents", reason)

    def test_mixed_archives_name_only_the_snapshot_being_recovered(self):
        old = "2026-08-17T02:00:00Z"
        recent = "2026-09-05T01:00:00Z"
        cases = [
            (self.snapshot_row(("present", "missing")), "restore", "target"),
            (self.snapshot_row(("missing", "verified"), dates={"target": old, "draft": recent}), "acquire", "target"),
            (self.snapshot_row(("unavailable", "present"), target_home=HOME), "restore", "draft"),
            (self.snapshot_row(("unknown", "verified")), "acquire", "target"),
            (self.snapshot_row(("missing", "unknown"), dates={"target": recent}), "restore", "target"),
        ]
        for state, expected, snapshot in cases:
            for reverse in (False, True):
                with self.subTest(states=state["archive_state"], snapshot=snapshot, reverse=reverse):
                    document = copy.deepcopy(state)
                    if reverse:
                        document["snapshots"] = dict(reversed(list(document["snapshots"].items())))
                    before = copy.deepcopy(document)
                    with patch("subprocess.run", side_effect=AssertionError("suggestions must not probe")):
                        action, reason = self.suggest(document)
                        command = menu.suggested_command(document, "configured", NAMES, now=self.NOW)
                    self.assertEqual(action, expected)
                    self.assertIn(f"for snapshot {snapshot}", reason)
                    self.assertIn(archive_facts(document, self.NOW)[snapshot][2], reason)
                    self.assertEqual(command, ["./pulsar", "model", expected, "abababababab",
                                               "--snapshot", snapshot, "--yes"])
                    self.assertEqual(document, before)

    def test_menu_and_catalog_use_the_same_reconciled_time(self):
        state = self.snapshot_row(("missing", "unknown"), dates={"target": "2026-09-05T01:00:00Z"})
        for width in (44, 80):
            with self.subTest(width=width):
                lines = menu.view_lines(state, "configured", width=width, names=NAMES, now=self.NOW)
                headers = [line.split("\t", 1)[1] for line in lines if line.startswith(("header\t", "next\t"))]
                text = " ".join(" ".join(headers).split())
                self.assertIn("target verified 1 hour ago", text)
                self.assertIn("Next Restore", text)
                self.assertIn("archive: verified 1 hour ago; Restore rechecks contents", text)
                self.assertTrue(all(len(line) <= width - 4 for line in headers))
                output = io.StringIO()
                render([state], writer=TerminalWriter(width=width, stream=output), names=NAMES,
                       location="configured", now=self.NOW)
                self.assertIn("model restore", output.getvalue())
                self.assertIn("--snapshot target", output.getvalue())


class View(unittest.TestCase):
    # TUI-22: aligned fields, Files and Archive apart, Next last.
    def test_the_frame_is_aligned_fields_with_next_last(self):
        state = checked(home=HOME, local_state="missing", blockers=["A long saved blocker that wraps at narrow widths."])
        for width in (64, 84):
            with self.subTest(width=width):
                lines = menu.view_lines(state, "configured", width=width, names=NAMES)
                kinds = [line.split("\t", 1)[0] for line in lines if line.startswith(("header\t", "next\t"))]
                self.assertEqual(kinds, sorted(kinds, key=lambda kind: kind == "next"))
                body = [line.split("\t", 1)[1] for line in lines if line.startswith("header\t")]
                fields = [line.split()[0] for line in body[1:] if not line.startswith(" ")]
                for field in ("Recipe", "Settings", "Image", "Files", "Archive", "Running?", "Results"):
                    self.assertIn(field, fields)
                # Values start in one column; wrapped values hang under them.
                for line in body[1:]:
                    self.assertTrue(line[10:11].strip() and not line[9:10].strip(), line)
                self.assertTrue(all(len(line) <= width - 4 for line in body))
                nexts = [line.split("\t", 1)[1] for line in lines if line.startswith("next\t")]
                self.assertTrue(nexts[0].startswith("Next      Prepare — "), nexts)
                self.assertTrue(all(line.startswith(" " * 10) for line in nexts[1:]), nexts)
        self.assertIn("Running? not checked here; choose Live status", frame_text(lines))
        read_only = menu.view_lines(state, "configured", width=84, names=NAMES, read_only=True)
        self.assertIn("Running? not checked here", frame_text(read_only))
        self.assertNotIn("choose Live status", frame_text(read_only))
        self.assertFalse(any(line.startswith("next\t") for line in read_only))

    # TUI-20: what each operation does, where it stands, in lifecycle order.
    def test_labels_add_their_effect_only_where_it_fits(self):
        state = checked(home=HOME, local_state="ready")
        for width, effects in ((80, True), (60, None)):
            with self.subTest(width=width):
                options = [line.split("\t") for line in menu.view_lines(state, "configured", width=width, names=NAMES)
                           if line.startswith("option\t")]
                self.assertTrue(all(len(label) <= width - 2 for *_, label in options))
                with_effect = [label for *_, label in options if " — " in label]
                if effects:
                    self.assertEqual(len(with_effect), len(options))
                else:
                    self.assertTrue(with_effect and len(with_effect) < len(options), with_effect)
                for _, _, action, label in options:
                    self.assertTrue(label.startswith(menu.LABELS[action]), label)
        self.assertEqual(menu.option_label("start", ("start", "why"), 80),
                         "Start (suggested) — launch the service; asks first")
        self.assertEqual(menu.option_label("start", ("start", "why"), 30), "Start (suggested)")
        self.assertEqual(menu.option_label("pin", None, 80), "Pin prepared copies — protect from purge")
        self.assertEqual(set(menu.EFFECTS), set(menu.LABELS))

    # TUI-50: the budget table, then each spec holding files with what holds them.
    def test_free_space_lists_specs_holding_files_and_what_holds_them(self):
        copies = [{"node_id": "n1", "rank": 0, "pinned": True}, {"node_id": "n2", "rank": 1, "pinned": False}]
        held = checked(home=HOME, prepared_copies=copies)
        bare = row(spec_id="cd" * 32)
        running = {"services": {held["spec_id"]: {"state": "running", "hosts": ["spark-2", "spark-3"],
                                                   "node_id": None, "modified": False}},
                   "age": 5, "unobserved": [], "incomplete": False}
        budget = {"nodes": [{"node_id": "n0", "available": 300 * 1024**3, "used": 50 * 1024**3,
                             "reserve": 100 * 1024**3, "limit": 200 * 1024**3, "path": "/fixture/views"}]}
        lines = menu.free_space_lines([held, bare], budget, None, width=100, names=NAMES, live=running)
        headers = [line.split("\t", 1)[1] for line in lines if line.startswith("header\t")]
        self.assertEqual(headers[0], "Node     Disk free  Reserve  Pulsar files  Can still copy")
        self.assertTrue(headers[1].startswith("spark-1"), headers)
        specs = [line.split("\t") for line in lines if line.startswith("spec\t")]
        self.assertEqual(specs, [["spec", held["spec_id"], f"[{held['spec_id'][:12]}] org/model · 2 prepared copies "
                                  "(1 pinned) · home · in use: running on 2 nodes"]])
        self.assertIn("Archives are never deleted.", " ".join(headers))
        # Unknown is not absent: a failed budget names why, and nothing holding files says so.
        lines = menu.free_space_lines([bare], None, "node storage is unobservable", width=44, names=NAMES)
        self.assertEqual(lines, ["header\tStorage budget not observed: node storage is unobservable",
                                 "header\tNo catalog spec has a home or prepared copies recorded here; nothing to "
                                 "reclaim."])
        # A schema-3 recipe's other required snapshots keep their homes under snapshots.
        draft_only = row(spec_id="ef" * 32, snapshots={
            "target": {"home": None, "model_id": "org/model", "model_commit": "0" * 40},
            "draft": {"home": HOME, "model_id": "org/draft", "model_commit": "1" * 40}})
        listed = [line.split("\t") for line in menu.free_space_lines([draft_only], budget, None, width=100, names=NAMES)
                  if line.startswith("spec\t")]
        self.assertEqual(listed, [["spec", "ef" * 32, f"[{'ef' * 6}] org/model · home only"]])
        # A long status shortens itself before the label would overflow the menu.
        busy = checked(home=HOME, prepared_copies=copies * 3, model_id="org/" + "long-model-" * 4)
        many = {**running, "services": {busy["spec_id"]: {"state": "running", "hosts": ["spark-1", "spark-2"],
                                                        "node_id": None, "modified": False}}}
        for width in (100, 80, 60, 44, 30):
            with self.subTest(width=width):
                label = menu.free_space_lines([busy], budget, None, width=width, names=NAMES, live=many)[-1]
                label = label.split("\t")[2]
                self.assertLessEqual(len(label), width - 2, label)
                self.assertTrue(label.startswith(f"[{busy['spec_id'][:12]}]"), label)
                if width >= 60:
                    self.assertIn("pinned", label)
                    self.assertIn("in use", label)
        # Long model names are cut so the label fits the menu.
        long = checked(home=HOME, model_id="org/" + "long-model-" * 10)
        label = menu.free_space_lines([long], budget, None, width=44, names=NAMES)[-1].split("\t")[2]
        self.assertLessEqual(len(label), 42)
        self.assertTrue(label.endswith("... · home only"), label)

    def test_live_status_comes_before_stop(self):
        self.assertLess(menu.MAIN.index("status"), menu.MAIN.index("stop"))
        offered, _, _ = menu.operations(checked(home=HOME), "configured")
        self.assertLess(offered.index("status"), offered.index("stop"))

    def test_lifecycle_marks_the_step_saved_records_point_to(self):
        running = {"kind": "service", "state": "running", "hosts": ["spark-1"], "modified": False,
                   "age": 30, "source": "inventory"}
        cases = (
            (row(), None, "[Download] → Prepare → Start · Stop → Purge"),
            (checked(home=HOME, local_state="missing"), None, "Download ✓ → [Prepare] → Start · Stop → Purge"),
            (checked(home=HOME, local_state="ready"), None, "Download ✓ → Prepare ✓ → [Start] · Stop → Purge"),
            (checked(home=HOME), None, "Download ✓ → Prepare → Start (files not checked) · Stop → Purge"),
            (checked(home=HOME, local_state="ready"), running,
             "Download ✓ → Prepare ✓ → Start ✓ (running) · Stop → Purge"),
            # An old sighting is not current, so prepared files never read as started.
            (checked(home=HOME, local_state="ready"), {**running, "age": 3600},
             "Download ✓ → Prepare ✓ → [Start] · Stop → Purge"),
            (row(snapshots=two_snapshots(HOME, None)), None, "[Download] → Prepare → Start · Stop → Purge"),
            # A guarded spec's Start is left out of the menu, so it is never the marked step.
            (checked(home=HOME, local_state="ready", **GUARDED), None,
             "Download ✓ → Prepare ✓ → Start (not offered here) · Stop → Purge"),
        )
        for state, live, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(menu.lifecycle(state, live), expected)
        self.assertEqual(menu.lifecycle(row(), None, mark=False), "Download → Prepare → Start · Stop → Purge")
        lines = menu.view_lines(checked(home=HOME, local_state="missing"), "configured", width=84, names=NAMES)
        self.assertIn("Lifecycle Download ✓ → [Prepare] → Start · Stop → Purge", frame_text(lines))

    def test_frame_lines_keep_alignment_but_never_a_tab(self):
        self.assertEqual(menu.frame_line("          wrapped\tvalue \n"), "          wrapped value")

    def test_lines_mark_suggestion_and_explain_hidden_operations(self):
        lines = menu.view_lines(checked(home=HOME, local_state="ready"), "not-configured", width=60, names=NAMES)
        self.assertEqual(lines[0], "recipe\t3\torg/model")
        text = "\n".join(line.split("\t", 1)[1] for line in lines if line.startswith(("header\t", "next\t")))
        self.assertIn("org/model [abababababab]", text)
        self.assertIn("checked 2 hours ago", text)
        self.assertIn("Next Start", " ".join(text.split()))
        self.assertIn("no archive location is configured", " ".join(text.split()))
        self.assertIn("option\tmain\tacquire\tDownload — fetch model files to one node", lines)
        self.assertFalse(any(line.startswith("option\tmain\trestore\t") for line in lines))
        self.assertNotIn("Check now refreshes", text)
        self.assertIn("option\tmain\tstart\tStart (suggested) — launch the service; asks first", lines)
        self.assertIn("suggest\tstart", lines)
        self.assertTrue(all(len(line.split("\t", 1)[1]) <= 56 for line in lines if line.startswith("header\t")))

    # TUI-60: each hidden operation says what makes it available; Check now
    # records file observations only, so it is never named as the fix.
    def test_hidden_operations_name_their_fix(self):
        def shown(state, location):
            # Each "Not shown" field with its wrapped lines, whitespace collapsed.
            entries, current = [], None
            for line in menu.view_lines(state, location, width=200, names=NAMES):
                kind, _, text = line.partition("\t")
                if kind == "header" and text.startswith("Not shown"):
                    current = [text]
                    entries.append(current)
                elif kind == "header" and current is not None and text.startswith(" "):
                    current.append(text)
                else:
                    current = None
            return [" ".join(" ".join(entry).split()) for entry in entries]
        self.assertEqual(shown(row(), "configured"), [
            "Not shown Prepare, Move home, Create archive, Remove home (no home is recorded). "
            "Download or Restore records a home."])
        self.assertEqual(shown(row(), "not-configured"), [
            "Not shown Restore, Verify archive (no archive location is configured). "
            "Set one in Archive storage configuration on the home menu.",
            "Not shown Prepare, Move home, Create archive, Remove home (no home is recorded). "
            "Download records a home."])
        self.assertEqual(shown(checked(home=HOME), "disabled"), [
            "Not shown Restore, Create archive, Verify archive (archives are disabled). "
            "Set a location in Archive storage configuration on the home menu."])
        self.assertEqual(shown(row(snapshots=two_snapshots(HOME, None)), "configured"), [
            "Not shown Prepare (no home is recorded for snapshot draft). Download or Restore records a home."])
        for state, location in ((row(), "not-configured"), (checked(home=HOME, **GUARDED), "configured")):
            lines = menu.view_lines(state, location, width=80, names=NAMES)
            self.assertNotIn("Check now refreshes", frame_text(lines))

    def test_guarded_view_explains_missing_start_without_a_suggestion(self):
        lines = menu.view_lines(checked(home=HOME, local_state="ready", **GUARDED), "configured",
                                width=60, names=NAMES)
        text = " ".join(line.split("\t", 1)[1] for line in lines if line.startswith("header\t"))
        # TUI-35: a serving guard points to the guarded trial procedure.
        self.assertIn(f"Not shown Start ({GUARD_REASON}). Use a guarded trial: ./pulsar guarded --help; "
                      "see docs/GUARDED_SERVING.md.", " ".join(text.split()))
        self.assertFalse([line for line in lines if line.startswith(("next\t", "suggest\t", "option\tmain\tstart\t"))])
        # TUI-43: Live status says it checks the nodes, and Stop that it applies if running.
        self.assertIn("option\tmain\tstop\tStop — remove it if running; files are kept", lines)
        self.assertIn("option\tmain\tstatus\tLive status — check every node for it now", lines)

    def test_view_performs_no_probes(self):
        with patch("subprocess.run", side_effect=AssertionError("menu must not probe")):
            menu.view_lines(row(), "configured", width=80, names=NAMES)

    # TUI-24: each required-snapshot choice says what it is for and where its home is.
    @staticmethod
    def snapshot_choices(state, width):
        return [line.split("\t")[1:] for line in menu.view_lines(state, "configured", width=width, names=NAMES)
                if line.startswith("snapshot\t")]

    def test_snapshot_choices_name_role_identity_and_home_with_missing_homes_first(self):
        draft = "draft — speculative-decoding checkpoint (org/draft @ cccccccc) · no home recorded"
        target = "target — served model (org/model @ cccccccc) · home on spark-1"
        self.assertEqual(self.snapshot_choices(row(snapshots=two_snapshots(HOME, None)), 200), [
            ["acquire", "draft", draft], ["acquire", "target", target],
            ["restore", "draft", draft], ["restore", "target", target],
            ["move", "target", target], ["remove", "target", target], ["archive", "target", target]])

    def test_snapshot_choices_keep_one_order_whatever_the_saved_key_order(self):
        snapshots = two_snapshots(HOME, HOME)
        for order in (snapshots, dict(reversed(snapshots.items()))):
            with self.subTest(first=next(iter(order))):
                choices = self.snapshot_choices(row(snapshots=order), 200)
                for action in ("acquire", "restore", "move", "remove", "archive"):
                    self.assertEqual([name for kind, name, _ in choices if kind == action], ["target", "draft"])

    def test_snapshot_choices_drop_identity_then_role_to_fit_the_row(self):
        snapshots = two_snapshots(HOME, None)
        for member in snapshots.values():
            member["model_id"] = "org/" + "long-model-name-" * 4
        state = row(snapshots=snapshots)
        self.assertEqual([label for *_, label in self.snapshot_choices(state, 80)][:2],
                         ["draft — speculative-decoding checkpoint · no home recorded",
                          "target — served model · home on spark-1"])
        self.assertEqual([label for *_, label in self.snapshot_choices(state, 44)][:2],
                         ["draft · no home recorded", "target — served model · home on spark-1"])
        for width in (44, 60, 80, 120):
            for _, name, label in self.snapshot_choices(state, width):
                with self.subTest(width=width, label=label):
                    self.assertLessEqual(len(label), width - 2)
                    self.assertTrue(label.startswith((name + " — ", name + " · ")), label)
                    self.assertTrue(label.endswith(("no home recorded", "home on spark-1")), label)
                    # The identity shows only with the role.
                    self.assertTrue("(" not in label or " — " in label, label)

    def test_snapshot_role_follows_the_contract_not_the_name(self):
        state = row(snapshots={"target": two_snapshots(HOME, None)["target"],
                               "eagle": two_snapshots(HOME, None)["draft"]})
        self.assertEqual(menu.snapshot_label(state, "eagle", 200, NAMES),
                         "eagle — speculative-decoding checkpoint (org/draft @ cccccccc) · no home recorded")

    def test_a_long_snapshot_name_is_shortened_before_its_home(self):
        # Spec names can be 64 characters.
        name = "n" * 64
        member = {"model_id": "org/draft", "model_commit": "c" * 40, "archive": None, "home": None}
        state = row(snapshots={"target": two_snapshots(HOME, None)["target"], name: member})
        self.assertEqual(menu.snapshot_label(state, name, 42, NAMES), "n" * 20 + "... · no home recorded")
        # With fewer than 8 of its characters left, the name keeps the row.
        member["home"] = {"node_id": "n9", "path": "/fixture/home"}
        self.assertEqual(menu.snapshot_label(state, name, 30, NAMES), "n" * 27 + "...")


class NodeChoices(unittest.TestCase):
    """TUI-25: the node question names what the machine is for; each choice
    names what saved records place on it. Nothing is probed."""

    def test_each_machine_names_the_home_and_prepared_copies_recorded_there(self):
        state = checked(home=HOME, prepared_copies=[{"rank": 0, "node_id": "n1"}, {"rank": 0, "node_id": "n0"}])
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78),
                         "spark-1 — home recorded here, prepared copy recorded here")
        self.assertEqual(menu.node_label(state, "n1", "spark-2", 78), "spark-2 — prepared copy recorded here")
        self.assertEqual(menu.node_label(state, "n2", "spark-3", 78), "spark-3")
        # Where the whole label doesn't fit, the home stays before the copy.
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 30), "spark-1 — home recorded here")
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 20), "spark-1")

    def test_the_selected_snapshots_home_is_the_one_named(self):
        state = row(snapshots=two_snapshots(HOME, None))
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78, "target"), "spark-1 — home recorded here")
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78, "draft"), "spark-1")
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78), "spark-1 — home recorded here")

    def choices(self, state, action, *nodes, snapshot=None):
        out = io.StringIO()
        args = ["node-choices", "--spec-id", state["spec_id"], "--action", action]
        args += ["--snapshot", snapshot] if snapshot else []
        for node in nodes:
            args += ["--node-name", node]
        with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", out), \
                patch.dict("os.environ", {"COLUMNS": "80"}), \
                patch("subprocess.run", side_effect=AssertionError("node choices must not probe")):
            rc = menu.main(args)
        return rc, [line.split("\t") for line in out.getvalue().splitlines()]

    def test_the_question_follows_the_operation(self):
        state = checked(home=HOME)
        cases = {"acquire": "Which machine should hold the model files?",
                 "restore": "Which machine should hold the model files?",
                 "move": "Which machine should the home move to?",
                 "check": "Which machine's files should be checked?",
                 "status": "Which machine runs the service?", "stop": "Which machine runs the service?",
                 "start": "Which machine should serve the model?", "prepare": "Which machine should serve the model?"}
        for action, question in cases.items():
            with self.subTest(action=action):
                rc, lines = self.choices(state, action, "n0=spark-1", "n1=spark-2")
                self.assertEqual(rc, 0)
                self.assertEqual(lines, [["title", question], ["node", "n0", "spark-1 — home recorded here"],
                                         ["node", "n1", "spark-2"]])

    def test_a_malformed_node_name_is_refused(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            rc, _ = self.choices(checked(home=HOME), "acquire", "spark-1")
        self.assertEqual(rc, 2)
        self.assertIn("NODE_ID=HOSTNAME", err.getvalue())


class SuggestedCommand(unittest.TestCase):
    def command(self, state, location="configured"):
        return menu.suggested_command(state, location, NAMES)

    def test_one_node_recipes_name_their_recorded_node_by_hostname(self):
        one = {"geometry": {"nodes": 1, "tp": 1, "pp": 1}}
        self.assertEqual(self.command(row(home=HOME, **one)), ["./pulsar", "models", "check", "abababababab",
                                                               "--node", "spark-1"])
        self.assertEqual(self.command(checked(home=HOME, local_state="ready", **one)),
                         ["./pulsar", "start", "abababababab", "--node", "spark-1"])
        # Without a home a prepared copy still names the node; an unknown node keeps its ID.
        copy = {"rank": 0, "node_id": "n7", "path": "/fixture/copy", "pinned": False}
        self.assertEqual(self.command(row(prepared_copies=[copy], **one))[-2:], ["--node", "n7"])
        self.assertEqual(self.command(row(**one)), ["./pulsar", "models", "check", "abababababab"])

    def test_multi_node_recipes_take_their_nodes_from_the_topology(self):
        self.assertEqual(self.command(checked(home=HOME, local_state="changed")),
                         ["./pulsar", "model", "prepare", "abababababab", "--yes"])
        self.assertEqual(self.command(checked(home=HOME, local_state="ready")), ["./pulsar", "start", "abababababab"])

    def test_acquire_and_restore_name_a_recorded_node_or_use_the_default(self):
        # No saved record places the recipe: the command's default destination.
        self.assertEqual(self.command(checked(local_state="missing")), ["./pulsar", "model", "acquire", "abababababab", "--yes"])
        self.assertEqual(self.command(checked(local_state="missing", archive_state="verified")),
                         ["./pulsar", "model", "restore", "abababababab", "--yes"])
        state = checked(local_state="missing", snapshots=two_snapshots(HOME, None))
        self.assertEqual(self.command(state), ["./pulsar", "model", "acquire", "abababababab",
                                               "--snapshot", "draft", "--yes"])
        # A one-node recipe keeps the node its other snapshot's home names.
        one = {"geometry": {"nodes": 1, "tp": 1, "pp": 1}}
        self.assertEqual(self.command(checked(local_state="missing", snapshots=two_snapshots(HOME, None), **one)),
                         ["./pulsar", "model", "acquire", "abababababab", "--snapshot", "draft",
                          "--node", "spark-1", "--yes"])
        self.assertEqual(self.command(checked(local_state="missing", **one)), ["./pulsar", "model", "acquire", "abababababab", "--yes"])

    def test_guarded_ready_has_no_command_but_unknown_files_need_check(self):
        self.assertIsNone(self.command(checked(home=HOME, local_state="ready", **GUARDED)))
        self.assertEqual(self.command(checked(home=HOME, local_state="unknown")),
                         ["./pulsar", "models", "check", "abababababab"])


class Labels(unittest.TestCase):
    def test_critical_warnings_survive_long_models_and_narrow_widths(self):
        for width in (32, 44, 80):
            for withdrawn, unsupported in ((True, False), (False, True), (True, True)):
                with self.subTest(width=width, withdrawn=withdrawn, unsupported=unsupported):
                    state = checked(home=HOME, local_state="ready", model_id="org/" + "long-model-" * 12,
                                    review={"status": "withdrawn"} if withdrawn else None,
                                    **(GUARDED if unsupported else {}))
                    label = menu.menu_label(state, width)
                    self.assertLessEqual(len(label), width - 2)  # Gum draws a two-column cursor before the label.
                    # 12 characters where they fit; a narrow label with two warnings keeps 8.
                    self.assertIn("[abababababab]" if width == 80 else "[abababab", label)
                    if withdrawn:
                        self.assertIn("withdrawn", label)
                    if unsupported and (not withdrawn or width >= 44):
                        self.assertTrue("start unsupported" in label or "start blocked" in label, label)

    def test_labels_show_saved_state_and_fit_80_and_44_columns(self):
        long_model = "org/" + "very-long-model-name-" * 4
        states = {
            "never checked": row(model_id=long_model),
            "files prepared · start unsupported": checked(home=HOME, local_state="ready", **GUARDED),
            "not prepared": checked(local_state="missing"),
            "files changed · withdrawn": checked(home=HOME, local_state="changed",
                                                 review={"status": "withdrawn", "reason": "Superseded."}),
        }
        for width in (80, 44):
            for expected, state in states.items():
                with self.subTest(width=width, state=expected):
                    text = menu.menu_label(state, width)
                    self.assertLessEqual(len(text), width - 2)
                    self.assertIn("[abababababab]", text)
                    self.assertNotIn("not specified", text)
                    if width == 80:
                        for part in expected.split(" · "):
                            self.assertIn(part, text)
        self.assertEqual(menu.short_state(checked(home=HOME, local_state="ready")), "files prepared")

    def test_cli_prints_the_spec_id_and_label_per_entry(self):
        document = json.dumps({"entries": [row(), checked(spec_id="cd" * 32, local_state="ready", home=HOME)]})
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output), \
                patch.dict("os.environ", {"COLUMNS": "80"}):
            self.assertEqual(menu.main(["labels"]), 0)
        self.assertEqual(output.getvalue().splitlines(),
                         ["ab" * 32 + "\torg/model [abababababab] 3 nodes · never checked",
                          "cd" * 32 + "\torg/model [cdcdcdcdcdcd] 3 nodes · files prepared"])

    # TUI-23: at 60 columns or wider, the label also names the node count and
    # the spec's state, where the whole label fits; state is metadata only.
    def test_wide_labels_name_the_node_count_and_state(self):
        state = checked(home=HOME, local_state="ready", state="candidate", geometry={"nodes": 1, "tp": 1, "pp": 1})
        self.assertEqual(menu.menu_label(state, 80), "org/model [abababababab] 1 node · files prepared · candidate")
        self.assertEqual(menu.menu_label(state, 44), "org/model [abababababab] files prepared")
        long_model = dict(state, model_id="org/" + "m" * 40)
        self.assertNotIn("candidate", menu.menu_label(long_model, 60))
        offered, hidden, _ = menu.operations(state, "configured")
        self.assertIn("start", offered)


LIVE_NOW = datetime(2026, 9, 5, 0, 10, tzinfo=timezone.utc)
HOSTS = {"head": ("n0", "spark-1"), "w1": ("n1", "spark-2"), "w2": ("n2", "spark-3")}


def inventory(*services, generated_at="2026-09-05T00:08:00Z", unobserved=()):
    """A service inventory document; each service is (spec_id, state, node keys[, matches])."""
    nodes = {key: {"node_id": node_id, "hostname": host, "confirmed": True,
                   "probe_status": "unreachable" if key in unobserved else "ok"}
             for key, (node_id, host) in HOSTS.items()}
    rows = [{"service_id": spec, "conf": spec, "state": state, "ownership": "managed", "safe_to_stop": True,
             "matches_selected_spec": rest[0] if rest else True,
             "ranks": [{"rank": str(i), "node": key} for i, key in enumerate(keys)]}
            for spec, state, keys, *rest in services]
    return {"schema_version": 1, "generated_at": generated_at, "nodes": nodes, "worker": {"status": "ok"},
            "unmanaged_gpu_processes": [], "services": rows}


def epoch(minutes_before):
    return int(LIVE_NOW.timestamp()) - minutes_before * 60


class SessionServices(unittest.TestCase):
    """What this menu session saw of a service; nothing here probes."""
    SPEC = "ab" * 32

    def fact(self, document=None, *outcomes, state=None):
        state = state or checked(home=HOME, local_state="ready")
        live = menu.live_inventory(document, [state], now=LIVE_NOW) if document else None
        found = menu.service_outcomes([f"{self.SPEC}:{value}" for value in outcomes], now=LIVE_NOW, names=NAMES)
        return menu.live_fact(state, live, found.get(self.SPEC))

    def suggest(self, fact, state=None, after=None):
        state = state or checked(home=HOME, local_state="ready")
        offered, _, _ = menu.operations(state, "configured")
        return menu.suggestion(state, offered, "configured", after, NAMES, live=fact)

    def test_a_running_service_is_named_with_its_nodes_source_and_age(self):
        fact = self.fact(inventory((self.SPEC, "running", ["head", "w1", "w2"])))
        self.assertEqual(menu.live_line(fact),
                         "Live service: running on spark-1, spark-2, spark-3 (service inventory, 2 minutes ago).")
        self.assertEqual(menu.live_label(fact), "running on 3 nodes")
        self.assertEqual(self.suggest(fact), ("status", "running on spark-1, spark-2, spark-3 "
                                                        "(service inventory, 2 minutes ago); Live status checks it now"))

    def test_an_old_observation_is_last_seen_and_still_suggests_live_status(self):
        fact = self.fact(inventory((self.SPEC, "running", ["head"]), generated_at="2026-09-04T23:30:00Z"))
        self.assertEqual(menu.live_line(fact),
                         "Live service: last seen running on spark-1 (service inventory, 40 minutes ago).")
        self.assertEqual(menu.live_label(fact), "last seen running 40 minutes ago")
        self.assertEqual(self.suggest(fact)[0], "status")
        self.assertIn("last seen", self.suggest(fact)[1])

    def test_unknown_is_not_absent(self):
        unknown = self.fact(inventory(unobserved=("w1",)))
        self.assertEqual(menu.live_line(unknown),
                         "Live service: unknown; spark-2 was not observed (service inventory, 2 minutes ago).")
        self.assertIsNone(menu.live_label(unknown))
        self.assertEqual(self.suggest(unknown)[0], "status")
        self.assertIn("check before starting", self.suggest(unknown)[1])
        absent = self.fact(inventory())
        self.assertEqual(menu.live_line(absent),
                         "Live service: not running on any node (service inventory, 2 minutes ago).")
        self.assertIsNone(menu.live_label(absent))
        self.assertEqual(self.suggest(absent)[0], "start")

    def test_a_failed_node_probe_summary_is_unknown_too(self):
        document = inventory()
        document["worker"] = {"status": "unreachable", "reason": "ssh timed out"}
        live = menu.live_inventory(document, [checked()], now=LIVE_NOW)
        self.assertEqual(menu.inventory_line(live),
                         "Running services: service inventory 2 minutes ago; a node could not be observed.")
        fact = self.fact(document)
        self.assertEqual(fact["kind"], "unknown")
        self.assertIn("a node could not be observed", menu.live_line(fact))
        self.assertEqual(self.suggest(fact)[0], "status")

    def test_a_rejected_start_or_stop_keeps_the_earlier_observation(self):
        running = inventory((self.SPEC, "running", ["head"]))
        for action in ("start", "stop", "status"):
            with self.subTest(action=action):
                self.assertEqual(self.fact(running, f"{action}:2:{epoch(1)}")["kind"], "service")

    def test_a_one_node_stop_leaves_other_nodes_unobserved(self):
        running = inventory((self.SPEC, "running", ["head"]))
        fact = self.fact(running, f"stop:0:{epoch(1)}:n0")
        self.assertEqual(menu.live_line(fact),
                         "Live service: stopped on spark-1; other nodes not observed since (Stop, 1 minute ago).")
        self.assertIsNone(menu.live_label(fact))
        self.assertEqual(self.suggest(fact)[0], "status")
        # A node_id may contain colons; it is the remainder of the value.
        found = menu.service_outcomes([f"{self.SPEC}:stop:0:{epoch(1)}:rack:7"], now=LIVE_NOW)
        self.assertEqual(found[self.SPEC][3], "rack:7")

    def test_newer_observations_replace_a_remembered_start_or_interruption(self):
        ready = checked(home=HOME, local_state="ready")
        started = self.fact(None, f"start:0:{epoch(2)}")
        self.assertEqual(self.suggest(started, after="start"),
                         ("status", "started from this menu; live status observes the service"))
        gone = self.fact(None, f"start:0:{epoch(2)}", f"status:1:{epoch(1)}")
        self.assertEqual(self.suggest(gone, state=ready, after="start")[0], "start")
        interrupted = self.fact(None, f"stop:interrupted:{epoch(2)}")
        self.assertEqual(self.suggest(interrupted, after="service-interrupted")[1],
                         "service outcome unconfirmed; current service state is unknown")
        resolved = self.fact(inventory(generated_at="2026-09-05T00:09:30Z"), f"stop:interrupted:{epoch(2)}")
        self.assertEqual(resolved["kind"], "absent")
        self.assertEqual(self.suggest(resolved, state=ready, after="service-interrupted")[0], "start")
        self.assertEqual(self.suggest(None, after="start")[0], "status")

    def test_live_status_exit_0_means_a_service_exists_not_that_it_runs(self):
        fact = self.fact(None, f"status:0:{epoch(1)}")
        self.assertEqual(menu.live_line(fact), "Live service: a service exists (Live status, 1 minute ago).")
        self.assertNotIn("running", menu.live_label(fact))
        self.assertEqual(self.suggest(fact), ("status", "a service exists (Live status, 1 minute ago); "
                                                        "Live status shows its state"))
        self.assertIn("not running", menu.live_line(self.fact(None, f"status:1:{epoch(1)}")))
        self.assertIn("a node could not be observed", menu.live_line(self.fact(None, f"status:3:{epoch(1)}")))
        self.assertIsNone(self.fact(None, f"status:2:{epoch(1)}"))
        self.assertIsNone(self.fact(None, f"status:interrupted:{epoch(1)}"))

    def test_the_newest_observation_wins(self):
        running = inventory((self.SPEC, "running", ["head"]))  # two minutes ago
        stopped = self.fact(running, f"stop:0:{epoch(1)}")
        self.assertEqual(menu.live_line(stopped), "Live service: not running on any node (Stop, 1 minute ago).")
        self.assertEqual(self.suggest(stopped)[0], "start")
        older = self.fact(running, f"stop:0:{epoch(5)}")
        self.assertEqual(older["kind"], "service")
        # A refused or rejected Live status keeps the inventory's answer.
        self.assertEqual(self.fact(running, f"status:2:{epoch(1)}")["kind"], "service")

    def test_start_and_stop_make_earlier_observations_out_of_date(self):
        for outcome, words in ((f"start:1:{epoch(1)}", "Start"), (f"stop:1:{epoch(1)}", "Stop"),
                               (f"start:interrupted:{epoch(1)}", "Start")):
            with self.subTest(outcome=outcome):
                fact = self.fact(inventory(), outcome)
                self.assertEqual(menu.live_line(fact), f"Live service: not observed since {words} (1 minute ago).")
                self.assertEqual(self.suggest(fact)[0], "status")

    def test_a_modified_recipe_and_partial_service_are_not_flattened(self):
        fact = self.fact(inventory((self.SPEC, "partial", ["head", "w1"], False)))
        self.assertEqual(menu.live_label(fact), "partly running on 2 nodes (modified recipe)")
        self.assertIn("partly running on spark-1, spark-2 (modified recipe)", menu.live_line(fact))

    def test_other_specs_and_no_observation(self):
        other = self.fact(inventory(("cd" * 32, "running", ["head"])))
        self.assertEqual(other["kind"], "absent")
        self.assertIsNone(self.fact())
        self.assertEqual(menu.live_line(None), "Live service: not observed in this menu session; Live status checks it.")
        self.assertEqual(menu.live_line(None, read_only=True), "Live service: not observed here.")

    def test_interruptions_and_saved_rules_keep_their_precedence(self):
        fact = self.fact(inventory((self.SPEC, "running", ["head"])))
        self.assertEqual(self.suggest(fact, after="check-interrupted")[0], "check")
        self.assertEqual(self.suggest(fact, after="prepare")[0], "status")
        guarded = checked(home=HOME, local_state="ready", **GUARDED)
        self.assertEqual(self.suggest(fact, state=guarded)[0], "status")

    def test_labels_keep_the_spec_prefix_and_yield_files_state_first(self):
        fact = self.fact(inventory((self.SPEC, "running", ["head", "w1", "w2"])))
        state = checked(home=HOME, local_state="ready", model_id="nvidia/GLM-5.3-Flash-NVFP4")
        wide, narrow = menu.menu_label(state, 100, fact), menu.menu_label(state, 70, fact)
        self.assertEqual(wide, "nvidia/GLM-5.3-Flash-NVFP4 [abababababab] running on 3 nodes · files prepared")
        self.assertEqual(narrow, "nvidia/GLM-5.3-Flash-NVFP4 [abababababab] running on 3 nodes")
        for width in (32, 44, 60, 80):
            with self.subTest(width=width):
                label = menu.menu_label(state, width, fact)
                self.assertLessEqual(len(label), width - 2)
                self.assertIn("[abababababab]", label)
        withdrawn = checked(home=HOME, local_state="ready", review={"status": "withdrawn"})
        self.assertIn("running on 3 nodes", menu.menu_label(withdrawn, 80, fact))

    def test_malformed_outcomes_are_rejected(self):
        for value in ("x", f"{self.SPEC}:restart:0:1", f"{self.SPEC}:status:0:soon", f"{self.SPEC}:status::1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                menu.service_outcomes([value])

    def test_cli_labels_date_the_inventory_and_view_uses_it(self):
        import tempfile
        state = checked(home=HOME, local_state="ready")
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(inventory((self.SPEC, "running", ["head"]), unobserved=("w2",),
                                generated_at=datetime.now(timezone.utc).isoformat()), handle)
            handle.flush()
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", output), \
                    patch.dict("os.environ", {"COLUMNS": "100"}):
                self.assertEqual(menu.main(["labels", "--live", handle.name]), 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "observed\tRunning services: service inventory less than a minute ago; not observed: spark-3.",
                self.SPEC + "\torg/model [abababababab] running on 1 node · files prepared"])
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", output), \
                    patch.object(menu.NodeNames, "saved", return_value=NAMES):
                self.assertEqual(menu.main(["view", "--spec-id", self.SPEC, "--archive-location", "configured",
                                            "--live", handle.name]), 0)
            frame = frame_text(output.getvalue().splitlines())
            self.assertIn("Running? running on spark-1 (service inventory, less than a minute ago)", frame)
            self.assertIn("suggest\tstatus", output.getvalue())


class Question(unittest.TestCase):
    def test_questions_name_model_placement_and_consequence(self):
        state = checked(home=HOME)
        prepare = {"kind": "pulsar-preparation-plan", "eligible": True, "actions": [
            {"rank": 0, "node_id": "n0", "action": "home-view"}, {"rank": 1, "node_id": "n1", "action": "copy"},
            {"rank": 2, "node_id": "n2", "action": "copy"}]}
        self.assertEqual(menu.question("prepare", state, plan=prepare, names=NAMES),
                         "Prepare org/model on spark-1, spark-2, spark-3? 2 new copies, 1 reused.")
        purge = {"plan": {"eligible": True, "actions": [{"action": "remove-copy"}, {"action": "release-binding"}]},
                 "incomplete_preparations": []}
        self.assertEqual(menu.question("purge", state, plan=purge, names=NAMES),
                         "Purge org/model: delete 1 working copy, release 1 binding? The home and the archive are kept.")
        self.assertIn("from spark-1 to spark-3",
                       menu.question("move", state, plan={"source_node": "n0", "destination_node": "n2"}, names=NAMES))
        self.assertIn("Files come back only through Restore or Download",
                      menu.question("remove", state, plan={"eligible": True, "home": HOME}, names=NAMES))
        self.assertIn("never replaces a running service", menu.question("start", state, names=NAMES))
        self.assertIn("Loading can take several minutes", menu.question("start", state, names=NAMES))

    def test_start_questions_name_each_rank_from_the_saved_topology_order(self):
        state = checked(home=HOME)
        ordered = NodeNames({"n0": "spark-1", "n1": "spark-2", "n2": "spark-3"}, ["n0", "n1", "n2"])
        where = "spark-1 (rank 0), spark-2 (rank 1), spark-3 (rank 2)"
        self.assertIn(f"Start org/model on {where}?", menu.question("start", state, names=ordered))
        self.assertIn(f"retry Start for org/model on {where}?", menu.question("start-memory", state, names=ordered))
        # Fewer saved nodes than ranks, or no saved order: start refuses, so only the count is named.
        short = NodeNames({"n0": "spark-1"}, ["n0"])
        for names in (short, NAMES, NodeNames()):
            with self.subTest(names=names.order):
                self.assertIn("Start org/model on 3 nodes?", menu.question("start", state, names=names))
        # A one-node recipe names the selected node only.
        one = checked(home=HOME, geometry={"nodes": 1, "tp": 1, "pp": 1})
        self.assertIn("Start org/model on spark-2?", menu.question("start", one, node="n1", names=ordered))
        self.assertEqual(menu.question("stop", state, node="n1", names=NAMES),
                         "Stop and remove the service containers of org/model [abababababab] on spark-2? "
                         "Model files, pins and archives are kept.")
        # TUI-43: a fresh inventory names where it runs, or that nothing ran.
        running = {"kind": "service", "state": "running", "hosts": ["spark-1", "spark-3"], "modified": False,
                   "age": 5, "source": "inventory"}
        self.assertEqual(menu.question("stop", state, names=NAMES, live=running),
                         "Stop and remove the service containers of org/model [abababababab] on spark-1, spark-3? "
                         "Model files, pins and archives are kept.")
        # A selected node is where Stop acts, so the question names it, and
        # says so when the inventory saw the service only elsewhere.
        self.assertEqual(menu.question("stop", state, node="n0", names=NAMES, live=running),
                         "Stop and remove the service containers of org/model [abababababab] on spark-1? "
                         "Model files, pins and archives are kept.")
        self.assertEqual(menu.question("stop", state, node="n1", names=NAMES, live=running),
                         "Stop and remove the service containers of org/model [abababababab] on spark-2? "
                         "Model files, pins and archives are kept. The service inventory saw it running on "
                         "spark-1, spark-3 just now, not on spark-2.")
        # The node ID decides when the inventory has one, whatever the hostname says.
        observed = {**running, "hosts": ["spark-2.local"], "node_id": "n1"}
        self.assertTrue(menu.question("stop", state, node="n1", names=NAMES, live=observed).endswith(" are kept."))
        self.assertIn("running on spark-2.local just now, not on spark-1.",
                      menu.question("stop", state, node="n0", names=NAMES, live=observed))
        absent = {"kind": "absent", "age": 5, "source": "inventory"}
        self.assertTrue(menu.question("stop", state, node="n1", names=NAMES, live=absent).startswith(
            "Stop org/model [abababababab] on spark-2? No container of it was seen running just now;"))
        self.assertEqual(menu.question("stop", state, names=NAMES, live=absent),
                         "Stop org/model [abababababab]? No container of it was seen running just now; Stop "
                         "removes any that remain. Model files, pins and archives are kept.")
        self.assertEqual(menu.question("stop", state, names=NAMES),
                         "Stop and remove the service containers of org/model [abababababab] on its nodes? "
                         "Model files, pins and archives are kept.")
        self.assertIn("Download org/model @ 01234567 to spark-2",
                      menu.question("acquire", state, plan={"selected_node": "n1"}, names=NAMES))

    def test_withdrawal_notice_is_visible_without_changing_eligibility(self):
        normal = checked(home=HOME, local_state="ready")
        state = {**normal, "review": {"status": "withdrawn", "reviewed_at": "2026-09-03T00:00:00Z",
                                      "reason": "Later testing found inconsistent answers."}}
        before = copy.deepcopy(state)
        self.assertEqual(menu.operations(state, "configured"), menu.operations(normal, "configured"))
        offered, _, _ = menu.operations(state, "configured")
        self.assertEqual(menu.suggestion(state, offered, "configured", names=NAMES)[0], "start")
        for width in (44, 80):
            lines = menu.view_lines(state, "configured", width=width, names=NAMES)
            headers = [line.split("\t", 1)[1] for line in lines if line.startswith(("header\t", "next\t"))]
            text = " ".join(" ".join(headers).split())
            for expected in ("Maintainer warning: withdrawn", state["review"]["reason"],
                             state["review"]["reviewed_at"], "Advisory metadata"):
                self.assertIn(expected, text)
            self.assertLess(text.index("Maintainer warning"), text.index("Next Start"))
            self.assertTrue(all(len(line) <= width - 4 for line in headers))
        self.assertEqual(state, before)

    def test_start_and_memory_retry_confirmations_repeat_the_wrapped_notice(self):
        state = checked(home=HOME, local_state="ready", review={
            "status": "withdrawn", "reviewed_at": "2026-09-03T00:00:00Z",
            "reason": "The retained result should be reviewed before operating this published spec."})
        for action in ("start", "start-memory"):
            with self.subTest(action=action):
                out, err = io.StringIO(), io.StringIO()
                with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), \
                        patch("sys.stdout", out), patch("sys.stderr", err), \
                        patch.dict("os.environ", {"COLUMNS": "44"}):
                    self.assertEqual(menu.main(["confirm", "--spec-id", state["spec_id"], "--action", action]), 0)
                self.assertIn("Withdrawn spec.", out.getvalue())
                self.assertEqual(len(out.getvalue().splitlines()), 1)
                notice = " ".join(err.getvalue().split())
                self.assertIn(state["review"]["reason"], notice)
                self.assertIn(state["review"]["reviewed_at"], notice)
                self.assertTrue(all(len(line) <= 44 for line in err.getvalue().splitlines()))

    def test_warning_never_infers_or_changes_metadata(self):
        for review in (None, {}, {"status": "validated"}):
            with self.subTest(review=review):
                state = checked(home=HOME, local_state="ready", state="released", review=review)
                before = copy.deepcopy(state)
                self.assertEqual(menu.withdrawal_notice(state), [])
                self.assertTrue(menu.question("start", state, names=NAMES).startswith("Start "))
                self.assertEqual(state, before)
        state = checked(home=HOME, local_state="ready", review={"status": "withdrawn"}, **GUARDED)
        notice = menu.withdrawal_notice(state)
        self.assertFalse(any(line.startswith("Reviewed:") for line in notice))
        self.assertTrue(any("No reason recorded" in line for line in notice))
        offered, _, _ = menu.operations(state, "configured")
        self.assertNotIn("start", offered)

    def test_blocked_plan_has_no_question(self):
        self.assertTrue(menu.plan_blocked({"plan": {"eligible": False}}))
        self.assertTrue(menu.plan_blocked({"eligible": False}))
        self.assertFalse(menu.plan_blocked({"kind": "pulsar-restore-plan"}))

    def test_cli_confirm_exits_3_for_blocked_plan(self):
        state = checked(home=HOME)
        document = json.dumps({"entries": [state]})
        with patch("sys.stdin", io.StringIO(document)), patch("builtins.open", unittest.mock.mock_open(
                read_data=json.dumps({"eligible": False, "blockers": ["pinned"]}))):
            self.assertEqual(menu.main(["confirm", "--spec-id", state["spec_id"], "--action", "purge",
                                        "--plan-file", "plan.json"]), 3)


class HomeNext(unittest.TestCase):
    """The home screen's Next line, from saved records only (TUI-26)."""
    def specs(self):
        return [row(spec_id="a" * 64), row(spec_id="b" * 64),
                checked(spec_id="c" * 64, observation_age_seconds=3 * 86400),
                checked(spec_id="d" * 64, local_state="missing"),
                checked(spec_id="e" * 64, local_state="missing", archive_state="verified"),
                checked(spec_id="f" * 64, home=HOME, local_state="changed", observation_age_seconds=600),
                checked(spec_id="0" * 64, home=HOME, local_state="ready")]

    def test_groups_suggestions_in_lifecycle_order_with_the_newest_check(self):
        self.assertEqual(menu.home_next(self.specs(), "configured"),
                         "Next: 2 specs never checked · 1 spec needs a check · 1 spec needs Download · "
                         "1 spec needs Restore · 1 spec needs Prepare · 1 spec ready to Start — open Catalog "
                         "and storage. Newest saved check: 10 minutes ago.")

    def test_counts_use_singular_and_plural_wording(self):
        ready = [checked(spec_id=c * 64, home=HOME, local_state="ready") for c in "ab"]
        self.assertEqual(menu.home_next(ready, "configured"),
                         "Next: 2 specs ready to Start — open Catalog and storage. Newest saved check: 2 hours ago.")
        stale = [checked(spec_id=c * 64, observation_age_seconds=3 * 86400) for c in "ab"]
        self.assertTrue(menu.home_next(stale, "configured").startswith("Next: 2 specs need a check — "))
        self.assertTrue(menu.home_next([row()], "configured").startswith("Next: 1 spec never checked — "))

    def test_never_checked_alone_has_no_check_age(self):
        self.assertEqual(menu.home_next([row(spec_id="a" * 64), row(spec_id="b" * 64)], "configured"),
                         "Next: 2 specs never checked — open Catalog and storage.")

    def test_the_archive_location_decides_restore(self):
        state = [checked(local_state="missing", archive_state="verified")]
        self.assertIn("1 spec needs Restore", menu.home_next(state, "configured"))
        self.assertIn("1 spec needs Download", menu.home_next(state, "disabled"))

    def test_no_suggested_step_and_no_catalog_give_no_line(self):
        self.assertIsNone(menu.home_next([], "configured"))
        self.assertIsNone(menu.home_next([checked(home=HOME, local_state="ready", **GUARDED)], "configured"))

    def test_saved_service_and_session_records_are_not_read(self):
        # Ready files suggest Start even though a service may run: the line
        # never probes or reads session outcomes.
        state = checked(home=HOME, local_state="ready")
        with patch.object(menu, "live_fact", side_effect=AssertionError("service read")), \
                patch.object(menu, "service_outcomes", side_effect=AssertionError("session read")):
            self.assertIn("ready to Start", menu.home_next([state], "configured"))

    def test_cli_reads_the_catalog_projection_and_prints_nothing_without_a_step(self):
        for entries, expected in ((self.specs()[:1], "Next: 1 spec never checked — open **Catalog and storage**.\n"),
                                  ([], "")):
            with self.subTest(entries=len(entries)):
                out = io.StringIO()
                with patch("sys.stdin", io.StringIO(json.dumps({"entries": entries}))), patch("sys.stdout", out):
                    self.assertEqual(menu.main(["next", "--archive-location", "configured"]), 0)
                self.assertEqual(out.getvalue(), expected)


GIB = 1024 ** 3
MANIFEST = "1" * 64


def preparation(*actions, manifest=MANIFEST, size=2 * GIB, budgets=None, **fields):
    return {"kind": "pulsar-preparation-plan", "eligible": True, "snapshot_manifest_id": manifest,
            "total_bytes": size, "actions": [{"rank": rank, "node_id": node, "action": action}
                                             for rank, (node, action) in enumerate(actions)],
            "budgets": budgets or {}, **fields}


def budget(available=100 * GIB, reserve=10 * GIB, used=0, limit=50 * GIB):
    return {"available": available, "reserve": reserve, "used": used, "limit": limit}


class PlanSummary(unittest.TestCase):
    """Each storage preview leads with a summary built from its own plan (TUI-73, #58)."""
    def summary(self, action, plan, state=None):
        return menu.plan_summary(action, state or checked(home=HOME), plan, names=NAMES)

    def test_prepare_copies_with_estimated_headroom(self):
        plan = preparation(("n0", "home-view"), ("n1", "copy"), ("n2", "copy"),
                           budgets={"n1": budget(), "n2": budget(limit=40 * GIB)})
        self.assertEqual(self.summary("prepare", plan), [
            "Copy 4.00 GiB to spark-2, spark-3 (2 new copies, 1 reused).",
            "Estimated headroom after copying: 38.00 GiB on spark-3 (file sizes only; space is not reserved)."])
        self.assertEqual(menu.plan_effect("prepare", plan), {"copied": 2, "reused": 1, "nothing": False})

    def test_prepare_shortfall_and_unobserved_headroom_are_estimates(self):
        plan = preparation(("n0", "home-view"), ("n1", "copy"), budgets={"n1": budget(limit=GIB)})
        self.assertEqual(self.summary("prepare", plan)[1], "Short by about 1.00 GiB on spark-2.")
        plan = preparation(("n0", "home-view"), ("n1", "copy"))
        self.assertEqual(self.summary("prepare", plan)[1], "Estimated headroom after copying: not observed.")

    def test_prepare_with_nothing_to_copy(self):
        plan = preparation(("n0", "home-view"), ("n1", "reuse"), ("n2", "bind"))
        self.assertEqual(self.summary("prepare", plan), [
            "Nothing to copy: the snapshot is already on spark-1, spark-2, spark-3. "
            "Prepare verifies the files and records them."])
        self.assertTrue(menu.plan_effect("prepare", plan)["nothing"])
        self.assertIn("Nothing is copied", menu.question("prepare", checked(home=HOME), plan=plan, names=NAMES))
        both = {"kind": "pulsar-preparation-set-plan", "eligible": True,
                "snapshots": [preparation(("n0", "home-view"), snapshot="target"),
                              preparation(("n0", "home-view"), manifest="2" * 64, snapshot="draft")]}
        self.assertTrue(self.summary("prepare", both)[0].startswith("Nothing to copy: both snapshots are already on spark-1."))

    def test_a_manifest_shared_by_two_snapshots_is_copied_once_per_node(self):
        plan = {"kind": "pulsar-preparation-set-plan", "eligible": True, "snapshots": [
            preparation(("n0", "home-view"), ("n1", "copy"), snapshot="target", budgets={"n1": budget()}),
            preparation(("n0", "home-view"), ("n1", "copy"), snapshot="draft", budgets={"n1": budget()})]}
        self.assertEqual(menu.plan_effect("prepare", plan), {"copied": 1, "reused": 3, "nothing": False})
        self.assertEqual(self.summary("prepare", plan), [
            "Copy 2.00 GiB to spark-2 (1 new copy, 3 reused).",
            "Estimated headroom after copying: 48.00 GiB on spark-2 (file sizes only; space is not reserved)."])
        self.assertEqual(menu.question("prepare", checked(home=HOME), plan=plan, names=NAMES),
                         "Prepare org/model on spark-1, spark-2? 1 new copy, 3 reused.")
        # Different manifests are separate copies on the same node.
        plan["snapshots"][1]["snapshot_manifest_id"] = "2" * 64
        self.assertEqual(menu.plan_effect("prepare", plan)["copied"], 2)

    def test_purge(self):
        plan = {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "actions": [
            {"node_id": "n1", "action": "remove-copy"}, {"node_id": "n2", "action": "remove-copy"},
            {"node_id": "n0", "action": "release-binding"}]}, "incomplete_preparations": [{}]}
        self.assertEqual(self.summary("purge", plan), [
            "Delete 2 working copies, release 1 binding whose files are kept, clear 1 incomplete preparation "
            "on spark-2, spark-3, spark-1."])
        empty = {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "actions": []}, "incomplete_preparations": []}
        self.assertEqual(self.summary("purge", empty), [
            "Nothing to purge: no prepared copies or incomplete preparations are recorded for this spec."])

    def test_pin_and_unpin(self):
        views = [{"node_id": "n0", "pinned": True}, {"node_id": "n1", "pinned": False}]
        self.assertEqual(self.summary("pin", {"views": views}), ["Pin 1 prepared copy on spark-2; 1 already pinned."])
        self.assertEqual(self.summary("unpin", {"views": views}), ["Unpin 1 prepared copy on spark-1; 1 already unpinned."])
        self.assertEqual(self.summary("pin", {"views": views[:1]}), ["The prepared copy is already pinned; nothing changes."])
        self.assertEqual(self.summary("unpin", {"views": [views[1]] * 2}),
                         ["All 2 prepared copies are already unpinned; nothing changes."])
        self.assertEqual(self.summary("pin", {"views": []}), ["No prepared copies are recorded for this spec; nothing to pin."])

    def test_move(self):
        plan = {"source_node": "n0", "destination_node": "n2", "transfer_route": "direct-ssh-roce"}
        self.assertEqual(self.summary("move", plan),
                         ["Move the home from spark-1 to spark-3: a direct copy over the confirmed RoCE rail."])
        self.assertEqual(self.summary("move", {**plan, "transfer_route": "already-home"}),
                         ["Nothing to move: the home is already on spark-3."])

    def test_an_existing_archive_is_verified_never_replaced(self):
        self.assertEqual(self.summary("archive", {"home": HOME, "archive_root": "/fixture/archive"}),
                         ["Copy the home on spark-1 into the archive at /fixture/archive; "
                          "an archive already there is verified, never replaced."])

    def test_restore_with_estimated_disk_free_and_shortfall(self):
        plan = {"selected_node": "n1", "total_bytes": 3 * GIB, "file_count": 40,
                "destination_space": {"available": 10 * GIB}}
        self.assertEqual(self.summary("restore", plan), [
            "Copy 3.00 GiB (40 files) from the archive to spark-2.",
            "Estimated disk free after the copy: 7.00 GiB on spark-2 (file sizes only; space is not reserved)."])
        plan["destination_space"]["available"] = GIB
        self.assertEqual(self.summary("restore", plan)[1], "Short by about 2.00 GiB on spark-2.")
        self.assertEqual(self.summary("restore", {"selected_node": "n1"}), [
            "Copy the snapshot (size not observed) from the archive to spark-2.", "Disk free on spark-2: not observed."])

    def test_download_and_a_reuse_naming_the_homes_node(self):
        files = [{"path": "a", "size": GIB}, {"path": "b", "size": GIB}]
        self.assertEqual(self.summary("acquire", {"selected_node": "n1", "snapshot_revision": "89abcdef" * 5,
                                                 "source": {"files": files}}),
                         ["Download 2.00 GiB (2 files) of org/model @ 89abcdef to spark-2."])
        # A reuse plan names its node only in the home it registers.
        reuse = {"action": "reuse", "home": {**HOME, "node_id": "n2"}}
        self.assertEqual(self.summary("acquire", reuse), [
            "Nothing to download: verified files are already recorded on spark-3; Download registers them."])
        self.assertEqual(menu.question("acquire", checked(), plan=reuse, names=NAMES),
                         "Register the existing verified files of org/model @ 01234567 on spark-3? Nothing is downloaded.")

    def test_remove(self):
        self.assertEqual(self.summary("remove", {"home": HOME}),
                         ["Delete the home on spark-1; its files come back only through Restore or Download."])

    def test_blocked_plans_name_each_distinct_cause_once(self):
        container = {"code": "container-reference", "message": "m", "node_id": "n1",
                     "container": {"id": "c1", "name": "vllm", "state": "running", "spec_id": "cd" * 32},
                     "mount": "/fixture/hub", "path": "/fixture/hub/snapshot", "fix": "stop the Pulsar service"}
        pinned = {"code": "pinned", "message": "p", "node_id": "n2", "fix": "unpin the prepared copies first"}
        plan = {"eligible": False, "blocker_details": [container, dict(container), pinned]}
        self.assertEqual(self.summary("purge", plan), [
            "Blocked: running Pulsar container vllm (spec cdcdcdcdcdcd) on spark-2 mounts /fixture/hub, which "
            "contains these files; stop the Pulsar service.",
            "Blocked: the prepared copy on spark-3 is pinned; unpin the prepared copies first."])
        self.assertTrue(menu.plan_blocked(plan))

    def test_more_than_three_causes_show_two_and_a_count(self):
        details = [{"code": "insufficient-space", "message": "m", "node_id": node, "fix": "free space"}
                   for node in ("n0", "n1", "n2", "n3")]
        lines = self.summary("prepare", {"eligible": False, "blocker_details": details})
        self.assertEqual(lines, ["Blocked: not enough copy budget or disk space on spark-1; free space.",
                                 "Blocked: not enough copy budget or disk space on spark-2; free space.",
                                 "2 more blockers in the full preview."])

    def test_blocked_plans_without_details_use_their_messages(self):
        plan = {"eligible": False, "blockers": ["n1: disk is full", "n1: disk is full"]}
        self.assertEqual(self.summary("restore", plan), ["Blocked: spark-2: disk is full."])
        self.assertEqual(self.summary("restore", {"eligible": False}), ["Blocked; the full preview has details."])

    def test_cli_preview_exits_3_when_blocked_and_0_otherwise(self):
        state = checked(home=HOME)
        for plan, status in (({"selected_node": "n1"}, 0), ({"eligible": False, "blockers": ["pinned"]}, 3)):
            with self.subTest(status=status):
                out = io.StringIO()
                with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", out), \
                        patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(plan))):
                    self.assertEqual(menu.main(["preview", "--spec-id", state["spec_id"], "--action", "restore",
                                                "--plan-file", "plan.json"]), status)
                self.assertTrue(out.getvalue().strip())


def container_detail(spec_id=None, node="n1"):
    container = {"id": "c" * 64, "name": "fixture-vllm", "state": "running", **({"spec_id": spec_id} if spec_id else {})}
    return {"code": "container-reference", "message": "a container references the prepared copy", "node_id": node,
            "container": container, "mount": "/fixture/hub", "path": "/fixture/hub", "fix": "fix"}


class BlockedChoices(unittest.TestCase):
    """A blocked plan offers Stop only for a Pulsar container of a catalog spec (TUI-74)."""
    def test_stop_then_inventory_for_each_catalog_spec_once(self):
        other = checked(spec_id="cd" * 32, model_id="org/other")
        plan = {"plan": {"eligible": False, "blocker_details": [
            container_detail("cd" * 32), container_detail("cd" * 32, node="n2"), container_detail(),
            {"code": "pinned", "message": "p", "node_id": "n0", "fix": "unpin"}]}}
        self.assertEqual(menu.blocked_choices(plan, [checked(), other]), [
            ("stop", "cd" * 32, "org/other", "Stop org/other (cdcdcdcdcdcd)"),
            ("inventory", "", "", "Open Live service inventory")])

    def test_a_spec_outside_the_catalog_gets_only_the_inventory(self):
        plan = {"eligible": False, "blocker_details": [container_detail("ef" * 32)]}
        self.assertEqual(menu.blocked_choices(plan, [checked()]), [("inventory", "", "", "Open Live service inventory")])

    def test_a_container_pulsar_did_not_start_gets_no_choice(self):
        plan = {"eligible": False, "blocker_details": [container_detail(), {"code": "pinned", "message": "p"}]}
        self.assertEqual(menu.blocked_choices(plan, [checked()]), [])
        self.assertEqual(menu.blocked_choices({"eligible": False, "blockers": ["pinned"]}, [checked()]), [])

    def test_cli_prints_tab_separated_choices(self):
        plan = {"eligible": False, "blocker_details": [container_detail("ab" * 32)]}
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"entries": [checked()]}))), patch("sys.stdout", out), \
                patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(plan))):
            self.assertEqual(menu.main(["blocked-choices", "--plan-file", "plan.json"]), 0)
        self.assertEqual(out.getvalue(), f"stop\t{'ab' * 32}\torg/model\tStop org/model (abababababab)\n"
                                         "inventory\t\t\tOpen Live service inventory\n")


class Historical(unittest.TestCase):
    def test_schema1_specs_are_not_startable_from_this_stack(self):
        from model_library.catalog import start_support
        self.assertEqual(start_support({"schema_version": 1}), (False, "historical_spec"))
        state = checked(home=HOME, local_state="ready", start_supported=False, start_unsupported_reason="historical_spec")
        offered, hidden, _ = menu.operations(state, "configured")
        self.assertNotIn("start", offered)
        self.assertEqual(hidden["start"], "historical schema-1 specs cannot be started")
        self.assertIsNone(menu.suggestion(state, offered, "configured", None, NAMES))


if __name__ == "__main__":
    unittest.main()
