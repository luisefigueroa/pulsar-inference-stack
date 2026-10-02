"""Confirmation behavior against the bundled Gum, without operator actions."""
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


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumConfirmation(unittest.TestCase):
    def confirm(self, keys, default=None):
        self.assertTrue(GUM.is_file() and os.access(GUM, os.X_OK), "bundled Gum is unavailable")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "GUM_")) and key not in ("BASH_ENV", "ENV")}
        env.update(REPO_DIR=str(ROOT), GUM_BIN=str(GUM), TERM="xterm-256color",
                   COLUMNS="80", LINES="24", NO_COLOR="1")
        script = '''
. "$REPO_DIR/scripts/ui.sh"
if confirm "Confirm fixture action?" "$@"; then
  printf '\\nACTION_CONFIRMED\\n'
else
  exit "$?"
fi
'''
        master, slave = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c", script, "confirm-test",
                 *([] if default is None else [default])],
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
                if not sent and b"Confirm fixture action?" in output:
                    os.write(master, keys)
                    sent = True
            text = output.decode(errors="replace")
            self.assertTrue(sent, f"Gum did not display the confirmation: {text!r}")
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


if __name__ == "__main__":
    unittest.main()
