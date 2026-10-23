"""Interactive ./pulsar menu: cluster setup and optional archive configuration."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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


def never_checked(prefix):
    """A catalog projection entry with no saved check."""
    return {"spec_id": prefix * 64, "model_id": "org/model", "snapshot_revision": "0" * 40,
            "geometry": {"nodes": 1, "tp": 1, "pp": 1}, "local_state": "unknown", "archive_state": "unknown",
            "checked_at": None, "observation_age_seconds": None, "blockers": [], "home": None,
            "archive": None, "prepared_copies": []}


def ready(prefix):
    """An entry whose saved check, two hours old, found every file prepared."""
    return {**never_checked(prefix), "local_state": "ready", "checked_at": "2026-09-05T00:00:00Z",
            "observation_age_seconds": 7200,
            "home": {"node_id": "n0", "path": "/fixture/home", "verified_at": "2026-09-05T00:00:00Z"}}


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
ui_keep_styles() { :; }
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
# Long output: PAGE_LOG records each spinner title ("" for none).
page_output() { [ -z "${PAGE_LOG:-}" ] || printf '%s\n' "$1" >> "$PAGE_LOG"; shift; "$@"; }
# The real hold: one menu with a single Continue.
wait_continue() {
  local rc=0
  choose_index "${1:-Review the output above}" "Continue" >/dev/null || rc=$?
  [ "$rc" -ne 130 ] || return 130
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
                          '  fi ;;\n'
                          '  "models menu") [ -z "${CATALOG_AFTER:-}" ] || cp "$CATALOG_AFTER" "$CATALOG_FILE" ;; esac\n'
                          'exit "${rc:-0}"\n')
        pulsar.chmod(0o755)
        overview = scripts / "cluster-overview.sh"
        overview.write_text('#!/usr/bin/env bash\necho "cluster overview" >> "$PULSAR_LOG"\nexit "${OVERVIEW_RC:-0}"\n')
        overview.chmod(0o755)
        for name in ("detect-fabric.sh", "topology-ssh-trust.sh"):
            probe = scripts / name
            probe.write_text('#!/usr/bin/env bash\necho "probe: $0" >> "$PULSAR_LOG"\nexit 1\n')
            probe.chmod(0o755)
        # The catalog is a double, so the menu never reads this checkout's
        # releases or saved records: `catalog list --json` prints CATALOG_FILE
        # (an empty catalog by default) or fails with CATALOG_RC. Node tools
        # record a probe.
        binary = self.root / "bin"
        binary.mkdir()
        self.catalog_log = self.root / "catalog.log"
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"entries": []}))
        python = binary / "python3"
        python.write_text('#!/usr/bin/env bash\n'
                          'if [ "${1:-}" = -m ] && [ "${2:-}" = model_library.catalog ]; then\n'
                          '  shift 2; printf "%s\\n" "$*" >> "$CATALOG_LOG"\n'
                          '  [ "${CATALOG_RC:-0}" = 0 ] || { echo "error: fixture catalog" >&2; exit "$CATALOG_RC"; }\n'
                          '  exec cat "$CATALOG_FILE"\n'
                          f'fi\nexec {sys.executable} "$@"\n')
        python.chmod(0o755)
        for name in ("ssh", "docker", "curl", "nvidia-smi"):
            (binary / name).write_text('#!/usr/bin/env bash\necho "probe: $0" >> "$PULSAR_LOG"\nexit 1\n')
            (binary / name).chmod(0o755)

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
            "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CATALOG_LOG": str(self.catalog_log),
            "CATALOG_FILE": str(self.catalog),
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
        result = self.run_home(['0', '0', '7'], PULSAR_RC='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ['models menu'])
        self.assertEqual(self.menus()[1], ['Catalog error', 'Continue'])
        self.assertEqual(len(self.menus()), 3)
        self.assertIn('✗ The catalog could not be displayed; review the error above.\n  exit 2\n', result.stdout)

    def test_first_run_catalog_error_can_be_interrupted(self):
        result = self.run_home(['1', 'ctrl-c'], PULSAR_RC='2')
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.menus()[1], ['Catalog error', 'Continue'])
        self.assertEqual(len(self.menus()), 2)

    def test_inspection_and_help_are_available_before_setup(self):
        pages = self.root / "pages.log"
        # Help → Command reference, then Back from Help.
        result = self.run_home(["2", "3", "4", "2", "3", "5"], PULSAR_RC="1,2,0", PAGE_LOG=str(pages))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["doctor", "topology show", "help"])
        # Their output opens in the pager; only diagnostics, which probe nodes, show a spinner.
        self.assertEqual(pages.read_text(), "Running host diagnostics…\n\n\n")
        self.assertEqual([menu[0] for menu in self.menus()],
                         ["Pulsar Inference Stack"] * 3 + ["Help"] * 2 + ["Pulsar Inference Stack"])
        self.assertNotIn("Setup step did not complete", result.stdout)
        self.assertFalse((self.root / ".cluster-topology.json").exists())
        self.assertFalse((self.root / ".env").exists())

    def test_help_opens_each_guide_page_and_returns_to_help(self):
        pages = self.root / "pages.log"
        result = self.run_home(["4", "0", "1", "2", "3", "5"], PAGE_LOG=str(pages))
        self.assertEqual(result.returncode, 0, result.stderr)
        menus = self.menus()
        self.assertEqual(menus[1], ["Help", "What the words mean", "How serving works", "Command reference", "Back"])
        self.assertEqual([menu[0] for menu in menus],
                         ["Pulsar Inference Stack"] + ["Help"] * 4 + ["Pulsar Inference Stack"])
        self.assertEqual(self.commands(), ["help terms", "help workflow", "help"])
        # Every page goes to the pager without a spinner; none probes a node.
        self.assertEqual(pages.read_text(), "\n\n\n")
        self.assertNotIn("probe:", self.pulsar_log.read_text())

    def test_help_from_the_full_menu_returns_home_on_esc(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["6", "1", "esc", "7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["help workflow"])
        self.assertEqual([menu[0] for menu in self.menus()],
                         ["Pulsar Inference Stack", "Help", "Help", "Pulsar Inference Stack"])
        self.assertEqual(self.menus()[-1][1], "Catalog and storage")

    def test_a_failed_help_page_returns_to_help(self):
        result = self.run_home(["4", "0", "3", "5"], PULSAR_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["help terms"])
        self.assertEqual([menu[0] for menu in self.menus()],
                         ["Pulsar Inference Stack", "Help", "Help", "Pulsar Inference Stack"])

    def test_ctrl_c_in_help_or_its_page_exits_130(self):
        for answers, rc, menus in ((["4", "ctrl-c"], "0", 2), (["4", "0"], "130", 2)):
            with self.subTest(answers=answers):
                self.answers.with_suffix(".n").unlink(missing_ok=True)
                self.choose_log.unlink(missing_ok=True)
                result = self.run_home(answers, PULSAR_RC=rc)
                self.assertEqual(result.returncode, 130, result.stderr)
                self.assertEqual(len(self.menus()), menus)

    def test_a_failed_setup_step_is_reported_before_the_menu_returns(self):
        result = self.run_home(["0", "5"], PULSAR_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertIn("✗ Setup step did not complete; details above", result.stdout)
        # The unchanged status is not printed again, so the report stays above the menu.
        self.assertEqual(result.stdout.count("Cluster membership is not configured."), 1)
        self.assertLess(result.stdout.index("Cluster membership is not configured."),
                        result.stdout.index("✗ Setup step"))

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
        result = self.run_home(["0", "1", "3", "6", "2", "3", "7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0][1], "Catalog and storage")
        self.assertEqual(self.commands(), ["models menu", "inventory menu", "doctor", "help"])
        self.assertNotIn("not bound", result.stdout)
        self.assertFalse((self.root / ".env").exists())

    def test_completing_cluster_setup_opens_full_menu_without_saving_archives(self):
        topology = self.root / "enrolled.json"
        topology.write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["0", "7"], SETUP_TOPOLOGY=str(topology))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertEqual(self.menus()[0][1], "Set up cluster membership and SSH trust")
        self.assertEqual(self.menus()[1][1], "Catalog and storage")
        self.assertFalse((self.root / ".env").exists())
        # The changed status is printed again before the full menu.
        self.assertEqual(result.stdout.count("Topology  not confirmed"), 1)
        self.assertEqual(result.stdout.count("Topology  2 nodes"), 1)
        self.assertLess(result.stdout.index("Topology  not confirmed"), result.stdout.index("Topology  2 nodes"))

    def test_returning_home_does_not_reprint_an_unchanged_status(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["3", "6", "2", "3", "7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["doctor", "help"])
        self.assertEqual(len(self.menus()), 5)
        self.assertEqual(result.stdout.count("Topology"), 1)

    def test_unset_archives_remain_an_explicit_configuration_choice(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["4", "7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["configure archive-root menu"])

    def test_ctrl_c_during_read_only_inspection_exits(self):
        result = self.run_home(["1"], PULSAR_RC="130")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.menus()), 1)

    def test_complete_menu_starts_with_catalog_and_archive_opens_menu(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["4", "7"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = self.menus()[0]
        self.assertEqual(options[1], "Catalog and storage")
        self.assertIn("Archive storage configuration", options)
        self.assertEqual(self.commands(), ["configure archive-root menu"])
        # The submenu returns to the home menu instead of ending the session.
        menus = self.menus()
        self.assertEqual(len(menus), 2)
        self.assertEqual(menus[1][0], "Pulsar Inference Stack")

    def test_cluster_overview_runs_only_when_chosen(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["2", "7"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0][1:4], ["Catalog and storage", "Live service inventory",
                                                "Cluster overview (checks nodes now)"])
        self.assertEqual(self.commands(), ["cluster overview"])
        # Entering home probes nothing; the overview's own commands run inside it.
        self.answers.with_suffix(".n").unlink()
        self.choose_log.unlink()
        result = self.run_home(["7"], PULSAR_COLD_ROOT="")
        self.assertEqual(self.commands(), ["cluster overview"])
        self.assertNotIn("probe:", self.pulsar_log.read_text())

    def test_inventory_choice_opens_the_service_menu(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["1", "7"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory menu"])

    def test_ctrl_c_at_the_home_menu_exits_with_130(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["4", "ctrl-c"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.menus()), 2)

    def catalog_entries(self, *entries):
        return json.dumps({"entries": list(entries)})

    def test_next_line_follows_the_setup_status_from_saved_records(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        self.catalog.write_text(self.catalog_entries(never_checked("a"), never_checked("b"), ready("c")))
        result = self.run_home(["7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        line = ("Next: 2 specs never checked · 1 spec ready to Start — open Catalog and storage. "
                "Newest saved check: 2 hours ago.")
        self.assertIn(line, " ".join(result.stdout.split()))
        self.assertLess(result.stdout.index("Topology"), result.stdout.index("Next:"))
        # Saved records only: the catalog is listed once and no node is probed.
        self.assertEqual(self.catalog_log.read_text(), "list --json\n")
        self.assertEqual(self.commands(), [])

    # TUI-07: an unreadable archive setting is named with its fix, and no
    # catalog suggestion is made without it.
    def test_unreadable_archive_setting_replaces_the_next_line(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        (self.root / "elsewhere.env").write_text("PULSAR_COLD_ROOT=/var/tmp/archives\n")
        (self.root / ".env").symlink_to(self.root / "elsewhere.env")
        self.catalog.write_text(self.catalog_entries(never_checked("a")))
        result = self.run_home(["7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Archives setting could not be read — ", text)
        self.assertIn("then open Archive storage configuration to check it", text)
        self.assertNotIn("Next:", text)
        self.assertNotIn("not configured", text)
        self.assertEqual(self.commands(), [])

    def test_next_line_prints_again_only_when_saved_records_change(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        self.catalog.write_text(self.catalog_entries(never_checked("a")))
        result = self.run_home(["3", "7"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("open Catalog and storage"), 1)
        self.assertEqual(result.stdout.count("Topology"), 1)
        after = self.root / "after.json"
        after.write_text(self.catalog_entries(ready("a")))
        self.answers.with_suffix(".n").unlink()
        self.choose_log.unlink()
        result = self.run_home(["0", "7"], CATALOG_AFTER=str(after))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("open Catalog and storage"), 2)
        self.assertLess(result.stdout.index("1 spec never checked"), result.stdout.index("1 spec ready to Start"))
        self.assertEqual(result.stdout.count("Topology"), 2)

    def test_next_line_is_left_out_without_a_suggested_step_or_a_readable_catalog(self):
        (self.root / ".cluster-topology.json").write_text(json.dumps(enrolled_two_node()))
        for fixture, extra, shown in ((self.catalog_entries(never_checked("a")), {}, True),
                                      (self.catalog_entries(), {}, False),
                                      (self.catalog_entries(never_checked("a")), {"CATALOG_RC": "2"}, False)):
            with self.subTest(extra=extra, shown=shown):
                self.catalog.write_text(fixture)
                self.answers.with_suffix(".n").unlink(missing_ok=True)
                result = self.run_home(["7"], **extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("open Catalog and storage" in result.stdout, shown, result.stdout)
                self.assertIn("Topology", result.stdout)
                self.assertNotIn("fixture catalog", result.stdout + result.stderr)
                # The menu still opens; Catalog and storage reports any error.
                self.assertEqual(self.menus()[-1][1], "Catalog and storage")

    def test_the_next_line_appears_once_setup_is_complete(self):
        self.catalog.write_text(self.catalog_entries(never_checked("a")))
        topology = self.root / "enrolled.json"
        topology.write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["0", "7"], SETUP_TOPOLOGY=str(topology))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        # First use reads no catalog; the full menu's status ends with the line.
        self.assertEqual(self.catalog_log.read_text(), "list --json\n")
        self.assertEqual(result.stdout.count("open Catalog and storage"), 1)
        self.assertLess(result.stdout.index("Topology  not confirmed"), result.stdout.index("Topology  2 nodes"))
        self.assertLess(result.stdout.index("Topology  2 nodes"), result.stdout.index("1 spec never checked"))


if __name__ == "__main__":
    unittest.main()
