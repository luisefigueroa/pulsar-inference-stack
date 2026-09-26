"""Catalog menu decisions come from saved records only and never hide unknown state."""
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library import catalog_menu as menu
from model_library.node_names import NodeNames

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


def checked(**fields):
    return row(**{"checked_at": "2026-09-05T00:00:00Z", "observation_age_seconds": 7200, **fields})


def two_snapshots(target_home, draft_home):
    member = {"model_id": "org/model", "model_commit": "c" * 40, "archive": None}
    return {"target": {**member, "home": target_home}, "draft": {**member, "model_id": "org/draft", "home": draft_home}}


class Operations(unittest.TestCase):
    def test_recorded_home_hides_download_and_restore_only(self):
        offered, hidden, _ = menu.operations(checked(home=HOME), "configured")
        self.assertEqual(hidden, {"acquire": "a home is recorded", "restore": "a home is recorded"})
        self.assertEqual(offered[:5], ["check", "prepare", "start", "stop", "status"])

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

    def test_schema3_names_snapshots_and_offers_eligible_ones(self):
        state = row(snapshots=two_snapshots(HOME, None))
        offered, hidden, eligible = menu.operations(state, "configured")
        self.assertEqual(hidden["prepare"], "no home is recorded for snapshot draft")
        self.assertIn("acquire", offered)
        self.assertEqual(eligible["acquire"], ["draft"])
        self.assertEqual(eligible["move"], ["target"])


class Suggestion(unittest.TestCase):
    def suggest(self, state, after=None, location="configured"):
        offered, _, _ = menu.operations(state, location)
        return menu.suggestion(state, offered, location, after, NAMES)

    def test_rules_in_order(self):
        self.assertEqual(self.suggest(row())[0], "check")
        self.assertEqual(self.suggest(checked(observation_age_seconds=3 * 86400)), ("check", "last check 3 days ago"))
        self.assertEqual(self.suggest(checked(blockers=["n1: disk full"])), ("check", "saved blocker: spark-2: disk full"))
        self.assertEqual(self.suggest(checked()), ("acquire", "no home recorded"))
        self.assertEqual(self.suggest(checked(archive_state="verified"))[0], "restore")
        self.assertEqual(self.suggest(checked(home=HOME, local_state="changed")),
                         ("prepare", "files changed since they were verified (checked 2 hours ago)"))
        self.assertEqual(self.suggest(checked(home=HOME, local_state="ready"))[0], "start")

    def test_restore_needs_a_configured_archive_location(self):
        self.assertEqual(self.suggest(checked(archive_state="verified"), location="disabled")[0], "acquire")

    def test_session_history_outranks_saved_state(self):
        ready = checked(home=HOME, local_state="ready")
        self.assertEqual(self.suggest(ready, after="prepare"), ("check", "prepare ran after the last check"))
        self.assertEqual(self.suggest(ready, after="start")[0], "status")
        self.assertEqual(self.suggest(ready, after="stop")[0], "start")
        self.assertEqual(self.suggest(ready, after="check")[0], "start")

    def test_suggestion_never_stops_or_touches_storage(self):
        for state in (row(), checked(home=HOME, local_state="ready"), checked(home=HOME, local_state="missing")):
            for after in (None, "start", "stop", "purge"):
                result = self.suggest(state, after)
                self.assertNotIn(result and result[0], {"stop", *menu.STORAGE})


class View(unittest.TestCase):
    def test_lines_mark_suggestion_and_explain_hidden_operations(self):
        lines = menu.view_lines(checked(home=HOME, local_state="ready"), "not-configured", width=60, names=NAMES)
        self.assertEqual(lines[0], "recipe\t3\torg/model")
        text = "\n".join(line.split("\t", 1)[1] for line in lines if line.startswith("header\t"))
        self.assertIn("org/model [abababab]", text)
        self.assertIn("checked 2 hours ago", text)
        self.assertIn("Suggested: Start", text)
        self.assertIn("Not shown: Download, Restore (a home is recorded)", text)
        self.assertIn("Check now refreshes them", text)
        self.assertIn("option\tmain\tstart\tStart (suggested)", lines)
        self.assertIn("suggest\tstart", lines)
        self.assertTrue(all(len(line.split("\t", 1)[1]) <= 56 for line in lines if line.startswith("header\t")))

    def test_view_performs_no_probes(self):
        with patch("subprocess.run", side_effect=AssertionError("menu must not probe")):
            menu.view_lines(row(), "configured", width=80, names=NAMES)


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
                         "Purge org/model: delete 1 working copies, release 1 bindings? The home and the archive are kept.")
        self.assertIn("from spark-1 to spark-3",
                       menu.question("move", state, plan={"source_node": "n0", "destination_node": "n2"}, names=NAMES))
        self.assertIn("Files come back only through Restore or Download",
                      menu.question("remove", state, plan={"eligible": True, "home": HOME}, names=NAMES))
        self.assertIn("never replaces a running service", menu.question("start", state, names=NAMES))
        self.assertEqual(menu.question("stop", state, node="n1", names=NAMES),
                         "Stop org/model on spark-2? Model files and pins are kept.")
        self.assertIn("Download org/model @ 01234567 to spark-2",
                      menu.question("acquire", state, plan={"selected_node": "n1"}, names=NAMES))

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


if __name__ == "__main__":
    unittest.main()
