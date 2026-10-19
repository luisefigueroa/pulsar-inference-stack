"""Screens across the menus share one style map (scripts/terminal_format.py):
status words, error labels, outcomes, cards and blocker lines are styled only
for a terminal or PULSAR_COLOR=always, and always strip to their plain text."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import connect_card, menu_outcome, start_blockers

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def environment(**values):
    clean = {key: value for key, value in os.environ.items()
             if key not in ("PULSAR_COLOR", "NO_COLOR", "PULSAR_ACCENT", "PULSAR_VERBOSE")}
    return {**clean, "TERM": "xterm-256color", **values}


def bash(script, **values):
    """SCRIPT with scripts/lib.sh loaded; output is captured, not a terminal."""
    return subprocess.run(["bash", "-c", f'. "{ROOT}/scripts/lib.sh"\n{script}'], env=environment(**values),
                          text=True, capture_output=True, timeout=60)


def printed(function, *args, **values):
    out = io.StringIO()
    with mock.patch.dict(os.environ, environment(**values), clear=True), contextlib.redirect_stdout(out):
        function(*args)
    return out.getvalue()


class BashStatusWords(unittest.TestCase):
    def test_status_words_take_the_shared_map_and_others_stay_plain(self):
        script = ('status_line "FAIL  port      8000 is in use"\nstatus_line "PASS  recipe    parsed"\n'
                  'status_line "READY"\nprint_hanging "WARN  memory    " "close to the floor"\n'
                  'print_hanging "  Next      " "./pulsar doctor"')
        styled = bash(script, PULSAR_COLOR="always").stdout
        plain = bash(script).stdout
        self.assertEqual(ANSI.sub("", styled), plain)
        self.assertNotIn("\x1b", plain)
        self.assertIn("\x1b[1;31mFAIL\x1b[0m  port      8000 is in use\n", styled)
        self.assertIn("\nPASS  recipe    parsed\n", styled)
        self.assertIn("\x1b[1;32mREADY\x1b[0m\n", styled)
        self.assertIn("\x1b[33mWARN\x1b[0m  memory    close to the floor\n", styled)
        self.assertIn("  Next      ./pulsar doctor\n", styled)
        # NO_COLOR keeps bold; never prints nothing extra.
        self.assertIn("\x1b[1mFAIL\x1b[0m", bash(script, PULSAR_COLOR="always", NO_COLOR="1").stdout)
        self.assertEqual(bash(script, PULSAR_COLOR="never").stdout, plain)

    def test_captured_error_and_warning_lines_stay_plain(self):
        # Callers parse captured stderr (lib.sh strips "error: "), even under always.
        result = bash('warn "close to the floor"\nerror_line "the port is in use"', PULSAR_COLOR="always")
        self.assertEqual(result.stderr, "warning: close to the floor\nerror: the port is in use\n")


class Outcomes(unittest.TestCase):
    def test_the_outcome_glyph_takes_the_pass_or_fail_style(self):
        for status, expected in ((0, "\x1b[1;32m✓\x1b[0m Prepare finished for spec x in 3s\n"),
                                 (1, "\x1b[1;31m✗\x1b[0m Prepare was refused or failed for spec x.\n")):
            args = ["--action", "prepare", "--label", "Prepare", "--subject", "spec x", "--status", str(status),
                    "--elapsed", "3s"]
            with self.subTest(status=status):
                styled = printed(menu_outcome.main, args, PULSAR_COLOR="always")
                self.assertTrue(styled.startswith(expected), styled)
                self.assertEqual(ANSI.sub("", styled), printed(menu_outcome.main, args))


class Cards(unittest.TestCase):
    def test_the_connect_card_styles_its_title_and_request(self):
        result = {"state": "running", "api_url": "http://192.0.2.10:8000", "served_name": "org/model",
                  "api_auth": False}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(json.dumps(result))
            styled = printed(connect_card.main, ["--result", str(path)], PULSAR_COLOR="always")
            plain = printed(connect_card.main, ["--result", str(path)])
        self.assertEqual(ANSI.sub("", styled), plain)
        self.assertIn("\n\x1b[1;94mConnect\x1b[0m\n  API:    http://192.0.2.10:8000/v1\n", styled)
        self.assertIn("\x1b[1;94mFirst request:\x1b[0m\n\x1b[36mcurl --fail-with-body", styled)


class BlockerLines(unittest.TestCase):
    def test_a_blocker_line_marks_its_word_and_next_command(self):
        record = start_blockers.blocker("port_in_use", spec="a" * 64, node="rank-1", rank=0, detail="8000")
        plain = start_blockers.human(record)
        with mock.patch.dict(os.environ, environment(PULSAR_COLOR="always"), clear=True):
            styled = start_blockers.styled_human(record)
        self.assertEqual(ANSI.sub("", styled), plain)
        self.assertTrue(styled.startswith("\x1b[1;31mBLOCKED\x1b[0m port_in_use: "), styled)
        if record.get("fix"):
            self.assertIn(f" Next: \x1b[36m{record['fix']}\x1b[0m", styled)
        with mock.patch.dict(os.environ, environment(), clear=True):
            self.assertEqual(start_blockers.styled_human(record), plain)


if __name__ == "__main__":
    unittest.main()
