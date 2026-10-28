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
        self.choose_log = self.root / "choose.log"
        self.ui = self.root / "ui.sh"
        self.ui.write_text(r'''
require_gum() { :; }
ui_keep_styles() { :; }
choose_index() {
  printf '%s\n' "$1" >> "$CHOOSE_LOG"
  local n value
  n=$(wc -l < "$CHOOSE_LOG")
  value=$(printf '%s' "$CHOICES" | cut -d, -f"$n")
  case "$value" in esc) return 1 ;; ctrl-c) return 130 ;; *) printf '%s\n' "${value:-2}" ;; esac
}
prompt_input() { [ "${INPUT_RC:-0}" = 0 ] || return "$INPUT_RC"; printf '%s\n' "$ARCHIVE_PATH"; }
emit_error() { cat >&2; }
confirm() {
  printf '%s\n' "$1" >> "$CONFIRM_LOG"
  return "${CONFIRM_RC:-1}"
}
''')

    def run_menu(self, path, confirm_rc=1, *, choices="0,2", input_rc=0, **extra):
        self.choose_log.unlink(missing_ok=True)
        self.confirm_log.unlink(missing_ok=True)
        base = {key: value for key, value in os.environ.items() if key != "PULSAR_COLD_ROOT"}
        return subprocess.run(
            ["bash", str(ROOT / "scripts/configure-archive.sh"), "menu"],
            env={
                **base, **extra,
                "PYTHONPATH": str(ROOT),
                "PULSAR_HOME_UI": str(self.ui),
                "PULSAR_SETUP_ROOT": str(self.root),
                "ARCHIVE_PATH": str(path),
                "CONFIRM_LOG": str(self.confirm_log),
                "CONFIRM_RC": str(confirm_rc),
                "CHOOSE_LOG": str(self.choose_log),
                "CHOICES": choices,
                "INPUT_RC": str(input_rc),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=15,
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
        self.assertEqual(self.choose_log.read_text().splitlines(), ["Archive storage"] * 2)

    def test_without_gum_the_menu_names_commands_and_changes_nothing(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "GUM"))}
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/configure-archive.sh"), "menu"],
            env={**env, "PULSAR_SETUP_ROOT": str(self.root), "TERM": "xterm-256color",
                 "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL, cwd=str(ROOT), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "error: the archive storage menu needs an interactive terminal with Gum; "
                                        "use: pulsar configure archive-root show | set PATH --yes | disable --yes\n")
        self.assertFalse((self.root / ".env").exists())

    def test_existing_directory_can_be_saved(self):
        archive = self.root / "archives"
        archive.mkdir()
        result = self.run_menu(archive, confirm_rc=0)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Save this archive location", self.confirm_log.read_text())
        self.assertIn(str(archive), (self.root / ".env").read_text())
        self.assertEqual(self.choose_log.read_text().splitlines(), ["Archive storage"] * 2)

    def test_ctrl_c_at_each_archive_prompt_preserves_status_and_configuration(self):
        archive = self.root / "archives"
        archive.mkdir()
        config = self.root / ".env"
        original = "# preserve unrelated configuration\nOTHER=example\n"
        config.write_text(original)
        cases = [dict(choices="ctrl-c"), dict(input_rc=130), dict(confirm_rc=130),
                 dict(choices="1", confirm_rc=130)]
        for options in cases:
            with self.subTest(options=options):
                result = self.run_menu(archive, **options)
                self.assertEqual(result.returncode, 130, result.stderr)
                self.assertEqual(config.read_text(), original)
                self.assertEqual(len(self.choose_log.read_text().splitlines()), 1)
        result = self.run_menu(self.root / "absent", confirm_rc=130)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(config.read_text(), original)

    def test_escape_and_decline_return_to_archive_menu(self):
        archive = self.root / "archives"
        archive.mkdir()
        for options in (dict(input_rc=1), dict(confirm_rc=1), dict(choices="1,2", confirm_rc=1)):
            with self.subTest(options=options):
                result = self.run_menu(archive, **options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.choose_log.read_text().splitlines()), 2)
                self.assertFalse((self.root / ".env").exists())

    def test_disabling_returns_to_archive_menu_without_deleting_archives(self):
        archive = self.root / "archives"
        archive.mkdir()
        sentinel = archive / "existing-content"
        sentinel.write_text("keep")
        result = self.run_menu(archive, confirm_rc=0, choices="1,2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PULSAR_COLD_ROOT=', (self.root / ".env").read_text())
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual(len(self.choose_log.read_text().splitlines()), 2)
        # TUI-51: the consequences come before the question.
        self.assertEqual(self.confirm_log.read_text(), "Disable archives?\n")
        self.assertIn("Existing archives stay on disk. Restore, Create archive and Verify archive are left out of "
                      "the catalog menu until a location is set again.", " ".join(result.stdout.split()))

    def test_disabling_under_a_process_value_says_it_changes_only_dotenv(self):
        archive = self.root / "archives"
        archive.mkdir()
        result = self.run_menu(archive, confirm_rc=0, choices="1,2", PULSAR_COLD_ROOT=str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.confirm_log.read_text(), "Disable the saved archive location?\n")
        text = " ".join(result.stdout.split())
        self.assertIn("PULSAR_COLD_ROOT is set in this process and takes precedence", text)
        self.assertNotIn("left out of the catalog menu", text)
        self.assertIn("PULSAR_COLD_ROOT=", (self.root / ".env").read_text())

    # TUI-07: the menu shows why the setting can't be read and stays open.
    def test_unreadable_setting_is_explained_and_the_menu_stays_open(self):
        (self.root / "elsewhere.env").write_text("PULSAR_COLD_ROOT=/var/tmp/archives\n")
        (self.root / ".env").symlink_to(self.root / "elsewhere.env")
        result = self.run_menu(self.root / "archives", choices="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".env", result.stderr)
        self.assertEqual(self.choose_log.read_text().splitlines(), ["Archive storage"])
        self.assertTrue((self.root / ".env").is_symlink())


if __name__ == "__main__":
    unittest.main()
