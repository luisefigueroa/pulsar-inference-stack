"""Menus and prompts draw only with Gum; without it they name commands instead."""
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UiFrame(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gum_log = self.root / "gum.log"
        gum = self.root / "gum"
        gum.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s NO_COLOR=%s\\n' \"$*\" \"${{NO_COLOR:-}}\" >> '{self.gum_log}'\n"
            "if [ \"$1\" = style ]; then cat; exit 0; fi\n"
            "if [ \"$1\" = input ]; then [ -z \"${GUM_RC:-}\" ] || exit \"$GUM_RC\"; printf '%s\\n' /var/tmp/archives; exit 0; fi\n"
            # choose prints the line GUM_CHOICE names (default: the first) or exits GUM_RC;
            # GUM_CHOOSE_INPUT keeps the lines it was given.
            "if [ \"$1\" = choose ]; then [ -z \"${GUM_RC:-}\" ] || exit \"$GUM_RC\"; "
            "tee \"${GUM_CHOOSE_INPUT:-/dev/null}\" | sed -n \"$(( ${GUM_CHOICE:-0} + 1 ))p\"; exit 0; fi\n"
            "if [ \"$1\" = confirm ]; then exit \"${GUM_RC:-0}\"; fi\n"
            # Like Gum versions without --show-output: run the command, drop its output.
            "if [ \"$1\" = spin ]; then while [ \"$1\" != -- ]; do shift; done; shift; \"$@\" >/dev/null 2>&1; exit $?; fi\n"
            # pager keeps what it was given and exits GUM_PAGER_RC (q or Esc: 0).
            "if [ \"$1\" = pager ]; then cat > \"${GUM_PAGER_INPUT:-/dev/null}\"; exit \"${GUM_PAGER_RC:-0}\"; fi\n"
            "exit 2\n"
        )
        gum.chmod(0o755)
        self.gum = gum
        # A read-only command: N lines with an error after the first, its
        # width, then the given exit status.
        self.emit = self.root / "emit.sh"
        self.emit.write_text('#!/usr/bin/env bash\necho "out 1"; echo "err" >&2\n'
                             'for i in $(seq 2 "$1"); do echo "out $i"; done\n'
                             'echo "width ${COLUMNS:-unset}"; exit "$2"\n')
        self.emit.chmod(0o755)
        self.pager_input = self.root / "pager.txt"

    def run_ui(self, body, *, gum=True, **extra):
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "REPO_DIR": str(ROOT),
            "TERM": "xterm-256color",
            "COLUMNS": "44",
            "PULSAR_FORCE_GUM": "1" if gum else "0",
            "GUM_BIN": str(self.gum),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.pop("NO_COLOR", None)
        env.pop("PULSAR_COLOR", None)
        env.update(extra)
        script = (
            f". {ROOT / 'scripts/ui.sh'}\n"
            f"{body}\n"
        )
        return subprocess.run(
            ["bash", "-c", script], stdin=subprocess.DEVNULL,
            env=env, cwd=str(ROOT), text=True, capture_output=True,
        )

    def gum_calls(self):
        return self.gum_log.read_text().splitlines() if self.gum_log.exists() else []

    def test_gum_spin_returns_output_and_status_even_when_gum_drops_it(self):
        result = self.run_ui('plan=$(spin "Planning" bash -c "echo plan-json; echo planning-error >&2; exit 3"); '
                             'rc=$?; printf "[%s] rc=%s\\n" "$plan" "$rc"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[plan-json] rc=3\n")
        self.assertIn("planning-error", result.stderr)
        self.assertIn("spin", self.gum_log.read_text())

    def test_gum_frame_uses_rounded_accent_border(self):
        result = self.run_ui('printf "Setup\\nTopology   not confirmed\\n" | emit_frame')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Setup", result.stdout)
        flags = self.gum_log.read_text()
        self.assertIn("--border rounded", flags)
        self.assertIn("--border-foreground 12", flags)
        self.assertIn("--padding 0 1", flags)
        self.assertIn("--margin 1 0", flags)
        self.assertNotIn("--width", flags)

    def test_gum_error_is_red(self):
        result = self.run_ui('printf "missing directory\\n" | emit_error')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("missing directory", result.stderr)
        flags = self.gum_log.read_text()
        self.assertIn("--foreground 1", flags)
        self.assertIn("--bold", flags)

    def test_gum_path_prompt_uses_input(self):
        result = self.run_ui('prompt_input "Existing absolute directory:" "/existing/absolute/directory"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/var/tmp/archives")
        flags = self.gum_log.read_text()
        self.assertIn("input", flags)
        self.assertIn("--header Existing absolute directory:", flags)
        self.assertIn("--placeholder /existing/absolute/directory", flags)
        self.assertIn("--header.foreground=12", flags)

    def test_color_passes_the_complete_blue_palette(self):
        result = self.run_ui('choose_index "Pick" one two; confirm "Sure?"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n")
        choose, confirm = self.gum_calls()
        for flag in ("--cursor.foreground=12", "--header.foreground=12", "--selected.foreground=12"):
            self.assertIn(flag, choose)
        for flag in ("--prompt.foreground=12", "--selected.foreground=15", "--selected.background=4"):
            self.assertIn(flag, confirm)
        self.assertTrue(all(call.endswith("NO_COLOR=") for call in self.gum_calls()))

    def test_no_color_runs_gum_without_color_flags(self):
        body = ('choose_index "Pick" one two; confirm "Sure?"; printf "x\\n" | emit_frame; '
                'printf "x\\n" | emit_error; prompt_input "Path:"; spin "Wait" true')
        for setting in ({"NO_COLOR": "1"}, {"PULSAR_COLOR": "never"}):
            with self.subTest(setting=setting):
                self.gum_log.unlink(missing_ok=True)
                result = self.run_ui(body, **setting)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.gum_calls()
                self.assertEqual([call.split()[0] for call in calls],
                                 ["choose", "confirm", "style", "style", "input", "spin"])
                for call in calls:
                    self.assertNotIn("foreground", call)
                    self.assertNotIn("background", call)
                    # Gum's own defaults are colored; NO_COLOR=1 turns them off.
                    self.assertTrue(call.endswith("NO_COLOR=1"), call)
                self.assertIn("--border rounded", calls[2])
                self.assertIn("--padding 1 0", calls[0])

    def test_choose_hides_the_index_and_fits_the_list_to_the_terminal(self):
        options = " ".join(f"o{number}" for number in range(13))
        # Gum needs 5 rows around the list; a terminal too short for 3 options is not capped.
        for lines, height in (("", 13), ("40", 13), ("12", 7), ("7", 13)):
            with self.subTest(lines=lines):
                self.gum_log.unlink(missing_ok=True)
                result = self.run_ui(f'choose_index "Pick" {options}', GUM_CHOICE="12", LINES=lines)
                self.assertEqual(result.stdout, "12\n", result.stderr)
                choose = self.gum_calls()[0]
                self.assertIn("--label-delimiter=\t", choose)
                self.assertIn(f"--height={height} ", choose)

    def test_repeated_labels_get_a_suffix_and_a_default_keeps_its_comma(self):
        given = self.root / "choose.in"
        result = self.run_ui("PULSAR_CHOOSE_DEFAULT=1 choose_index Pick 'a, b' 'a, b' $'c\\td'",
                             GUM_CHOICE="1", GUM_CHOOSE_INPUT=str(given))
        self.assertEqual(result.stdout, "1\n", result.stderr)
        self.assertEqual(given.read_text().splitlines(), ["a, b\t1", "a, b (2)\t2", "c d\t3"])
        self.assertIn("--selected=a\\, b (2) ", self.gum_calls()[0])

    def test_escape_and_ctrl_c_keep_their_meaning(self):
        for rc, expected in (("1", 1), ("130", 130)):
            with self.subTest(gum_rc=rc):
                result = self.run_ui('choose_index "Pick" one two || echo "rc=$?"', GUM_RC=rc)
                self.assertEqual(result.stdout, f"rc={expected}\n")
                result = self.run_ui('prompt_input "Path" || echo "rc=$?"', GUM_RC=rc)
                self.assertEqual(result.stdout, f"rc={expected}\n")
        result = self.run_ui('confirm "Sure?" || echo "rc=$?"', GUM_RC="130")
        self.assertEqual(result.stdout, "rc=130\n")

    def page(self, lines, status, title="", **extra):
        self.gum_log.unlink(missing_ok=True)
        self.pager_input.unlink(missing_ok=True)
        return self.run_ui(f'rc=0; page_output "{title}" "{self.emit}" {lines} {status} || rc=$?; echo "rc=$rc"',
                           GUM_PAGER_INPUT=str(self.pager_input), **extra)

    def test_short_output_prints_in_place_with_its_status(self):
        for status in (0, 3):
            with self.subTest(status=status):
                result = self.page(3, status)
                self.assertEqual(result.returncode, 0, result.stderr)
                # Errors stay in order with the output, at the pager's width.
                self.assertEqual(result.stdout, f"out 1\nerr\nout 2\nout 3\nwidth 40\nrc={status}\n")
                self.assertEqual(self.gum_calls(), [])

    def test_long_output_opens_the_pager_and_keeps_the_status(self):
        for status in (0, 3):
            with self.subTest(status=status):
                result = self.page(20, status)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"rc={status}\n")
                text = self.pager_input.read_text()
                self.assertTrue(text.startswith("out 1\nerr\nout 2\n"), text)
                self.assertTrue(text.endswith("out 20\nwidth 40\n"), text)
                (pager,) = self.gum_calls()
                self.assertTrue(pager.startswith("pager --show-line-numbers=false"), pager)
                self.assertIn("--match.foreground=12", pager)
        result = self.page(20, 0, NO_COLOR="1")
        (pager,) = self.gum_calls()
        self.assertNotIn("foreground", pager)

    def test_a_slow_command_shows_a_spinner_before_its_output(self):
        result = self.page(20, 0, title="Checking fixture nodes…")
        self.assertEqual(result.stdout, "rc=0\n")
        spin, pager = self.gum_calls()
        self.assertIn("spin", spin)
        self.assertIn("--title Checking fixture nodes…", spin)
        self.assertIn("out 20", self.pager_input.read_text())

    def test_ctrl_c_in_the_command_or_the_pager_returns_130(self):
        result = self.page(20, 130)
        self.assertEqual(result.stdout, "rc=130\n")
        self.assertEqual(self.gum_calls(), [])
        result = self.page(20, 0, GUM_PAGER_RC="130")
        self.assertEqual(result.stdout, "rc=130\n")

    def test_a_pager_that_cannot_open_prints_the_output_instead(self):
        result = self.page(20, 3, GUM_PAGER_RC="1")
        self.assertIn("out 1\nerr\nout 2\n", result.stdout)
        self.assertTrue(result.stdout.endswith("out 20\nwidth 40\nrc=3\n"), result.stdout)

    def test_continue_holds_the_screen_and_keeps_ctrl_c(self):
        # choose_index turns errexit back on, so the status is kept explicitly.
        result = self.run_ui('rc=0; wait_continue "Fixture outcome above" || rc=$?; echo "rc=$rc"',
                             GUM_CHOOSE_INPUT=str(self.root / "choose.txt"))
        self.assertEqual(result.stdout, "rc=0\n")
        self.assertIn("--header Fixture outcome above", self.gum_calls()[0])
        self.assertEqual((self.root / "choose.txt").read_text(), "Continue\t1\n")
        for rc, expected in (("1", 0), ("130", 130)):
            with self.subTest(gum_rc=rc):
                result = self.run_ui('rc=0; wait_continue || rc=$?; echo "rc=$rc"', GUM_RC=rc)
                self.assertEqual(result.stdout, f"rc={expected}\n")

    def test_without_gum_a_menu_names_commands_and_exits_2(self):
        # A PATH with only what ui.sh runs, so an installed gum cannot be found.
        bare = self.root / "bin"
        bare.mkdir()
        for tool in ("bash", "uname"):
            (bare / tool).symlink_to(shutil.which(tool))
        cases = {
            "no terminal": ({}, False),
            "dumb terminal": ({"TERM": "dumb"}, True),
            "no Gum executable": ({"GUM_BIN": "", "VENDORED_GUM": str(self.root / "absent"),
                                   "PATH": str(bare)}, True),
        }
        for name, (extra, forced) in cases.items():
            with self.subTest(name):
                result = self.run_ui('require_gum "the topology menu" "pulsar topology show | check"; echo opened',
                                     gum=forced, **extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "error: the topology menu needs an interactive terminal "
                                                "with Gum; use: pulsar topology show | check\n")
                self.assertEqual(self.gum_calls(), [])

    def test_without_gum_prompts_refuse_instead_of_waiting(self):
        for body in ('choose_index "Pick" one two', 'confirm "Sure?"', 'prompt_input "Path:"',
                     'printf "x\\n" | emit_frame', 'spin "Wait" true'):
            with self.subTest(body=body):
                result = self.run_ui(body + '; echo "rc=$?"', gum=False)
                self.assertEqual(result.stdout.splitlines()[-1], "rc=2")
                self.assertIn("error: this prompt needs an interactive terminal with Gum", result.stderr)
                self.assertEqual(self.gum_calls(), [])


class MenuEntries(unittest.TestCase):
    """Every menu entry point refuses without Gum; nothing else runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_in_terminal(self, *args, term="xterm-256color"):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "CLUSTER_", "GUM"))}
        # A GUM_BIN that cannot run disables Gum whatever else is installed.
        unusable = self.root / "gum-not-executable"
        unusable.write_text("")
        env.update(TERM=term, GUM_BIN=str(unusable),
                   CLUSTER_TOPOLOGY_FILE=str(self.root / "topology.json"),
                   PULSAR_MODEL_LIBRARY_DIR=str(self.root / "model-library"),
                   PULSAR_SSH="/bin/false", PULSAR_DOCKER="/bin/false", PYTHONDONTWRITEBYTECODE="1")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen([str(ROOT / "pulsar"), *args], env=env,
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = b""
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                    break
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        return process.returncode, output.decode(errors="replace").replace("\r\n", "\n")

    def test_menus_on_a_terminal_without_gum_name_their_commands(self):
        entries = {
            (): "error: the Pulsar menu needs an interactive terminal with Gum; use: pulsar",
            ("models", "menu"): "error: the catalog menu needs an interactive terminal with Gum; "
                                "use: pulsar models list | show SPEC | check SPEC",
            ("inventory", "menu"): "error: the inventory menu needs an interactive terminal with Gum",
            ("wizard",): "error: the catalog menu needs",
            ("topology", "menu"): "error: the topology menu needs an interactive terminal with Gum; "
                                  "use: pulsar topology show | check | setup | detect | configure",
            ("topology", "setup"): "error: guided setup needs an interactive terminal with Gum; use: ",
            ("configure", "archive-root", "menu"): "error: the archive storage menu needs an interactive "
                                                  "terminal with Gum; use: pulsar configure archive-root show",
        }
        for args, expected in entries.items():
            for term in ("xterm-256color", "dumb"):
                with self.subTest(args=args, term=term):
                    rc, output = self.run_in_terminal(*args, term=term)
                    self.assertEqual(rc, 2, output)
                    self.assertIn(expected, " ".join(output.split()))
                    if not args:
                        # Help's pages are commands too.
                        self.assertIn("| help [terms|workflow]", " ".join(output.split()))
                    self.assertFalse((self.root / "model-library").exists())
                    self.assertFalse((self.root / "topology.json").exists())
        # pulsar models, which names no menu, shows the catalog list instead.
        for term in ("xterm-256color", "dumb"):
            with self.subTest(args=("models",), term=term):
                rc, output = self.run_in_terminal("models", term=term)
                self.assertEqual(rc, 0, output)
                self.assertIn("warning: the catalog menu needs Gum on this terminal; showing pulsar models list",
                              " ".join(output.split()))
                self.assertIn("Saved records.", output)


if __name__ == "__main__":
    unittest.main()
