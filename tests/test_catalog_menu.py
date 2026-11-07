"""Catalog menu decisions come from saved records only and never hide unknown state."""
import copy
from datetime import datetime, timezone
import io
import json
import re
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


def screen(cards):
    """The catalog list as Gum draws it, cursor gutter included: an option's
    first line gives its two leading spaces to the gutter (scripts/ui.sh) and
    later lines print as given."""
    lines = []
    for card in cards:
        option = ([card.heading] if card.heading is not None else []) + card.lines + ([""] if card.gap else [])
        first = option[0][2:] if option[0].startswith("  ") else option[0]
        lines.extend(["  " + first, *option[1:]])
    return lines


def flat(lines):
    """The words of LINES, whitespace collapsed."""
    return " ".join(" ".join(lines).split())


def tokens(lines):
    """The parts of a spec's lines, each line split at " · "."""
    return [token for line in lines for token in line.strip().split(" · ")]


class CardChecks:
    def assert_spec_lines(self, lines, spec_id, width):
        """A spec's line starts at column 4 with its 12-character prefix, each
        further line at column 6, and no line is wider than WIDTH."""
        self.assertTrue(lines[0] == "    " + spec_id[:12] or lines[0].startswith("    " + spec_id[:12] + " · "), lines)
        for line in lines[1:]:
            self.assertRegex(line, r"^ {6}\S")
        self.assertTrue(all(len(line) <= width for line in lines), lines)

    def assert_cards(self, cards, width):
        """Headings at column 2, spec lines as assert_spec_lines says, and every
        line Gum draws within WIDTH."""
        for card in cards:
            if card.heading is not None:
                self.assertRegex(card.heading, r"^  \S")
            self.assert_spec_lines(card.lines, card.spec_id, width)
        self.assertTrue(all(len(line) <= width for line in screen(cards)), screen(cards))

    def assert_in_order(self, lines, parts):
        """Each part shown whole, wrapped at its spaces at most, in order."""
        shown, at = flat(lines), 0
        for part in parts:
            found = shown.find(" ".join(part.split()), at)
            self.assertGreaterEqual(found, at, (part, shown))
            at = found + len(part)


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
        self.assertEqual(self.suggest(checked(home=HOME, local_state="missing", observation_age_seconds=3 * 86400)),
                         ("check", "last check 3 days ago"))
        self.assertEqual(self.suggest(checked(blockers=["n1: disk full"])), ("check", "saved blocker: spark-2: disk full"))
        self.assertEqual(self.suggest(checked(local_state="missing"))[0], "acquire")
        self.assertEqual(self.suggest(checked(local_state="missing", archive_state="verified"))[0], "restore")
        self.assertEqual(self.suggest(checked(home=HOME, local_state="changed")),
                         ("prepare", "files changed since they were verified (checked 2 hours ago)"))
        self.assertEqual(self.suggest(checked(home=HOME, local_state="ready")),
                         ("start", "files prepared as of the last check (2 hours ago); Start rechecks them"))

    def test_prepared_files_suggest_start_at_any_age(self):
        # Start rechecks the files, so an old check that found them prepared
        # still leads to Start; any other old observation is checked again.
        old = 30 * 86400
        self.assertEqual(self.suggest(checked(home=HOME, local_state="ready", observation_age_seconds=old)),
                         ("start", "files prepared as of the last check (30 days ago); Start rechecks them"))
        for state in (checked(home=HOME, local_state=local, observation_age_seconds=old)
                      for local in ("unknown", "missing", "changed")):
            with self.subTest(local=state["local_state"]):
                self.assertEqual(self.suggest(state), ("check", "last check 30 days ago"))
        blocked = checked(home=HOME, local_state="ready", observation_age_seconds=old, blockers=["n1: disk full"])
        self.assertEqual(self.suggest(blocked), ("check", "last check 30 days ago"))
        self.assertEqual(self.suggest(checked(home=HOME, local_state="ready", observation_age_seconds=None))[0], "check")

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
        old_ready = checked(home=HOME, local_state="ready", observation_age_seconds=9 * 86400, **GUARDED)
        for after in (None, "stop", "check"):
            with self.subTest(after=after, age="9 days"):
                self.assertIsNone(self.suggest(old_ready, after))

    def test_suggestion_never_starts_a_guarded_spec(self):
        for state in (row(**GUARDED), checked(**GUARDED), checked(home=HOME, local_state="ready", **GUARDED),
                      checked(home=HOME, local_state="ready", observation_age_seconds=9 * 86400, **GUARDED),
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

    def choices(self, state, action, *nodes, snapshot=None, live=None):
        out = io.StringIO()
        args = ["node-choices", "--spec-id", state["spec_id"], "--action", action]
        args += ["--snapshot", snapshot] if snapshot else []
        args += ["--live", live] if live else []
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
                default = [] if action in ("acquire", "restore", "move") else [["default", "n0"]]
                self.assertEqual(lines, [["title", question], *default,
                                         ["node", "n0", "spark-1 — home recorded here"], ["node", "n1", "spark-2"]])

    # R6: the cursor starts on the node saved records name, except where the
    # operation chooses where files go.
    def test_the_recorded_node_is_preselected(self):
        copies = checked(prepared_copies=[{"rank": 0, "node_id": "n1"}])
        cases = [(checked(home=HOME), "check", ["default", "n0"]),
                 (copies, "start", ["default", "n1"]),
                 (copies, "status", ["default", "n1"]),
                 (copies, "acquire", None), (copies, "restore", None), (copies, "move", None),
                 (checked(), "check", None),
                 # A recorded node that is not a confirmed node is never preselected.
                 (checked(home={**HOME, "node_id": "n9"}), "check", None)]
        for state, action, default in cases:
            with self.subTest(action=action, default=default):
                rc, lines = self.choices(state, action, "n0=spark-1", "n1=spark-2")
                self.assertEqual(rc, 0)
                self.assertEqual([line for line in lines if line[0] in ("default", "seen")],
                                 [default] if default else [])

    def live_file(self, document):
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(Path(handle.name).unlink)
        with handle:
            json.dump(document, handle)
        return handle.name

    def test_a_stop_of_a_service_seen_on_one_node_needs_no_question(self):
        state = checked(home=HOME, geometry={"nodes": 1, "tp": 1, "pp": 1})
        live = self.live_file(inventory((state["spec_id"], "running", ["w1"])))
        rc, lines = self.choices(state, "stop", "n0=spark-1", "n1=spark-2", live=live)
        self.assertEqual(rc, 0)
        self.assertEqual(lines, [["title", "Which machine runs the service?"], ["seen", "n1"],
                                 ["node", "n0", "spark-1 — home recorded here"],
                                 ["node", "n1", "spark-2 — service seen here just now"]])
        # Exited containers on one node are what Stop removes there, too.
        live = self.live_file(inventory((state["spec_id"], "stale", ["w1"])))
        self.assertIn(["seen", "n1"], self.choices(state, "stop", "n0=spark-1", "n1=spark-2", live=live)[1])
        # Only Stop reads the inventory.
        live = self.live_file(inventory((state["spec_id"], "running", ["w1"])))
        _, lines = self.choices(state, "status", "n0=spark-1", "n1=spark-2", live=live)
        self.assertEqual([line for line in lines if line[0] in ("default", "seen")], [["default", "n0"]])

    # Stop without a node removes containers on every node, so anything short
    # of one confirmed node keeps the question, with the recorded node preselected.
    def test_a_stop_keeps_the_question_unless_the_service_was_seen_on_one_confirmed_node(self):
        state = checked(home=HOME, geometry={"nodes": 1, "tp": 1, "pp": 1})
        spec = state["spec_id"]
        cases = {"not running": inventory(),
                 "on two nodes": inventory((spec, "running", ["head", "w1"])),
                 "on a node not confirmed here": inventory((spec, "running", ["w2"])),
                 "a node not observed": inventory((spec, "running", ["w1"]), unobserved=("w2",)),
                 "another spec": inventory(("cd" * 32, "running", ["w1"]))}
        for name, document in cases.items():
            with self.subTest(name):
                rc, lines = self.choices(state, "stop", "n0=spark-1", "n1=spark-2", live=self.live_file(document))
                self.assertEqual(rc, 0)
                self.assertEqual([line for line in lines if line[0] in ("default", "seen")], [["default", "n0"]])
                self.assertNotIn("seen here", json.dumps(lines))

    def test_the_seen_service_is_named_first_where_the_label_fits(self):
        state = checked(home=HOME)
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78, seen="n0"),
                         "spark-1 — service seen here just now, home recorded here")
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 40, seen="n0"),
                         "spark-1 — service seen here just now")
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 30, seen="n0"), "spark-1")
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78, seen="n1"), "spark-1 — home recorded here")

    def test_a_malformed_node_name_is_refused(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            rc, _ = self.choices(checked(home=HOME), "acquire", "spark-1")
        self.assertEqual(rc, 2)
        self.assertIn("NODE_ID=HOSTNAME", err.getvalue())


# R8a: files a Model files scan found on a node, with no record naming them.
MANIFEST_FOUND = "e" * 64


def scan_location(node_id, kind="home", state="present", registered=False, manifest=MANIFEST_FOUND):
    return manifest, {"node_id": node_id, "kind": kind, "hub_path": f"/fixture/{node_id}/{kind}", "state": state,
                      "registered_home": registered, "bindings": [], "bindings_observed": True, "pinned": False,
                      "shared": False, "container_references": []}


def scan(*locations, envelope=True):
    """A model files --json document (see model_library.file_inventory.combine)."""
    snapshots: dict[str, dict] = {}
    for manifest, location in locations:
        snapshots.setdefault(manifest, {"snapshot_manifest_id": manifest, "model_id": "org/model",
                                        "model_commit": "c" * 40, "payload_bytes": 1024, "locations": [],
                                        "catalog_specs": [], "archive": None, "cleanup_spec_ids": []})
        snapshots[manifest]["locations"].append(location)
    document = {"schema_version": 1, "kind": "pulsar-model-file-inventory", "observed_at": "2026-09-05T00:00:00Z",
                "nodes": [], "snapshots": list(snapshots.values()), "issues": []}
    return {"schema_version": 1, "ok": True, "result": document} if envelope else document


class FilesFound(unittest.TestCase):
    """R8a: after a records loss the scan sees a home no record names. Download's
    own plan registers it when its discovery finds it, so it comes before
    Restore, which copies. The choice keeps its label: Download decides."""

    def scan_file(self, document):
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(Path(handle.name).unlink)
        with handle:
            handle.write(document if isinstance(document, str) else json.dumps(document))
        return handle.name

    @staticmethod
    def missing(**fields):
        """Checked, no home recorded, a verified archive: Restore is suggested without a scan."""
        return checked(local_state="missing", archive_state="verified", snapshot_manifest_id=MANIFEST_FOUND,
                       **fields)

    def suggest(self, state, files, after=None):
        offered, _, _ = menu.operations(state, "configured")
        return menu.suggestion(state, offered, "configured", after, NAMES, files=files)

    def test_only_present_homes_that_no_record_names_count(self):
        locations = (scan_location("n1"), scan_location("n1"), scan_location("n0", registered=True),
                     scan_location("n2", kind="prepared"), scan_location("n2", state="missing"),
                     scan_location("n2", state="unknown"), scan_location("n2", manifest="f" * 64))
        for envelope in (True, False):
            with self.subTest(envelope=envelope):
                found = menu.found_files(self.scan_file(scan(*locations, envelope=envelope)))
                self.assertEqual(found, {MANIFEST_FOUND: ["n1"], "f" * 64: ["n2"]})

    def test_a_failed_or_unreadable_scan_is_ignored(self):
        failed = {"schema_version": 1, "ok": False, "error": {"code": "prerequisite_failed", "message": "no topology"}}
        cases = {"no file": None, "missing file": "/nonexistent/files.json", "not JSON": self.scan_file("error: x\n"),
                 "failed scan": self.scan_file(failed), "a list": self.scan_file([]),
                 "another document": self.scan_file({"ok": True, "result": {"kind": "pulsar-storage-budget"}}),
                 "a malformed location": self.scan_file(scan(("e" * 64, None)))}
        for name, path in cases.items():
            with self.subTest(name):
                self.assertEqual(menu.found_files(path), {})

    def test_found_files_lead_to_download_ahead_of_restore(self):
        state = self.missing()
        reason = ("files found on spark-2 but not in Pulsar's records; "
                  "Download checks them before fetching anything")
        self.assertEqual(self.suggest(state, {MANIFEST_FOUND: ["n1"]}), ("acquire", reason))
        self.assertEqual(self.suggest(state, None)[0], "restore")
        # Files of another snapshot, or none, leave the archive suggestion.
        self.assertEqual(self.suggest(state, {"f" * 64: ["n1"]})[0], "restore")
        self.assertEqual(self.suggest(state, {})[0], "restore")
        # Two folders: Download stops on the second, and its preview says so.
        self.assertIn("files found on spark-2, spark-3 but", self.suggest(state, {MANIFEST_FOUND: ["n1", "n2"]})[1])
        # A recorded home still wins: the scan's files are another folder.
        self.assertEqual(self.suggest(checked(home=HOME, local_state="missing", snapshot_manifest_id=MANIFEST_FOUND),
                                      {MANIFEST_FOUND: ["n1"]})[0], "prepare")
        with patch("subprocess.run", side_effect=AssertionError("suggestions must not probe")):
            self.suggest(state, {MANIFEST_FOUND: ["n1"]})

    # Check now reads homes from records only, so it cannot see these files:
    # no saved check, a stale one or one that left files unknown waits for nothing.
    def test_found_files_need_no_saved_or_current_check(self):
        reason = ("files found on spark-2 but not in Pulsar's records; "
                  "Download checks them before fetching anything")
        files = {MANIFEST_FOUND: ["n1"]}
        cases = {"no saved check": row(snapshot_manifest_id=MANIFEST_FOUND),
                 "stale check": self.missing(observation_age_seconds=menu.STALE_SECONDS + 1),
                 "files unknown": checked(snapshot_manifest_id=MANIFEST_FOUND),
                 "stale check with a blocker": self.missing(observation_age_seconds=menu.STALE_SECONDS + 1,
                                                            blockers=["home missing"])}
        for name, state in cases.items():
            with self.subTest(name):
                self.assertEqual(self.suggest(state, files), ("acquire", reason))
                # Without the scan, the check rules apply as before.
                self.assertEqual(self.suggest(state, None)[0], "check")
        # A guarded spec gets Download, as without the scan; never anything toward Start.
        self.assertEqual(self.suggest(row(snapshot_manifest_id=MANIFEST_FOUND, **GUARDED), files),
                         ("acquire", reason))

    def test_session_outcomes_still_come_first(self):
        files = {MANIFEST_FOUND: ["n1"]}
        for state in (self.missing(), row(snapshot_manifest_id=MANIFEST_FOUND)):
            with self.subTest(checked=bool(state["checked_at"])):
                for after in ("purge", "storage-failed", "storage-interrupted", "check-failed"):
                    self.assertEqual(self.suggest(state, files, after=after)[0], "check", after)
                self.assertEqual(self.suggest(state, files, after="start")[0], "status")
                self.assertEqual(self.suggest(state, files, after="service-interrupted")[0], "status")

    def test_a_snapshot_is_named_after_the_previous_download(self):
        snapshots = two_snapshots(HOME, None)
        snapshots["target"]["snapshot_manifest_id"] = "f" * 64
        snapshots["draft"]["snapshot_manifest_id"] = MANIFEST_FOUND
        state = checked(local_state="missing", snapshots=snapshots)
        reason = ("files of snapshot draft found on spark-1 but not in Pulsar's records; "
                  "Download checks them before fetching anything")
        self.assertEqual(self.suggest(state, {MANIFEST_FOUND: ["n0"]}, after="acquire"), ("acquire", reason))
        self.assertEqual(self.suggest(state, {MANIFEST_FOUND: ["n0"]}), ("acquire", reason))
        # The target's files have a home already; only the snapshot without one counts.
        self.assertNotIn("files", self.suggest(state, {"f" * 64: ["n0"]})[1])

    def test_the_files_field_says_where_and_names_download(self):
        hint = "files found on spark-2, not in Pulsar's records; Download checks them"
        for state in (self.missing(), row(snapshot_manifest_id=MANIFEST_FOUND)):
            for width in (44, 60, 80):
                with self.subTest(checked=bool(state["checked_at"]), width=width):
                    lines = menu.view_lines(state, "configured", width=width, names=NAMES,
                                            files={MANIFEST_FOUND: ["n1"]})
                    frame = [line.split("\t", 1)[1] for line in lines if line.startswith(("header\t", "next\t"))]
                    self.assertTrue(all(len(line) <= width - 4 for line in frame), frame)
                    text = frame_text(lines)
                    self.assertIn(hint, text)
                    # One hint per value: Check now cannot see an unrecorded home.
                    self.assertNotIn(menu.CHECK_HINT, text)
                    # With or without a saved check: Check now cannot see these files.
                    self.assertIn("Next Download — files found on spark-2 but not in Pulsar's records", text)
                    self.assertIn("suggest\tacquire", lines)
                    # The choice keeps its label and effect.
                    self.assertIn("option\tmain\tacquire\tDownload (suggested)", "\n".join(lines))
        read_only = menu.view_lines(self.missing(), "configured", width=80, names=NAMES, read_only=True,
                                    files={MANIFEST_FOUND: ["n1"]})
        self.assertNotIn("files found", frame_text(read_only))
        self.assertNotIn("files found", frame_text(menu.view_lines(self.missing(home=HOME), "configured", width=80,
                                                                   names=NAMES, files={MANIFEST_FOUND: ["n1"]})))

    def node_choices(self, state, path, *, snapshot=None, action="acquire", columns="80"):
        out = io.StringIO()
        args = ["node-choices", "--spec-id", state["spec_id"], "--action", action, "--files", path,
                "--node-name", "n0=spark-1", "--node-name", "n1=spark-2"]
        args += ["--snapshot", snapshot] if snapshot else []
        with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", out), \
                patch.dict("os.environ", {"COLUMNS": columns}), \
                patch("subprocess.run", side_effect=AssertionError("node choices must not probe")):
            self.assertEqual(menu.main(args), 0)
        return [line.split("\t") for line in out.getvalue().splitlines()]

    def test_the_machine_holding_the_files_is_preselected_and_labelled(self):
        state = self.missing()
        lines = self.node_choices(state, self.scan_file(scan(scan_location("n1"))))
        self.assertEqual(lines, [["title", "Which machine should hold the model files?"], ["default", "n1"],
                                 ["node", "n0", "spark-1"],
                                 ["node", "n1", "spark-2 — files found here, not in Pulsar's records"]])
        # Too narrow for the fact: the hostname alone, still preselected.
        lines = self.node_choices(state, self.scan_file(scan(scan_location("n1"))), columns="40")
        self.assertEqual(lines[1:], [["default", "n1"], ["node", "n0", "spark-1"], ["node", "n1", "spark-2"]])
        # Two machines: both named, neither preselected, as Download would stop.
        lines = self.node_choices(state, self.scan_file(scan(scan_location("n0"), scan_location("n1"))))
        self.assertEqual([line for line in lines if line[0] == "default"], [])
        self.assertEqual(sum("files found here" in line[-1] for line in lines), 2)
        # A recorded home, a failed scan or files of another snapshot change nothing.
        for name, other, path in (("home", self.missing(home=HOME), self.scan_file(scan(scan_location("n1")))),
                                  ("failed", state, self.scan_file("error: no topology\n")),
                                  ("other", state, self.scan_file(scan(scan_location("n1", manifest="f" * 64))))):
            with self.subTest(name):
                lines = self.node_choices(other, path)
                self.assertNotIn("files found", json.dumps(lines))
                self.assertEqual([line for line in lines if line[0] == "default"], [])

    def test_the_selected_snapshot_is_the_one_looked_up(self):
        snapshots = two_snapshots(None, None)
        snapshots["target"]["snapshot_manifest_id"] = "f" * 64
        snapshots["draft"]["snapshot_manifest_id"] = MANIFEST_FOUND
        state = checked(local_state="missing", snapshots=snapshots)
        path = self.scan_file(scan(scan_location("n1")))
        self.assertIn(["default", "n1"], self.node_choices(state, path, snapshot="draft"))
        self.assertNotIn("files found", json.dumps(self.node_choices(state, path, snapshot="target")))

    def test_cards_and_view_take_the_scan_and_ignore_a_failed_one(self):
        state = self.missing()
        good, bad = self.scan_file(scan(scan_location("n1"))), self.scan_file("error: no topology\n")
        document = json.dumps({"entries": [state]})
        cards = []
        for args in ([], ["--files", good], ["--files", bad]):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output), \
                    patch.dict("os.environ", {"COLUMNS": "80"}):
                self.assertEqual(menu.main(["cards", *args]), 0)
            cards.append(output.getvalue())
        # A card adds no word for found files; the frame says it.
        self.assertEqual(cards[0], cards[1])
        self.assertEqual(cards[0], cards[2])
        frames = {}
        for name, path in (("good", good), ("bad", bad)):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output), \
                    patch.object(menu.NodeNames, "saved", return_value=NAMES), \
                    patch.dict("os.environ", {"COLUMNS": "80"}):
                self.assertEqual(menu.main(["view", "--spec-id", state["spec_id"], "--archive-location",
                                            "configured", "--files", path]), 0)
            frames[name] = output.getvalue()
        self.assertIn("suggest\tacquire", frames["good"])
        self.assertIn("files found on spark-2, not in Pulsar's records", frame_text(frames["good"].splitlines()))
        self.assertIn("suggest\trestore", frames["bad"])
        self.assertNotIn("files found", frames["bad"])
        # Stop's question is asked with the same session arguments.
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output):
            self.assertEqual(menu.main(["confirm", "--spec-id", state["spec_id"], "--action", "stop",
                                        "--files", good]), 0)
        self.assertTrue(output.getvalue().startswith("Stop and remove"), output.getvalue())


# Another spec's prepared copies of this spec's snapshot: the files Prepare can bind.
SHARED = "d" * 64
OTHER, THIRD = "cd" * 32, "ef" * 32
SAME_FILES_HERE = "same files prepared here for cdcdcdcdcdcd"
SAME_FRAME = ("the same files are prepared for cdcdcdcdcdcd on spark-2, spark-3; "
              "Prepare binds them there without copying")


def copy_of(spec_id, node_id, rank=0, manifest=SHARED):
    return {"spec_id": spec_id, "rank": rank, "node_id": node_id, "snapshot_manifest_id": manifest,
            "path": f"/fixture/{node_id}/copy", "pinned": False}


def sharing(**fields):
    """A recipe with a home whose own files are not prepared, while another
    spec has prepared copies of the same snapshot on n1 and n2."""
    return checked(**{"home": HOME, "local_state": "missing", "snapshot_manifest_id": SHARED,
                      "other_specs_prepared_copies": [copy_of(OTHER, "n1"), copy_of(OTHER, "n2", rank=1)],
                      **fields})


def without_others(state):
    return {key: value for key, value in state.items() if key != "other_specs_prepared_copies"}


class SameFilesPrepared(CardChecks, unittest.TestCase):
    """Another spec's prepared copies count only for a whole required set and
    only while this spec's saved check has not found its own copies prepared."""

    def test_the_claim_needs_every_required_snapshot_and_no_prepared_own_files(self):
        records = sharing()["other_specs_prepared_copies"]
        cases = {"missing": sharing(), "unknown": sharing(local_state="unknown"),
                 "not checked yet": sharing(checked_at=None, observation_age_seconds=None),
                 "no home": sharing(home=None)}
        for name, state in cases.items():
            with self.subTest(name):
                self.assertEqual(menu.same_files_prepared(state), records)
        empty = {"ready": sharing(local_state="ready"), "changed": sharing(local_state="changed"),
                 "no field": without_others(sharing()),
                 # A row that does not name its snapshot claims nothing.
                 "no manifest": sharing(snapshot_manifest_id=None),
                 "another snapshot": sharing(other_specs_prepared_copies=[copy_of(OTHER, "n1", manifest="f" * 64)]),
                 "its own records": sharing(other_specs_prepared_copies=[copy_of("ab" * 32, "n1")])}
        for name, state in empty.items():
            with self.subTest(name):
                self.assertEqual(menu.same_files_prepared(state), [])

    def test_a_schema3_recipe_needs_every_required_snapshot_on_one_machine(self):
        snapshots = two_snapshots(HOME, HOME)
        snapshots["target"]["snapshot_manifest_id"] = SHARED
        snapshots["draft"]["snapshot_manifest_id"] = "f" * 64
        target, draft = copy_of(OTHER, "n1"), copy_of(THIRD, "n2", manifest="f" * 64)
        state = checked(local_state="missing", snapshots=snapshots, other_specs_prepared_copies=[target])
        # A partial set is never claimed.
        self.assertEqual(menu.same_files_prepared(state), [])
        # Each snapshot on another machine: Prepare plans each snapshot on each
        # machine apart, so it would still copy the draft to spark-2 and the
        # target to spark-3. Nothing is claimed.
        split = dict(state, other_specs_prepared_copies=[target, draft])
        self.assertEqual(menu.same_files_prepared(split), [])
        self.assertNotIn("same files", frame_text(menu.view_lines(split, "configured", width=80, names=NAMES)))
        offered, _, _ = menu.operations(split, "configured")
        self.assertNotIn("without copying", menu.suggestion(split, offered, "configured", None, NAMES)[1])
        # Both on one machine, from any other specs: that machine's records only.
        beside = copy_of(THIRD, "n1", manifest="f" * 64)
        whole = dict(state, other_specs_prepared_copies=[target, beside, draft])
        self.assertEqual(menu.same_files_prepared(whole), [target, beside])
        self.assertIn("the same files are prepared for cdcdcdcdcdcd and 1 more on spark-2; "
                      "Prepare binds them there without copying",
                      frame_text(menu.view_lines(whole, "configured", width=80, names=NAMES)))

    def test_the_suggestion_counts_the_other_specs(self):
        several = sharing(other_specs_prepared_copies=[copy_of(THIRD, "n0"), copy_of(OTHER, "n1"),
                                                       copy_of(OTHER, "n2", rank=1)])
        offered, _, _ = menu.operations(several, "configured")
        self.assertIn("Prepare binds the same files prepared for cdcdcdcdcdcd and 1 more without copying",
                      menu.suggestion(several, offered, "configured", None, NAMES)[1])

    def test_catalog_specs_are_named_first_and_specs_outside_it_only_counted(self):
        # Experiment specs started from spec files sort before the catalog spec here.
        outside = ["11" * 32, "22" * 32, "33" * 32]
        copies = [copy_of(spec, f"n{index % 3}", rank=index) for index, spec in enumerate(outside + [THIRD, OTHER])]

        def words(records, in_catalog):
            return menu._other_specs(records, sharing(other_prepared_specs_in_catalog=in_catalog))

        self.assertEqual(words(copies[3:], [OTHER, THIRD]), "cdcdcdcdcdcd and 1 more")
        self.assertEqual(words(copies[4:], [OTHER]), "cdcdcdcdcdcd")
        self.assertEqual(words(copies, [THIRD]), "efefefefefef and 4 more (4 outside the catalog)")
        self.assertEqual(words(copies, [OTHER, THIRD]), "cdcdcdcdcdcd and 4 more (3 outside the catalog)")
        self.assertEqual(words(copies[:1], []), "a spec outside the catalog")
        self.assertEqual(words(copies[:3], []), "3 specs outside the catalog")
        # A catalog spec without a record here is not named; the records' own specs are.
        self.assertEqual(words(copies[:2], [OTHER]), "2 specs outside the catalog")
        # Membership unknown (a row projected without the catalog): the first spec ID, as before.
        self.assertEqual(words(copies, None), "111111111111 and 4 more")

    def test_a_spec_outside_the_catalog_is_counted_wherever_the_spec_is_named(self):
        mixed = sharing(other_specs_prepared_copies=[copy_of("11" * 32, "n1"), copy_of(OTHER, "n2", rank=1)],
                        other_prepared_specs_in_catalog=[OTHER])
        lines = menu.view_lines(mixed, "configured", width=80, names=NAMES)
        self.assertEqual(self.files_value(lines),
                         "not prepared on every serving node (checked 2 hours ago); the same files are prepared "
                         "for cdcdcdcdcdcd and 1 more (1 outside the catalog) on spark-2, spark-3; "
                         "Prepare binds them there without copying")
        frame = [line.split("\t", 1)[1] for line in lines if line.startswith(("header\t", "next\t"))]
        self.assertTrue(all(len(line) <= 76 for line in frame), frame)
        self.assertIn("Prepare binds the same files prepared for cdcdcdcdcdcd and 1 more (1 outside the catalog) "
                      "without copying", frame_text(lines))
        self.assertEqual(menu.node_label(mixed, "n1", "spark-2", 78),
                         "spark-2 — same files prepared here for a spec outside the catalog")
        self.assertEqual(menu.node_label(mixed, "n2", "spark-3", 78), "spark-3 — " + SAME_FILES_HERE)
        outside = dict(mixed, other_prepared_specs_in_catalog=[])
        self.assertIn("the same files are prepared for 2 specs outside the catalog on spark-2, spark-3;",
                      self.files_value(menu.view_lines(outside, "configured", width=80, names=NAMES)))

    def test_the_list_leaves_the_same_files_to_the_recipe(self):
        # The recipe frame, the Prepare suggestion and the machine question
        # name the other spec and its machines; the list card reads as for a
        # spec whose files no other spec holds.
        running = {"kind": "service", "state": "running", "hosts": ["spark-2"], "modified": False,
                   "age": 30, "source": "inventory"}
        cases = {"missing": sharing(), "unknown": sharing(local_state="unknown"),
                 "not checked yet": sharing(checked_at=None, observation_age_seconds=None),
                 "review status": sharing(review={"status": "experimental"})}
        for name, state in cases.items():
            with self.subTest(name):
                self.assertTrue(menu.same_files_prepared(state))
                self.assertEqual(menu.state_parts(state), menu.state_parts(without_others(state)))
                self.assertEqual(menu.card_parts(state, running), menu.card_parts(without_others(state), running))
        self.assertEqual(menu.card_parts(sharing()), ["3 nodes", "files not prepared"])
        # A service on 1 node of a 3-node spec does not name its node count.
        self.assertEqual(menu.card_parts(sharing(state="candidate"), running, told(["context 500K"])),
                         ["context 500K", "3 nodes", "running on 1 node", "files not prepared", "candidate"])
        for model in ("ab/m", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "org/" + "long-model-" * 8):
            for width in (100, 80, 60, 44, 32):
                with self.subTest(model=model, width=width):
                    card, = menu.list_cards([sharing(model_id=model)], width)
                    self.assert_cards([card], width)
                    self.assertNotIn("same files", flat(card.lines))

    def test_cli_cards_name_no_same_files(self):
        document = json.dumps({"entries": [sharing(model_id="ab/m"), without_others(sharing(spec_id="cd" * 32))]})
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output), \
                patch.dict("os.environ", {"COLUMNS": "80"}):
            self.assertEqual(menu.main(["cards"]), 0)
        self.assertEqual(output.getvalue().splitlines(),
                         ["heading\t  ab/m",
                          "spec\t" + "ab" * 32 + "\t    abababababab · 3 nodes · files not prepared",
                          "gap",
                          "heading\t  org/model",
                          "spec\t" + "cd" * 32 + "\t    cdcdcdcdcdcd · 3 nodes · files not prepared",
                          "gap"])

    def files_value(self, lines):
        """The frame's Files value, whitespace collapsed."""
        text = frame_text(lines)
        return text.split(" Files ", 1)[1].split(" Archive ", 1)[0]

    def test_the_files_field_names_the_spec_and_its_machines(self):
        for width in (80, 60, 44):
            with self.subTest(width=width):
                lines = menu.view_lines(sharing(), "configured", width=width, names=NAMES)
                frame = [line.split("\t", 1)[1] for line in lines if line.startswith(("header\t", "next\t"))]
                self.assertTrue(all(len(line) <= width - 4 for line in frame), frame)
                self.assertEqual(self.files_value(lines),
                                 "not prepared on every serving node (checked 2 hours ago); " + SAME_FRAME)
                self.assertIn("suggest\tprepare", lines)
        # Not checked yet: the check hint stays with its value, and the claim follows.
        lines = menu.view_lines(sharing(checked_at=None, observation_age_seconds=None), "configured", width=80,
                                names=NAMES)
        self.assertEqual(self.files_value(lines), f"not checked yet; {menu.CHECK_HINT}; {SAME_FRAME}")
        # Several specs: the first is named; hosts in record order, each once.
        several = sharing(other_specs_prepared_copies=[copy_of(OTHER, "n2"), copy_of(OTHER, "n1", rank=1),
                                                       copy_of(THIRD, "n2"), copy_of(THIRD, "n0", rank=1)])
        self.assertIn("the same files are prepared for cdcdcdcdcdcd and 1 more on spark-3, spark-2, spark-1; "
                      "Prepare binds them there without copying",
                      self.files_value(menu.view_lines(several, "configured", width=80, names=NAMES)))
        # Unknown machines keep their node ID, marked as such.
        self.assertIn("on n1, n2;", self.files_value(menu.view_lines(sharing(), "configured", width=80,
                                                                     names=NodeNames())))

    def test_read_only_and_unoffered_prepare_keep_the_fact_only(self):
        fact = "the same files are prepared for cdcdcdcdcdcd on spark-2, spark-3"
        read_only = self.files_value(menu.view_lines(sharing(), "configured", width=80, names=NAMES, read_only=True))
        self.assertEqual(read_only, "not prepared on every serving node (checked 2 hours ago); " + fact)
        # Without a home Prepare is not offered: the fact stays, the Prepare clause goes.
        no_home = self.files_value(menu.view_lines(sharing(home=None), "configured", width=80, names=NAMES))
        self.assertTrue(no_home.endswith(fact), no_home)
        self.assertNotIn("Prepare binds", no_home)

    def test_no_claim_when_ready_changed_or_just_prepared(self):
        for name, state, after in (("ready", sharing(local_state="ready"), None),
                                   ("changed", sharing(local_state="changed"), None),
                                   ("prepared here", sharing(), "prepare")):
            with self.subTest(name):
                lines = menu.view_lines(state, "configured", width=80, names=NAMES, after=after, after_age=60)
                self.assertNotIn("same files", frame_text(lines))

    def test_the_suggestion_says_prepare_binds_the_files(self):
        def suggest(state):
            offered, _, _ = menu.operations(state, "configured")
            return menu.suggestion(state, offered, "configured", None, NAMES)

        self.assertEqual(suggest(sharing()), ("prepare", "files not prepared on every serving node (checked 2 hours "
                                                         "ago); Prepare binds the same files prepared for cdcdcdcdcdcd "
                                                         "without copying"))
        self.assertEqual(suggest(without_others(sharing())),
                         ("prepare", "files not prepared on every serving node (checked 2 hours ago)"))
        self.assertEqual(suggest(sharing(local_state="changed")),
                         ("prepare", "files changed since they were verified (checked 2 hours ago)"))
        # Not checked yet or unknown: Check now comes first, as without the other spec's copies.
        for state in (sharing(checked_at=None), sharing(local_state="unknown")):
            self.assertEqual(suggest(state), suggest(without_others(state)))
        self.assertEqual(menu.suggested_command(sharing(), "configured", NAMES),
                         ["./pulsar", "model", "prepare", "abababababab", "--yes"])

    def test_the_machine_question_names_the_other_specs_copy(self):
        state = sharing()
        self.assertEqual(menu.node_label(state, "n1", "spark-2", 78), "spark-2 — " + SAME_FILES_HERE)
        self.assertEqual(menu.node_label(state, "n0", "spark-1", 78), "spark-1 — home recorded here")
        # Records show only where the whole label fits.
        self.assertEqual(menu.node_label(state, "n1", "spark-2", 40), "spark-2")
        own = sharing(prepared_copies=[{"rank": 0, "node_id": "n1"}])
        self.assertEqual(menu.node_label(own, "n1", "spark-2", 100),
                         "spark-2 — prepared copy recorded here, " + SAME_FILES_HERE)
        self.assertEqual(menu.node_label(own, "n1", "spark-2", 60), "spark-2 — prepared copy recorded here")
        self.assertEqual(menu.node_label(sharing(local_state="ready"), "n1", "spark-2", 78), "spark-2")
        # A schema-3 machine question for one snapshot names that snapshot's
        # copies, on machines that hold copies of every required snapshot.
        snapshots = two_snapshots(HOME, HOME)
        snapshots["target"]["snapshot_manifest_id"] = SHARED
        snapshots["draft"]["snapshot_manifest_id"] = "f" * 64
        schema3 = checked(local_state="missing", snapshots=snapshots,
                          other_specs_prepared_copies=[copy_of(OTHER, "n1"), copy_of(THIRD, "n1", manifest="f" * 64),
                                                       copy_of(OTHER, "n2", rank=1)])
        self.assertEqual(menu.node_label(schema3, "n1", "spark-2", 78, "target"), "spark-2 — " + SAME_FILES_HERE)
        self.assertEqual(menu.node_label(schema3, "n1", "spark-2", 78, "draft"),
                         "spark-2 — same files prepared here for efefefefefef")
        # spark-3 holds the target only: Prepare would still copy the draft there.
        self.assertEqual(menu.node_label(schema3, "n2", "spark-3", 78, "target"), "spark-3")
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", out), \
                patch.dict("os.environ", {"COLUMNS": "80"}), \
                patch("subprocess.run", side_effect=AssertionError("node choices must not probe")):
            self.assertEqual(menu.main(["node-choices", "--spec-id", state["spec_id"], "--action", "prepare",
                                        "--node-name", "n0=spark-1", "--node-name", "n1=spark-2"]), 0)
        self.assertEqual([line.split("\t") for line in out.getvalue().splitlines()],
                         [["title", "Which machine should serve the model?"], ["default", "n0"],
                          ["node", "n0", "spark-1 — home recorded here"],
                          ["node", "n1", "spark-2 — " + SAME_FILES_HERE]])


class SameFilesAndFoundFiles(unittest.TestCase):
    """A folder another spec's record names is in Pulsar's records. A home
    folder no record names stays found even beside another spec's prepared
    copy, which is another folder, and the frame never says both "files found
    on" and "the same files are prepared for" about one machine: found files
    come first, since no home is recorded then and Prepare is not offered."""

    scan_file = FilesFound.scan_file

    @staticmethod
    def without_home(**fields):
        """No home recorded, a verified archive, the scan's manifest."""
        return checked(**{"local_state": "missing", "archive_state": "verified",
                          "snapshot_manifest_id": MANIFEST_FOUND, **fields})

    @staticmethod
    def named(lines):
        """(machines "files found" names, machines "the same files" names) in a frame."""
        text = frame_text(lines)
        found = re.search(r"files found on (.+?),? (?:not in|but not in) Pulsar's records", text)
        same = re.search(r"the same files are prepared for [0-9a-f]{12}(?: and \d+ more)? on (.+?)(?:;| Archive )",
                         text)
        return ({host.strip() for host in found.group(1).split(",")} if found else set(),
                {host.strip() for host in same.group(1).split(",")} if same else set())

    def test_a_folder_another_specs_binding_names_is_not_found_files(self):
        # The scan saw the home folder on n1 with another spec's binding, and no home record.
        manifest, location = scan_location("n1")
        location["bindings"] = [copy_of(OTHER, "n1", manifest=MANIFEST_FOUND)]
        bound = self.scan_file(scan((manifest, location)))
        self.assertEqual(menu.found_files(bound), {})
        # The Model files page's "not in Pulsar's records" needs both: no home record and no binding.
        found = menu.found_files(self.scan_file(scan(scan_location("n1"), scan_location("n2"))))
        self.assertEqual(found, {MANIFEST_FOUND: ["n1", "n2"]})

    def test_the_two_clauses_never_name_one_machine(self):
        records = {"none": [], "n1": [copy_of(OTHER, "n1", manifest=MANIFEST_FOUND)],
                   "n1 and n2": [copy_of(OTHER, "n1", manifest=MANIFEST_FOUND),
                                 copy_of(OTHER, "n2", rank=1, manifest=MANIFEST_FOUND)],
                   "n0": [copy_of(OTHER, "n0", manifest=MANIFEST_FOUND)]}
        scans = {"none": None, "n1": {MANIFEST_FOUND: ["n1"]}, "n1 and n2": {MANIFEST_FOUND: ["n1", "n2"]}}
        seen = set()
        for record_name, copies in records.items():
            for scan_name, files in scans.items():
                for read_only in (False, True):
                    state = self.without_home(other_specs_prepared_copies=copies)
                    lines = menu.view_lines(state, "configured", width=80, names=NAMES, files=files,
                                            read_only=read_only)
                    found, same = self.named(lines)
                    with self.subTest(records=record_name, scan=scan_name, read_only=read_only):
                        self.assertFalse(found & same, frame_text(lines))
                        seen.add((bool(found), bool(same)))
                        # Read-only frames offer no Download, so they name no found files.
                        if not read_only:
                            self.assertEqual(found, {NAMES(node) for node in (files or {}).get(MANIFEST_FOUND, [])})
                        self.assertEqual(same, {NAMES(copy["node_id"]) for copy in copies} - found)
                        if copies and not same:
                            # Every machine with a copy is named as found: the clause goes, not its hosts only.
                            self.assertNotIn("the same files", frame_text(lines))
        # Each clause was shown alone and together, on different machines.
        self.assertTrue({(True, False), (False, True), (True, True)} <= seen, seen)
        # The machine question follows the same rule, and starts on the one
        # machine with found files, though another spec's copy is recorded there.
        state = self.without_home(other_specs_prepared_copies=[copy_of(OTHER, "n0", manifest=MANIFEST_FOUND),
                                                               copy_of(OTHER, "n1", rank=1, manifest=MANIFEST_FOUND)])
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", out), \
                patch.dict("os.environ", {"COLUMNS": "80"}), \
                patch("subprocess.run", side_effect=AssertionError("node choices must not probe")):
            self.assertEqual(menu.main(["node-choices", "--spec-id", state["spec_id"], "--action", "acquire",
                                        "--files", self.scan_file(scan(scan_location("n1"))),
                                        "--node-name", "n0=spark-1", "--node-name", "n1=spark-2",
                                        "--node-name", "n2=spark-3"]), 0)
        self.assertEqual([line.split("\t") for line in out.getvalue().splitlines()],
                         [["title", "Which machine should hold the model files?"], ["default", "n1"],
                          ["node", "n0", "spark-1 — " + SAME_FILES_HERE],
                          ["node", "n1", "spark-2 — " + menu.FILES_FOUND_HERE], ["node", "n2", "spark-3"]])

    def test_a_home_folder_beside_another_specs_copy_is_still_found(self):
        # spark-2 holds a home folder no record names and, under the
        # prepared-copy root, another spec's prepared copy: two folders.
        home_folder = scan_location("n1")
        copy_folder = scan_location("n1", kind="prepared")
        copy_folder[1]["bindings"] = [copy_of(OTHER, "n1", manifest=MANIFEST_FOUND)]
        files = menu.found_files(self.scan_file(scan(home_folder, copy_folder)))
        self.assertEqual(files, {MANIFEST_FOUND: ["n1"]})
        state = self.without_home(other_specs_prepared_copies=[copy_of(OTHER, "n1", manifest=MANIFEST_FOUND)])
        self.assertEqual(menu.unrecorded_files(state, files), ["n1"])
        # Download checks that folder before fetching anything, ahead of
        # Restore's copy from the verified archive.
        offered, _, _ = menu.operations(state, "configured")
        self.assertEqual(menu.suggestion(state, offered, "configured", None, NAMES, files=files),
                         ("acquire", "files found on spark-2 but not in Pulsar's records; "
                                     "Download checks them before fetching anything"))
        lines = menu.view_lines(state, "configured", width=80, names=NAMES, files=files)
        self.assertIn("suggest\tacquire", lines)
        self.assertEqual(self.named(lines), ({"spark-2"}, set()))
        self.assertNotIn("the same files", frame_text(lines))


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


class Cards(CardChecks, unittest.TestCase):
    """The catalog list: a card per spec, grouped by model, with every part."""

    def test_adjacent_specs_of_one_model_share_one_heading(self):
        rows = [checked(spec_id="aa" * 32, model_id="org/alpha", local_state="ready", home=HOME),
                checked(spec_id="bb" * 32, model_id="org/alpha"),
                row(spec_id="cc" * 32, model_id="org/beta"),
                checked(spec_id="dd" * 32, model_id="org/alpha", review={"status": "withdrawn"})]
        cards = menu.list_cards(rows, 80)
        self.assertEqual([(card.heading, card.spec_id[:2], card.gap) for card in cards],
                         [("  org/alpha", "aa", False), (None, "bb", True), ("  org/beta", "cc", True),
                          ("  org/alpha", "dd", True)])
        self.assertEqual(screen(cards), [
            "  org/alpha",
            "    aaaaaaaaaaaa · 3 nodes · files prepared",
            "    bbbbbbbbbbbb · 3 nodes · files unknown",
            "",
            "  org/beta",
            "    cccccccccccc · 3 nodes · not checked yet",
            "",
            # A withdrawn spec sorts last (catalog.entries), under a second heading.
            "  org/alpha",
            "    dddddddddddd · withdrawn · 3 nodes · files unknown",
            ""])

    def test_the_80_column_list_reads_as_planned(self):
        mimo = checked(spec_id="11" * 32, model_id="XiaomiMiMo/MiMo-V2.6-Flash-RL", local_state="missing",
                       geometry={"nodes": 2, "tp": 2, "pp": 1})
        short = dict(recipe("15", "500000"), local_state="missing", state="candidate", snapshot_manifest_id=SHARED,
                     other_specs_prepared_copies=[copy_of("e5" * 32, node, rank) for rank, node in
                                                  enumerate(("n0", "n1", "n2"))])
        long = recipe("e5", "1048576")
        glm = row(spec_id="b1" * 32, model_id="nvidia/GLM-5.3-Flash-NVFP4", state="candidate")
        rows = [mimo, short, long, glm]
        live = menu.live_inventory(inventory(("e5" * 32, "running", ["head", "w1", "w2"])), rows, now=LIVE_NOW)
        self.assertEqual(screen(menu.list_cards(rows, 80, live=live)), [
            "  XiaomiMiMo/MiMo-V2.6-Flash-RL",
            "    111111111111 · 2 nodes · files not prepared",
            "",
            "  deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "    151515151515 · context 500K · 3 nodes · files not prepared · candidate",
            "    e5e5e5e5e5e5 · context 1M · running on 3 nodes · files prepared",
            "",
            "  nvidia/GLM-5.3-Flash-NVFP4",
            "    b1b1b1b1b1b1 · 3 nodes · not checked yet · candidate",
            ""])

    def test_every_part_is_shown_whole_at_every_width(self):
        partial = {"kind": "service", "state": "partial", "hosts": ["spark-1", "spark-2"], "modified": True,
                   "age": 30, "source": "inventory"}
        cases = {
            "plain": (checked(home=HOME, local_state="ready", state="candidate"), None, None,
                      ["3 nodes", "files prepared", "candidate"]),
            "not checked": (row(model_id="org/" + "very-long-model-name-" * 4), None, None,
                            ["3 nodes", "not checked yet"]),
            "warnings": (checked(home=HOME, local_state="changed", review={"status": "withdrawn"}, **GUARDED),
                         None, None, ["withdrawn", "start unsupported", "3 nodes", "files changed"]),
            "everything": (sharing(review={"status": "experimental"}, state="measured"), partial,
                           told(["context 500K", "max sequences 8"]),
                           ["context 500K", "max sequences 8", "3 nodes", "partly running on 2 nodes (modified recipe)",
                            "files not prepared", "experimental", "measured"]),
        }
        for name, (state, live, facts, expected) in cases.items():
            parts = menu.card_parts(state, live, facts)
            self.assertEqual(parts, expected, name)
            for width in (100, 80, 60, 44, 32):
                lines = menu.card_lines(state["spec_id"], parts, width)
                with self.subTest(name, width=width, lines=lines):
                    self.assert_spec_lines(lines, state["spec_id"], width)
                    self.assert_in_order(lines, parts)
                    self.assertNotIn("…", flat(lines))

    def test_warnings_lead_the_spec_line_once(self):
        state = checked(home=HOME, local_state="ready", review={"status": "withdrawn"}, **GUARDED)
        for width in (100, 80, 60, 44, 32):
            lines = menu.card_lines(state["spec_id"], menu.card_parts(state), width)
            with self.subTest(width=width, lines=lines):
                self.assertEqual(tokens(lines)[:3], ["abababababab", "withdrawn", "start unsupported"])
                self.assertEqual(flat(lines).count("withdrawn"), 1)
                self.assertEqual(flat(lines).count("start unsupported"), 1)
        self.assertEqual(menu.card_lines(state["spec_id"], menu.card_parts(state), 32),
                         ["    abababababab · withdrawn", "      start unsupported", "      3 nodes · files prepared"])
        self.assertNotIn("start blocked", flat(menu.card_lines(state["spec_id"], menu.card_parts(state), 32)))

    def test_the_model_name_is_cut_in_the_middle_only_when_longer_than_its_line(self):
        nemotron = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
        rows = [checked(spec_id="aa" * 32, model_id=nemotron), checked(spec_id="bb" * 32, model_id="org/" + "m" * 40)]
        for width in (100, 80, 60, 44, 32):
            cards = menu.list_cards(rows, width)
            with self.subTest(width=width, headings=[card.heading for card in cards]):
                self.assert_cards(cards, width)
                for card, model in zip(cards, (nemotron, "org/" + "m" * 40)):
                    name = card.heading[2:]
                    if len(model) <= width - 2:
                        self.assertEqual(name, model)
                    else:
                        # Without the organization, then cut in the middle so the end stays.
                        head, cut, tail = name.partition("…")
                        short = model.partition("/")[2]
                        self.assertTrue(short.startswith(head) and short.endswith(tail), name)
                        self.assertEqual(len(name), min(width - 2, len(short)))
        self.assertEqual(menu.list_cards(rows, 44)[0].heading, "  NVIDIA-Nemotron-3.5-…er-VL-120B-A12B-NVFP4")
        self.assertEqual(menu.list_cards(rows, 44)[1].heading, "  " + "m" * 40)

    def test_cli_prints_the_card_protocol(self):
        document = json.dumps({"entries": [row(), checked(spec_id="cd" * 32, local_state="ready", home=HOME),
                                           checked(spec_id="ef" * 32, model_id="org/other", local_state="missing")]})
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output), \
                patch.dict("os.environ", {"COLUMNS": "44"}):
            self.assertEqual(menu.main(["cards"]), 0)
        self.assertEqual(output.getvalue().splitlines(),
                         ["heading\t  org/model",
                          "spec\t" + "ab" * 32 + "\t    abababababab · 3 nodes · not checked yet",
                          "spec\t" + "cd" * 32 + "\t    cdcdcdcdcdcd · 3 nodes · files prepared",
                          "gap",
                          "heading\t  org/other",
                          "spec\t" + "ef" * 32 + "\t    efefefefefef · 3 nodes",
                          "line\t      files not prepared",
                          "gap"])
        self.assertNotIn("\x1b", output.getvalue())

    def test_compare_with_leaves_out_the_compared_spec_and_its_emptied_group(self):
        rows = [checked(spec_id="aa" * 32, model_id="org/alpha"), checked(spec_id="bb" * 32, model_id="org/beta"),
                checked(spec_id="cc" * 32, model_id="org/beta"),
                dict(checked(spec_id="dd" * 32, model_id="org/gamma"), historical=True)]

        def kinds(compared):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"entries": rows}))), patch("sys.stdout", output), \
                    patch.dict("os.environ", {"COLUMNS": "80"}):
                self.assertEqual(menu.main(["cards", "--compare-with", compared]), 0)
            return [line.split("\t")[1][:2] if line.startswith("spec\t") else line.strip()
                    for line in output.getvalue().splitlines()]

        # The historical spec is left out too; a group left empty has no heading.
        self.assertEqual(kinds("aa" * 32), ["heading\t  org/beta", "bb", "cc", "gap"])
        self.assertEqual(kinds("bb" * 32), ["heading\t  org/alpha", "aa", "gap", "heading\t  org/beta", "cc", "gap"])


# R7: recipes of one model that differ in one setting read alike unless the
# label names that setting; DeepSeek's two recipes differ only in context.
LONG_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"


def recipe(spec_byte, context="500000", nodes=3, model=LONG_MODEL, digest="sha256:" + "b" * 64, **fields):
    return checked(spec_id=spec_byte * 32, model_id=model, home=HOME, local_state="ready",
                   geometry={"nodes": nodes, "tp": nodes, "pp": 1}, image={"digest": digest},
                   engine_args=["--max-model-len", context, "--max-num-seqs", "1"], **fields)


def told_apart(rows):
    """Each recipe's fullest distinguishing facts, as words."""
    return {spec: facts.words() for spec, facts in menu.distinguishing_facts(rows).items()}


def told(*sets, unique=True):
    return menu.RecipeFacts([list(words) for words in sets], unique)


class RecipesOfOneModel(CardChecks, unittest.TestCase):
    def test_short_count_uses_k_and_m_only_for_round_values(self):
        for value, expected in ((1048576, "1M"), ("500000", "500K"), ("131072", "128K"), ("1000000", "1M"),
                                ("8192", "8K"), ("8", "8"), ("1000", "1K"), ("123456", "123456"), ("0", "0"),
                                ("fp8", "fp8"), ("not supplied", "not supplied")):
            with self.subTest(value=value):
                self.assertEqual(menu.short_count(value), expected)

    def test_the_first_setting_that_differs_tells_recipes_apart(self):
        rows = [recipe("aa", "500000"), recipe("bb", "1048576"), checked(spec_id="cc" * 32, model_id="org/other")]
        self.assertEqual(told_apart(rows), {"aa" * 32: "context 500K", "bb" * 32: "context 1M"})
        # Node count comes first; a recipe without the flag says so rather than guessing a default.
        nodes = [recipe("aa", nodes=2), recipe("bb", nodes=1, context="1048576")]
        self.assertEqual(told_apart(nodes), {"aa" * 32: "2 nodes", "bb" * 32: "1 node"})
        unset = [recipe("aa"), dict(recipe("bb"), engine_args=["--max-num-seqs", "1"])]
        self.assertEqual(told_apart(unset)["bb" * 32], "context not set")
        unrecorded = [recipe("aa"), {key: value for key, value in recipe("bb").items() if key != "engine_args"}]
        self.assertEqual(told_apart(unrecorded)["bb" * 32], "context not recorded")
        sequences = [recipe("aa"), dict(recipe("bb"), engine_args=["--max-model-len", "500000", "--max-num-seqs=8"])]
        self.assertEqual(told_apart(sequences)["bb" * 32], "max sequences 8")
        quantized = [recipe("aa"), dict(recipe("bb"), engine_args=[*recipe("bb")["engine_args"], "--quantization", "fp8"])]
        self.assertEqual(told_apart(quantized)["bb" * 32], "quantization fp8")
        # Otherwise the image; recipes that differ in none of these keep their spec IDs only.
        images = [recipe("aa"), recipe("bb", digest="sha256:" + "0123456789ab" + "c" * 52)]
        self.assertEqual(told_apart(images)["bb" * 32], "image 0123456789ab")
        self.assertEqual(told_apart([recipe("aa"), recipe("bb")]), {})

    def test_short_words_that_read_alike_give_way_to_the_recorded_values(self):
        rows = [recipe("aa", "1000000"), recipe("bb", "1048576")]
        self.assertEqual(told_apart(rows), {"aa" * 32: "context 1000000", "bb" * 32: "context 1048576"})

    def test_a_second_fact_is_added_only_where_the_first_leaves_recipes_alike(self):
        rows = [recipe("aa", "500000", nodes=2), recipe("bb", "1048576", nodes=2), recipe("cc", "1048576", nodes=1)]
        self.assertEqual(told_apart(rows), {"aa" * 32: "2 nodes · context 500K",
                                                           "bb" * 32: "2 nodes · context 1M",
                                                           "cc" * 32: "1 node · context 1M"})

    def test_the_org_is_left_out_only_where_no_other_model_reads_the_same(self):
        rows = [recipe("aa"), recipe("bb"), checked(spec_id="cc" * 32, model_id="one/Model"),
                checked(spec_id="dd" * 32, model_id="two/Model"), checked(spec_id="ee" * 32, model_id="plain")]
        self.assertEqual(menu.short_model_names(rows), {"aa" * 32: "DeepSeek-V4.1-Flash-1017",
                                                        "bb" * 32: "DeepSeek-V4.1-Flash-1017"})

    def test_cards_name_the_fact_and_keep_the_end_of_the_model_name(self):
        rows = [recipe("aa", "500000"), recipe("bb", "1048576")]
        for width in (100, 80, 60, 44, 32):
            cards = menu.list_cards(rows, width)
            with self.subTest(width=width, screen=screen(cards)):
                self.assert_cards(cards, width)
                self.assertEqual([card.heading is None for card in cards], [False, True])
                self.assertTrue(cards[0].heading.endswith("017"))  # the end of the name, where recipes differ
                for card, fact in zip(cards, ("context 500K", "context 1M")):
                    self.assertEqual(tokens(card.lines)[:2], [card.spec_id[:12], fact])
                    self.assertIn("files prepared", flat(card.lines))
        self.assertEqual(screen(menu.list_cards(rows, 80)), [
            "  deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "    aaaaaaaaaaaa · context 500K · 3 nodes · files prepared",
            "    bbbbbbbbbbbb · context 1M · 3 nodes · files prepared",
            ""])
        # Narrow, the name leaves out its organization and whole parts wrap.
        self.assertEqual(screen(menu.list_cards(rows, 32)), [
            "  DeepSeek-V4.1-Flash-1017",
            "    aaaaaaaaaaaa · context 500K",
            "      3 nodes · files prepared",
            "    bbbbbbbbbbbb · context 1M",
            "      3 nodes · files prepared",
            ""])

    def test_a_node_count_fact_is_named_once(self):
        two = recipe("aa", nodes=2, model="org/model")
        self.assertEqual(menu.card_parts(two, None, told(["2 nodes"])), ["2 nodes", "files prepared"])
        self.assertEqual(menu.card_parts(two, None, told(["context 500K"])), ["context 500K", "2 nodes", "files prepared"])

    def test_critical_warnings_come_before_the_fact(self):
        state = recipe("aa", **GUARDED, review={"status": "withdrawn"})
        self.assertEqual(menu.card_parts(state, None, told(["context 500K"])),
                         ["withdrawn", "start unsupported", "context 500K", "3 nodes", "files prepared"])

    def test_cli_cards_name_the_fact_for_every_recipe_of_a_repeated_model(self):
        document = json.dumps({"entries": [recipe("aa", "500000"), recipe("bb", "1048576"),
                                           checked(spec_id="cc" * 32, local_state="ready", home=HOME)]})
        for columns in ("80", "60", "44"):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", output), \
                    patch.dict("os.environ", {"COLUMNS": columns}):
                self.assertEqual(menu.main(["cards"]), 0)
            lines = output.getvalue().splitlines()
            specs = [line.split("\t")[2] for line in lines if line.startswith("spec\t")]
            with self.subTest(columns=columns, lines=lines):
                self.assertEqual([kind.split("\t")[0] for kind in lines].count("heading"), 2)
                self.assertEqual(specs[0].split(" · ")[1], "context 500K")
                self.assertEqual(specs[1].split(" · ")[1], "context 1M")
                self.assertNotIn("context", " ".join(lines[lines.index("heading\t  org/model"):]))
                self.assertTrue(all(len(line.split("\t")[-1]) <= int(columns) for line in lines))

    def test_start_questions_name_the_fact_but_not_a_node_count_twice(self):
        state = recipe("aa", "1048576")
        self.assertIn(f"Start {LONG_MODEL} (context 1M) on 3 nodes?",
                      menu.question("start", state, distinguishing_fact="context 1M"))
        self.assertIn(f"retry Start for {LONG_MODEL} (context 1M) on 3 nodes?",
                      menu.question("start-memory", state, distinguishing_fact="context 1M"))
        self.assertIn(f"Start {LONG_MODEL} on 3 nodes?", menu.question("start", state, distinguishing_fact="3 nodes"))
        self.assertIn(f"Start {LONG_MODEL} (context 1M) on 3 nodes?",
                      menu.question("start", state, distinguishing_fact="3 nodes · context 1M"))
        self.assertNotIn("context 1M", menu.question("stop", state, distinguishing_fact="context 1M"))

    def test_confirm_reads_the_fact_from_the_whole_catalog_document(self):
        rows = [recipe("aa", "500000"), recipe("bb", "1048576")]

        def ask(entries, action="start"):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"entries": entries}))), patch("sys.stdout", output):
                self.assertEqual(menu.main(["confirm", "--spec-id", "bb" * 32, "--action", action]), 0)
            return output.getvalue()

        self.assertIn(f"Start {LONG_MODEL} (context 1M) on", ask(rows))
        self.assertIn(f"retry Start for {LONG_MODEL} (context 1M) on", ask(rows, "start-memory"))
        # One recipe's document has nothing to compare.
        self.assertIn(f"Start {LONG_MODEL} on", ask(rows[1:]))

    # Review of PR 97: at narrow widths a group needing several facts must not
    # lose the one that separates two recipes, and no fact is ever cut.
    @staticmethod
    def four(last):
        """Four recipes told apart by node count, context and max sequences."""
        rows = []
        for byte, (nodes, context, sequences) in zip(("aa", "bb", "cc", "dd"), (
                (2, "500000", "1"), (2, "500000", "8"), (2, "1048576", "1"), last)):
            rows.append(dict(recipe(byte, context, nodes=nodes),
                             engine_args=["--max-model-len", context, "--max-num-seqs", sequences]))
        return rows

    def assert_told_apart(self, rows, widths):
        facts = menu.distinguishing_facts(rows)
        for width in widths:
            cards = menu.list_cards(rows, width)
            with self.subTest(width=width, screen=screen(cards)):
                self.assert_cards(cards, width)
                told = {}
                for item, card in zip(rows, cards):
                    # Only the model name is ever cut; every fact is whole, in order.
                    self.assertNotIn("…", flat(card.lines))
                    named = [word for word in tokens(card.lines) if word in facts[item["spec_id"]].sets[0]]
                    self.assertEqual(named, facts[item["spec_id"]].sets[0])
                    told.setdefault(item["model_id"], []).append(tuple(named))
                for model, sets in told.items():
                    self.assertEqual(len(set(sets)), len(sets), model)

    def test_shorter_fact_sets_keep_only_facts_that_still_tell_a_recipe_apart(self):
        facts = menu.distinguishing_facts(self.four((1, "500000", "1")))
        self.assertEqual(facts["aa" * 32].sets, [["2 nodes", "context 500K", "max sequences 1"]])
        self.assertEqual(facts["bb" * 32].sets, [["2 nodes", "context 500K", "max sequences 8"],
                                                 ["context 500K", "max sequences 8"], ["max sequences 8"]])
        self.assertTrue(all(told.unique for told in facts.values()))
        # Recipes alike in every fact are not unique, and keep their fullest facts only.
        alike = menu.distinguishing_facts([recipe("aa", nodes=2), recipe("bb", nodes=2), recipe("cc", nodes=1)])
        self.assertEqual(alike["aa" * 32], menu.RecipeFacts([["2 nodes"]], False))
        self.assertTrue(alike["cc" * 32].unique)

    def test_four_recipes_differ_and_fit_at_44_and_32_columns(self):
        for last in ((1, "1048576", "8"), (1, "500000", "1")):
            with self.subTest(last=last):
                self.assert_told_apart(self.four(last), (100, 80, 60, 44, 32))
        cards = menu.list_cards(self.four((1, "1048576", "8")), 44)
        self.assertEqual(cards[0].lines + cards[1].lines, ["    aaaaaaaaaaaa · 2 nodes · context 500K",
                                                           "      max sequences 1 · files prepared",
                                                           "    bbbbbbbbbbbb · 2 nodes · context 500K",
                                                           "      max sequences 8 · files prepared"])
        self.assertEqual(menu.list_cards(self.four((1, "1048576", "8")), 32)[0].lines,
                         ["    aaaaaaaaaaaa · 2 nodes", "      context 500K", "      max sequences 1",
                          "      files prepared"])

    # Second review of PR 97: facts alone could read the same for two models,
    # and a warning could give way to facts.
    def test_two_models_with_alike_recipes_keep_their_spec_prefixes(self):
        first = self.four((1, "1048576", "8"))
        second = [dict(item, spec_id=byte * 32, model_id="other-org/Other-Model-7B")
                  for item, byte in zip(self.four((1, "1048576", "8")), ("ee", "ff", "11", "22"))]
        self.assert_told_apart(first + second, (80, 60, 44, 32))
        cards = menu.list_cards(first + second, 32)
        self.assertEqual([card.heading for card in cards if card.heading],
                         ["  DeepSeek-V4.1-Flash-1017", "  other-org/Other-Model-7B"])
        self.assertEqual(cards[4].lines[0], "    eeeeeeeeeeee · 2 nodes")

    def test_a_withdrawn_recipe_keeps_its_warning_and_prefix(self):
        rows = self.four((1, "1048576", "8"))
        rows[0] = dict(rows[0], review={"status": "withdrawn"})
        self.assert_told_apart(rows, (80, 60, 44, 32))
        self.assertEqual(menu.list_cards(rows, 44)[0].lines, ["    aaaaaaaaaaaa · withdrawn · 2 nodes",
                                                              "      context 500K · max sequences 1",
                                                              "      files prepared"])

    def test_guarded_recipes_of_one_model_differ_at_32_columns(self):
        rows = [recipe("aa", "500000", **GUARDED), recipe("bb", "1048576", **GUARDED)]
        self.assert_told_apart(rows, (100, 80, 60, 44, 32))
        self.assertEqual(menu.list_cards(rows, 44)[0].lines, ["    aaaaaaaaaaaa · start unsupported",
                                                              "      context 500K · 3 nodes",
                                                              "      files prepared"])
        withdrawn = [dict(item, review={"status": "withdrawn"}) for item in rows]
        self.assert_told_apart(withdrawn, (80, 60, 44, 32))


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

    def test_cards_name_the_service_beside_the_saved_files_state(self):
        fact = self.fact(inventory((self.SPEC, "running", ["head", "w1", "w2"])))
        state = checked(home=HOME, local_state="ready", model_id="nvidia/GLM-5.3-Flash-NVFP4")
        # The live words name the node count, so it is not repeated.
        self.assertEqual(menu.card_parts(state, fact), ["running on 3 nodes", "files prepared"])
        self.assertEqual(menu.card_parts(state, fact, told(["context 1M"])),
                         ["context 1M", "running on 3 nodes", "files prepared"])
        partial = self.fact(inventory((self.SPEC, "partial", ["head", "w1"], False)))
        self.assertEqual(menu.card_parts(state, partial),
                         ["3 nodes", "partly running on 2 nodes (modified recipe)", "files prepared"])
        old = self.fact(inventory((self.SPEC, "running", ["head", "w1", "w2"]), generated_at="2026-09-04T23:30:00Z"))
        self.assertEqual(menu.card_parts(state, old), ["3 nodes", "last seen running 40 minutes ago", "files prepared"])
        withdrawn = checked(home=HOME, local_state="ready", review={"status": "withdrawn"})
        self.assertEqual(menu.card_parts(withdrawn, fact), ["withdrawn", "running on 3 nodes", "files prepared"])
        one = checked(home=HOME, local_state="ready", geometry={"nodes": 1, "tp": 1, "pp": 1})
        self.assertEqual(menu.card_parts(one, self.fact(inventory((self.SPEC, "running", ["head"])), state=one)),
                         ["running on 1 node", "files prepared"])
        for width in (32, 44, 60, 80, 100):
            with self.subTest(width=width):
                lines = menu.card_lines(self.SPEC, menu.card_parts(state, fact), width)
                self.assertEqual(tokens(lines)[:2], ["abababababab", "running on 3 nodes"])
                self.assertTrue(all(len(line) <= width for line in lines), lines)

    def test_malformed_outcomes_are_rejected(self):
        for value in ("x", f"{self.SPEC}:restart:0:1", f"{self.SPEC}:status:0:soon", f"{self.SPEC}:status::1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                menu.service_outcomes([value])

    def test_cli_cards_date_the_inventory_and_view_uses_it(self):
        import tempfile
        state = checked(home=HOME, local_state="ready")
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(inventory((self.SPEC, "running", ["head"]), unobserved=("w2",),
                                generated_at=datetime.now(timezone.utc).isoformat()), handle)
            handle.flush()
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps({"entries": [state]}))), patch("sys.stdout", output), \
                    patch.dict("os.environ", {"COLUMNS": "100"}):
                self.assertEqual(menu.main(["cards", "--live", handle.name]), 0)
            self.assertEqual(output.getvalue().splitlines(), [
                "observed\tRunning services: service inventory less than a minute ago; not observed: spark-3.",
                "heading\t  org/model",
                "spec\t" + self.SPEC + "\t    abababababab · 3 nodes · running on 1 node · files prepared",
                "gap"])
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
                         "Purge org/model: delete 1 prepared copy and release 1 prepared copy whose files stay? "
                         "The home and the archive are kept.")
        self.assertIn("from spark-1 to spark-3",
                       menu.question("move", state, plan={"source_node": "n0", "destination_node": "n2"}, names=NAMES))
        self.assertIn("Recovery requires a usable archive or an available download source.",
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
        return [row(spec_id="a" * 64, home=HOME), row(spec_id="b" * 64, home=HOME),
                checked(spec_id="c" * 64, home=HOME, observation_age_seconds=3 * 86400),
                checked(spec_id="d" * 64, local_state="missing"),
                checked(spec_id="e" * 64, local_state="missing", archive_state="verified"),
                checked(spec_id="f" * 64, home=HOME, local_state="changed", observation_age_seconds=600),
                checked(spec_id="0" * 64, home=HOME, local_state="ready")]

    def test_groups_suggestions_in_lifecycle_order_with_the_newest_check(self):
        self.assertEqual(menu.home_next(self.specs(), "configured"),
                         "Next: 2 specs not checked yet · 1 spec needs a check · 1 spec needs Download · "
                         "1 spec needs Restore · 1 spec needs Prepare · 1 spec ready to Start — open Catalog "
                         "and storage. Newest saved check: 10 minutes ago.")

    def test_an_old_check_that_found_prepared_files_counts_as_ready_to_start(self):
        old = [checked(spec_id="a" * 64, home=HOME, local_state="ready", observation_age_seconds=9 * 86400),
               checked(spec_id="b" * 64, home=HOME, local_state="missing", observation_age_seconds=9 * 86400)]
        self.assertEqual(menu.home_next(old, "configured"),
                         "Next: 1 spec needs a check · 1 spec ready to Start — open Catalog and storage. "
                         "Newest saved check: 9 days ago.")

    def test_check_counts_cover_specs_with_a_home_once_any_spec_has_one(self):
        # Specs this cluster never downloaded would otherwise fill the line.
        specs = [row(spec_id="a" * 64), row(spec_id="b" * 64, home=HOME),
                 checked(spec_id="c" * 64, observation_age_seconds=3 * 86400),
                 checked(spec_id="d" * 64, local_state="missing"),
                 checked(spec_id="0" * 64, home=HOME, local_state="ready")]
        self.assertEqual(menu.home_next(specs, "configured"),
                         "Next: 1 spec not checked yet · 1 spec needs Download · 1 spec ready to Start — open "
                         "Catalog and storage. Newest saved check: 2 hours ago.")
        # Without any home (a fresh checkout or lost records), every spec counts.
        self.assertTrue(menu.home_next(specs[:1] + specs[2:4], "configured").startswith(
            "Next: 1 spec not checked yet · 1 spec needs a check · 1 spec needs Download — "))

    def test_counts_use_singular_and_plural_wording(self):
        ready = [checked(spec_id=c * 64, home=HOME, local_state="ready") for c in "ab"]
        self.assertEqual(menu.home_next(ready, "configured"),
                         "Next: 2 specs ready to Start — open Catalog and storage. Newest saved check: 2 hours ago.")
        stale = [checked(spec_id=c * 64, observation_age_seconds=3 * 86400) for c in "ab"]
        self.assertTrue(menu.home_next(stale, "configured").startswith("Next: 2 specs need a check — "))
        self.assertTrue(menu.home_next([row()], "configured").startswith("Next: 1 spec not checked yet — "))

    def test_never_checked_alone_has_no_check_age(self):
        self.assertEqual(menu.home_next([row(spec_id="a" * 64), row(spec_id="b" * 64)], "configured"),
                         "Next: 2 specs not checked yet — open Catalog and storage.")

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
        for entries, expected in ((self.specs()[:1], "Next: 1 spec not checked yet — open **Catalog and storage**.\n"),
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
        # Without the plan's views a release cannot be named as the home or a shared copy.
        self.assertEqual(self.summary("purge", plan), [
            "Delete 2 prepared copies, release 1 prepared copy whose files stay and clear 1 unfinished copy "
            "on spark-2, spark-3, spark-1."])
        empty = {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "actions": []}, "incomplete_preparations": []}
        self.assertEqual(self.summary("purge", empty), [
            "Nothing to purge: no prepared copies or unfinished copies are recorded for this spec."])

    @staticmethod
    def purge_views():
        """Views in the order of their actions: a copy with 3 GiB of files, a shared copy and the home."""
        def view(node, home=False, files=None):
            return {"node_id": node, "is_home_view": home,
                    "verification": {"files": files or {"w.bin": [1, 2, 3, 1024]}}}
        views = [view("n1", files={"a.bin": [1, 2, 33188, 2 * GIB, 0, 0], "b.bin": [1, 3, 33188, GIB, 0, 0]}),
                 view("n2"), view("n0", home=True)]
        actions = [{"node_id": "n1", "action": "remove-copy"}, {"node_id": "n2", "action": "release-binding"},
                   {"node_id": "n0", "action": "release-binding"}]
        return {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "views": views, "actions": actions},
                "incomplete_preparations": []}

    def test_purge_names_shared_copies_the_home_and_the_size_it_deletes(self):
        plan = self.purge_views()
        self.assertEqual(self.summary("purge", plan), [
            "Delete 1 prepared copy (3.00 GiB), release 1 shared copy and stop using the home as a prepared copy "
            "on spark-2, spark-3, spark-1. Shared files stay for other specs."])
        self.assertEqual(menu.question("purge", checked(home=HOME), plan=plan, names=NAMES),
                         "Purge org/model: delete 1 prepared copy (3.00 GiB), release 1 shared copy and stop using "
                         "the home as a prepared copy on spark-2, spark-3, spark-1? Shared files stay for other "
                         "specs; the home and the archive are kept.")
        # Only what the purge does is named: no zero counts.
        plan["plan"]["views"], plan["plan"]["actions"] = plan["plan"]["views"][:1], plan["plan"]["actions"][:1]
        self.assertEqual(menu.question("purge", checked(home=HOME), plan=plan, names=NAMES),
                         "Purge org/model: delete 1 prepared copy (3.00 GiB) on spark-2? "
                         "The home and the archive are kept.")
        # A copy without a readable size is still counted, without a size.
        plan["plan"]["views"][0]["verification"] = {}
        self.assertEqual(self.summary("purge", plan), ["Delete 1 prepared copy on spark-2."])

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
                         ["Delete the home model files on spark-1. No archive is required or checked.",
                          "Recovery requires a usable archive or an available download source."])

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

    def test_an_unrecorded_folder_shows_its_whole_path_and_the_fix(self):
        path = "/fixture/views/" + "ab" * 32
        detail = {"code": "unrecorded-destination", "message": "m", "node_id": "n1", "path": path,
                  "fix": "Pulsar has no record of this folder and never deletes files it did not record. Inspect it; if it is not needed, remove it outside Pulsar, then Prepare again."}
        plan = {"eligible": False, "blocker_details": [detail]}
        self.assertEqual(self.summary("prepare", plan), [
            f"Blocked: a folder already exists where Prepare would place the prepared copy on spark-2: {path}; "
            "Pulsar has no record of this folder and never deletes files it did not record. Inspect it; if it is not needed, remove it outside Pulsar, then Prepare again."])
        # No menu operation removes a folder that no record names.
        self.assertEqual(menu.blocked_choices(plan, [checked()]), [])

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


OTHER = "cd" * 32
OUTSIDE_CONF = "ef" * 32
IN_CATALOG = [{"spec_id": "ab" * 32, "model_id": "org/model", "nodes": 1},
              {"spec_id": OTHER, "model_id": "org/other", "nodes": 2}]


def running(conf, keys, *, mib=None, port=None, served=None, **fields):
    """One inventory service on node KEYS; MIB and PORT as each rank measured them."""
    ranks = [{"rank": str(index), "node": key, **({"gpu_memory": {"measured_mib": mib}} if mib is not None else {}),
              **({"observed_api_port": port} if port is not None else {})} for index, key in enumerate(keys)]
    return {"service_id": conf or served, "conf": conf, "state": "running", "ownership": "managed",
            "safe_to_stop": True, **({"served_name": served} if served else {}), "ranks": ranks, **fields}


def crowded(*services, processes=()):
    document = inventory()
    document["services"] = list(services)
    document["unmanaged_gpu_processes"] = list(processes)
    return document


class ServicesInTheWay(unittest.TestCase):
    """R1: a refused Start names the services on its serving nodes and offers Stop for each it may stop."""
    ONE = {"geometry": {"nodes": 1, "tp": 1, "pp": 1}}

    def way(self, document, node_ids=("n1",), ports=(8000,), **fields):
        return menu.services_in_the_way(document, checked(**{**self.ONE, **fields}), list(node_ids), IN_CATALOG,
                                        list(ports))

    def test_only_services_on_the_serving_nodes_are_named(self):
        document = crowded(running(OTHER, ["w1", "w2"], mib=91136, port=8000),
                           running(OUTSIDE_CONF, ["w2"], served="far-away", port=8001))
        stops, notes = self.way(document)
        self.assertEqual(stops, [{"conf": OTHER, "name": "org/other", "hosts": ["spark-2", "spark-3"],
                                  "port": 8000, "mib": 91136}])
        self.assertEqual(notes, [])
        # A node the inventory does not name, or no known serving node, names nothing.
        self.assertEqual(self.way(document, node_ids=("elsewhere",)), ([], []))
        self.assertEqual(self.way(document, node_ids=()), ([], []))

    def test_the_refused_spec_itself_is_not_another_service(self):
        stops, _ = self.way(crowded(running("ab" * 32, ["w1"], port=8000), running(OTHER, ["w1"])))
        self.assertEqual([stop["conf"] for stop in stops], [OTHER])

    def test_a_service_outside_the_catalog_is_named_by_its_served_name(self):
        stops, _ = self.way(crowded(running(OUTSIDE_CONF, ["w1"], served="experiment-model", api_port="8001")))
        self.assertEqual(stops, [{"conf": OUTSIDE_CONF, "name": "experiment-model", "hosts": ["spark-2"],
                                  "port": 8001, "mib": None}])

    def test_what_cannot_be_stopped_here_is_a_note_without_a_choice(self):
        document = crowded(
            running(OTHER, ["w1"], safe_to_stop=False, reasons=["managed candidate lacks usable exact spec labels"]),
            running(None, ["w1"], ownership="unknown", container_name="someone-else", safe_to_stop=False),
            processes=[{"node": "w1", "pid": 4242, "process_name": "/usr/bin/python3", "used_memory_mib": 12288},
                       {"node": "w2", "pid": 7, "process_name": "elsewhere", "used_memory_mib": 1024}])
        stops, notes = self.way(document)
        self.assertEqual(stops, [])
        self.assertEqual(notes, [
            f"On spark-2: org/other [{OTHER[:12]}] · no Stop offered: managed candidate lacks usable exact spec labels",
            "On spark-2: someone-else · running · not started by Pulsar · read-only",
            "On spark-2: GPU process python3 (PID 4242) · 12 GiB · not started by Pulsar · read-only"])
        many = crowded(processes=[{"node": "w1", "pid": n, "process_name": "p", "used_memory_mib": 512}
                                  for n in range(1, 6)])
        _, notes = self.way(many)
        self.assertEqual(len(notes), 4)
        self.assertEqual(notes[0], "On spark-2: GPU process p (PID 1) · 0.5 GiB · not started by Pulsar · read-only")
        self.assertEqual(notes[-1], "and 2 more GPU processes not started by Pulsar")

    def test_a_service_on_the_busy_port_comes_first_then_the_most_memory(self):
        third = "01" * 32
        document = crowded(running(OUTSIDE_CONF, ["w1"], served="small", mib=2048, port=8001),
                           running(third, ["w1"], served="unmeasured", port=8002),
                           running(OTHER, ["w1"], mib=1024, port=8000))
        self.assertEqual([stop["conf"] for stop in self.way(document)[0]], [OTHER, OUTSIDE_CONF, third])
        self.assertEqual([stop["conf"] for stop in self.way(document, ports=())[0]], [OUTSIDE_CONF, OTHER, third])

    def test_multi_node_specs_use_the_first_saved_nodes_as_start_does(self):
        names = NodeNames({"n0": "spark-1", "n1": "spark-2", "n2": "spark-3"}, ["n0", "n1", "n2"])
        self.assertEqual(menu.serving_node_ids(checked(), names, None), ["n0", "n1", "n2"])
        self.assertEqual(menu.serving_node_ids(checked(geometry={"nodes": 2}), names, None), ["n0", "n1"])
        # Without enough saved nodes start refuses anyway: nothing is guessed.
        self.assertEqual(menu.serving_node_ids(checked(geometry={"nodes": 4}), names, None), [])
        self.assertEqual(menu.serving_node_ids(checked(**self.ONE), names, "n2"), ["n2"])
        self.assertEqual(menu.serving_node_ids(checked(**self.ONE), names, None), [])

    def test_blocker_codes_and_ports(self):
        records = [{"blocker": "image_missing"}, {"blocker": "port_in_use", "detail": "port 8000"},
                   {"blocker": "port_in_use", "detail": "port 8000"}, {"blocker": "port_in_use", "detail": "odd"}]
        self.assertTrue(menu.service_blockers(records))
        self.assertTrue(menu.service_blockers([{"blocker": "memory_insufficient"}]))
        self.assertFalse(menu.service_blockers([{"blocker": "service_exists"}, None]))
        self.assertEqual(menu.blocked_ports(records), [8000])

    def test_stop_labels_drop_facts_then_hosts_then_cut_the_name_from_its_start(self):
        stop = {"conf": OTHER, "name": "nvidia/Nemotron-Super-VL", "hosts": ["spark-1", "spark-2"],
                "port": 8000, "mib": 91136}
        self.assertEqual(menu.stop_other_label(stop, 80),
                         "Stop nvidia/Nemotron-Super-VL on spark-1, spark-2 (port 8000 · 89 GiB per node)")
        # An 80-column terminal leaves 78: the hosts become a count before the facts give way.
        self.assertEqual(menu.stop_other_label(stop, 78),
                         "Stop nvidia/Nemotron-Super-VL on 2 nodes (port 8000 · 89 GiB per node)")
        self.assertEqual(menu.stop_other_label(stop, 58), "Stop nvidia/Nemotron-Super-VL on spark-1, spark-2")
        self.assertEqual(menu.stop_other_label(stop, 44), f"Stop nvidia/Nemotron-Super-VL [{OTHER[:12]}]")
        narrow = menu.stop_other_label(stop, 30)
        self.assertEqual(narrow, f"Stop …-Super-VL [{OTHER[:12]}]")
        self.assertEqual(len(narrow), 30)
        # A long catalog name is cut from its start before the facts give way.
        long = {**stop, "name": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"}
        self.assertEqual(menu.stop_other_label(long, 78),
                         "Stop …per-VL-120B-A12B-NVFP4 on spark-1, spark-2 (port 8000 · 89 GiB per node)")
        self.assertEqual(menu.stop_other_label({**long, "hosts": ["node-alpha", "node-beta"]}, 78),
                         "Stop …on-3.5-Super-VL-120B-A12B-NVFP4 on 2 nodes (port 8000 · 89 GiB per node)")
        self.assertEqual(menu.stop_other_label(long, 58), "Stop …ron-3.5-Super-VL-120B-A12B-NVFP4 on spark-1, spark-2")
        for width in (78, 58, 44, 32):
            self.assertLessEqual(len(menu.stop_other_label(long, width)), width)
        # Facts are named only when known; one node takes no "per node".
        unknown = {**stop, "hosts": ["spark-1"], "port": None, "mib": None}
        self.assertEqual(menu.stop_other_label(unknown, 78), "Stop nvidia/Nemotron-Super-VL on spark-1")
        self.assertEqual(menu.stop_other_label({**unknown, "mib": 2048}, 78),
                         "Stop nvidia/Nemotron-Super-VL on spark-1 (2.0 GiB)")

    def test_choice_lines_put_start_again_first_and_stops_before_the_inventory(self):
        records = [{"blocker": "image_missing"}, {"blocker": "port_in_use", "detail": "port 8000"}]
        stop = {"conf": OTHER, "name": "org/other", "hosts": ["spark-2"], "port": 8000, "mib": None}
        lines = menu.start_choice_lines(records, checked(), "configured", stops=[stop], notes=["On spark-2: x"],
                                        width=78)
        self.assertEqual(lines, ["title\tStart was refused: 2 blockers. Choose a next step",
                                 "note\tOn spark-2: x",
                                 "image-stage\tStage serving image",
                                 f"stop-other\t{OTHER}\torg/other\tStop org/other on spark-2 (port 8000)",
                                 "inventory\tOpen Live service inventory"])
        lines = menu.start_choice_lines(records, checked(), "configured", start_again=True, width=78)
        self.assertEqual(lines[:2], ["title\tStart was refused: 2 blockers. Choose a next step",
                                     "start-again\tStart again"])
        lines = menu.start_choice_lines(records, checked(), "configured", start_again=True, dry_run=True, width=78)
        self.assertEqual(lines[1], "start-again\tStart now")
        # Without stops the choices are today's.
        self.assertEqual(menu.start_choices(records, checked(), "configured", []),
                         menu.start_choices(records, checked(), "configured"))
        self.assertEqual(menu.start_choice_lines([], checked(), "configured"), [])

    def test_stop_question_for_a_service_outside_the_catalog(self):
        document = crowded(running(OUTSIDE_CONF, ["w1", "w2"], served="experiment-model"))
        self.assertEqual(menu.other_stop_question(OUTSIDE_CONF, "experiment-model", document),
                         f"Stop and remove the service containers of experiment-model [{OUTSIDE_CONF[:12]}] on "
                         f"spark-2, spark-3? {menu.STOP_KEEPS} {menu.OUTSIDE_CATALOG}")
        self.assertEqual(menu.other_stop_question(OUTSIDE_CONF, "experiment-model", crowded()),
                         f"Stop experiment-model [{OUTSIDE_CONF[:12]}]? No container of it was seen running just "
                         f"now; Stop removes any that remain. {menu.STOP_KEEPS} {menu.OUTSIDE_CATALOG}")
        blind = crowded()
        blind["nodes"]["w2"]["probe_status"] = "unreachable"
        for document in (blind, None):
            with self.subTest(document=document is None):
                self.assertEqual(menu.other_stop_question(OUTSIDE_CONF, "experiment-model", document),
                                 f"Stop and remove the service containers of experiment-model "
                                 f"[{OUTSIDE_CONF[:12]}] on its nodes? {menu.STOP_KEEPS} {menu.OUTSIDE_CATALOG}")

    def test_cli_names_the_service_in_the_way_and_falls_back_without_a_readable_inventory(self):
        import tempfile
        record = {"field": "blocker", "stage": "check", "blocker": "port_in_use", "detail": "port 8000"}
        with tempfile.TemporaryDirectory() as folder:
            blockers, live = Path(folder) / "blockers", Path(folder) / "live.json"
            blockers.write_text(json.dumps(record) + "\n")
            live.write_text(json.dumps(crowded(running(OUTSIDE_CONF, ["w1"], served="experiment-model", port=8000))))

            def run(*extra):
                out = io.StringIO()
                document = json.dumps({"entries": [checked(**self.ONE)]})
                with patch("sys.stdin", io.StringIO(document)), patch("sys.stdout", out), \
                        patch.dict("os.environ", {"COLUMNS": "80"}):
                    code = menu.main(["start-choices", "--spec-id", "ab" * 32, "--archive-location", "configured",
                                      "--blockers-file", str(blockers), "--repo-root", folder, *extra])
                return code, out.getvalue().splitlines()

            code, lines = run("--live", str(live), "--node", "n1")
            self.assertEqual(code, 0)
            self.assertEqual(lines[1:], [f"stop-other\t{OUTSIDE_CONF}\texperiment-model\t"
                                         "Stop experiment-model on spark-2 (port 8000)",
                                         "inventory\tOpen Live service inventory"])
            live.write_text("not json")
            self.assertEqual(run("--live", str(live), "--node", "n1")[1][1:], ["inventory\tOpen Live service inventory"])
            self.assertEqual(run("--start-again")[1][1], "start-again\tStart again")
            out = io.StringIO()
            with patch("sys.stdout", out):
                self.assertEqual(menu.main(["service-blockers", "--blockers-file", str(blockers)]), 0)
                self.assertEqual(menu.main(["other-stop-question", "--conf", OUTSIDE_CONF,
                                            "--served-name", "experiment-model"]), 0)
            self.assertIn(menu.OUTSIDE_CATALOG, out.getvalue())


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
