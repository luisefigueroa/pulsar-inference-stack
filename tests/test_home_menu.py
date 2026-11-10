"""Interactive ./pulsar menu: the intent home, its status from saved records, and Help."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HOME = ["Pulsar Inference Stack", "Serve a model…", "Running services…", "Models and disk space…", "Cluster…",
        "Help and troubleshooting…", "Exit"]
FIRST_RUN = ["Pulsar Inference Stack", "Set up this cluster…", "Browse the catalog (read-only)",
             "Help and troubleshooting…", "Exit"]
HELP = ["Help and troubleshooting", "Run diagnostics (checks nodes now)", "What the words mean",
        "What the labels mean", "How serving works", "Command reference", "Advanced: single operations…", "Back"]
ADVANCED = ["Advanced: single operations", "Catalog and storage", "Model files and disk space (checks nodes now)",
            "Live service inventory", "Cluster topology", "Archive storage configuration", "Back"]
sys.path.insert(0, str(ROOT))
from tests.test_cluster_menu import FORBIDDEN  # noqa: E402


def node(rank, hostname, node_id):
    return {"rank": rank, "hostname": hostname, "node_id": node_id}


def enrolled_two_node(topology_id="a" * 64):
    return {
        "schema_version": 2,
        "topology_id": topology_id,
        "nodes": [node(0, "fixture-local", "node-0"), node(1, "fixture-remote", "node-1")],
        "validation": {"ssh_identity_enrolled": True},
    }


def unenrolled_two_node():
    return {"schema_version": 1, "topology_id": "b" * 64,
            "nodes": [node(0, "fixture-local", "node-0"), node(1, "fixture-remote", "node-1")]}


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


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class MenuHarness(unittest.TestCase):
    """The home menu with doubles for ui.sh, pulsar, the catalog and node tools."""

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
        # option index, its label, "esc" or "ctrl-c"; a menu past the answers is an error.
        (scripts / "ui.sh").write_text(r'''
emit_frame() { cat; }
_ui_terminal_columns() { return 1; }
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
    [0-9]*) printf '%s\n' "$answer" ;;
    *)
      local index=0 option
      shift
      for option in "$@"; do
        [ "$option" != "$answer" ] || { printf '%s\n' "$index"; return 0; }
        index=$((index+1))
      done
      echo "UNEXPECTED MENU ANSWER: $answer" >> "$CHOOSE_LOG"; return 1 ;;
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
        # Every menu action is a pulsar command or a menu script; the double
        # records it and exits with the next PULSAR_RC status (default 0).
        pulsar = self.root / "pulsar"
        pulsar.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PULSAR_LOG"\n'
                          'n=$(wc -l < "$PULSAR_LOG")\n'
                          'rc=$(printf "%s" "${PULSAR_RC:-0}" | cut -d, -f"$n")\n'
                          'case "$*" in "topology setup")\n'
                          '  if [ "${rc:-0}" = 0 ] && [ -n "${SETUP_TOPOLOGY:-}" ]; then\n'
                          '    cp "$SETUP_TOPOLOGY" .cluster-topology.json\n'
                          '  fi ;;\n'
                          '  "topology forget") [ "${rc:-0}" != 0 ] || rm -f .cluster-topology.json ;;\n'
                          '  "models menu") [ -z "${CATALOG_AFTER:-}" ] || cp "$CATALOG_AFTER" "$CATALOG_FILE" ;;\n'
                          # Each inventory and budget run saves its observation.
                          '  "inventory menu") [ -z "${INVENTORY_AFTER:-}" ] \\\n'
                          '    || cp "$INVENTORY_AFTER" "$PULSAR_SERVICE_INVENTORY_FILE" ;;\n'
                          '  "models files") [ -z "${BUDGET_AFTER:-}" ] \\\n'
                          '    || cp "$BUDGET_AFTER" "$PULSAR_STORAGE_BUDGET_FILE" ;; esac\n'
                          'exit "${rc:-0}"\n')
        pulsar.chmod(0o755)
        # The Cluster menu and the topology menu are their own scripts.
        for name, word in (("cluster-menu.sh", "cluster menu"), ("topology.sh", "topology"),
                           ("serve-menu.sh", "serve menu")):
            script = scripts / name
            script.write_text(f'#!/usr/bin/env bash\necho "{word} $*" >> "$PULSAR_LOG"\n'
                              '[ -z "${CHANGES_AFTER:-}" ] || cp "$CHANGES_AFTER" "$PULSAR_CLUSTER_CHANGES_FILE"\n'
                              'exit "${MENU_RC:-0}"\n')
            script.chmod(0o755)
        for name in ("detect-fabric.sh", "topology-ssh-trust.sh", "cluster-overview.sh"):
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
        (self.root / "saved").mkdir()
        self.saved_inventory = self.root / "saved/service-inventory.json"
        self.saved_budget = self.root / "saved/storage-budget.json"
        self.saved_changes = self.root / "saved/cluster-changes.json"
        python = binary / "python3"
        python.write_text('#!/usr/bin/env bash\n'
                          'if [ "${1:-}" = -m ] && [ "${2:-}" = model_library.catalog ]; then\n'
                          '  shift 2; printf "%s\\n" "$*" >> "$CATALOG_LOG"\n'
                          '  [ "${CATALOG_RC:-0}" = 0 ] || { echo "error: fixture catalog" >&2; exit "$CATALOG_RC"; }\n'
                          '  exec cat "$CATALOG_FILE"\n'
                          'fi\n'
                          # The status runs for real; its arguments are recorded.
                          'if [ "${1:-}" = -m ] && [ "${2:-}" = scripts.cluster_status ] '
                          '&& [ -n "${OBSERVATIONS_LOG:-}" ]; then\n'
                          '  printf "%s\\n" "${*:3}" >> "$OBSERVATIONS_LOG"\n'
                          f'fi\nexec {sys.executable} "$@"\n')
        python.chmod(0o755)
        for name in ("ssh", "docker", "curl", "nvidia-smi", "hf"):
            (binary / name).write_text('#!/usr/bin/env bash\necho "probe: $0" >> "$PULSAR_LOG"\nexit 1\n')
            (binary / name).chmod(0o755)

    def enroll(self, topology=None):
        (self.root / ".cluster-topology.json").write_text(json.dumps(topology or enrolled_two_node()))

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
            # The saved records; absent until a test writes them.
            "PULSAR_SERVICE_INVENTORY_FILE": str(self.saved_inventory),
            "PULSAR_STORAGE_BUDGET_FILE": str(self.saved_budget),
            "PULSAR_CLUSTER_CHANGES_FILE": str(self.saved_changes),
        }
        for name in ("PULSAR_COLD_ROOT", "CLUSTER_TOPOLOGY_FILE", "VLLM_API_KEY", "API_KEY", "PULSAR_COLOR"):
            env.pop(name, None)
        env.update(extra)
        result = subprocess.run(
            ["bash", str(self.root / "scripts/home.sh")],
            env=env, cwd=str(self.root), text=True, capture_output=True, timeout=60,
        )
        self.assertNotIn("UNEXPECTED MENU", self.choose_log.read_text() if self.choose_log.exists() else "")
        return result

    def again(self):
        """Forget the answers and menus of the last run."""
        self.answers.with_suffix(".n").unlink(missing_ok=True)
        self.choose_log.unlink(missing_ok=True)

    def menus(self):
        text = self.choose_log.read_text()
        return [block.splitlines() for block in text.strip().split("\n\n") if block.strip()]

    def commands(self):
        return self.pulsar_log.read_text().splitlines() if self.pulsar_log.exists() else []


class FirstRun(MenuHarness):
    def test_first_run_offers_setup_read_only_browsing_and_help(self):
        result = self.run_home(["Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [FIRST_RUN])
        self.assertEqual(self.commands(), [])
        self.assertIn("Cluster membership is not configured.", result.stdout)
        self.assertIn("Next: Set up this cluster…", " ".join(result.stdout.split()))
        self.assertFalse((self.root / ".env").exists())

    def test_set_up_this_cluster_runs_guided_setup(self):
        result = self.run_home(["Set up this cluster…", "Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertNotIn("Setup step did not complete", result.stdout)
        self.assertEqual(len(self.menus()), 2)

    def test_saved_membership_without_identities_is_the_same_setup_choice(self):
        self.enroll(unenrolled_two_node())
        result = self.run_home(["0", "Exit"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], FIRST_RUN)
        # Guided setup reuses the saved membership and records the identities.
        self.assertEqual(self.commands(), ["topology setup"])

    def test_invalid_membership_offers_start_over_instead_of_setup(self):
        (self.root / ".cluster-topology.json").write_text("{broken")
        result = self.run_home(["Start over: forget this cluster…", "Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], ["Pulsar Inference Stack", "Start over: forget this cluster…",
                                           *FIRST_RUN[2:]])
        self.assertEqual(self.commands(), ["topology forget"])
        text = " ".join(result.stdout.split())
        self.assertIn("Saved cluster membership is invalid, and setup cannot replace it.", text)
        self.assertIn("Next: Start over: forget this cluster…", text)
        # After forget, home is back at the first run, offering setup.
        self.assertEqual(self.menus()[1], FIRST_RUN)
        self.assertNotIn("./pulsar topology forget", result.stdout)

    def test_catalog_details_are_read_only_and_return_to_the_menu(self):
        result = self.run_home(["Browse the catalog (read-only)", "Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["models menu --read-only"])
        menus = self.menus()
        self.assertEqual(len(menus), 2)
        self.assertEqual(menus[0], menus[1])

    def test_first_run_catalog_error_can_be_interrupted(self):
        result = self.run_home(["1", "ctrl-c"], PULSAR_RC="2")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.menus()[1], ["Catalog error", "Continue"])
        self.assertEqual(len(self.menus()), 2)

    def test_a_failed_setup_step_is_reported_before_the_menu_returns(self):
        result = self.run_home(["0", "Exit"], PULSAR_RC="1")
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

    def test_ctrl_c_during_read_only_inspection_exits(self):
        result = self.run_home(["1"], PULSAR_RC="130")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.menus()), 1)

    def test_completing_setup_opens_the_intent_home_without_saving_archives(self):
        topology = self.root / "enrolled.json"
        topology.write_text(json.dumps(enrolled_two_node()))
        result = self.run_home(["0", "Exit"], SETUP_TOPOLOGY=str(topology))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology setup"])
        self.assertEqual([self.menus()[0], self.menus()[1]], [FIRST_RUN, HOME])
        self.assertFalse((self.root / ".env").exists())
        # The changed status is printed again before the new home.
        self.assertEqual(result.stdout.count("Topology  not confirmed"), 1)
        self.assertEqual(result.stdout.count("Cluster    2 nodes"), 1)
        self.assertLess(result.stdout.index("Topology  not confirmed"), result.stdout.index("Cluster    2 nodes"))


class Help(MenuHarness):
    def test_help_and_troubleshooting_runs_diagnostics_and_opens_each_guide(self):
        pages = self.root / "pages.log"
        result = self.run_home(["Help and troubleshooting…", "0", "1", "2", "3", "4", "Back", "Exit"],
                               PAGE_LOG=str(pages))
        self.assertEqual(result.returncode, 0, result.stderr)
        menus = self.menus()
        self.assertEqual(menus[1], HELP)
        self.assertEqual([menu[0] for menu in menus],
                         ["Pulsar Inference Stack"] + ["Help and troubleshooting"] * 6 + ["Pulsar Inference Stack"])
        self.assertEqual(self.commands(), ["doctor", "help terms", "help labels", "help workflow", "help"])
        # Every page goes to the pager; only diagnostics, which probe nodes, show a spinner.
        self.assertEqual(pages.read_text(), "Running diagnostics…\n\n\n\n\n")
        self.assertNotIn("probe:", self.pulsar_log.read_text())

    def test_help_returns_home_on_esc_and_a_failed_page_returns_to_help(self):
        self.enroll()
        result = self.run_home(["Help and troubleshooting…", "1", "esc", "Exit"], PULSAR_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["help terms"])
        self.assertEqual([menu[0] for menu in self.menus()],
                         ["Pulsar Inference Stack", "Help and troubleshooting", "Help and troubleshooting",
                          "Pulsar Inference Stack"])
        self.assertEqual(self.menus()[-1], HOME)

    def test_ctrl_c_in_help_or_its_page_exits_130(self):
        for answers, rc, menus in ((["Help and troubleshooting…", "ctrl-c"], "0", 2),
                                   (["Help and troubleshooting…", "1"], "130", 2)):
            with self.subTest(answers=answers):
                self.again()
                result = self.run_home(answers, PULSAR_RC=rc)
                self.assertEqual(result.returncode, 130, result.stderr)
                self.assertEqual(len(self.menus()), menus)

    def test_advanced_holds_todays_menus_unchanged(self):
        self.enroll()
        result = self.run_home(["Help and troubleshooting…", "Advanced: single operations…",
                                "Catalog and storage", "Model files and disk space (checks nodes now)",
                                "Live service inventory", "Cluster topology", "Archive storage configuration",
                                "Back", "Back", "Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[2], ADVANCED)
        self.assertEqual(self.commands(), ["models menu", "models files", "inventory menu", "topology menu",
                                           "configure archive-root menu"])
        self.assertEqual([menu[0] for menu in self.menus()][-3:],
                         ["Advanced: single operations", "Help and troubleshooting", "Pulsar Inference Stack"])

    def test_advanced_before_setup_reads_saved_membership_only(self):
        pages = self.root / "pages.log"
        result = self.run_home(["Help and troubleshooting…", "Advanced: single operations…",
                                "Cluster topology (read-only)", "Back", "Back", "Exit"], PAGE_LOG=str(pages))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[2], ["Advanced: single operations", "Cluster topology (read-only)", "Back"])
        self.assertEqual(self.commands(), ["topology show"])
        self.assertEqual(pages.read_text(), "\n")

    def test_a_catalog_error_under_advanced_waits_before_the_menu_returns(self):
        self.enroll()
        result = self.run_home(["Help and troubleshooting…", "Advanced: single operations…",
                                "Catalog and storage", "0", "Back", "Back", "Exit"], PULSAR_RC="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[3], ["Catalog error", "Continue"])
        self.assertIn("✗ The catalog could not be displayed; review the error above.\n  exit 2\n", result.stdout)


class Home(MenuHarness):
    def setUp(self):
        super().setUp()
        self.enroll()

    def test_home_offers_the_five_intents_and_exit(self):
        result = self.run_home(["Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [HOME])
        # Opening home probes nothing: no command, and the catalog is listed once.
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.catalog_log.read_text(), "list --json\n")
        self.assertEqual(list((self.root / "saved").iterdir()), [])

    def test_each_intent_opens_its_flow_or_todays_menu_until_later_phases_replace_it(self):
        result = self.run_home(["Serve a model…", "Running services…", "Models and disk space…", "Cluster…", "Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # Serve a model… answers with a plan; today's catalog menu stays under Advanced.
        self.assertEqual(self.commands(), ["serve menu ", "inventory menu", "models files", "cluster menu "])
        self.assertEqual(self.menus(), [HOME] * 5)

    def test_serve_a_model_returns_home_and_ctrl_c_there_exits(self):
        result = self.run_home(["Serve a model…"], MENU_RC="130")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.commands(), ["serve menu "])

    def test_ctrl_c_at_home_or_in_a_menu_exits_with_130(self):
        result = self.run_home(["Cluster…", "ctrl-c"])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(self.menus()), 2)
        self.again()
        result = self.run_home(["Cluster…"], MENU_RC="130")
        self.assertEqual(result.returncode, 130, result.stderr)

    def test_unset_archives_do_not_restrict_home(self):
        result = self.run_home(["Exit"], PULSAR_COLD_ROOT="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [HOME])
        self.assertNotIn("Attention", result.stdout)

    def test_returning_home_does_not_reprint_an_unchanged_status(self):
        result = self.run_home(["Help and troubleshooting…", "Back", "Running services…", "Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.menus()), 4)
        self.assertEqual(result.stdout.count("Cluster    2 nodes"), 1)


def saved_inventory(generated_at=None):
    """A saved service inventory: example/model runs on fixture-remote, taken now."""
    from tests.test_inventory_menu import snapshot
    document = snapshot()
    document["generated_at"] = generated_at or utc(datetime.now(timezone.utc))
    document["nodes"]["head"].update(mem_available_gib=15.2, mem_total_gib=121.7)
    document["nodes"]["worker"].update(mem_available_gib=19.4, mem_total_gib=121.7)
    document["services"][0]["ranks"][0]["observed_api_port"] = 8000
    return json.dumps(document)


def saved_budget(saved_at=None):
    """A saved storage budget for both fixture nodes, saved now."""
    G = 1024 ** 3
    nodes = [{"node_id": f"node-{index}", "available": 300 * G, "used": 50 * G, "total": 1000 * G,
              "reserve": 100 * G, "limit": 200 * G, "path": "/fixture/views"} for index in (0, 1)]
    return json.dumps({"schema_version": 1, "kind": "pulsar-saved-storage-budget",
                       "saved_at": saved_at or utc(datetime.now(timezone.utc)), "nodes": nodes})


def saved_changes(topology_id="a" * 64, *, observed_at=None, missing=(), check=None, kept=False):
    """A changes noticed record for the fixture membership, observed now."""
    record = {"schema_version": 1, "kind": "pulsar-cluster-changes", "topology_id": topology_id,
              "observed_at": observed_at or utc(datetime.now(timezone.utc)),
              "result": "changed" if missing else "unchanged", "issues": [], "found": [], "changed": [],
              "untrusted": [],
              "missing": [{"hostname": name, "node_id": f"id-{name}", "position": 1, "answered": False}
                          for name in missing]}
    if check is not None:
        record["check"] = {"checked_at": record["observed_at"], "links": "verified", "identities": "verified",
                           "nodes": check}
    if kept:
        from scripts.saved_observations import changes_signature
        record["kept"] = {"kept_at": record["observed_at"], "signature": changes_signature(record),
                          "missing": [f"id-{name}" for name in missing]}
    return json.dumps(record)


def checked(name, answered=True, failures=(), warnings=()):
    return {"hostname": name, "node_id": name, "answered": answered, "failures": list(failures),
            "warnings": list(warnings)}


class StatusBlock(MenuHarness):
    """Home's status: Cluster, Serving, Disk and Attention, from saved records only."""

    def setUp(self):
        super().setUp()
        self.enroll()
        self.catalog.write_text(json.dumps({"entries": [{**never_checked("a"), "model_id": "example/model"}]}))

    def block(self, result):
        """The status block as printed, one string with single spaces."""
        return " ".join(result.stdout.split())

    def test_before_any_observation_each_line_names_the_choice_that_makes_one(self):
        result = self.run_home(["Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1:4], [
            "Cluster    2 nodes · not checked yet; Cluster… looks",
            "Serving    not observed yet; Running services… looks",
            "Disk       not observed yet; Models and disk space… reads it"])
        self.assertNotIn("Attention", result.stdout)
        self.assertEqual(self.commands(), [])
        self.assertEqual(list((self.root / "saved").iterdir()), [])

    def test_saved_records_give_each_line_with_its_age_without_a_probe(self):
        self.saved_inventory.write_text(saved_inventory())
        self.saved_budget.write_text(saved_budget())
        self.saved_changes.write_text(saved_changes(check=[checked("fixture-local"), checked("fixture-remote")]))
        result = self.run_home(["Exit"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.block(result)
        self.assertIn("Cluster 2 nodes · all answered less than a minute ago", text)
        # The saved inventory holds no API URL: the port, and where to see more.
        self.assertIn("Serving example/model running on fixture-remote · port 8000 · seen less than a minute ago; "
                      "Running services… has the details", text)
        self.assertIn("Disk room for 150 GiB on fixture-local, fixture-remote · checked less than a minute ago", text)
        self.assertNotIn("Attention", text)
        self.assertEqual(self.commands(), [])
        self.assertNotIn("probe:", self.pulsar_log.read_text() if self.pulsar_log.exists() else "")
        self.assertEqual(self.catalog_log.read_text(), "list --json\n")

    def test_an_old_inventory_says_its_age_once_and_none_running_says_so(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.saved_inventory.write_text(saved_inventory(utc(old)))
        result = self.run_home(["Exit"])
        self.assertIn("Serving example/model running on fixture-remote · port 8000 · seen 30 minutes ago",
                      self.block(result))
        self.assertNotIn("last seen", result.stdout)
        document = json.loads(saved_inventory())
        document["services"] = []
        self.saved_inventory.write_text(json.dumps(document))
        self.again()
        result = self.run_home(["Exit"])
        self.assertIn("Serving none running · seen less than a minute ago", self.block(result))

    def test_only_exited_containers_lead_with_none_running(self):
        document = json.loads(saved_inventory(utc(datetime.now(timezone.utc) - timedelta(hours=6))))
        document["services"][0]["state"] = "stale"
        self.saved_inventory.write_text(json.dumps(document))
        result = self.run_home(["Exit"])
        text = self.block(result)
        self.assertIn("Serving none running · exited containers of example/model remain on fixture-remote · "
                      "seen 6 hours ago", text)
        self.assertNotIn("last seen", text)
        self.assertNotIn("port 8000", text)
        # A node the inventory did not observe may run a service: no "none running".
        document["nodes"]["head"].update(probe_status="unreachable", probe_reason="ssh timed out")
        self.saved_inventory.write_text(json.dumps(document))
        self.again()
        text = self.block(self.run_home(["Exit"]))
        self.assertIn("Serving exited containers of example/model remain on fixture-remote · seen 6 hours ago; "
                      "others unknown, fixture-local not observed", text)
        self.assertNotIn("none running", text)

    def test_a_node_that_did_not_answer_is_under_attention_and_unknown_is_not_absent(self):
        document = json.loads(saved_inventory())
        document["services"] = []
        document["nodes"]["worker"].update(probe_status="unreachable", probe_reason="ssh timed out")
        self.saved_inventory.write_text(json.dumps(document))
        self.saved_changes.write_text(saved_changes(missing=["fixture-remote"]))
        result = self.run_home(["Exit"])
        text = self.block(result)
        self.assertIn("Serving unknown; fixture-remote not observed", text)
        self.assertIn("Cluster 2 nodes · 1 of 2 answered less than a minute ago", text)
        self.assertIn("Attention fixture-remote did not answer (less than a minute ago); Cluster… → Review what "
                      "changed…", text)

    def test_a_node_the_last_check_could_not_observe_is_not_counted_as_silent(self):
        # Without the membership check, the check saves no answer: unknown, not "did not answer".
        self.saved_changes.write_text(saved_changes(check=[checked("fixture-local", answered=None),
                                                           checked("fixture-remote", answered=None)]))
        result = self.run_home(["Exit"])
        text = self.block(result)
        self.assertIn("Cluster 2 nodes · not observed at the last check less than a minute ago; Cluster… looks "
                      "again", text)
        self.assertNotIn("0 of 2", text)
        self.assertNotIn("Attention", text)
        self.saved_changes.write_text(saved_changes(check=[checked("fixture-local"),
                                                           checked("fixture-remote", answered=None)]))
        self.again()
        self.assertIn("Cluster 2 nodes · 1 of 2 answered, 1 not observed less than a minute ago",
                      self.block(self.run_home(["Exit"])))

    def test_a_kept_node_stays_under_attention(self):
        self.saved_changes.write_text(saved_changes(missing=["fixture-remote"], kept=True))
        result = self.run_home(["Exit"])
        self.assertIn("fixture-remote did not answer (less than a minute ago); kept as only down", self.block(result))

    def test_a_record_of_an_earlier_membership_is_not_shown_as_current(self):
        self.saved_changes.write_text(saved_changes("e" * 64, missing=["fixture-remote"],
                                                    check=[checked("fixture-local")]))
        result = self.run_home(["Exit"])
        text = self.block(result)
        self.assertIn("Cluster 2 nodes · not checked since membership was saved; Cluster… looks", text)
        self.assertNotIn("Attention", text)
        self.assertNotIn("did not answer", text)

    def test_the_last_checks_blocking_issues_and_an_unreadable_archive_setting_need_attention(self):
        self.saved_changes.write_text(saved_changes(check=[
            checked("fixture-local", warnings=["hf CLI not found"]),
            checked("fixture-remote", failures=["rsync not found; install rsync on this node"])]))
        result = self.run_home(["Exit"])
        text = self.block(result)
        self.assertIn("Attention fixture-remote: rsync not found; install rsync on this node (less than a minute "
                      "ago); Cluster… has the details", text)
        # A warning, such as a missing hf CLI, is not an attention item.
        self.assertNotIn("hf CLI", text)
        (self.root / "elsewhere.env").write_text("PULSAR_COLD_ROOT=/var/tmp/archives\n")
        (self.root / ".env").symlink_to(self.root / "elsewhere.env")
        self.again()
        result = self.run_home(["Exit"])
        self.assertIn("Attention the archive location setting could not be read; Cluster… → Settings… says how to "
                      "fix it", self.block(result))

    def test_attention_shows_two_items_and_counts_the_rest(self):
        self.saved_changes.write_text(saved_changes(missing=["fixture-remote", "fixture-third", "fixture-fourth"]))
        result = self.run_home(["Exit"])
        self.assertIn("and 1 more; Cluster… shows them", self.block(result))

    def test_the_block_fits_with_the_home_menu_on_80_by_24(self):
        # The worst case: three nodes with long names, two services, a disk
        # row per node, and more changes than Attention shows.
        topology = enrolled_two_node()
        topology["nodes"] = [node(index, f"gb10-spark-workstation-{index}", f"node-{index}") for index in range(3)]
        self.enroll(topology)
        document = json.loads(saved_inventory())
        second = json.loads(json.dumps(document["services"][0]))
        second.update(service_id="b" * 64, conf="b" * 64)
        document["services"].append(second)
        document["nodes"]["head"]["hostname"] = "gb10-spark-workstation-0"
        document["nodes"]["worker"]["hostname"] = "gb10-spark-workstation-1"
        self.saved_inventory.write_text(json.dumps(document))
        G = 1024 ** 3
        self.saved_budget.write_text(json.dumps({**json.loads(saved_budget()), "nodes": [
            {"node_id": f"node-{index}", "available": (300 + 70 * index) * G, "used": 50 * G, "total": 1000 * G,
             "reserve": 100 * G, "limit": 900 * G, "path": "/fixture/views"} for index in range(3)]}))
        self.saved_changes.write_text(saved_changes(missing=["gb10-spark-workstation-2"], check=[
            checked("gb10-spark-workstation-1", failures=["rsync not found; install rsync on this node"]),
            checked("gb10-spark-workstation-0", answered=False)]))
        result = self.run_home(["Exit"], COLUMNS="80")
        lines = result.stdout.splitlines()
        self.assertTrue(all(len(line) <= 80 for line in lines), result.stdout)
        # A blank line, the block, then the menu: Gum adds 5 rows to its 6 choices.
        self.assertLessEqual(len(lines) + 5 + 6, 24, result.stdout)
        self.assertEqual([line.split()[0] for line in lines if line and not line.startswith(" ")],
                         ["Cluster", "Serving", "Disk", "Attention"])

    def test_narrow_terminals_wrap_every_line_inside_the_width(self):
        self.saved_inventory.write_text(saved_inventory())
        self.saved_budget.write_text(saved_budget())
        self.saved_changes.write_text(saved_changes(missing=["fixture-remote"]))
        for width in (60, 44):
            with self.subTest(width=width):
                self.again()
                result = self.run_home(["Exit"], COLUMNS=str(width))
                self.assertTrue(all(len(line) <= width for line in result.stdout.splitlines()), result.stdout)
                self.assertIn("Running services… has the details", self.block(result))

    def test_the_block_prints_again_only_when_a_saved_record_changes(self):
        self.saved_inventory.write_text(saved_inventory())
        result = self.run_home(["Help and troubleshooting…", "Back", "Exit"])
        self.assertEqual(result.stdout.count("Serving"), 1)
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        changes = self.root / "changes.json"
        changes.write_text(saved_changes(missing=["fixture-remote"]))
        for choice, name, content in (
                ("Running services…", "INVENTORY_AFTER", saved_inventory(utc(later))),
                ("Models and disk space…", "BUDGET_AFTER", saved_budget("2030-01-01T00:00:00Z")),
                ("Cluster…", "CHANGES_AFTER", saved_changes(missing=["fixture-remote"]))):
            with self.subTest(choice=choice):
                after = self.root / "after.json"
                after.write_text(content)
                self.again()
                self.saved_changes.unlink(missing_ok=True)
                result = self.run_home([choice, "Exit"], **{name: str(after)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("Serving"), 2, result.stdout)

    def test_one_call_reads_the_records_at_one_instant(self):
        log = self.root / "observations.log"
        result = self.run_home(["Exit"], OBSERVATIONS_LOG=str(log))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [line.split() for line in log.read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ["home"])
        self.assertIn("--now", calls[0])
        self.assertNotIn("key\t", result.stdout)


class Vocabulary(MenuHarness):
    """Every choice label outside Advanced names what the operator wants (design rule 1)."""

    def labels(self):
        """Each choice of every menu the runs drew, outside Advanced and the menus it opens."""
        found = []
        for menu in self.menus():
            if menu[0].startswith("Advanced"):
                continue
            found += menu[1:]
        return found

    def test_no_choice_outside_advanced_uses_a_low_level_term(self):
        runs = (
            (None, ["Help and troubleshooting…", "Back", "Exit"]),
            ("{broken", ["Exit"]),
            (json.dumps(unenrolled_two_node()), ["Exit"]),
            (json.dumps(enrolled_two_node()), ["Help and troubleshooting…", "Back", "Exit"]),
        )
        labels = []
        for topology, answers in runs:
            with self.subTest(topology=topology and topology[:8]):
                self.again()
                path = self.root / ".cluster-topology.json"
                path.unlink(missing_ok=True)
                if topology:
                    path.write_text(topology)
                result = self.run_home(answers)
                self.assertEqual(result.returncode, 0, result.stderr)
                labels += self.labels()
        # The Cluster menu, Review and Settings are checked in tests.test_cluster_menu.
        self.assertIn("Set up this cluster…", labels)
        self.assertIn("Start over: forget this cluster…", labels)
        self.assertIn("Serve a model…", labels)
        self.assertEqual([label for label in labels if FORBIDDEN.search(label)], [])

    def test_the_rule_catches_each_forbidden_term(self):
        for label in ("Remove home", "Pin prepared copies", "Purge", "Prepared copies", "rank 1", "Snapshot",
                      "Enroll SSH trust", "Show 5e5e5e5e5e5e"):
            self.assertRegex(label, FORBIDDEN)
        for label in ("Models and disk space…", "Help and troubleshooting…", "Set up this cluster…"):
            self.assertNotRegex(label, FORBIDDEN)


class WithoutGum(unittest.TestCase):
    """Without Gum the home menu names the commands behind its choices; nothing runs."""

    def test_each_home_names_its_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            topology = Path(temporary) / "topology.json"
            for document, expected in ((enrolled_two_node(), "./pulsar inventory menu"),
                                       (None, "./pulsar topology setup"),
                                       ("{broken", "./pulsar topology forget")):
                with self.subTest(expected=expected):
                    topology.unlink(missing_ok=True)
                    if document is not None:
                        topology.write_text(document if isinstance(document, str) else json.dumps(document))
                    env = {key: value for key, value in os.environ.items()
                           if not key.startswith(("PULSAR_", "GUM_"))}
                    env.update(PULSAR_FORCE_MENU="1", TERM="dumb", CLUSTER_TOPOLOGY_FILE=str(topology),
                               PULSAR_COLD_ROOT="")
                    result = subprocess.run(["bash", str(ROOT / "scripts/home.sh")], env=env, text=True,
                                            capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error: the Pulsar menu needs an interactive terminal with Gum", result.stderr)
                    self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
