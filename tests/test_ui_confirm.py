"""Prompt behavior against the bundled Gum, without operator actions."""
import errno
import fcntl
import os
from pathlib import Path
import platform
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUM = ROOT / "third_party/gum/linux-arm64/gum"


class BundledGum(unittest.TestCase):
    def drive(self, script, keys, *, ready, args=(), columns=80, rows=24, then=()):
        """Run SCRIPT on a terminal, send KEYS once READY is drawn; return (exit, output).

        THEN is further (ready, keys) stages, each sent once its READY is drawn
        after the previous send: Gum reads a whole burst of input at once.
        """
        stages = [(ready, keys), *then]
        self.assertTrue(GUM.is_file() and os.access(GUM, os.X_OK), "bundled Gum is unavailable")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "GUM_")) and key not in ("BASH_ENV", "ENV")}
        env.update(REPO_DIR=str(ROOT), GUM_BIN=str(GUM), TERM="xterm-256color",
                   COLUMNS=str(columns), LINES=str(rows), NO_COLOR="1")
        master, slave = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            process = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c", script, "gum-test", *args],
                cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            output = b""
            pending = b""
            sent = 0
            since = 0
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not select.select([master], [], [], .1)[0]:
                    if process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:  # The child closed its terminal.
                        break
                    raise
                if not chunk:
                    break
                output += chunk
                pending += chunk
                # A PTY has no terminal emulator to answer Gum's background
                # color and cursor queries. Supply only those protocol replies.
                for query, reply in ((b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:0000/0000/0000\x1b\\"),
                                     (b"\x1b[6n", b"\x1b[1;1R")):
                    if query in pending:
                        os.write(master, reply)
                        pending = pending.replace(query, b"")
                pending = pending[-64:]
                if sent < len(stages) and stages[sent][0] in output[since:]:
                    os.write(master, stages[sent][1])
                    sent += 1
                    since = len(output)
            text = output.decode(errors="replace")
            missing = stages[sent][0] if sent < len(stages) else None
            self.assertIsNone(missing, f"Gum did not display {missing!r}: {text!r}")
            try:
                code = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.fail(f"Gum did not finish after the test input: {text!r}")
            return code, text
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if slave is not None:
                os.close(slave)
            os.close(master)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumConfirmation(BundledGum):
    def confirm(self, keys, default=None, *, primitive="confirm"):
        script = '''
. "$REPO_DIR/scripts/ui.sh"
if confirm "Confirm fixture action?" "$@"; then
  printf '\\nACTION_CONFIRMED\\n'
else
  exit "$?"
fi
'''
        if primitive == "input":
            script = script.replace('confirm "Confirm fixture action?" "$@"', 'prompt_input "Confirm fixture action?"')
        elif primitive == "choose":
            script = script.replace('confirm "Confirm fixture action?" "$@"', 'choose_index "Confirm fixture action?" one two')
        return self.drive(script, keys, ready=b"Confirm fixture action?",
                          args=[] if default is None else [default])

    def assert_confirmation(self, keys, expected, default=None):
        code, output = self.confirm(keys, default)
        self.assertEqual(code, expected, output)
        self.assertEqual("ACTION_CONFIRMED" in output, expected == 0, output)

    def test_enter_declines_by_default(self):
        self.assert_confirmation(b"\r", 1)

    def test_enter_declines_explicit_no_default(self):
        self.assert_confirmation(b"\r", 1, "no")

    def test_enter_accepts_explicit_yes_default(self):
        self.assert_confirmation(b"\r", 0, "yes")

    def test_selecting_yes_overrides_no_default(self):
        self.assert_confirmation(b"\x1b[D\r", 0, "no")

    def test_selecting_no_overrides_yes_default(self):
        self.assert_confirmation(b"\x1b[C\r", 1, "yes")

    def test_answer_shortcuts_override_default(self):
        for keys, expected, default in ((b"y", 0, "no"), (b"n", 1, "yes")):
            with self.subTest(keys=keys):
                self.assert_confirmation(keys, expected, default)

    def test_escape_does_not_accept_yes_default(self):
        self.assert_confirmation(b"\x1b", 1, "yes")

    def test_ctrl_c_preserves_interrupt_status(self):
        self.assert_confirmation(b"\x03", 130)

    def test_input_and_selection_preserve_escape_and_ctrl_c(self):
        for primitive in ("input", "choose"):
            for keys, expected in ((b"\x1b", 1), (b"\x03", 130)):
                with self.subTest(primitive=primitive, keys=keys):
                    code, output = self.confirm(keys, primitive=primitive)
                    self.assertEqual(code, expected, output)
                    self.assertNotIn("ACTION_CONFIRMED", output)

    def test_input_keeps_the_submitted_value(self):
        code, output = self.confirm(b"fixture-value\r", primitive="input")
        self.assertEqual(code, 0, output)
        self.assertIn("fixture-value", output)
        self.assertIn("ACTION_CONFIRMED", output)



@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumMenu(BundledGum):
    """Every option on one page, whole labels, and selection by position."""
    SCRIPT = '''
. "$REPO_DIR/scripts/ui.sh"
if index=$(choose_index "Pick fixture option" "$@"); then printf '\\nPICKED=%s\\n' "$index"; else exit "$?"; fi
'''

    def choose(self, options, keys, *, ready="Pick fixture option", default=None, **terminal):
        script = self.SCRIPT if default is None else f"export PULSAR_CHOOSE_DEFAULT={default}" + self.SCRIPT
        return self.drive(script, keys, ready=ready.encode(), args=options, **terminal)

    def test_thirteen_options_fit_one_page_at_80_by_24(self):
        options = [f"Fixture option {number:02d}" for number in range(1, 14)]
        code, output = self.choose(options, b"\x1b", ready=options[-1])
        self.assertEqual(code, 1, output)
        for option in options:
            self.assertIn(option, output)
        self.assertNotIn("\u2022\u2022", output)  # Gum's page dots
        self.assertNotIn("1\tFixture", output)

    def test_labels_use_the_whole_row_after_the_cursor(self):
        for columns in (60, 80, 120):
            with self.subTest(columns=columns):
                label = f"W{columns}-".ljust(columns - 3, "x") + "Z"
                code, output = self.choose([label, "Back"], b"\x1b", ready=label, columns=columns)
                self.assertEqual(code, 1, output)

    def test_default_label_with_a_comma_is_selected(self):
        code, output = self.choose(["C", "A, B", "A"], b"\r", default=1)
        self.assertEqual(code, 0, output)
        self.assertIn("PICKED=1", output)

    def test_repeated_labels_stay_distinct_and_selectable(self):
        code, output = self.choose(["Same", "Same", "Other"], b"\x1b[B\r", ready="Other")
        self.assertEqual(code, 0, output)
        self.assertIn("Same (2)", output)
        self.assertIn("PICKED=1", output)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumOutput(BundledGum):
    """Long read-only output opens in the pager; an outcome waits for Continue."""
    PAGE = '''
. "$REPO_DIR/scripts/ui.sh"
rc=0
page_output "" bash -c 'for i in $(seq 1 "$1"); do echo "fixture line $i"; done; exit "$2"' _ "$@" || rc=$?
printf '\\nPAGED=%s\\n' "$rc"
exit "$rc"
'''
    CONTINUE = '''
. "$REPO_DIR/scripts/ui.sh"
rc=0
wait_continue "Fixture outcome above" || rc=$?
printf '\\nCONTINUED=%s\\n' "$rc"
exit "$rc"
'''

    def page(self, lines, keys, status=0):
        return self.drive(self.PAGE, keys, ready=b"fixture line 1", args=[str(lines), str(status)])

    def test_long_output_opens_the_pager_until_q_or_esc(self):
        for keys in (b"q", b"\x1b"):
            with self.subTest(keys=keys):
                code, output = self.page(40, keys, status=3)
                self.assertEqual(code, 3, output)
                self.assertIn("PAGED=3", output)
                self.assertIn("esc quit", output)
                self.assertIn("\x1b[?1049h", output)  # the pager's own screen

    def test_ctrl_c_in_the_pager_returns_130(self):
        code, output = self.page(40, b"\x03")
        self.assertEqual(code, 130, output)
        self.assertIn("PAGED=130", output)

    def test_a_page_keeps_terminal_styles_unless_they_are_off(self):
        # The capture is shown on this terminal: PULSAR_COLOR=always, unless
        # never; NO_COLOR (set by drive) still reaches the command.
        script = '''
{setting}
. "$REPO_DIR/scripts/ui.sh"
page_output "" bash -c 'for i in $(seq 1 12); do echo "color=${{PULSAR_COLOR-unset}} no_color=${{NO_COLOR-unset}} line $i"; done'
'''
        for setting, expected in (("", "color=always no_color=1"), ("export PULSAR_COLOR=never", "color=never no_color=1")):
            with self.subTest(setting=setting):
                code, output = self.drive(script.format(setting=setting), b"q", ready=b"line 1")
                self.assertEqual(code, 0, output)
                self.assertIn(expected, output)

    def test_a_menu_keeps_styles_for_output_it_captures(self):
        # On a terminal, menus style captured output unless styles are off; NO_COLOR still applies.
        script = '{setting}\n. "$REPO_DIR/scripts/ui.sh"\nui_keep_styles\necho "KEPT=${{PULSAR_COLOR-unset}} ${{NO_COLOR-unset}}"\n'
        for setting, expected in (("", "KEPT=always 1"), ("export PULSAR_COLOR=never", "KEPT=never 1"),
                                  ("export PULSAR_COLOR=off", "KEPT=off 1")):
            with self.subTest(setting=setting):
                code, output = self.drive(script.format(setting=setting), b"", ready=b"KEPT=")
                self.assertEqual(code, 0, output)
                self.assertIn(expected, output)
        # Off a terminal nothing changes.
        clean = {key: value for key, value in os.environ.items() if key != "PULSAR_COLOR"}
        piped = subprocess.run(["bash", "-c", script.format(setting="")], env={**clean, "REPO_DIR": str(ROOT)},
                               stdout=subprocess.PIPE, text=True, timeout=30)
        self.assertNotIn("KEPT=always", piped.stdout)

    def test_a_help_page_is_styled_in_the_pager(self):
        script = 'unset NO_COLOR\n. "$REPO_DIR/scripts/ui.sh"\npage_output "" "$REPO_DIR/pulsar" help terms\n'
        code, output = self.drive(script, b"q", ready=b"What the words mean")
        self.assertEqual(code, 0, output)
        self.assertIn("\x1b[?1049h", output)
        self.assertIn("\x1b[1;94mWhat the words mean\x1b[0m", output)
        self.assertIn("\x1b[1;94mspec\x1b[0m", output)

    def test_short_output_prints_without_the_pager(self):
        code, output = self.page(3, b"")
        self.assertEqual(code, 0, output)
        self.assertIn("fixture line 3", output)
        self.assertNotIn("esc quit", output)
        self.assertNotIn("\x1b[?1049h", output)

    def test_continue_returns_on_enter_or_esc_and_keeps_ctrl_c(self):
        for keys, expected in ((b"\r", 0), (b"\x1b", 0), (b"\x03", 130)):
            with self.subTest(keys=keys):
                code, output = self.drive(self.CONTINUE, keys, ready=b"Fixture outcome above")
                self.assertEqual(code, expected, output)
                self.assertIn(f"CONTINUED={expected}", output)
                self.assertIn("Continue", output)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumReview(BundledGum):
    """A storage question: No first, Yes only when chosen, and the full preview on request."""
    SCRIPT = '''
. "$REPO_DIR/scripts/ui.sh"
rc=0
confirm_or_review "Purge fixture/model: delete 1 working copy?" "Purge prepared copies" \\
  bash -c 'echo PREVIEW_RAN >>"$1"; for i in $(seq 1 40); do echo "full preview line $i"; done' _ "$PREVIEW_LOG" || rc=$?
printf '\\nREVIEWED=%s\\n' "$rc"
exit "$rc"
'''

    def review(self, keys, then=()):
        log = Path(self.enterContext(tempfile.TemporaryDirectory())) / "preview.log"
        code, output = self.drive(f"export PREVIEW_LOG={log}" + self.SCRIPT, keys, ready=b"No, change nothing",
                                  then=then)
        return code, output, log.read_text().count("PREVIEW_RAN") if log.exists() else 0

    def test_the_question_prints_above_choices_with_no_selected_first(self):
        code, output, previews = self.review(b"\r")
        self.assertEqual((code, previews), (1, 0), output)
        self.assertIn("REVIEWED=1", output)
        self.assertLess(output.index("Purge fixture/model: delete 1 working copy?"), output.index("Yes, purge prepared copies"))
        for option in ("Yes, purge prepared copies", "Show full preview", "No, change nothing"):
            self.assertIn(option, output)

    def test_escape_declines_and_ctrl_c_keeps_130(self):
        for keys, expected in ((b"\x1b", 1), (b"\x03", 130)):
            with self.subTest(keys=keys):
                code, output, previews = self.review(keys)
                self.assertEqual((code, previews), (expected, 0), output)
                self.assertIn(f"REVIEWED={expected}", output)

    def test_only_choosing_yes_confirms(self):
        code, output, previews = self.review(b"\x1b[A\x1b[A\r")
        self.assertEqual((code, previews), (0, 0), output)
        self.assertIn("REVIEWED=0", output)

    def test_show_full_preview_opens_the_pager_and_asks_again(self):
        code, output, previews = self.review(b"\x1b[A\r", then=[(b"full preview line 1", b"q"),
                                                                    (b"No, change nothing", b"\x1b")])
        self.assertEqual((code, previews), (1, 1), output)
        self.assertIn("\x1b[?1049h", output)  # the pager's own screen
        self.assertIn("REVIEWED=1", output)
        # After the pager, Yes still confirms.
        code, output, previews = self.review(b"\x1b[A\r", then=[(b"full preview line 1", b"q"),
                                                                    (b"No, change nothing", b"\x1b[A\x1b[A\r")])
        self.assertEqual((code, previews), (0, 1), output)

    def test_ctrl_c_in_the_full_preview_returns_130(self):
        code, output, previews = self.review(b"\x1b[A\r", then=[(b"full preview line 1", b"\x03")])
        self.assertEqual((code, previews), (130, 1), output)
        self.assertIn("REVIEWED=130", output)


if __name__ == "__main__":
    unittest.main()
