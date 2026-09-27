"""Interactive ./pulsar menu offers one bind step until the checkout is bound."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def enrolled_two_node():
    return {
        "schema_version": 2,
        "topology_id": "a" * 64,
        "nodes": [{"rank": 0}, {"rank": 1}],
        "validation": {"ssh_identity_enrolled": True},
    }


class HomeMenu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        (self.root / "releases").mkdir()
        shutil.copyfile(ROOT / "scripts/home.sh", scripts / "home.sh")
        os.chmod(scripts / "home.sh", 0o755)
        self.choose_log = self.root / "choose.log"
        self.choose_count = self.root / "choose.count"
        self.fabric_log = self.root / "fabric.log"
        self.pulsar_log = self.root / "pulsar.log"
        (scripts / "ui.sh").write_text(r'''
emit_frame() { cat; }
require_gum() { :; }
choose_index() {
  printf '%s\n' "$@" >> "$CHOOSE_LOG"
  printf '\n' >> "$CHOOSE_LOG"
  if [ "${CHOOSE_MODE:-}" = last ]; then
    printf '%s\n' "$(($# - 2))"
    return 0
  fi
  if [ -f "$CHOOSE_COUNT" ]; then n=$(cat "$CHOOSE_COUNT"); else n=0; fi
  echo $((n + 1)) > "$CHOOSE_COUNT"
  if [ "${CHOOSE_MODE:-}" = archive ]; then
    # Open the archive menu once, then choose Exit when the home menu returns.
    if [ "$n" = 0 ]; then printf '3\n'; else printf '%s\n' "$(($# - 2))"; fi
    return 0
  fi
  if [ "${CHOOSE_MODE:-}" = interrupt ]; then
    if [ "$n" = 0 ]; then printf '3\n'; return 0; fi
    return 130
  fi
  if [ "$n" = 0 ]; then printf '0\n'; else printf '1\n'; fi
}
''')
        fabric = scripts / "detect-fabric.sh"
        fabric.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$FABRIC_LOG"\n')
        fabric.chmod(0o755)
        pulsar = self.root / "pulsar"
        pulsar.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$PULSAR_LOG"\n')
        pulsar.chmod(0o755)

    def env(self, **extra):
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PULSAR_FORCE_MENU": "1",
            "PULSAR_SETUP_STATUS_PY": str(ROOT / "scripts/setup_status.py"),
            "CHOOSE_LOG": str(self.choose_log),
            "CHOOSE_COUNT": str(self.choose_count),
            "FABRIC_LOG": str(self.fabric_log),
            "PULSAR_LOG": str(self.pulsar_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(extra)
        return env

    def run_home(self, **extra):
        return subprocess.run(
            ["bash", str(self.root / "scripts/home.sh")],
            env=self.env(**extra), cwd=str(self.root), text=True, capture_output=True,
        )

    def choose_blocks(self):
        text = self.choose_log.read_text()
        return [block.splitlines() for block in text.strip().split("\n\n") if block.strip()]

    def test_incomplete_menu_is_next_action_and_exit(self):
        result = self.run_home(CHOOSE_MODE="last")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.fabric_log.exists())
        options = self.choose_blocks()[0]
        self.assertEqual(options[0], "Pulsar Inference Stack")
        self.assertEqual(options[1:], ["Confirm cluster membership", "Exit"])

    def test_selecting_membership_runs_write_topology_without_yes(self):
        result = self.run_home()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fabric_log.read_text().split(), ["--write-topology"])
        blocks = self.choose_blocks()
        self.assertGreaterEqual(len(blocks), 2)
        self.assertEqual(blocks[0][1:], ["Confirm cluster membership", "Exit"])

    def test_complete_menu_starts_with_catalog_and_archive_opens_menu(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(CHOOSE_MODE="archive", PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = self.choose_blocks()[0]
        self.assertEqual(options[1], "Catalog and storage")
        self.assertIn("Archive storage configuration", options)
        self.assertEqual(self.pulsar_log.read_text().split(), ["configure", "archive-root", "menu"])
        self.assertFalse(self.fabric_log.exists())
        # The submenu returns to the home menu instead of ending the session.
        blocks = self.choose_blocks()
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1][0], "Pulsar Inference Stack")

    def test_ctrl_c_at_the_home_menu_exits_with_130(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(CHOOSE_MODE="interrupt", PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.choose_blocks()), 2)


if __name__ == "__main__":
    unittest.main()
