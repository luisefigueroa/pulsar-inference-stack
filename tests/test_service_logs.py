"""pulsar logs reads Stack-owned containers' output and changes nothing.

A real three-node topology (with enrolled SSH trust) drives the real lib.sh
helpers; ssh relays each remote command to a per-node docker double. No real
node, Docker or SSH is contacted.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from support.topology_fixture import Fixture

SPEC = "ab" * 32
OTHER = "cd" * 32
LABEL = "io.pulsar.gb10."

# The docker double: containers.json holds {node, id, name, labels, state,
# logs, stderr}; every call is logged as [node, args]. Only read operations
# are answered; anything else fails loudly.
DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["LOGS_FIXTURE"]
node = int(os.environ.get("LOGS_NODE", "0"))
args = sys.argv[1:]
with open(os.path.join(root, "docker-calls.jsonl"), "a") as stream:
    stream.write(json.dumps([node, args]) + "\n")
containers = [c for c in json.load(open(os.path.join(root, "containers.json"))) if c["node"] == node]
def find(ref):
    for c in containers:
        if c["id"] == ref or c["name"] == ref:
            return c
    print(f"Error: No such object: {ref}", file=sys.stderr)
    sys.exit(1)
if args == ["info"]:
    sys.exit(0)
if args[:2] == ["ps", "-aq"]:
    wanted = [a.split("=", 1)[1] for a in args if a.startswith("label=")]
    for c in containers:
        if all(c["labels"].get(w.split("=", 1)[0]) == w.split("=", 1)[1] for w in wanted):
            print(c["id"])
    sys.exit(0)
if args[:2] == ["inspect", "--format"]:
    c = find(args[3])
    if args[2] == "{{.State.Status}}":
        print(c["state"])
    else:
        print(json.dumps({"id": c["id"], "name": "/" + c["name"], "labels": c["labels"]}))
    sys.exit(0)
if args[:1] == ["logs"]:
    c = find(args[-1])
    tail = int(args[args.index("--tail") + 1]) if "--tail" in args else None
    lines = c["logs"][-tail:] if tail else c["logs"]
    for line in lines:
        print(line)
    sys.stdout.flush()
    for line in c.get("stderr", []):
        print(line, file=sys.stderr)
    sys.exit(0)
print("unexpected docker operation: " + " ".join(args), file=sys.stderr)
sys.exit(97)
'''

# The ssh double runs the remote command on the node its alias names, or
# fails like an unreachable host.
SSH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
root = os.environ["LOGS_FIXTURE"]
args = sys.argv[1:]
alias = args[args.index("--") + 1]
node = json.load(open(os.path.join(root, "map.json")))[alias]
if str(node) in os.environ.get("LOGS_UNREACHABLE", "").split(","):
    print(f"ssh: connect to host {alias} port 22: Connection timed out", file=sys.stderr)
    sys.exit(255)
sys.exit(subprocess.call(["bash", "-c", args[-1]], env=dict(os.environ, LOGS_NODE=str(node))))
'''


class ServiceLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root, nodes=3)
        for name, body in (("docker", DOCKER), ("ssh", SSH)):
            path = self.root / "bin" / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = dict(self.fixture.env, LOGS_FIXTURE=str(self.root), COLUMNS="80")
        self.containers = []

    def container(self, node, rank, *, spec=SPEC, nodes=3, state="running", node_id=None, managed="true",
                  logs=None):
        labels = {LABEL + "managed": managed, LABEL + "conf": spec,
                  LABEL + "rank": "single" if nodes == 1 else str(rank), LABEL + "world-size": str(nodes),
                  LABEL + "node-id": node_id or f"fixture-node-{node}",
                  LABEL + "topology": self.fixture.topology["topology_id"]}
        self.containers.append({"node": node, "id": f"{spec[:4]}{node}{rank}" + "0" * 58, "name": f"vllm-{node}",
                                "labels": labels, "state": state,
                                "logs": logs if logs is not None else [f"rank {rank} line {i}" for i in range(1, 4)],
                                "stderr": [f"rank {rank} engine warning"]})

    def run_logs(self, *args, **env):
        (self.root / "containers.json").write_text(json.dumps(self.containers))
        return subprocess.run([str(ROOT / "pulsar"), "logs", *args], env={**self.env, **env},
                              text=True, capture_output=True, timeout=60)

    def docker_calls(self):
        path = self.root / "docker-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_read_only(self):
        for _, args in self.docker_calls():
            self.assertIn(args[0], ("info", "ps", "inspect", "logs"), args)

    def test_every_rank_in_rank_order_under_its_node_and_state(self):
        for node in (2, 0, 1):
            self.container(node, node, state="exited" if node == 2 else "running")
        result = self.run_logs(SPEC, "--tail", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         "fixture-rank-0 (rank 0) · running\nrank 0 line 2\nrank 0 line 3\nrank 0 engine warning\n\n"
                         "fixture-rank-1 (rank 1) · running\nrank 1 line 2\nrank 1 line 3\nrank 1 engine warning\n\n"
                         "fixture-rank-2 (rank 2) · exited\nrank 2 line 2\nrank 2 line 3\nrank 2 engine warning\n")
        logs = [args for _, args in self.docker_calls() if args[0] == "logs"]
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(args[:3] == ["logs", "--tail", "2"] for args in logs), logs)
        self.assert_read_only()

    def test_default_tail_is_200_lines(self):
        self.container(0, 0, nodes=1, logs=[f"line {i}" for i in range(250)])
        result = self.run_logs(SPEC)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("line 50\n", result.stdout)
        self.assertNotIn("line 49\n", result.stdout)

    def test_an_exited_one_node_container_is_read_as_rank_zero(self):
        self.container(1, 0, nodes=1, state="exited")
        result = self.run_logs(SPEC, "--rank", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("fixture-rank-1 (rank 0) · exited\nrank 0 line 1\n"), result.stdout)

    def test_node_or_rank_selects_one_container_of_a_multi_node_service(self):
        for node in range(3):
            self.container(node, node)
        by_node = self.run_logs(SPEC, "--node", "fixture-rank-1")
        self.assertEqual(by_node.returncode, 0, by_node.stderr)
        self.assertIn("fixture-rank-1 (rank 1)", by_node.stdout)
        self.assertNotIn("(rank 0)", by_node.stdout + by_node.stderr)
        # Only the selected node is probed.
        self.assertEqual({node for node, _ in self.docker_calls()}, {1})
        by_rank = self.run_logs(SPEC, "--rank", "2")
        self.assertEqual(by_rank.returncode, 0, by_rank.stderr)
        self.assertEqual([line for line in by_rank.stdout.splitlines() if "(rank" in line],
                         ["fixture-rank-2 (rank 2) · running"])

    def test_containers_the_stack_does_not_own_are_never_read(self):
        self.container(0, 0)
        self.container(0, 1, spec=OTHER)
        self.container(1, 1, managed="false")
        self.container(2, 2, node_id="fixture-node-1")
        result = self.run_logs(SPEC)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture-rank-0 (rank 0)", result.stdout)
        self.assertIn("not reading container", result.stderr)
        self.assertIn("on fixture-rank-2", result.stderr)
        read = [args[-1] for _, args in self.docker_calls() if args[0] == "logs"]
        self.assertEqual(read, [self.containers[0]["id"]])

    def test_no_container_says_so_and_points_to_inventory(self):
        self.container(0, 0, spec=OTHER)
        result = self.run_logs(SPEC)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("no container of spec abababababab exists on any confirmed node", result.stderr)
        self.assertIn("./pulsar inventory", result.stderr)

    def test_an_unreachable_node_is_unknown_and_the_others_are_still_read(self):
        for node in range(3):
            self.container(node, node)
        result = self.run_logs(SPEC, LOGS_UNREACHABLE="2")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("fixture-rank-0 (rank 0)", result.stdout)
        self.assertIn("fixture-rank-1 (rank 1)", result.stdout)
        self.assertNotIn("rank 2", result.stdout)
        self.assertIn("fixture-rank-2 could not be observed; its logs for spec abababababab are unknown",
                      " ".join(result.stderr.split()))
        self.assertNotIn("Connection timed out", result.stdout)
        # Nothing found and a node unknown: unknown, never "no container".
        self.containers = [c for c in self.containers if c["node"] == 2]
        result = self.run_logs(SPEC, LOGS_UNREACHABLE="2")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn("no container", result.stderr)

    def test_follow_reads_exactly_one_container(self):
        for node in range(3):
            self.container(node, node)
        result = self.run_logs(SPEC, "--follow")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--follow reads one container, and spec abababababab has 3; add --node NODE or --rank N",
                      result.stderr)
        self.assertFalse([args for _, args in self.docker_calls() if args[0] == "logs"])
        result = self.run_logs(SPEC, "--follow", "--rank", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = [(node, args) for node, args in self.docker_calls() if args[0] == "logs"]
        self.assertEqual(logs, [(1, ["logs", "--tail", "200", "--follow", self.containers[1]["id"]])])
        self.assertIn("rank 1 line 3", result.stdout)

    def test_usage_mistakes_exit_2_before_any_probe(self):
        cases = {("--tail", "0"): "--tail needs a positive number of lines",
                 ("--rank", "one"): "--rank needs a rank number",
                 ("--node", "fixture-rank-1", "--rank", "1"): "use --node or --rank, not both",
                 ("--json",): "has no --json form",
                 ("--since", "1h"): "unknown option: --since",
                 ("zz",): "use a complete spec ID"}
        for args, message in cases.items():
            with self.subTest(args=args):
                result = self.run_logs(*((SPEC,) + args if args != ("zz",) else args))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.docker_calls(), [])
        result = self.run_logs()
        self.assertEqual(result.returncode, 2)
        self.assertIn("logs requires a spec ID", result.stderr)

    def test_headings_fit_a_narrow_terminal(self):
        self.container(1, 1, nodes=3)
        result = self.run_logs(SPEC, "--rank", "1", COLUMNS="44")
        self.assertEqual(result.returncode, 0, result.stderr)
        heading = result.stdout.splitlines()[0]
        self.assertEqual(heading, "fixture-rank-1 (rank 1) · running")
        self.assertLessEqual(len(heading), 44)


if __name__ == "__main__":
    unittest.main()
