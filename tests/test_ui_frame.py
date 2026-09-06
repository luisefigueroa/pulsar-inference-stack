"""Gum frames static status text; plain mode prints the same copy without a box."""
import os
from pathlib import Path
import subprocess
import tempfile
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
            f"printf '%s\\n' \"$*\" >> '{self.gum_log}'\n"
            "if [ \"$1\" = style ]; then cat; exit 0; fi\n"
            "if [ \"$1\" = input ]; then printf '%s\\n' /var/tmp/archives; exit 0; fi\n"
            "exit 2\n"
        )
        gum.chmod(0o755)
        self.gum = gum

    def run_ui(self, body, *, gum=True):
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "REPO_DIR": str(ROOT),
            "TERM": "xterm-256color",
            "COLUMNS": "44",
            "PULSAR_FORCE_GUM": "1" if gum else "0",
            "GUM": "1" if gum else "0",
            "GUM_BIN": str(self.gum),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.pop("NO_COLOR", None)
        env.pop("PULSAR_COLOR", None)
        script = (
            f". {ROOT / 'scripts/ui.sh'}\n"
            f"{body}\n"
        )
        return subprocess.run(
            ["bash", "-c", script],
            env=env, cwd=str(ROOT), text=True, capture_output=True,
        )

    def test_plain_frame_prints_text_without_invoking_gum(self):
        result = self.run_ui('printf "Setup\\n" | emit_frame', gum=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Setup\n")
        self.assertFalse(self.gum_log.exists())

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


if __name__ == "__main__":
    unittest.main()
