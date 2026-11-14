"""Check the cluster now: one report from discovery, the membership check, the
SSH identity check, diagnostics with every node's prerequisites, inventory and
model budget, with every saved node shown and a source that failed reported,
never hidden. What it finds is saved beside the changes noticed."""
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import cluster_overview
from scripts.terminal_format import TerminalWriter

ANSI = re.compile(r"\x1b\[[0-9;]*m")
GIB = 1024 ** 3
SERVICE = "5e" * 32
TOPOLOGY_ID = "7" * 64


def changes(**fields):
    return {"topology_id": TOPOLOGY_ID, "result": "unchanged", "issues": [], "found": [], "missing": [],
            "changed": [], "untrusted": [], **fields}


def discovery(**fields):
    noticed = changes(**fields)
    if noticed["found"] or noticed["missing"] or noticed["changed"]:
        noticed["result"] = "changed"
    return {"schema_version": 1, "result": "ok", "changes_noticed": noticed}


def topology(status="ready", issues=(), cluster_issues=()):
    return {"kind": "pulsar-topology-observation", "status": "ready" if status == "ready" else "blocked",
            "topology": {"topology_id": TOPOLOGY_ID}, "issues": list(cluster_issues),
            "nodes": [{"hostname": f"rank-{index}", "node_id": f"node-{index}", "rank": index,
                       "status": status if index == 1 else "ready",
                       "issues": list(issues) if index == 1 else []} for index in range(3)]}


def identities(failed=()):
    return {"kind": "topology-ssh-trust-check", "ok": not failed, "nodes": [
        {"rank": index, "node_id": f"node-{index}", "hostname": f"rank-{index}", "ok": index not in failed,
         "endpoints": [{"ok": index not in failed, "state": "host-key-changed" if index in failed else "pass"}]}
        for index in range(3)]}


def diagnostics():
    checks = [("ok", "python", "Python 3.12"), ("ok", "rsync", "rsync present"),
              ("warn", "hf_cli", "hf CLI not found, so models cannot be downloaded to this node; it can still serve"),
              ("ok", "topology", "Confirmed membership: 3 nodes"),
              ("ok", "rank_1_python", "rank-1: Python 3.12"),
              ("fail", "rank_1_rsync", "rank-1: rsync not found; install rsync on this node"),
              ("ok", "rank_2_python", "rank-2: Python 3.12"),
              ("warn", "api_auth", "API key authentication is not configured"),
              ("ok", "cold_storage", "archive location is configured"),
              ("ok", "catalog", "3 catalog specs readable")]
    return {"kind": "pulsar-doctor", "result": "fail",
            "checks": [{"level": level, "id": identifier, "message": message} for level, identifier, message in checks]}


def inventory():
    keys = ("head", "worker", "rank-2")
    nodes = {key: {"hostname": f"rank-{index}", "node_id": f"node-{index}", "topology_index": index,
                   "local": index == 0, "mem_available_gib": 6.5 + index, "mem_total_gib": 121.69,
                   "mem_status": "ok", "probe_status": "ok", "probe_reason": None}
             for index, key in enumerate(keys)}
    managed = {"service_id": SERVICE, "selected_spec_id": SERVICE, "served_name": "org/example-model",
               "state": "running", "ownership": "managed",
               "ranks": [{"node": key, "gpu_memory": {"measured_mib": 102400}} for key in keys]}
    other = {"service_id": "unknown:head:legacy", "served_name": None, "state": "running", "ownership": "unknown",
             "container_name": "legacy-vllm", "ranks": [{"node": "head", "container_name": "legacy-vllm",
                                                         "gpu_memory": {"measured_mib": None}}]}
    return {"schema_version": 1, "nodes": nodes, "services": [managed, other],
            "unmanaged_gpu_processes": [{"node": "worker", "pid": 4242, "process_name": "/usr/bin/trainer",
                                         "used_memory_mib": 2048}]}


def budget():
    return {"ok": True, "result": {"kind": "pulsar-storage-budget", "nodes": [
        {"node_id": f"node-{index}", "path": "/fixture/views", "available": (300 + index) * GIB,
         "reserve": 100 * GIB, "limit": (200 + index) * GIB, "used": 50 * GIB, "total": 1000 * GIB}
        for index in range(3)]}}


DOCUMENTS = {"discovery": discovery, "topology": topology, "identities": identities, "diagnostics": diagnostics,
             "inventory": inventory, "budget": budget}


def write_sources(directory, **overrides):
    for name, make in DOCUMENTS.items():
        output, status, error = overrides.get(name, (make(), 0, ""))
        (directory / f"{name}.json").write_text(json.dumps(output) if isinstance(output, dict) else (output or ""))
        (directory / f"{name}.rc").write_text(f"{status}\n")
        (directory / f"{name}.err").write_text(error)
    return directory


class Overview(unittest.TestCase):
    def sources(self, **overrides):
        return write_sources(Path(self.enterContext(tempfile.TemporaryDirectory())), **overrides)

    def render(self, directory, width=80, terminal=False, **values):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        out = Terminal() if terminal else io.StringIO()
        clean = {key: value for key, value in os.environ.items()
                 if key not in ("PULSAR_COLOR", "NO_COLOR", "PULSAR_ACCENT", "TERM")}
        with mock.patch.dict(os.environ, {**clean, "TERM": "xterm-256color", **values}, clear=True):
            cluster_overview.render(directory, 1_700_000_000, 1_700_000_019, TerminalWriter(width=width, stream=out))
        return out.getvalue()

    def section(self, text, heading):
        return text.split(f"\n{heading}\n", 1)[1].split("\n\n", 1)[0] + "\n"

    def test_each_node_shows_status_identity_checks_memory_disk_and_services(self):
        text = self.render(self.sources())
        self.assertTrue(text.startswith("Cluster check\n  Nodes     3 saved · ready\n  Checked   "), text)
        self.assertIn("; the checks took 19 s\n  Links     verified between every pair of nodes\n", text)
        self.assertIn("\nChanges noticed\n  none: every member answered as saved\n", text)
        local = self.section(text, "rank-0 (this node)")
        self.assertIn("  Status    ready\n  Identity  verified\n  Checks    2 in place\n"
                      "  WARN      hf CLI not found, so models cannot be downloaded to this node; it\n"
                      "            can still serve\n", local)
        self.assertIn("  Memory    6.50 GiB available of 121.69 GiB\n", local)
        # TUI-80: the storage budget table's terms; the tighter of reserve and copy budget.
        self.assertIn("  Disk      can still copy 150 GiB · 300 GiB free · 50.0 GiB Pulsar files\n", local)
        self.assertIn("  Services  org/example-model · running · spec 5e5e5e5e5e5e · 100.00 GiB GPU\n"
                      "            memory in use\n", local)
        self.assertIn("  Unmanaged container legacy-vllm · running; Pulsar does not manage it", local)
        worker = self.section(text, "rank-1")
        # A remote node's rows leave its name off: the section names it.
        self.assertIn("  Checks    1 in place\n  FAIL      rsync not found; install rsync on this node\n", worker)
        self.assertIn("  Other GPU PID 4242 trainer · 2.00 GiB; Pulsar does not manage it", worker)
        self.assertNotIn("(this node)", worker)
        self.assertIn("  Checks    1 in place, none needs attention\n", self.section(text, "rank-2"))
        settings = self.section(text, "Settings")
        self.assertIn("  API key   API key authentication is not configured; Settings… says where it is\n"
                      "            set\n", settings)
        self.assertIn("  Archives  archive location is configured\n", settings)
        self.assertNotIn("catalog specs", text)
        self.assertTrue(text.endswith("\nResult    1 blocking issue · 2 warnings; each finding above names its "
                                      "fix.\nSame as   ./pulsar topology detect, topology check, ssh-trust "
                                      "check, doctor,\n          inventory and model budget\n"), text)

    def test_changes_noticed_name_their_fix_and_an_untrusted_machine_its_check(self):
        noticed = discovery(missing=[{"hostname": "rank-2", "node_id": "node-2", "position": 2, "answered": False}],
                            found=[{"hostname": "rank-2", "node_id": "node-9", "ssh_host": "rank-2.local",
                                    "possible_reinstall": True}],
                            changed=[{"hostname": "rank-1", "node_id": "node-1",
                                      "changes": {"control": {"old": {}, "new": {}}}}],
                            untrusted=[{"address": "printer.local", "possible_reinstall": False}])
        text = " ".join(self.render(self.sources(discovery=(noticed, 1, ""))).split())
        self.assertIn("rank-2 did not answer; it may be down or gone. Review what changed… below keeps it or saves "
                      "the cluster without it", text)
        self.assertIn("found rank-2, which qualifies but is not a member; Review what changed… below can add it; it "
                      "may be rank-2 reinstalled", text)
        self.assertIn("rank-1: address changed; Review what changed… below shows old against new and saves it", text)
        self.assertIn("printer.local answers, but SSH on this machine does not trust it yet; check its host key with "
                      "ssh -o StrictHostKeyChecking=ask printer.local, then choose Check again", text)
        failed = discovery()
        failed["changes_noticed"].update(result="not-checked", issues=["Pairwise RoCE connectivity failed."])
        text = " ".join(self.render(self.sources(discovery=(failed, 1, ""))).split())
        self.assertIn("discovery did not finish: Pairwise RoCE connectivity failed.. Fix it, then choose Check "
                      "again.", text)

    def test_an_unreachable_node_and_a_changed_identity_name_their_fixes(self):
        directory = self.sources(topology=(topology("blocked", [cluster_overview.UNREACHABLE],
                                                    [cluster_overview.FABRIC_FAILED]), 1, ""),
                                 identities=(identities(failed=(2,)), 1, ""))
        text = self.render(directory)
        joined = " ".join(text.split())
        self.assertIn("Links failed between some nodes; check the cables and the links' addresses, then choose Check "
                      "again", joined)
        self.assertIn("Issue Node could not be checked through its confirmed control endpoint. Check that it is on "
                      "and reachable; if it is gone, Review what changed… can save the cluster without it.", joined)
        self.assertIn("Identity did not match (host-key-changed); if the machine was reinstalled or its host key "
                      "rotated, verify the new key with its console or administrator, then run ./pulsar ssh-trust "
                      "enroll --accept-key-change", joined)
        self.assertIn("Result 4 blocking issues · 2 warnings", joined)

    def test_styles_mark_headings_and_what_needs_attention(self):
        directory = self.sources(topology=(topology("blocked", ["fabric link to rank-2 is down"]), 1, ""))
        for width in (44, 60, 80):
            with self.subTest(width=width):
                plain = self.render(directory, width)
                styled = self.render(directory, width, terminal=True)
                self.assertEqual(ANSI.sub("", styled), plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()), plain)
        styled = self.render(directory, 80, terminal=True)
        self.assertIn("\x1b[1;94mCluster check\x1b[0m\n", styled)
        self.assertIn("\n\x1b[1;94mrank-1\x1b[0m\n", styled)
        self.assertIn("  \x1b[1;31mStatus\x1b[0m    blocked\n  \x1b[1;31mIssue\x1b[0m     fabric link to rank-2 is "
                      "down", styled)
        self.assertIn("  \x1b[33mUnmanaged\x1b[0m container legacy-vllm", styled)
        # A blocked topology still reports every node: its JSON is the answer.
        self.assertNotIn("Not observed", ANSI.sub("", styled))

    def test_a_source_that_failed_is_not_observed_and_named(self):
        directory = self.sources(budget=("", 1, "error: storage lock is busy\n"),
                                 inventory=("not json", 2, ""), diagnostics=("", 3, ""),
                                 identities=("", 2, "error: topology-bound SSH identity is not enrolled\n"))
        text = self.render(directory)
        local = self.section(text, "rank-0 (this node)")
        self.assertIn("  Identity  not observed\n  Checks    not observed\n  Memory    not observed\n"
                      "  Disk      not observed\n  Services  not observed\n", local)
        self.assertIn("\nNot observed\n", text)
        for line in ("  pulsar inventory exited 2 without a result\n",
                     "  pulsar model budget exited 1: storage lock is busy\n",
                     "  pulsar doctor exited 3 without a result\n",
                     "  pulsar ssh-trust check exited 2: topology-bound SSH identity is not enrolled\n"):
            self.assertIn(line, text)
        self.assertIn("4 checks not observed", text)

    def test_a_node_inventory_could_not_inspect_has_unknown_services(self):
        document = inventory()
        document["nodes"]["rank-2"].update(probe_status="unreachable", probe_reason="ssh timed out")
        document["services"] = []
        text = self.render(self.sources(inventory=(document, 0, "")))
        self.assertIn("  Memory    not observed: ssh timed out\n", self.section(text, "rank-2"))
        self.assertIn("  Services  not observed\n", self.section(text, "rank-2"))
        # An inspected node with nothing running really has no services.
        self.assertIn("  Services  none\n", self.section(text, "rank-1"))

    def test_a_mixed_service_labels_each_rank_by_its_own_ownership(self):
        document = inventory()
        managed = document["services"][0]
        managed["ownership"] = "mixed"
        for rank in managed["ranks"]:
            rank["ownership"] = "managed"
        managed["ranks"][2]["ownership"] = "unknown"
        managed["ranks"][2]["container_name"] = "stray-rank"
        text = self.render(self.sources(inventory=(document, 0, "")))
        self.assertIn("  Services  org/example-model · running · spec 5e5e5e5e5e5e", self.section(text, "rank-1"))
        self.assertIn("  Unmanaged container stray-rank · running", self.section(text, "rank-2"))

    def test_a_node_without_a_name_is_shown_not_a_crash(self):
        document = inventory()
        document["nodes"]["worker"].update(hostname=None, node_id=None)
        text = self.render(self.sources(topology=("", 1, ""), inventory=(document, 0, "")))
        self.assertIn("\nunnamed node\n", text)

    def test_without_topology_check_the_observed_nodes_still_show(self):
        text = self.render(self.sources(topology=("", 1, "error: no confirmed topology\n")))
        self.assertIn("\nrank-0 (this node)\n  Status    not observed\n", text)
        self.assertIn("\nrank-2\n", text)
        self.assertIn("pulsar topology check exited 1: no confirmed topology", text)
        self.assertIn("  Links     not checked\n", text)
        nothing = self.render(self.sources(topology=("", 1, ""), inventory=("", 1, ""), budget=("", 1, "")))
        self.assertIn("\nNo node was observed.\n", nothing)

    def test_the_check_record_keeps_each_nodes_answer_and_findings(self):
        noticed = discovery(missing=[{"hostname": "rank-2", "node_id": "node-2", "position": 2, "answered": False}])
        directory = self.sources(discovery=(noticed, 1, ""),
                                 topology=(topology("blocked", [cluster_overview.UNREACHABLE]), 1, ""))
        record = cluster_overview.Report(directory).check_record()
        self.assertEqual(record["topology_id"], TOPOLOGY_ID)
        self.assertEqual((record["links"], record["identities"]), ("verified", "verified"))
        self.assertEqual([(node["hostname"], node["answered"]) for node in record["nodes"]],
                         [("rank-0", True), ("rank-1", False), ("rank-2", False)])
        self.assertEqual(record["nodes"][0]["warnings"], [diagnostics()["checks"][2]["message"]])
        self.assertEqual(record["nodes"][1]["failures"], ["rsync not found; install rsync on this node"])
        # Unknown is not absent: without the membership check, an answer is not known.
        unknown = cluster_overview.Report(self.sources(topology=("", 1, ""))).check_record()
        self.assertEqual({node["answered"] for node in unknown["nodes"]}, {None})
        self.assertEqual(unknown["links"], "not-checked")
        from scripts.saved_observations import _check_check
        _check_check({**{key: value for key, value in record.items() if key != "topology_id"},
                      "checked_at": "2026-11-09T00:00:00Z"})


class Runner(unittest.TestCase):
    """scripts/cluster-overview.sh runs the six sources in turn, saves the check and offers Check again."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("cluster-overview.sh", "cluster_overview.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        (scripts / "ui.sh").write_text(r'''
require_gum() { :; }
ui_keep_styles() { :; }
wait_continue() { printf 'continue: %s\n' "$1" >> "$SPIN_LOG"; }
page_output() { printf 'page: %s\n' "$1" >> "$SPIN_LOG"; shift; "$@"; }
# Like Gum, the spinner draws on stderr while the command runs.
spin() { printf '%s\n' "$1" >> "$SPIN_LOG"; printf 'spinner: %s\n' "$1" >&2; shift; "$@"; }
choose_index() {
  local n answer
  { printf '%s\n' "$@"; printf '\n'; } >> "$CHOOSE_LOG"
  n=$(cat "$ANSWERS.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$ANSWERS.n"
  answer=$(sed -n "$((n + 1))p" "$ANSWERS")
  case "$answer" in
    esc) return 1 ;;
    ctrl-c) return 130 ;;
    *) printf '%s\n' "$answer" ;;
  esac
}
''')
        review = scripts / "cluster-review.sh"
        review.write_text('#!/usr/bin/env bash\necho "review from-check=${_PULSAR_REVIEW_FROM_CHECK:-0}" '
                          '>> "$PULSAR_LOG"\nexit "${REVIEW_RC:-0}"\n')
        review.chmod(0o755)
        self.fixtures = self.root / "fixtures"
        self.fixtures.mkdir()
        for command, name in (("topology detect", "discovery"), ("topology check", "topology"),
                              ("ssh-trust check", "identities"), ("doctor", "diagnostics"),
                              ("inventory", "inventory"), ("model budget", "budget")):
            (self.fixtures / command).write_text(json.dumps(DOCUMENTS[name]()))
        pulsar = self.root / "pulsar"
        pulsar.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PULSAR_LOG"\n'
                          'if [ -n "${PULSAR_RC:-}" ]; then echo "error: fixture failure" >&2; exit "$PULSAR_RC"; fi\n'
                          'args="$*"; cat "$FIXTURES/${args% --json}"\n')
        pulsar.chmod(0o755)
        self.changes = self.root / "saved/cluster-changes.json"
        self.changes.parent.mkdir()

    def run_overview(self, answers, **extra):
        (self.root / "answers").write_text("".join(f"{answer}\n" for answer in answers))
        (self.root / "answers.n").unlink(missing_ok=True)
        env = {**{k: v for k, v in os.environ.items() if k not in ("PULSAR_COLOR", "NO_COLOR")},
               "ANSWERS": str(self.root / "answers"), "PULSAR_LOG": str(self.root / "pulsar.log"),
               "SPIN_LOG": str(self.root / "spin.log"), "FIXTURES": str(self.fixtures),
               "CHOOSE_LOG": str(self.root / "choose.log"), "PULSAR_SELFTEST": "1",
               "PULSAR_CLUSTER_CHANGES_FILE": str(self.changes),
               "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "80", **extra}
        return subprocess.run(["bash", str(self.root / "scripts/cluster-overview.sh")], env=env, text=True,
                              capture_output=True, timeout=60)

    def log(self):
        return (self.root / "pulsar.log").read_text().splitlines()

    def menus(self):
        text = (self.root / "choose.log").read_text()
        return [block.splitlines() for block in text.strip().split("\n\n") if block.strip()]

    SOURCES = ["topology detect --json", "topology check --json", "ssh-trust check --json", "doctor --json",
               "inventory --json", "model budget --json"]

    def test_check_again_runs_the_six_sources_again_in_order(self):
        result = self.run_overview(["1", "2"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log(), self.SOURCES * 2)
        self.assertEqual((self.root / "spin.log").read_text().splitlines()[:6],
                         ["Looking for the machines (1/6)…", "Checking nodes and fabric (2/6)…",
                          "Checking SSH identities (3/6)…", "Checking every node's prerequisites (4/6)…",
                          "Listing services and memory (5/6)…", "Reading storage budgets (6/6)…"])
        self.assertEqual(result.stdout.count("Cluster check\n"), 2)
        self.assertIn("rank-0 (this node)", result.stdout)
        self.assertEqual(self.menus()[0], ["Cluster check", "Show the report again", "Check again", "Back"])

    def test_the_findings_are_saved_beside_the_changes_noticed(self):
        from scripts import saved_observations as saved
        saved.save_changes(changes(), self.changes)
        result = self.run_overview(["esc"])
        self.assertEqual(result.returncode, 0, result.stderr)
        check = saved.load_changes(self.changes)["check"]
        self.assertEqual(check["nodes"][1]["failures"], ["rsync not found; install rsync on this node"])
        # A record of another membership cannot hold it: one warning, and the report still shows.
        saved.save_changes({**changes(), "topology_id": "e" * 64}, self.changes)
        result = self.run_overview(["esc"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the cluster check was not saved", result.stderr)
        self.assertNotIn("check", saved.load_changes(self.changes))
        self.assertIn("Cluster check\n", result.stdout)

    def test_show_the_report_again_reads_the_same_results_without_probing(self):
        result = self.run_overview(["0", "2"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.log()), 6)
        self.assertEqual(result.stdout.count("Cluster check\n"), 2)
        # Each showing goes through the pager, which opens when it is long.
        self.assertEqual((self.root / "spin.log").read_text().count("page: \n"), 2)

    def test_a_change_offers_its_review_which_returns_to_the_cluster_menu(self):
        noticed = discovery(missing=[{"hostname": "rank-2", "node_id": "node-2", "position": 2, "answered": False}])
        (self.fixtures / "topology detect").write_text(json.dumps(noticed))
        result = self.run_overview(["0"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], ["Cluster check", "Review what changed…", "Show the report again",
                                           "Check again", "Back"])
        # Design rule 1: no choice outside Advanced uses a low-level term.
        from tests.test_cluster_menu import FORBIDDEN
        self.assertEqual([label for label in self.menus()[0][1:] if FORBIDDEN.search(label)], [])
        self.assertEqual(self.log(), self.SOURCES + ["review from-check=1"])
        # The review's Check the cluster now checks again here.
        (self.root / "pulsar.log").unlink()
        (self.root / "choose.log").unlink()
        result = self.run_overview(["0", "esc"], REVIEW_RC="10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log(), self.SOURCES + ["review from-check=1"] + self.SOURCES)

    def test_a_failed_source_reports_its_own_error_and_the_spinner_stays_on_screen(self):
        result = self.run_overview(["2"], PULSAR_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  pulsar topology check exited 1: fixture failure\n", result.stdout)
        self.assertNotIn("spinner", result.stdout)
        self.assertEqual(result.stderr.count("spinner: "), 6)

    def test_a_renderer_failure_is_held_on_screen(self):
        (self.root / "scripts/cluster_overview.py").write_text("raise SystemExit('renderer defect')\n")
        result = self.run_overview([])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("renderer defect", result.stderr)
        self.assertIn("✗ The cluster check could not be displayed; review the error above.\n  exit 1\n",
                      result.stdout)
        self.assertEqual((self.root / "spin.log").read_text().splitlines()[-1], "continue: Cluster check error")

    def test_esc_returns_and_ctrl_c_stops(self):
        self.assertEqual(self.run_overview(["esc"]).returncode, 0)
        self.assertEqual(self.run_overview(["1", "ctrl-c"]).returncode, 130)
        stopped = self.run_overview(["1"], PULSAR_RC="130")
        self.assertEqual(stopped.returncode, 130)
        self.assertEqual(self.log()[-1], "topology detect --json")


if __name__ == "__main__":
    unittest.main()
