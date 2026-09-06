"""Archive menu reports a missing directory and does not create it."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ArchiveMenu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "releases").mkdir()
        self.confirm_log = self.root / "confirm.log"
        self.ui = self.root / "ui.sh"
        self.ui.write_text(r'''
choose_index() { printf '0\n'; }
prompt_input() { printf '%s\n' "$ARCHIVE_PATH"; }
emit_error() { cat >&2; }
confirm() {
  printf '%s\n' "$1" >> "$CONFIRM_LOG"
  return "${CONFIRM_RC:-1}"
}
''')

    def run_menu(self, path, confirm_rc=1):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/configure-archive.sh"), "menu"],
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "GUM": "0",
                "PULSAR_FORCE_MENU": "1",
                "PULSAR_HOME_UI": str(self.ui),
                "PULSAR_SETUP_ROOT": str(self.root),
                "ARCHIVE_PATH": str(path),
                "CONFIRM_LOG": str(self.confirm_log),
                "CONFIRM_RC": str(confirm_rc),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )

    def test_missing_directory_is_explained_and_not_created(self):
        missing = self.root / "missing"
        result = self.run_menu(missing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not an existing directory", result.stderr)
        self.assertIn("does not create archive storage", result.stderr)
        self.assertIn("Try another path?", self.confirm_log.read_text())
        self.assertNotIn("Save this archive location", self.confirm_log.read_text())
        self.assertFalse(missing.exists())
        self.assertFalse((self.root / ".env").exists())

    def test_existing_directory_can_be_saved(self):
        archive = self.root / "archives"
        archive.mkdir()
        result = self.run_menu(archive, confirm_rc=0)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Save this archive location", self.confirm_log.read_text())
        self.assertIn(str(archive), (self.root / ".env").read_text())


if __name__ == "__main__":
    unittest.main()
