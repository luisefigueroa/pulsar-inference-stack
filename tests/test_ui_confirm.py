"""Prompt behavior against the bundled Gum, without operator actions."""
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import pty
import re
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

    def test_the_key_hint_names_esc(self):
        code, output = self.choose(["one", "two"], b"\x1b", ready="esc back")
        self.assertEqual(code, 1, output)
        self.assertIn("↑/↓ move · enter choose · esc back", output)
        self.assertNotIn("enter submit", output)

    def test_default_label_with_a_comma_is_selected(self):
        code, output = self.choose(["C", "A, B", "A"], b"\r", default=1)
        self.assertEqual(code, 0, output)
        self.assertIn("PICKED=1", output)

    def test_repeated_labels_stay_distinct_and_selectable(self):
        code, output = self.choose(["Same", "Same", "Other"], b"\x1b[B\r", ready="Other")
        self.assertEqual(code, 0, output)
        self.assertIn("Same (2)", output)
        self.assertIn("PICKED=1", output)


ESCAPE = re.compile(r"\x1b\[([?0-9;]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[^\[\]]")


def views(output, rows, columns):
    """What a ROWS by COLUMNS terminal shows as Gum draws OUTPUT: the screen
    when each view after the first starts, then the final screen. Enough of a
    terminal for Gum's inline renderer, which redraws in place after moving
    the cursor up to its view's first line; a line feed on the last row
    scrolls. Styles are dropped. The last view before Gum exits is [-2]."""
    screen = [[" "] * columns for _ in range(rows)]
    row = column = 0
    shots = []

    def feed():
        nonlocal row
        if row == rows - 1:
            screen.pop(0)
            screen.append([" "] * columns)
        else:
            row += 1

    position = 0
    while position < len(output):
        match = ESCAPE.match(output, position)
        if match:
            position = match.end()
            params, final = match.group(1) or "", match.group(2)
            count = int(params) if params.isdigit() else 1
            if final == "A":
                shots.append(["".join(line).rstrip() for line in screen])
                row = max(0, row - count)
            elif final == "B":
                row = min(rows - 1, row + count)
            elif final == "C":
                column = min(columns - 1, column + count)
            elif final == "D":
                column = max(0, column - count)
            elif final == "K":
                start, end = {"": (column, columns), "0": (column, columns), "1": (0, column + 1),
                              "2": (0, columns)}.get(params, (0, 0))
                screen[row][start:end] = [" "] * (end - start)
            elif final == "J" and params in ("", "0"):
                screen[row][column:] = [" "] * (columns - column)
                for line in screen[row + 1:]:
                    line[:] = [" "] * columns
            continue
        char = output[position]
        position += 1
        if char == "\r":
            column = 0
        elif char == "\n":
            feed()
        elif char >= " ":
            if column >= columns:
                column = 0
                feed()
            screen[row][column] = char
            column += 1
    shots.append(["".join(line).rstrip() for line in screen])
    return shots


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumCards(BundledGum):
    """Options of several lines, such as the catalog list's specs under their
    model's name: one cursor stop each, at the columns they were given."""
    SCRIPT = BundledGumMenu.SCRIPT
    HINT = "↑/↓ move · enter choose · esc back"
    CARDS = ["  \x1b[1mfixture/model\x1b[0m\n    aaaaaaaaaaaa · 3 nodes", "    bbbbbbbbbbbb · 1 node\n", "Back"]

    def choose(self, options, keys, *, ready, then=(), **terminal):
        return self.drive(self.SCRIPT, keys, ready=ready.encode(), args=options, then=then, **terminal)

    def test_down_moves_one_card_and_enter_returns_it(self):
        code, output = self.choose(self.CARDS, b"\x1b[B\r", ready="bbbbbbbbbbbb")
        self.assertEqual(code, 0, output)
        self.assertIn("PICKED=1", output)

    def test_card_lines_keep_their_columns_and_bold(self):
        code, output = self.choose(self.CARDS, b"\x1b", ready="bbbbbbbbbbbb")
        self.assertEqual(code, 1, output)
        screen = views(output, 24, 80)[-2]
        self.assertEqual(screen[:7], ["", "Pick fixture option", self.HINT, "> fixture/model",
                                      "    aaaaaaaaaaaa · 3 nodes",
                                      # The next card's first line, in the column of the line above.
                                      "    bbbbbbbbbbbb · 1 node", ""])
        self.assertEqual(screen[7], "  Back")
        # Gum keeps the model name's bold, also under NO_COLOR (drive sets it).
        self.assertRegex(output, r"\x1b\[1m[^\x1b]*fixture/model")

    def test_three_line_cards_page_with_the_title_on_screen(self):
        # With its wrapped part, the first card makes a page of six 19 lines,
        # one more than fits under the title on 24 rows when the list pages.
        options = ["  fixture/model-01\n    spec-01 · 3 nodes\n      partly running on 2 nodes (modified recipe)\n"]
        options += [f"  fixture/model-{number:02d}\n    spec-{number:02d} · 3 nodes\n" for number in range(2, 13)]
        code, output = self.choose(options + ["Back"], b"\x1b[B" * 5, ready="spec-01",
                                   then=[(b"> fixture/model-06", b"\x1b")])
        self.assertEqual(code, 1, output)
        shots = views(output, 24, 80)
        first, second = shots[0], shots[-2]
        for screen in (first, second):
            with self.subTest(screen="\n".join(screen)):
                self.assertEqual(screen[:3], ["", "Pick fixture option", self.HINT])
                self.assertTrue(any(line.strip().startswith("••") for line in screen))
        self.assertIn("> fixture/model-01", first)
        self.assertIn("  fixture/model-05", first)
        self.assertNotIn("  fixture/model-06", first)
        self.assertEqual(second[3], "> fixture/model-06")

    def test_the_next_page_starts_with_a_model_name(self):
        # Two pages of five would fit 24 rows, but page 2 would start with the
        # second spec of fixture/model-05, away from its model's name; three
        # pages of four keep the model together.
        options = ["  fixture/model-01\n    spec-01 · 3 nodes\n      partly running on 2 nodes (modified recipe)\n      candidate\n"]
        options += [f"  fixture/model-{number:02d}\n    spec-{number:02d} · 3 nodes\n" for number in range(2, 5)]
        options += ["  fixture/model-05\n    spec-05a · 3 nodes",
                    "    spec-05b · 1 node\n      partly running on 2 nodes (modified recipe)\n"]
        options += [f"  fixture/model-{number:02d}\n    spec-{number:02d} · 3 nodes\n" for number in range(6, 9)]
        code, output = self.choose(options + ["Back"], b"\x1b[B" * 4, ready="spec-01",
                                   then=[(b"> fixture/model-05", b"\x1b")])
        self.assertEqual(code, 1, output)
        shots = views(output, 24, 80)
        self.assertNotIn("  fixture/model-05", shots[0])
        self.assertEqual(shots[-2][:8], ["", "Pick fixture option", self.HINT, "> fixture/model-05",
                                         "    spec-05a · 3 nodes", "    spec-05b · 1 node",
                                         "      partly running on 2 nodes (modified recipe)", ""])


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

    def test_the_labels_page_is_styled_in_the_pager_at_44_columns(self):
        script = 'unset NO_COLOR\n. "$REPO_DIR/scripts/ui.sh"\npage_output "" "$REPO_DIR/pulsar" help labels\n'
        code, output = self.drive(script, b"q", ready=b"What the labels mean", columns=44)
        self.assertEqual(code, 0, output)
        self.assertIn("\x1b[?1049h", output)
        self.assertIn("\x1b[1;94mWhat the labels mean\x1b[0m", output)
        self.assertIn("\x1b[1;94mFiles\x1b[0m", output)

    def test_model_file_inventory_page_renders_the_executable_command_at_44_columns(self):
        fixture = Path(self.enterContext(tempfile.TemporaryDirectory())) / "files.json"
        fixture.write_text(json.dumps({
            "schema_version": 1, "kind": "pulsar-model-file-inventory",
            "observed_at": "2026-09-05T00:00:00Z", "snapshots": [], "issues": [],
            "nodes": [{"node_id": "fixture-node", "hostname": "fixture-node", "observed_at": None,
                       "status": "unknown", "error": "fixture node unavailable", "scopes": [],
                       "disks": [], "issues": [], "unidentified": [], "container_error": ""}],
        }))
        # The page as the menu shows it, with the storage budget under the nodes.
        budget = fixture.with_name("budget.json")
        budget.write_text(json.dumps({"schema_version": 1, "ok": True, "result": {
            "kind": "pulsar-storage-budget", "nodes": [{
                "node_id": "fixture-node", "available": 300 * 1024**3, "used": 50 * 1024**3,
                "total": 1000 * 1024**3, "reserve": 100 * 1024**3, "limit": 200 * 1024**3,
                "path": "/fixture/views"}]}}))
        script = ('. "$REPO_DIR/scripts/ui.sh"\n'
                  'page_output "" python3 -m model_library.file_inventory render --file "$1" --menu --budget "$2"\n')
        code, output = self.drive(script, b"q", ready=b"Model files and disk space",
                                  args=[str(fixture), str(budget)], columns=44)
        self.assertEqual(code, 0, output)
        self.assertIn("\x1b[?1049h", output)
        self.assertIn("esc quit", output)
        self.assertIn("Storage budget", output)

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
confirm_or_review "Purge fixture/model: delete 1 prepared copy?" "Purge prepared copies" \\
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
        self.assertLess(output.index("Purge fixture/model: delete 1 prepared copy?"), output.index("Yes, purge prepared copies"))
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



@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumIntentHome(BundledGum):
    """The intent home and the Cluster menu with real Gum over fixture records; Esc only, nothing runs."""

    def fixture(self):
        from datetime import datetime, timezone
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        nodes = [{"rank": index, "hostname": f"fixture-node-{index + 1}", "node_id": f"node-{index}"}
                 for index in range(3)]
        topology_id = "a" * 64
        (root / "topology.json").write_text(json.dumps({
            "schema_version": 2, "topology_id": topology_id, "nodes": nodes,
            "validation": {"ssh_identity_enrolled": True}}))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (root / "changes.json").write_text(json.dumps({
            "schema_version": 1, "kind": "pulsar-cluster-changes", "topology_id": topology_id,
            "observed_at": now, "result": "changed", "issues": [], "found": [], "changed": [], "untrusted": [],
            "missing": [{"hostname": "fixture-node-3", "node_id": "node-2", "position": 2, "answered": False}]}))
        # Every record the menus read is the fixture's; nothing is read from or written to the checkout.
        return (f'export PULSAR_FORCE_MENU=1 CLUSTER_TOPOLOGY_FILE={root}/topology.json '
                f'PULSAR_CLUSTER_CHANGES_FILE={root}/changes.json '
                f'PULSAR_SERVICE_INVENTORY_FILE={root}/inventory.json PULSAR_STORAGE_BUDGET_FILE={root}/budget.json '
                f'PULSAR_MODEL_LIBRARY_DIR={root}/model-library PULSAR_COLD_ROOT=\n')

    def test_the_home_draws_its_status_above_the_six_choices_at_80_by_24(self):
        for columns in (80, 60):
            with self.subTest(columns=columns):
                code, output = self.drive(self.fixture() + 'exec "$REPO_DIR/scripts/home.sh"\n', b"\x1b",
                                          ready=b"Help and troubleshooting", columns=columns)
                self.assertEqual(code, 0, output)
                plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output)
                for text in ("Cluster    3 nodes · 2 of 3 answered", "Serving", "Disk", "Attention",
                             "fixture-node-3 did not answer", "Pulsar Inference Stack", "esc exit",
                             "Serve a model…", "Running services…", "Models and disk space…", "Cluster…", "Exit"):
                    self.assertIn(text, plain)

    def test_the_cluster_menu_draws_its_frame_and_offers_review(self):
        code, output = self.drive(self.fixture() + 'exec "$REPO_DIR/scripts/cluster-menu.sh"\n', b"\x1b",
                                  ready=b"Start over: forget this cluster")
        self.assertEqual(code, 0, output)
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output)
        for text in ("Nodes      3 · fixture-node-1, fixture-node-2, fixture-node-3",
                     "Changes    fixture-node-3 did not answer", "Check the cluster now", "Review what changed…",
                     "Settings… (archive location, API key)", "Back"):
            self.assertIn(text, plain)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "aarch64",
                     "the bundled Gum requires Linux arm64")
class BundledGumServe(BundledGum):
    """Serve a model… with real Gum in a scratch checkout of doubles (tests/test_serve_menu.build_root):
    every command the menu runs is the ./pulsar double over fixture records. Enter opens a fixture
    recipe's plan; Esc leaves every screen, so nothing is approved."""

    def fixture(self):
        import shlex
        from tests.test_serve_menu import (OTHER_RUNNING, build_root, changes, root_environment,  # noqa: PLC0415
                                           NOW, utc)
        from tests.test_serve_plan import OTHER, envelope, image_plan, prepare_plan, start_blocked  # noqa: PLC0415
        from tests.test_catalog_menu import HOME, checked  # noqa: PLC0415
        from scripts import saved_observations as saved  # noqa: PLC0415
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_root(root, gum=True)
        fixtures = root / "fixtures"
        (root / "catalog.json").write_text(json.dumps({"entries": [
            checked(home=HOME, local_state="ready"),
            checked(spec_id=OTHER, model_id="org/other", home=HOME, local_state="ready")]}))
        saved.save_changes(changes()[0], root / "cluster-changes.json", clock=lambda: utc(NOW))
        for key, document in (("inventory", OTHER_RUNNING),
                              ("prepare-plan", envelope(prepare_plan(("n0", "reuse"), ("n1", "reuse"),
                                                                     ("n2", "reuse")))),
                              ("image-plan", envelope(image_plan((0, "ok"), (1, "ok"), (2, "ok")))),
                              ("start-plan", start_blocked("port_in_use"))):
            (fixtures / f"{key}.json").write_text(json.dumps(document))
        (fixtures / "start-plan.rc").write_text("1")
        exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in root_environment(root).items())
        return root, exports + f'exec bash {shlex.quote(str(root / "scripts/serve-menu.sh"))}\n'

    def test_the_serve_list_draws_its_cards_at_80_by_24(self):
        root, script = self.fixture()
        code, output = self.drive(script, b"\x1b", ready=b"running on spark-1")
        self.assertEqual(code, 0, output)
        screen = views(output, 24, 80)[-2]
        self.assertIn("Serve a model", screen)
        self.assertIn("↑/↓ move · enter choose · esc back", screen)
        self.assertIn("> org/model", screen)
        self.assertIn("    3 nodes · ready to start", screen)
        self.assertIn("  org/other", screen)
        self.assertIn("    3 nodes · running on spark-1, spark-2, spark-3", screen)
        self.assertTrue(all(len(line) <= 80 for line in screen))
        # Only the inventory ran; nothing was approved.
        self.assertEqual((root / "pulsar.log").read_text().splitlines(), ["inventory --json"])

    def test_a_plan_prints_above_its_choices_with_the_cursor_on_cancel_at_80_by_24(self):
        root, script = self.fixture()
        code, output = self.drive(script, b"\r", ready=b"running on spark-1",
                                  then=[(b"Details", b"\x1b"), (b"running on spark-1", b"\x1b")])
        self.assertEqual(code, 0, output)
        plan = next(shot for shot in views(output, 24, 80) if "> Cancel" in shot)
        text = "\n".join(plan)
        for line in ("Serve org/model on spark-1, spark-2 and spark-3", "Pulsar will:",
                     "  1. Stop org/other on spark-1, spark-2 and spark-3. Its model files are kept.",
                     "> Cancel", "  Go ahead", "  Details"):
            self.assertIn(line, plan, text)
        # The plan is printed in place, above the choices, not in the pager.
        self.assertNotIn("\x1b[?1049h", output)
        self.assertLess(plan.index("Pulsar will:"), plan.index("> Cancel"))
        self.assertTrue(all(len(line) <= 80 for line in plan))
        commands = (root / "pulsar.log").read_text().splitlines()
        self.assertFalse([line for line in commands if "--json" not in line], commands)

    def test_running_services_draws_its_card_above_its_choices_at_80_by_24(self):
        import shlex
        from tests.test_serve_menu import build_root, root_environment  # noqa: PLC0415
        from tests.test_services_menu import inventory, service  # noqa: PLC0415
        from tests.test_serve_plan import SPEC  # noqa: PLC0415
        from tests.test_catalog_menu import HOME, checked  # noqa: PLC0415
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_root(root, ("services-menu.sh", "serve-menu.sh", "connect_card.py"), gum=True)
        (root / "catalog.json").write_text(json.dumps({"entries": [checked(home=HOME, local_state="ready")]}))
        container = {"service_id": "vllm-port80", "conf": None, "state": "running", "ownership": "unknown",
                     "container_name": "vllm-port80", "ranks": [{"rank": "0", "node": "head"}]}
        (root / "fixtures/inventory.json").write_text(json.dumps(
            inventory(service(SPEC), container, gpu=(("head", 9216),), unobserved=())))
        exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in root_environment(root).items())
        for columns in (80, 60):
            with self.subTest(columns=columns):
                (root / "pulsar.log").unlink(missing_ok=True)
                code, output = self.drive(exports + f'exec bash {shlex.quote(str(root / "scripts/services-menu.sh"))}\n',
                                          b"\x1b", ready=b"Stop every service", columns=columns)
                self.assertEqual(code, 0, output)
                screen = views(output, 24, columns)[-2]
                text = "\n".join(screen)
                for line in ("Seen      by the service inventory less than a minute ago",
                             "Health    not checked here; choose Check it now", "Running services",
                             "> Check it now", "  Stop every service…", "  Back"):
                    self.assertIn(line, screen, text)
                # The whole card stays on screen above its choices.
                self.assertTrue(any(line.startswith("org/model running on") for line in screen), text)
                self.assertTrue(all(len(line) <= columns for line in screen))
                self.assertEqual((root / "pulsar.log").read_text().splitlines(), ["inventory --json"])


if __name__ == "__main__":
    unittest.main()
