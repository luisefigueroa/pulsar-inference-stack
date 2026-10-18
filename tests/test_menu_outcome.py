"""Menu outcomes read by exit status: a "no" never looks like "couldn't tell"."""
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import menu_outcome  # noqa: E402

LABELS = {
    "check": "Check now", "acquire": "Download", "restore": "Restore", "prepare": "Prepare",
    "start": "Start", "stop": "Stop", "status": "Live status",
    "readiness": "Check launch prerequisites", "image-check": "Check pinned image",
    "image-stage": "Stage pinned image", "budget": "Inspect storage budget",
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
        self.assertIn("Stage pinned image", outcome("image-check", 1)[1])
        self.assertIn("Live status", outcome("stop", 1)[1])
        self.assertEqual(outcome("restore", 1)[0], "✗ Restore was refused or failed for Fixture-Model.")

    def test_unknown_outcomes_name_the_inspection_that_establishes_them(self):
        for action, inspection in (("start", "Live status"), ("stop", "Live status"),
                                   ("image-stage", "Check pinned image"), ("prepare", "Check now"),
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
                                          ("image-stage", 143, "Check pinned image"),
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


if __name__ == "__main__":
    unittest.main()
