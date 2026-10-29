"""Menu outcomes read by exit status: a "no" never looks like "couldn't tell"."""
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
import unittest.mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import menu_outcome  # noqa: E402

LABELS = {
    "check": "Check now", "acquire": "Download", "restore": "Restore", "prepare": "Prepare",
    "start": "Start", "stop": "Stop", "status": "Live status",
    "readiness": "Check launch prerequisites", "image-check": "Check serving image",
    "image-stage": "Stage serving image", "budget": "Inspect storage budget",
    "move": "Move home", "archive": "Create archive", "verify": "Verify archive",
    "pin": "Pin prepared copies", "unpin": "Unpin prepared copies",
    "purge": "Purge prepared copies", "remove": "Remove home",
}


def outcome(action, status, **options):
    return menu_outcome.outcome(action, LABELS[action], "Fixture-Model", status, "3s", **options)


class OutcomeWording(unittest.TestCase):
    def test_every_action_words_each_status_by_its_meaning(self):
        for action in LABELS:
            for status, glyph in ((0, "✓"), (1, None), (2, "✗"), (3, "?"), (4, "✗"), (255, "✗")):
                with self.subTest(action=action, status=status):
                    headline, detail = outcome(action, status)
                    self.assertIn("Fixture-Model", headline)
                    if status == 0:
                        self.assertEqual(headline, f"✓ {LABELS[action]} finished for Fixture-Model in 3s")
                        self.assertIsNone(detail)
                        continue
                    self.assertEqual(headline[0], glyph or ("▲" if action == "status" else "✗"))
                    self.assertTrue(detail.startswith(f"exit {status} after 3s · "), detail)
                    self.assertNotIn(f"exit {status}", headline)

    def test_live_status_separates_safe_to_start_from_unknown(self):
        absent, absent_detail = outcome("status", 1)
        unknown, unknown_detail = outcome("status", 3)
        self.assertEqual(absent, "▲ Fixture-Model is not running.")
        self.assertIn("won't conflict", absent_detail)
        self.assertEqual(unknown, "? Whether Fixture-Model is running is unknown: a node could not be observed.")
        self.assertIn("don't start it", unknown_detail)

    def test_no_answers_name_the_blocker_and_where_to_go(self):
        self.assertEqual(outcome("readiness", 1)[0], "✗ Fixture-Model is not ready to start.")
        self.assertIn("blockers", outcome("readiness", 1)[1])
        self.assertIn("Stage serving image", outcome("image-check", 1)[1])
        self.assertIn("Live status", outcome("stop", 1)[1])
        self.assertEqual(outcome("restore", 1)[0], "✗ Restore was refused or failed for Fixture-Model.")

    def test_unknown_outcomes_name_the_inspection_that_establishes_them(self):
        for action, inspection in (("start", "Live status"), ("stop", "Live status"),
                                   ("image-stage", "Check serving image"), ("prepare", "Check now"),
                                   ("purge", "Check now")):
            with self.subTest(action=action):
                headline, detail = outcome(action, 3)
                self.assertTrue(headline.startswith("? The outcome of"), headline)
                self.assertIn(f"{inspection} before retrying", detail)
        for action in ("check", "readiness", "image-check", "verify", "budget"):
            with self.subTest(action=action):
                self.assertIn("result is unknown", outcome(action, 3)[0])

    def test_rejected_requests_changed_nothing(self):
        headline, detail = outcome("prepare", 2)
        self.assertIn("rejected before it ran", headline)
        self.assertIn("nothing changed", headline)
        self.assertIn("menu bug", detail)

    def test_previews_change_nothing_whatever_their_status(self):
        for status, glyph, phrase in ((1, "✗", "is not possible"), (2, "✗", "was rejected"),
                                      (3, "?", "could not finish"), (5, "✗", "failed")):
            with self.subTest(status=status):
                headline, detail = outcome("restore", status, preview=True)
                self.assertEqual(headline[0], glyph)
                self.assertIn(phrase, headline)
                self.assertIn("nothing changed", headline)
                self.assertTrue(detail.startswith(f"exit {status} · "), detail)

    def test_an_interruption_confirms_nothing_and_names_the_next_inspection(self):
        for action, status, follow_up in (("restore", 130, "Use Check now"), ("stop", 130, "Use Live status"),
                                          ("image-stage", 143, "Check serving image"),
                                          ("check", 0, "Rerun this inspection")):
            with self.subTest(action=action):
                headline, details = menu_outcome.interruption(action, LABELS[action], "Fixture-Model",
                                                              status, "3s")
                self.assertEqual(headline, f"Interruption requested: {LABELS[action]} for Fixture-Model after 3s.")
                self.assertTrue(details[0].startswith(f"exit {status} · this menu has not confirmed cleanup"),
                                details)
                self.assertIn(follow_up, details[1])

    def test_a_check_that_saved_an_observation_reports_findings(self):
        headline, detail = outcome("check", 1, recorded=True)
        self.assertEqual(headline, "▲ Check now saved an observation with findings for Fixture-Model in 3s.")
        self.assertEqual(detail, "exit 1 · missing or changed files are above")
        self.assertIn("unknown", outcome("check", 3, recorded=True)[1])


class OutcomeCommand(unittest.TestCase):
    def test_lines_wrap_under_the_headline_on_narrow_terminals(self):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.menu_outcome", "--action", "status", "--label", "Live status",
             "--subject", "org/a-rather-long-model-name", "--status", "3", "--elapsed", "2s"],
            cwd=ROOT, env={**os.environ, "COLUMNS": "44", "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith("? Whether org/a-rather-long-model-name"), lines)
        self.assertTrue(all(len(line) <= 44 for line in lines), lines)
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]), lines)

    def test_an_interruption_keeps_its_status_on_a_detail_line(self):
        output = io.StringIO()
        with redirect_stdout(output):
            menu_outcome.main(["--action", "prepare", "--label", "Prepare", "--subject", "Fixture-Model",
                               "--status", "130", "--elapsed", "4s", "--interrupted"])
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "Interruption requested: Prepare for Fixture-Model after 4s.")
        self.assertTrue(lines[1].startswith("  exit 130 · this menu has not confirmed cleanup"), lines)
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]), lines)
        self.assertIn("Use Check now", " ".join(lines))

    def test_success_is_one_line(self):
        output = io.StringIO()
        with redirect_stdout(output):
            menu_outcome.main(["--action", "stop", "--label", "Stop", "--subject", "spec 0123456789ab",
                               "--status", "0", "--elapsed", "0s"])
        self.assertEqual(output.getvalue(), "✓ Stop finished for spec 0123456789ab in 0s\n")


# TUI-75: every outcome names the ./pulsar command equivalent to what the menu ran.
class SameAs(unittest.TestCase):
    SPEC = "ab" * 32

    def test_internal_commands_become_the_operator_command(self):
        spec, names = self.SPEC, {"n1": "spark-2"}
        cases = [
            (["/repo/scripts/model-library.sh", "restore", spec, "--node", "n1", "--yes"],
             "./pulsar model restore abababababab --node spark-2 --yes"),
            (["/repo/scripts/model-library.sh", "check", spec, "--node", "n1"],
             "./pulsar models check abababababab --node spark-2"),
            (["/repo/scripts/model-library.sh", "archive", "create", spec, "--snapshot", "draft", "--yes"],
             "./pulsar model archive create abababababab --snapshot draft --yes"),
            # The menu's own plumbing is left out; the permission it granted is not.
            (["env", "PULSAR_START_BLOCKERS_FILE=/tmp/blockers", "/repo/pulsar", "start", spec, "--node", "n1",
              "--accept-memory-warn"], "./pulsar start abababababab --node spark-2 --accept-memory-warn"),
            (["/repo/scripts/with-connect-card.sh", "/repo/scripts/status.sh", spec], "./pulsar status abababababab"),
            (["/repo/scripts/model-library.sh", "restore", spec, "--plan", "--json"],
             "./pulsar model restore abababababab --plan"),
            # A node outside the saved topology keeps its node_id.
            (["/repo/scripts/down.sh", spec, "--node", "n9"], "./pulsar stop abababababab --node n9"),
            (["/repo/pulsar", "model", "budget"], "./pulsar model budget"),
            # Where the command takes no prefix, the complete ID stays.
            (["/repo/scripts/model-library.sh", "purge", spec, "--yes"], f"./pulsar model purge {spec} --yes"),
            (["/repo/pulsar", "image", "stage", spec, "--pull-image", "--yes"],
             f"./pulsar image stage {spec} --pull-image --yes"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(menu_outcome.operator_command(argv, names), expected)
        self.assertIsNone(menu_outcome.operator_command(["/repo/scripts/unknown.sh", spec]))

    def test_the_card_ends_with_the_command(self):
        output = io.StringIO()
        with redirect_stdout(output):
            menu_outcome.main(["--action", "restore", "--label", "Restore", "--subject", "Fixture-Model",
                               "--status", "1", "--elapsed", "3s", "--command", "/repo/scripts/model-library.sh",
                               "restore", self.SPEC, "--yes"])
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("✗ Restore was refused or failed"), lines)
        self.assertEqual(lines[-1], "  Same as: ./pulsar model restore abababababab --yes")

    def test_a_command_too_long_for_the_line_is_never_broken(self):
        # A purge keeps the complete spec ID, longer than a 44-column line.
        output = io.StringIO()
        with redirect_stdout(output), unittest.mock.patch.dict(os.environ, {"COLUMNS": "44"}):
            menu_outcome.main(["--action", "purge", "--label", "Purge prepared copies", "--subject", "M",
                               "--status", "0", "--elapsed", "1s", "--command", "/repo/scripts/model-library.sh",
                               "purge", self.SPEC, "--yes"])
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[-2:], ["  Same as:", f"    ./pulsar model purge {self.SPEC} --yes"])


if __name__ == "__main__":
    unittest.main()
