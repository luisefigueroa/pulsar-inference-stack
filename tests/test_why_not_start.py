"""Why will a model not start?: Start's checks without starting, each blocker
with its fix as an intent. Driven with doubles for ui.sh, ./pulsar and the
menus a fix opens (tests/test_serve_menu.build_root); nothing here reaches
Docker, SSH, a GPU or the Hub."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import start_reasons  # noqa: E402
from tests.test_catalog_menu import HOME, checked  # noqa: E402
from tests.test_serve_menu import Harness  # noqa: E402
from tests.test_serve_plan import SPEC, envelope, start_blocked  # noqa: E402

TITLE = "Why will a model not start?"


def check(name, outcome, message=""):
    return {"id": name, "outcome": outcome, "node": None, "message": message}


FILES_CHECKS = [check("recipe", "pass"), check("topology", "pass"),
                check("model-files", "fail", "model files are not prepared on every serving rank"),
                check("image", "not_run", "not checked: an earlier blocker ended the checks")]


class WhyNotStart(Harness):
    MENUS = ("why-not-start.sh",)
    MENU = "why-not-start.sh"

    def setUp(self):
        super().setUp()
        for name, word in (("serve-menu.sh", "serve menu"), ("services-menu.sh", "services menu"),
                           ("cluster-overview.sh", "cluster check")):
            script = self.root / "scripts" / name
            script.write_text(f'#!/usr/bin/env bash\necho "{word} $*" >> "$PULSAR_LOG"\n')
            script.chmod(0o755)
        self.entries(checked(home=HOME, local_state="ready"))

    def blocked(self, *codes, checks=FILES_CHECKS, fix=None):
        document = start_blocked(*codes, checks=checks)
        for record in document["error"]["details"]:
            record["fix"] = fix
        self.fixture("start-plan", document, rc=1)

    def text(self, result):
        return " ".join(result.stdout.split())

    def test_each_blocker_is_said_with_its_fix_as_an_intent(self):
        self.blocked("model_files_not_ready", fix="./pulsar start abababababab --replace --pull-image")
        result = self.run_menu(["0", "Serve a model…", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0][0], TITLE)
        self.assertEqual(self.menus()[1], [TITLE, "Serve a model…", "Check again", "Back"])
        text = self.text(result)
        self.assertIn("Why will org/model not start? Start's checks, run just now without starting: ✓ recipe ✓ nodes "
                      "✗ model files: model files are not prepared on every serving rank – serving image: not run: an "
                      "earlier blocker ended the checks", text)
        self.assertIn("Blocked: The model files are not on every serving node yet. Fix: Serve a model… plans the "
                      "files, the copies and the serving image it needs", text)
        self.assertIn("Same as: ./pulsar start abababababab --dry-run", text)
        # The record's own fix names flags only an explicit choice may add: it is never shown.
        self.assertNotIn("--replace", result.stdout)
        self.assertNotIn("--pull-image", result.stdout)
        self.assertEqual(self.commands(), [f"start {SPEC} --dry-run --json", "serve menu "])
        self.assert_plain_choices()

    def test_blockers_with_different_fixes_offer_each_once_in_starts_order(self):
        self.blocked("node_unreachable", "port_in_use", "image_missing", "memory_insufficient")
        result = self.run_menu(["0", "Check the cluster now", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], [TITLE, "Check the cluster now", "Serve a model…", "Running services…",
                                           "Check again", "Back"])
        self.assertEqual(self.commands()[-1], "cluster check ")
        self.assert_plain_choices()

    def test_nothing_blocking_is_said_and_serve_is_offered(self):
        self.fixture("start-plan", envelope({"checks": [check("recipe", "pass"), check("memory", "info",
                                                                                         "below the recommended "
                                                                                         "headroom; accepted for the "
                                                                                         "dry run")]}))
        result = self.run_menu(["0", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.text(result)
        self.assertIn("• free memory: below the recommended headroom; accepted for the dry run", text)
        self.assertIn("✓ Nothing blocks it: every check passed. Serve a model… starts it, after its plan.", text)
        self.assertEqual(self.menus()[1], [TITLE, "Serve a model…", "Check again", "Back"])

    def test_checks_that_could_not_run_offer_diagnostics_and_check_again_runs_them_once_more(self):
        self.fixture("start-plan", rc=3)
        result = self.run_menu(["0", "Check again", "Run diagnostics", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✗ Start's checks could not run:", result.stdout)
        self.assertEqual(self.menus()[1], [TITLE, "Run diagnostics", "Check again", "Back"])
        self.assertEqual(self.commands(), [f"start {SPEC} --dry-run --json", f"start {SPEC} --dry-run --json",
                                           "doctor"])

    def test_a_screen_that_does_not_fit_the_rows_opens_in_the_pager(self):
        many = [check(name, "pass") for name in ("recipe", "topology", "fabric", "image", "service")]
        self.blocked("node_unreachable", "port_in_use", "image_missing", "memory_insufficient", "preflight_failed",
                     checks=many + [check("model-files", "fail", "x"), check("memory", "fail", "y"),
                                    check("port", "fail", "z")])
        pages = self.root / "pages.log"
        for rows, paged in (("60", False), ("24", True)):
            with self.subTest(rows=rows):
                for path in (pages, self.choose_log, self.answers.with_suffix(".n")):
                    path.unlink(missing_ok=True)
                result = self.run_menu(["0", "Back", "Back"], ROWS=rows, PAGE_LOG=str(pages))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(pages.exists(), paged)
                self.assertEqual(result.stdout.count("Start's checks, run just now"), 1)

    def test_a_one_node_recipe_asks_which_machine(self):
        self.entries(checked(geometry={"nodes": 1, "tp": 1, "pp": 1}, home={**HOME, "node_id": "n1"},
                             local_state="ready"))
        self.blocked("model_files_not_ready")
        result = self.run_menu(["0", "0", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Which machine should serve org/model?\t1", self.default_log.read_text().splitlines())
        self.assertEqual(self.commands(), [f"start {SPEC} --node spark-1 --dry-run --json"])
        self.assertIn("Same as: ./pulsar start abababababab --node spark-1 --dry-run", self.text(result))


class Screen(unittest.TestCase):
    def test_it_fits_80_60_and_44_columns(self):
        document = start_blocked("model_files_not_ready", "port_in_use", checks=FILES_CHECKS)
        for width in (80, 60, 44):
            with self.subTest(width=width):
                text = start_reasons.screen(document, 1, "Why will org/model not start?", width=width)
                self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)

    def test_a_check_not_reached_is_never_shown_as_passed(self):
        lines = start_reasons.check_lines([check("image", "not_run"), check("memory", "pass"),
                                           check("memory", "fail", "spark-2 has 9 GiB free")])
        self.assertEqual(lines, [("–", "serving image: not run: an earlier blocker ended the checks"),
                                 ("✗", "free memory: spark-2 has 9 GiB free")])


if __name__ == "__main__":
    unittest.main()
