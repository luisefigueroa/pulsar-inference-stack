"""Interactive ./pulsar menu: cluster setup and optional archive configuration."""
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


def unenrolled_two_node():
    return {"schema_version": 1, "topology_id": "b" * 64, "nodes": [{"rank": 0}, {"rank": 1}]}


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
        self.answers = self.root / "answers"
        self.pulsar_log = self.root / "pulsar.log"
        # A parameterized ui.sh double: each menu takes the next answer, as an
        # option index, "esc" or "ctrl-c"; a menu past the answers is an error.
        (scripts / "ui.sh").write_text(r'''
emit_frame() { cat; }
require_gum() { :; }
choose_index() {
  local n answer
  { printf '%s\n' "$@"; printf '\n'; } >> "$CHOOSE_LOG"
  n=$(cat "$ANSWERS.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$ANSWERS.n"
  answer=$(sed -n "$((n + 1))p" "$ANSWERS")
  case "$answer" in
    "") echo "UNEXPECTED MENU" >> "$CHOOSE_LOG"; return 1 ;;
    esc) return 1 ;;
    ctrl-c) return 130 ;;
    *) printf '%s\n' "$answer" ;;
  esac
}
''')
        # Every menu action is a pulsar command; the double records it and
        # exits with the next PULSAR_RC status (default 0).
        pulsar = self.root / "pulsar"
        pulsar.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PULSAR_LOG"\n'
                          'n=$(wc -l < "$PULSAR_LOG")\n'
                          'rc=$(printf "%s" "${PULSAR_RC:-0}" | cut -d, -f"$n")\n'
                          'case "$*" in "topology setup"|"ssh-trust enroll")\n'
                          '  if [ "${rc:-0}" = 0 ] && [ -n "${SETUP_TOPOLOGY:-}" ]; then\n'
                          '    cp "$SETUP_TOPOLOGY" .cluster-topology.json\n'
                          '  fi ;; esac\n'
                          'exit "${rc:-0}"\n')
        pulsar.chmod(0o755)
        for name in ("detect-fabric.sh", "topology-ssh-trust.sh"):
            probe = scripts / name
            probe.write_text('#!/usr/bin/env bash\necho "probe: $0" >> "$PULSAR_LOG"\nexit 1\n')
            probe.chmod(0o755)

    def run_home(self, answers, **extra):
        self.answers.write_text("".join(f"{answer}\n" for answer in answers))
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PULSAR_FORCE_MENU": "1",
            "PULSAR_SETUP_STATUS_PY": str(ROOT / "scripts/setup_status.py"),
            "CHOOSE_LOG": str(self.choose_log),
            "ANSWERS": str(self.answers),
            "PULSAR_LOG": str(self.pulsar_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.pop("PULSAR_COLD_ROOT", None)
        env.pop("CLUSTER_TOPOLOGY_FILE", None)
        env.update(extra)
        result = subprocess.run(
            ["bash", str(self.root / "scripts/home.sh")],
            env=env, cwd=str(self.root), text=True, capture_output=True, timeout=60,
        )
        self.assertNotIn("UNEXPECTED MENU", self.choose_log.read_text() if self.choose_log.exists() else "")
        return result

    def menus(self):
        text = self.choose_log.read_text()
        return [block.splitlines() for block in text.strip().split("\n\n") if block.strip()]

    def commands(self):
        return self.pulsar_log.read_text().splitlines() if self.pulsar_log.exists() else []

    def test_first_run_offers_setup_read_only_inspection_and_help(self):
        result = self.run_home(["5"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [["Pulsar Inference Stack", "Set up cluster membership and SSH trust",
                                         "Browse the catalog (read-only)", "Host diagnostics",
                                         "Cluster topology (read-only)", "Help", "Exit"]])
        self.assertEqual(self.commands(), [])
        self.assertIn("Cluster membership is not configured.", result.stdout)
        self.assertFalse((self.root / ".env").exists())

    def test_membership_step_runs_guided_topology_setup(self):
        result = self.run_home(["0", "5"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertNotIn("Setup step did not complete", result.stdout)
        self.assertEqual(len(self.menus()), 2)

    def test_catalog_details_are_read_only_and_return_to_the_menu(self):
        result = self.run_home(["1", "5"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["models menu --read-only"])
        menus = self.menus()
        self.assertEqual(len(menus), 2)
        self.assertEqual(menus[0], menus[1])

    def test_catalog_error_waits_before_redrawing_the_home_menu(self):
        (self.root / '.cluster-topology.json').write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(['0', '0', '6'], PULSAR_RC='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ['models menu'])
        self.assertEqual(self.menus()[1], ['Catalog error', 'Back'])
        self.assertEqual(len(self.menus()), 3)
        self.assertIn('Catalog could not be displayed (exit 2)', result.stdout)

    def test_first_run_catalog_error_can_be_interrupted(self):
        result = self.run_home(['1', 'ctrl-c'], PULSAR_RC='2')
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.menus()[1], ['Catalog error', 'Back'])
        self.assertEqual(len(self.menus()), 2)

    def test_inspection_and_help_are_available_before_setup(self):
        result = self.run_home(["2", "3", "4", "5"], PULSAR_RC="1,2,0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["doctor", "topology show", "help"])
        self.assertEqual(len(self.menus()), 4)
        self.assertNotIn("Setup step did not complete", result.stdout)
        self.assertFalse((self.root / ".cluster-topology.json").exists())
        self.assertFalse((self.root / ".env").exists())

    def test_a_failed_setup_step_is_reported_before_the_menu_returns(self):
        result = self.run_home(["0", "5"], PULSAR_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertIn("✗ Setup step did not complete; details above", result.stdout)
        # The report comes before the status and menu are shown again.
        self.assertEqual(result.stdout.count("Cluster membership is not configured."), 2)
        self.assertLess(result.stdout.index("✗ Setup step"),
                        result.stdout.rindex("Cluster membership is not configured."))

    def test_ctrl_c_during_a_setup_step_leaves_the_menu(self):
        result = self.run_home(["0"], PULSAR_RC="130")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertNotIn("Setup step did not complete", result.stdout)

    def test_saved_membership_without_trust_enrolls_ssh_trust(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(unenrolled_two_node()))
        result = self.run_home(["0", "5"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0][1], "Enroll SSH trust")
        self.assertEqual(self.commands(), ["ssh-trust enroll"])

    def test_configured_cluster_without_archives_opens_full_operations(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["0", "1", "2", "5", "6"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0][1], "Catalog and storage")
        self.assertEqual(self.commands(), ["models menu", "inventory menu", "doctor", "help"])
        self.assertNotIn("not bound", result.stdout)
        self.assertFalse((self.root / ".env").exists())

    def test_completing_cluster_setup_opens_full_menu_without_saving_archives(self):
        topology = self.root / "enrolled.json"
        topology.write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["0", "6"], SETUP_TOPOLOGY=str(topology))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertEqual(self.menus()[0][1], "Set up cluster membership and SSH trust")
        self.assertEqual(self.menus()[1][1], "Catalog and storage")
        self.assertFalse((self.root / ".env").exists())

    def test_unset_archives_remain_an_explicit_configuration_choice(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["3", "6"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["configure archive-root menu"])

    def test_ctrl_c_during_read_only_inspection_exits(self):
        result = self.run_home(["1"], PULSAR_RC="130")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.menus()), 1)

    def test_complete_menu_starts_with_catalog_and_archive_opens_menu(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["3", "6"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = self.menus()[0]
        self.assertEqual(options[1], "Catalog and storage")
        self.assertIn("Archive storage configuration", options)
        self.assertEqual(self.commands(), ["configure archive-root menu"])
        # The submenu returns to the home menu instead of ending the session.
        menus = self.menus()
        self.assertEqual(len(menus), 2)
        self.assertEqual(menus[1][0], "Pulsar Inference Stack")

    def test_inventory_choice_opens_the_service_menu(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["1", "6"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory menu"])

    def test_ctrl_c_at_the_home_menu_exits_with_130(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["3", "ctrl-c"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.menus()), 2)


if __name__ == "__main__":
    unittest.main()
