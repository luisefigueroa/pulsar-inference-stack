"""Cluster overview: one screen from topology check, inventory and model budget,
with every saved node shown and a source that failed reported, never hidden."""
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


def topology(status="ready", issues=()):
    return {"kind": "pulsar-topology-observation", "status": "ready" if status == "ready" else "blocked",
            "issues": [], "nodes": [{"hostname": f"rank-{index}", "node_id": f"node-{index}", "rank": index,
                                     "status": status if index == 1 else "ready",
                                     "issues": list(issues) if index == 1 else []} for index in range(3)]}


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


class Overview(unittest.TestCase):
    def sources(self, **overrides):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        documents = {"topology": topology(), "inventory": inventory(), "budget": budget()}
        for name, document in documents.items():
            value = overrides.get(name, (document, 0, ""))
            output, status, error = value
            (directory / f"{name}.json").write_text(json.dumps(output) if isinstance(output, dict) else (output or ""))
            (directory / f"{name}.rc").write_text(f"{status}\n")
            (directory / f"{name}.err").write_text(error)
        return directory

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

    def test_each_node_shows_status_memory_disk_and_services(self):
        text = self.render(self.sources())
        self.assertTrue(text.startswith("Cluster overview\n  Topology  ready · 3 saved nodes\n  Observed  "), text)
        self.assertIn("; the checks took 19 s\n", text)
        local = self.section(text, "rank-0 (this node)")
        self.assertIn("  Status    ready\n  Memory    6.50 GiB available of 121.69 GiB\n", local)
        self.assertIn("  Disk      200.00 GiB free after reserve · 150.00 GiB copy allowance left\n", local)
        self.assertIn("  Services  org/example-model · running · spec 5e5e5e5e5e5e · 100.00 GiB GPU\n"
                      "            memory in use\n", local)
        self.assertIn("  Unmanaged container legacy-vllm · running; Pulsar does not manage it", local)
        worker = self.section(text, "rank-1")
        self.assertIn("  Other GPU PID 4242 trainer · 2.00 GiB; Pulsar does not manage it", worker)
        self.assertNotIn("(this node)", worker)
        self.assertIn("\nDetails: Live service inventory, Host diagnostics and Cluster topology.\n", text)

    def test_styles_mark_headings_and_what_needs_attention(self):
        directory = self.sources(topology=(topology("blocked", ["fabric link to rank-2 is down"]), 1, ""))
        for width in (44, 80):
            with self.subTest(width=width):
                plain = self.render(directory, width)
                styled = self.render(directory, width, terminal=True)
                self.assertEqual(ANSI.sub("", styled), plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()), plain)
        styled = self.render(directory, 80, terminal=True)
        self.assertIn("\x1b[1;94mCluster overview\x1b[0m\n", styled)
        self.assertIn("\n\x1b[1;94mrank-1\x1b[0m\n", styled)
        self.assertIn("  \x1b[1;31mStatus\x1b[0m    blocked\n  \x1b[33mIssue\x1b[0m     fabric link to rank-2 is down",
                      styled)
        self.assertIn("  \x1b[33mUnmanaged\x1b[0m container legacy-vllm", styled)
        # A blocked topology still reports every node: its JSON is the answer.
        self.assertNotIn("Not observed", ANSI.sub("", styled))

    def test_a_source_that_failed_is_not_observed_and_named(self):
        directory = self.sources(budget=("", 1, "error: storage lock is busy\n"),
                                 inventory=("not json", 2, ""))
        text = self.render(directory)
        local = self.section(text, "rank-0")
        self.assertIn("  Memory    not observed\n  Disk      not observed", local)
        self.assertNotIn("Services", local)
        self.assertIn("\nNot observed\n  `pulsar inventory` exited 2 without a result\n".replace("`", ""), text)
        self.assertIn("  pulsar model budget exited 1: storage lock is busy\n", text)

    def test_without_topology_check_the_observed_nodes_still_show(self):
        text = self.render(self.sources(topology=("", 1, "error: no confirmed topology\n")))
        self.assertIn("\nrank-0 (this node)\n  Status    not observed\n", text)
        self.assertIn("\nrank-2\n", text)
        self.assertIn("pulsar topology check exited 1: no confirmed topology", text)
        nothing = self.render(self.sources(topology=("", 1, ""), inventory=("", 1, ""), budget=("", 1, "")))
        self.assertIn("\nNo node was observed.\n", nothing)


class Runner(unittest.TestCase):
    """scripts/cluster-overview.sh runs the three sources in turn and offers Refresh."""

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
# Like Gum, the spinner draws on stderr while the command runs.
spin() { printf '%s\n' "$1" >> "$SPIN_LOG"; printf 'spinner: %s\n' "$1" >&2; shift; "$@"; }
choose_index() {
  local n answer
  n=$(cat "$ANSWERS.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$ANSWERS.n"
  answer=$(sed -n "$((n + 1))p" "$ANSWERS")
  case "$answer" in
    esc) return 1 ;;
    ctrl-c) return 130 ;;
    *) printf '%s\n' "$answer" ;;
  esac
}
''')
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        (fixtures / "topology check").write_text(json.dumps(topology()))
        (fixtures / "inventory").write_text(json.dumps(inventory()))
        (fixtures / "model budget").write_text(json.dumps(budget()))
        pulsar = self.root / "pulsar"
        pulsar.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PULSAR_LOG"\n'
                          'if [ -n "${PULSAR_RC:-}" ]; then echo "error: fixture failure" >&2; exit "$PULSAR_RC"; fi\n'
                          'args="$*"; cat "$FIXTURES/${args% --json}"\n')
        pulsar.chmod(0o755)

    def run_overview(self, answers, **extra):
        (self.root / "answers").write_text("".join(f"{answer}\n" for answer in answers))
        env = {**{k: v for k, v in os.environ.items() if k not in ("PULSAR_COLOR", "NO_COLOR")},
               "ANSWERS": str(self.root / "answers"), "PULSAR_LOG": str(self.root / "pulsar.log"),
               "SPIN_LOG": str(self.root / "spin.log"), "FIXTURES": str(self.root / "fixtures"),
               "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "80", **extra}
        return subprocess.run(["bash", str(self.root / "scripts/cluster-overview.sh")], env=env, text=True,
                              capture_output=True, timeout=60)

    def test_refresh_runs_the_three_sources_again_in_order(self):
        result = self.run_overview(["0", "1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        sources = ["topology check --json", "inventory --json", "model budget --json"]
        self.assertEqual((self.root / "pulsar.log").read_text().splitlines(), sources * 2)
        self.assertEqual((self.root / "spin.log").read_text().splitlines()[:3],
                         ["Checking nodes and fabric (1/3)…", "Listing services and memory (2/3)…",
                          "Reading storage budgets (3/3)…"])
        self.assertEqual(result.stdout.count("Cluster overview\n"), 2)
        self.assertIn("rank-0 (this node)", result.stdout)

    def test_a_failed_source_reports_its_own_error_and_the_spinner_stays_on_screen(self):
        result = self.run_overview(["1"], PULSAR_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  pulsar topology check exited 1: fixture failure\n", result.stdout)
        self.assertNotIn("spinner", result.stdout)
        self.assertEqual(result.stderr.count("spinner: "), 3)

    def test_a_renderer_failure_is_held_on_screen(self):
        (self.root / "scripts/cluster_overview.py").write_text("raise SystemExit('renderer defect')\n")
        result = self.run_overview([])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("renderer defect", result.stderr)
        self.assertIn("✗ The cluster overview could not be displayed; review the error above.\n  exit 1\n",
                      result.stdout)
        self.assertEqual((self.root / "spin.log").read_text().splitlines()[-1], "continue: Cluster overview error")

    def test_esc_returns_and_ctrl_c_stops(self):
        self.assertEqual(self.run_overview(["esc"]).returncode, 0)
        self.assertEqual(self.run_overview(["0", "ctrl-c"]).returncode, 130)
        stopped = self.run_overview(["1"], PULSAR_RC="130")
        self.assertEqual(stopped.returncode, 130)
        self.assertEqual((self.root / "pulsar.log").read_text().splitlines()[-1], "topology check --json")


if __name__ == "__main__":
    unittest.main()
