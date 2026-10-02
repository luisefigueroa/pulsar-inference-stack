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
            # choose prints the line GUM_CHOICE names (default: the first) or exits GUM_RC.
            "if [ \"$1\" = choose ]; then [ -z \"${GUM_RC:-}\" ] || exit \"$GUM_RC\"; "
            "sed -n \"$(( ${GUM_CHOICE:-0} + 1 ))p\"; exit 0; fi\n"
            "if [ \"$1\" = confirm ]; then exit \"${GUM_RC:-0}\"; fi\n"
            # Like Gum versions without --show-output: run the command, drop its output.
            "if [ \"$1\" = spin ]; then while [ \"$1\" != -- ]; do shift; done; shift; \"$@\" >/dev/null 2>&1; exit $?; fi\n"
            "exit 2\n"
        )
        gum.chmod(0o755)
        self.gum = gum

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

    def test_escape_and_ctrl_c_keep_their_meaning(self):
        for rc, expected in (("1", 1), ("130", 130)):
            with self.subTest(gum_rc=rc):
                result = self.run_ui('choose_index "Pick" one two || echo "rc=$?"', GUM_RC=rc)
                self.assertEqual(result.stdout, f"rc={expected}\n")
                result = self.run_ui('prompt_input "Path" || echo "rc=$?"', GUM_RC=rc)
                self.assertEqual(result.stdout, f"rc={expected}\n")
        result = self.run_ui('confirm "Sure?" || echo "rc=$?"', GUM_RC="130")
        self.assertEqual(result.stdout, "rc=130\n")

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
            ("models",): "error: the catalog menu needs an interactive terminal with Gum; "
                         "use: pulsar models list | show SPEC | check SPEC",
            ("models", "menu"): "error: the catalog menu needs",
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
                    self.assertFalse((self.root / "model-library").exists())
                    self.assertFalse((self.root / "topology.json").exists())


if __name__ == "__main__":
    unittest.main()
