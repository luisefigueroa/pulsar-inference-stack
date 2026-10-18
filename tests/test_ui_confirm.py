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
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUM = ROOT / "third_party/gum/linux-arm64/gum"


class BundledGum(unittest.TestCase):
    def drive(self, script, keys, *, ready, args=(), columns=80, rows=24):
        """Run SCRIPT on a terminal, send KEYS once READY is drawn; return (exit, output)."""
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
            sent = False
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
                if not sent and ready in output:
                    os.write(master, keys)
                    sent = True
            text = output.decode(errors="replace")
            self.assertTrue(sent, f"Gum did not display {ready!r}: {text!r}")
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

if __name__ == "__main__":
    unittest.main()
