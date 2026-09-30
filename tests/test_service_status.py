"""Status says what it established: verified or not, the service state, health."""
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from scripts import public_cli, service_status

SPEC = "ab" * 32
SERVICE_ID = "5e" * 32
PLAN_ID = "7a" * 32


class Health:
    """A local API whose /health answers with the given status, or redirects."""

    def __init__(self, code, location=None):
        requests = self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(code if self.path == "/health" else 404)
                if location:
                    self.send_header("Location", location)
                self.end_headers()

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def observation(api_url, matches=True):
    """A serving observation shaped like observe-serving's: ranks carry no node identity."""
    return {"schema_version": 2, "kind": "pulsar-serving-observation", "selected_spec_id": SPEC,
            "spec_id": SPEC if matches else "cd" * 32, "matches_selected_spec": matches, "api_url": api_url,
            "service_id": SERVICE_ID, "ranks": [{"rank": 0, "running": True, "owned": True, "spec_id": SPEC}]}


def record_service(root):
    """The service's index and launch plan, where status finds each rank's node."""
    for namespace, key, value in (("services", SERVICE_ID, {"plan_id": PLAN_ID}),
                                  ("service-plans", PLAN_ID, {"ranks": [{"node_id": "node-0"}]})):
        (root / namespace).mkdir(parents=True, exist_ok=True)
        (root / namespace / f"{key}.json").write_text(json.dumps(value))


def inventory(services=(), nodes=None, worker_status="unset"):
    return {"services": list(services), "worker": {"status": worker_status, "reason": None},
            "nodes": nodes if nodes is not None else {
                "head": {"hostname": "spark-1", "node_id": "node-0", "local": True, "confirmed": True,
                         "probe_status": "ok"}}}


def service(state="running", port=None, node="head", configured=None):
    """An inventory row; port is what rank 0's container runs with, configured is today's setting."""
    return {"conf": SPEC, "state": state, "api_port": configured,
            "ranks": [{"rank": "0", "node": node, "observed_api_port": int(port) if port else None}]}


class Results(unittest.TestCase):
    def setUp(self):
        self.healthy = Health(200); self.addCleanup(self.healthy.close)
        self.broken = Health(503); self.addCleanup(self.broken.close)

    def test_a_verified_observation_reports_state_and_health(self):
        result = service_status.verified(observation(self.healthy.url))
        self.assertEqual((result["state"], result["verified"], result["healthy"], result["reason"]),
                         ("running", True, True, None))
        self.assertEqual(result["kind"], "pulsar-serving-observation")
        self.assertIs(service_status.verified(observation(self.broken.url))["healthy"], False)
        self.assertIsNone(service_status.verified(observation(None))["healthy"])

    def test_the_probe_goes_straight_to_the_service(self):
        # An environment proxy would intercept the request and see the key.
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                                     "API_KEY": "fixture-key", "VLLM_API_KEY": ""}):
            self.assertIs(service_status.health(self.healthy.url, authenticate=True), True)
            # A redirect is not followed, so the key never reaches another origin.
            elsewhere = Health(200); self.addCleanup(elsewhere.close)
            moved = Health(302, location=f"{elsewhere.url}/health"); self.addCleanup(moved.close)
            self.assertIs(service_status.health(moved.url, authenticate=True), False)
            self.assertEqual(elsewhere.requests, [])
        self.assertEqual(self.healthy.requests, [("/health", "Bearer fixture-key")])

    def test_a_malformed_reply_is_unhealthy_not_a_crash(self):
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1)
        self.addCleanup(listener.close)

        def reply():
            connection, _ = listener.accept()
            with connection:
                connection.recv(1024)
                connection.sendall(b"not http\r\n\r\n")
        threading.Thread(target=reply, daemon=True).start()
        self.assertIs(service_status.health(f"http://127.0.0.1:{listener.getsockname()[1]}", authenticate=False),
                      False)

    def test_only_a_stack_owned_service_receives_the_key(self):
        port = self.healthy.url.rsplit(":", 1)[1]
        with patch.dict(os.environ, {"API_KEY": "fixture-key", "VLLM_API_KEY": ""}):
            # A legacy match is a name or served-name guess, not established ownership.
            for ownership, header in (("legacy", None), ("managed", "Bearer fixture-key")):
                with self.subTest(ownership=ownership):
                    self.healthy.requests.clear()
                    service_status.from_inventory(SPEC, inventory([{**service(port=port), "ownership": ownership}]),
                                                  "")
                    self.assertEqual(self.healthy.requests, [("/health", header)])

    def test_the_probe_uses_the_port_the_container_runs_with(self):
        port = self.healthy.url.rsplit(":", 1)[1]
        # Today's configured port may belong to another listener; it is never probed.
        result = service_status.from_inventory(SPEC, inventory([service(port=port, configured=1)]), "")
        self.assertEqual((result["api_url"], result["healthy"]), (self.healthy.url, True))
        result = service_status.from_inventory(SPEC, inventory([service(configured=port)]), "")
        self.assertEqual((result["api_url"], result["healthy"]), (None, None))

    def test_url_hosts_bracket_ipv6_literals(self):
        script = f". {ROOT}/scripts/lib.sh\nurl_host 2001:db8::1; url_host 192.0.2.1; url_host '[2001:db8::2]'"
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.stdout.split(), ["[2001:db8::1]", "192.0.2.1", "[2001:db8::2]"])
        # The verified cluster API URL is built with it.
        self.assertEqual((ROOT / "scripts/observe-serving.sh").read_text().count(
            'API_URL="http://$(url_host "${CLUSTER_NODE_CONTROL_IPS[0]}"):$PORT"'), 2)

    def test_an_ipv6_control_address_is_bracketed(self):
        nodes = {"worker": {"hostname": "spark-2", "control_ip": "2001:db8::1", "local": False}}
        self.assertEqual(service_status.inventory_api_url(service(port=8000, node="worker"), nodes),
                         "http://[2001:db8::1]:8000")

    def test_the_inventory_view_is_not_verified_and_says_why(self):
        port = self.healthy.url.rsplit(":", 1)[1]
        result = service_status.from_inventory(SPEC, inventory([service(port=port)]), "no running service is recorded")
        self.assertEqual((result["kind"], result["state"], result["verified"], result["healthy"]),
                         ("pulsar-service-status", "running", False, True))
        self.assertEqual(result["api_url"], self.healthy.url)
        self.assertEqual(result["reason"], "no running service is recorded")
        # An exited service is found, so this is a result, not an error; it is not probed.
        result = service_status.from_inventory(SPEC, inventory([service("stale", port=port)]), "")
        self.assertEqual((result["state"], result["healthy"], result["api_url"]), ("stale", None, None))
        self.assertEqual(result["reason"], "the complete observation was unavailable")

    def test_absent_and_unknown_are_different_errors(self):
        with self.assertRaises(service_status.StatusError) as absent:
            service_status.from_inventory(SPEC, inventory(), "")
        self.assertEqual(absent.exception.code, "service_absent")
        self.assertIn("Start it with ./pulsar start abababababab", str(absent.exception))
        nodes = {**inventory()["nodes"], "worker": {"hostname": "spark-2", "node_id": "node-1", "confirmed": True,
                                                    "probe_status": "unreachable", "probe_reason": "SSH timed out"}}
        with self.assertRaises(service_status.StatusError) as unknown:
            service_status.from_inventory(SPEC, inventory(nodes=nodes, worker_status="unreachable"), "")
        self.assertEqual(unknown.exception.code, "service_state_unknown")
        self.assertEqual(unknown.exception.details,
                         [{"field": "node", "node": "spark-2", "node_id": "node-1", "message": "SSH timed out"}])
        self.assertIn("spark-2: SSH timed out. Run ./pulsar topology check", str(unknown.exception))
        # Details stay node-shaped when the inventory cannot say which node.
        with self.assertRaises(service_status.StatusError) as unnamed:
            service_status.from_inventory(SPEC, {**inventory(worker_status="unreachable"),
                                                 "worker": {"status": "unreachable", "reason": "rank 1 timed out"}}, "")
        self.assertEqual(unnamed.exception.details,
                         [{"field": "node", "node": None, "node_id": None, "message": "rank 1 timed out"}])
        with self.assertRaises(service_status.StatusError) as no_inventory:
            service_status.from_inventory(SPEC, None, "")
        self.assertEqual(no_inventory.exception.code, "service_state_unknown")
        self.assertEqual(no_inventory.exception.details[0]["node"], None)

    def human(self, result, nodes="spark-1"):
        buffer = io.StringIO()
        service_status.human(result, nodes, service_status.TerminalWriter(width=200, stream=buffer))
        return buffer.getvalue().splitlines()

    def test_people_read_the_answer_first(self):
        self.assertEqual(self.human(service_status.verified(observation(self.healthy.url))),
                         ["spec abababababab: running and healthy on spark-1; recipe and files verified",
                          f"API: {self.healthy.url}/v1"])
        lines = self.human(service_status.verified(observation(self.broken.url, matches=False)))
        self.assertEqual(lines[0], "spec abababababab: running, but its API did not answer /health on spark-1; "
                                   "recipe and files verified")
        self.assertIn("Selected-recipe measurements are reference only.", lines)
        stale = service_status.from_inventory(SPEC, inventory([service("stale")]), "no running service is recorded")
        # The complete ID: a prefix resolves only for specs in the current catalog.
        self.assertEqual(" ".join(" ".join(self.human(stale)).split()),
                         "spec abababababab: exited (its containers exist, but none is running) on spark-1; "
                         f"not verified (no running service is recorded) Remove it with ./pulsar stop {SPEC}")


class Defects(unittest.TestCase):
    def status(self, content):
        with tempfile.TemporaryDirectory() as temp:
            observation, error = Path(temp) / "observation.json", Path(temp) / "error.json"
            observation.write_text(content)
            with patch.dict(os.environ, {"PULSAR_STATUS_ERROR_FILE": str(error)}), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                status = service_status.main(["--spec", SPEC, "--observation", str(observation), "--json"])
            return status, json.loads(error.read_text())["code"] if error.exists() else None

    def inventory_status(self, document):
        with tempfile.TemporaryDirectory() as temp:
            path, error = Path(temp) / "inventory.json", Path(temp) / "error.json"
            path.write_text(json.dumps(document))
            with patch.dict(os.environ, {"PULSAR_STATUS_ERROR_FILE": str(error)}), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                status = service_status.main(["--spec", SPEC, "--inventory", str(path), "--json"])
            return status, json.loads(error.read_text())["code"] if error.exists() else None

    def test_a_malformed_inventory_is_a_stack_defect_not_absence(self):
        good = inventory([service("stale")])
        for broken in ({**good, "nodes": {}}, {**good, "nodes": []}, {**good, "nodes": {"head": {}}},
                       {**good, "services": ["row"]}, {**good, "services": [{"conf": SPEC, "ranks": []}]},
                       {**good, "services": [{"conf": SPEC, "state": "stale", "ranks": "0"}]},
                       {**good, "worker": None}):
            with self.subTest(broken=json.dumps(broken)[:60]):
                self.assertEqual(self.inventory_status(broken), (1, "invalid_stack_output"))
        self.assertEqual(self.inventory_status(good), (0, None))

    def test_a_malformed_observation_is_a_stack_defect(self):
        # Unreadable, empty, or naming another spec: never reported as verified.
        valid = observation("http://127.0.0.1:1")
        broken = [{**valid, "selected_spec_id": "cd" * 32}, {**valid, "spec_id": ""}, {**valid, "api_url": ""},
                  {**valid, "ranks": [{}]}, {**valid, "ranks": []}]
        for content in ("not json", "{}", *map(json.dumps, broken)):
            with self.subTest(content=content[:20]):
                self.assertEqual(self.status(content), (1, "invalid_stack_output"))
        self.assertEqual(self.status(json.dumps(observation("http://127.0.0.1:1")))[1], None)


class Command(unittest.TestCase):
    """status.sh with doubles for the observation and the inventory."""

    OBSERVE = r'''#!/usr/bin/env bash
touch "$FIXTURE_DIR/observed"
case "$FIXTURE_OBSERVE" in
  ok) cat "$FIXTURE_DIR/observation.json" ;;
  usage) echo "error: unknown argument: --bogus" >&2; exit "${PULSAR_USAGE_EXIT:-2}" ;;
  *) echo "error: recorded service files differ from the verified prepared set" >&2; exit 2 ;;
esac
'''
    INVENTORY = '#!/usr/bin/env bash\ntouch "$FIXTURE_DIR/inventoried"\ncat "$FIXTURE_DIR/inventory.json"\n'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"; scripts.mkdir()
        shutil.copyfile(ROOT / "scripts/status.sh", scripts / "status.sh")
        shutil.copyfile(ROOT / "scripts/service_status.py", scripts / "service_status.py")
        for name, body in (("observe-serving.sh", self.OBSERVE), ("inventory.sh", self.INVENTORY)):
            (scripts / name).write_text(body); (scripts / name).chmod(0o700)
        topology = self.root / "topology.json"
        topology.write_text(json.dumps({"nodes": [{"node_id": "node-0", "hostname": "spark-1"}]}))
        record_service(self.root / "library")
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), "FIXTURE_DIR": str(self.root),
                    "CLUSTER_TOPOLOGY_FILE": str(topology), "PULSAR_MODEL_LIBRARY_DIR": str(self.root / "library")}
        self.env.pop("PULSAR_USAGE_EXIT", None)
        self.healthy = Health(200); self.addCleanup(self.healthy.close)
        (self.root / "observation.json").write_text(json.dumps(observation(self.healthy.url)))
        (self.root / "inventory.json").write_text(json.dumps(inventory([service("stale")])))

    def status(self, *args, observe="ok", **env):
        return subprocess.run(["bash", str(self.root / "scripts/status.sh"), *args], text=True, capture_output=True,
                              env={**self.env, "FIXTURE_OBSERVE": observe, **env}, timeout=60)

    def ran(self, marker):
        return (self.root / marker).exists()

    def test_a_missing_spec_is_a_usage_error_before_any_probe(self):
        for args in (["--json"], ["--node", "spark-1"]):
            with self.subTest(args=args):
                result = self.status(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("status requires a spec ID", result.stderr)
        result = self.status("--json", PULSAR_USAGE_EXIT="64")
        self.assertEqual(result.returncode, 64)
        # status selects a service by its spec; a service ID could name another spec's service.
        result = self.status(SPEC, "--service-id", SERVICE_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("use ./pulsar observe --service-id ID", result.stderr)
        # A value-taking option never takes the next option, such as the appended --json.
        for args in ((SPEC, "--node"), (SPEC, "--node", "--json")):
            result = self.status(*args)
            self.assertEqual((result.returncode, result.stderr.strip()), (2, "error: --node requires a value"))
        self.assertFalse(self.ran("observed"))

    def test_a_verified_service_is_named_by_its_recorded_nodes(self):
        shutil.rmtree(self.root / "library")
        result = self.status(SPEC)
        self.assertEqual(result.stdout.splitlines()[0],
                         "spec abababababab: running and healthy on 1 rank; recipe and files verified")

    def test_verified_results_carry_state_and_health(self):
        result = self.status(SPEC, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual((document["state"], document["verified"], document["healthy"]), ("running", True, True))
        result = self.status(SPEC)
        self.assertEqual(result.stdout.splitlines()[0],
                         "spec abababababab: running and healthy on spark-1; recipe and files verified")
        self.assertFalse(self.ran("inventoried"))

    def test_usage_mistakes_are_not_hidden_by_the_inventory(self):
        result = self.status(SPEC, "--bogus", observe="usage")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument: --bogus", result.stderr)
        self.assertFalse(self.ran("inventoried"))

    def test_a_failed_verification_reports_the_inventory_view(self):
        result = self.status(SPEC, "--json", observe="fail")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual((document["state"], document["verified"]), ("stale", False))
        self.assertEqual(document["reason"], "recorded service files differ from the verified prepared set")
        self.assertTrue(self.ran("inventoried"))

    def test_no_service_is_an_error_that_names_the_next_step(self):
        (self.root / "inventory.json").write_text(json.dumps(inventory()))
        error_file = self.root / "error.json"
        result = self.status(SPEC, "--json", observe="fail", PULSAR_STATUS_ERROR_FILE=str(error_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Start it with ./pulsar start abababababab", " ".join(result.stderr.split()))
        self.assertEqual(json.loads(error_file.read_text())["code"], "service_absent")


class Envelope(unittest.TestCase):
    def main(self, error):
        def fail(command, env, cwd):
            Path(env["PULSAR_STATUS_ERROR_FILE"]).write_text(json.dumps(error))
            return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": f"error: {error['message']}\n"})()
        output = io.StringIO()
        with patch.object(public_cli, "run_command", side_effect=fail), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = public_cli.main(["status", SPEC, "--json"])
        return status, json.loads(output.getvalue())["error"]

    def test_status_errors_keep_their_codes(self):
        for code in ("service_absent", "service_state_unknown", "invalid_stack_output"):
            details = [{"field": "node", "node": "spark-2", "node_id": "node-1", "message": "SSH timed out"}]
            with self.subTest(code=code):
                status, error = self.main({"code": code, "message": "fixture", "details": details})
                self.assertEqual((status, error["code"], error["details"]), (3, code, details))


if __name__ == "__main__":
    unittest.main()
