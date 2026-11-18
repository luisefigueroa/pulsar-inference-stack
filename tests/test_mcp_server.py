"""What a tool of the MCP server returns, and how `pulsar mcp` behaves when it is not given a client.

Every server here runs a double of the pulsar file; see tests/support/mcp_session.py.
"""
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.support.mcp_session import CONTRACT, PULSAR, SPEC, TOPOLOGY, Session, envelope, spec

RESULT_FIELDS = ["operation", "state", "exit_status", "output_form", "output", "text", "diagnostics",
                 "saved_to", "bytes", "outline", "command_id"]

# A table with one more entry, for a command that prints text: none of the
# shipped tools does. It runs the python double like every other tool here.
TEXT_TOOL_LAUNCHER = """\
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {root!r})
from mcp_server import __main__ as server, tool_table
logs = tool_table.operation_tool(
    "pulsar_logs", "logs", "logs", "text", "A test entry: the recent output of a service.",
    arguments=(tool_table.Argument("spec_id", "spec_id", "A spec ID.", required=True),
               tool_table.Argument("tail", "integer", "How many lines.", flag="--tail", minimum=1, maximum=1000)))
sys.exit(server.main([], tools=(*tool_table.TOOLS, logs)))
"""


class Results(unittest.TestCase):
    def test_an_envelope_is_passed_on_unchanged_and_the_exit_status_decides_the_error_flag(self):
        session = Session(self)
        session.initialize()
        printed = {0: envelope({"spec_id": SPEC, "nodes": ["fixture-node-1"], "ratio": 0.5, "none": None}),
                   1: envelope(code="prerequisite_failed"), 2: envelope(code="usage_error"),
                   3: envelope(code="check_incomplete")}
        for status, document in printed.items():
            with self.subTest(exit_status=status):
                session.plan({"contract --json": {"json": document, "exit": status, "stderr": f"log {status}\n"}})
                result = session.call("pulsar_contract")
                self.assertIs(result["isError"], status != 0)
                answer = session.checked_object(result)
                self.assertEqual(list(answer), RESULT_FIELDS)
                self.assertEqual(answer, {
                    "operation": "contract", "state": "finished", "exit_status": status,
                    "output_form": "envelope", "output": document, "text": None,
                    "diagnostics": f"log {status}\n", "saved_to": None, "bytes": None, "outline": None,
                    "command_id": None})

    def test_a_document_with_exit_status_1_is_the_output_and_an_error(self):
        blocked = {**TOPOLOGY, "status": "missing"}
        session = Session(self, plan={"topology show --json": {"json": blocked, "exit": 1}})
        session.initialize()
        result = session.call("pulsar_topology_show")
        self.assertIs(result["isError"], True)
        answer = session.checked_object(result)
        self.assertEqual((answer["operation"], answer["output_form"], answer["state"], answer["exit_status"]),
                         ("topology.show", "document", "finished", 1))
        self.assertEqual(answer["output"], blocked)
        self.assertIsNone(answer["text"])

    def test_the_output_form_decides_how_a_result_is_read(self):
        launcher = Path(self.enterContext(tempfile.TemporaryDirectory())) / "launcher.py"
        launcher.write_text(TEXT_TOOL_LAUNCHER.format(root=str(ROOT)))
        # The same bytes from both commands; they happen to be JSON.
        printed = json.dumps({"kind": "fixture", "line": "2026-01-01 fixture-node-1 ready"}) + "\n"
        session = Session(self, launcher=launcher, plan={
            f"models show {SPEC} --json": {"stdout": printed},
            f"logs {SPEC} --tail 20": {"stdout": printed},
            f"logs {SPEC}": {"stdout": "line one\nline two\n", "exit": 3}})
        session.initialize()
        listed = [tool["name"] for tool in session.request("tools/list")["result"]["tools"]]
        self.assertEqual(listed[-1], "pulsar_logs")
        document = session.structured("pulsar_models_show", {"spec_id": SPEC})
        self.assertEqual((document["output_form"], document["output"], document["text"]),
                         ("document", json.loads(printed), None))
        result = session.call("pulsar_logs", {"spec_id": SPEC, "tail": 20})
        self.assertIs(result["isError"], False)
        text = session.checked_object(result)
        self.assertEqual((text["operation"], text["output_form"], text["output"], text["text"]),
                         ("logs", "text", None, printed))
        result = session.call("pulsar_logs", {"spec_id": SPEC})
        self.assertIs(result["isError"], True)
        self.assertEqual(session.checked_object(result)["text"], "line one\nline two\n")
        refused = session.call("pulsar_logs", {"spec_id": SPEC, "tail": 0})
        self.assertIn("argument tail must be between 1 and 1000", refused["content"][0]["text"])
        # A text command gets no --json; the table adds it to the others.
        self.assertEqual(session.calls(), [["models", "show", SPEC, "--json"], ["logs", SPEC, "--tail", "20"],
                                           ["logs", SPEC]])

    def test_output_that_is_not_json_is_reported_as_that_with_the_text(self):
        session = Session(self)
        session.initialize()
        cases = {
            "words": ("Traceback (most recent call last):\n  fixture\n", 0),
            "cut short": ('{"kind": "pulsar-topology-observation", "nodes": [', 0),
            "two documents": ('{"a": 1}\n{"b": 2}\n', 0),
            "not a number JSON has": ('{"free": NaN}\n', 0),
            "a number out of range": ('{"free": 1e999}\n', 0),
            "words and a failure": ("usage: fixture\n", 2),
        }
        for name, (printed, status) in cases.items():
            with self.subTest(name):
                session.plan({"topology show --json": {"stdout": printed, "exit": status}})
                result = session.call("pulsar_topology_show")
                # Not JSON where JSON was promised is a fault, also with exit status 0.
                self.assertIs(result["isError"], True)
                answer = session.checked_object(result)
                self.assertEqual((answer["output"], answer["text"], answer["exit_status"]),
                                 (None, printed, status))

    def test_a_command_that_printed_nothing_has_no_output_and_keeps_the_end_of_its_standard_error(self):
        session = Session(self)
        session.initialize()
        tail = "error: the spec is not in the catalog\n"
        session.plan({f"models show {SPEC} --json": {"stderr": "x" * 5000 + tail, "exit": 2}})
        result = session.call("pulsar_models_show", {"spec_id": SPEC})
        self.assertIs(result["isError"], True)
        answer = session.checked_object(result)
        self.assertEqual((answer["output"], answer["text"], answer["exit_status"]), (None, None, 2))
        self.assertEqual(len(answer["diagnostics"]), 4000)
        self.assertTrue(answer["diagnostics"].endswith(tail))
        # Nothing printed with exit status 0 is no result either.
        session.plan({f"models show {SPEC} --json": {}})
        self.assertIs(session.call("pulsar_models_show", {"spec_id": SPEC})["isError"], True)

    def test_a_command_that_cannot_be_run_is_a_result_with_the_reason(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        double = home / "not-a-program"
        double.write_text("this file is no program\n")
        double.chmod(0o700)
        session = Session(self, double=double)
        session.initialize()
        result = session.call("pulsar_contract")
        self.assertIs(result["isError"], True)
        answer = session.checked_object(result)
        self.assertEqual((answer["exit_status"], answer["output"]), (126, None))
        self.assertIn("pulsar mcp: cannot run", answer["diagnostics"])


class SlowCommands(unittest.TestCase):
    def slow(self, number=1, **step):
        return {f"models show {spec(number)} --json": {"name": f"slow-{number}", "started": True, "release": True,
                                                        "json": {"kind": "fixture", "number": number}, **step}}

    def test_a_slow_command_is_running_and_then_command_get_returns_its_result(self):
        session = Session(self, plan=self.slow())
        session.initialize()
        result = session.call("pulsar_models_show", {"spec_id": spec(1), "wait_seconds": 0})
        self.assertIs(result["isError"], False)
        running = session.checked_object(result)
        self.assertEqual(list(running), RESULT_FIELDS)
        self.assertEqual(running, {
            "operation": "models.show", "state": "running", "exit_status": None, "output_form": "document",
            "output": None, "text": None, "diagnostics": None, "saved_to": None, "bytes": None, "outline": None,
            "command_id": "command-1"})
        session.marker("slow-1.started")
        again = session.structured("pulsar_command_get", {"command_id": "command-1", "wait_seconds": 0})
        self.assertEqual(again, running)
        listed = session.structured("pulsar_command_list")["commands"]
        self.assertEqual([(row["command_id"], row["operation"], row["state"]) for row in listed],
                         [("command-1", "models.show", "running")])
        waiting = session.post_call("pulsar_command_get", {"command_id": "command-1"})
        session.release("slow-1")
        finished = session.checked_object(session.response(waiting)["result"])
        self.assertEqual((finished["state"], finished["exit_status"], finished["command_id"]), ("finished", 0, None))
        self.assertEqual(finished["output"], {"kind": "fixture", "number": 1})
        # A result is returned once; then the server holds the command no longer.
        self.assertEqual(session.structured("pulsar_command_list"), {"commands": []})
        gone = session.call("pulsar_command_get", {"command_id": "command-1"})
        self.assertIs(gone["isError"], True)
        self.assertIn("this server holds no command command-1", gone["content"][0]["text"])
        self.assertEqual(len(session.calls()), 1)

    def test_a_call_waits_wait_seconds_before_it_answers_running(self):
        session = Session(self, plan=self.slow())
        session.initialize()
        started = time.monotonic()
        running = session.structured("pulsar_models_show", {"spec_id": spec(1), "wait_seconds": 0.3})
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual((running["state"], running["command_id"]), ("running", "command-1"))
        started = time.monotonic()
        still = session.structured("pulsar_command_get", {"command_id": "command-1", "wait_seconds": 0.2})
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(still["state"], "running")

    def test_a_result_that_ended_unasked_waits_for_command_get(self):
        session = Session(self, plan=self.slow(exit=1))
        session.initialize()
        session.structured("pulsar_models_show", {"spec_id": spec(1), "wait_seconds": 0})
        session.release("slow-1")
        session.wait_until(lambda: session.structured("pulsar_command_list")["commands"][0]["state"] == "finished",
                           "the command has ended")
        result = session.call("pulsar_command_get", {"command_id": "command-1", "wait_seconds": 0})
        self.assertIs(result["isError"], True)
        self.assertEqual(session.checked_object(result)["exit_status"], 1)

    def test_command_tools_refuse_an_id_this_server_does_not_hold(self):
        session = Session(self)
        session.initialize()
        for tool in ("pulsar_command_get", "pulsar_command_cancel"):
            for command_id, message in (("command-7", "this server holds no command command-7"),
                                        ("7", "must be a command_id this server returned"),
                                        ("-9", "must be a command_id this server returned")):
                with self.subTest(tool=tool, command_id=command_id):
                    result = session.call(tool, {"command_id": command_id})
                    self.assertIs(result["isError"], True)
                    self.assertIn(message, result["content"][0]["text"])
            self.assertIn("argument command_id is required", session.call(tool)["content"][0]["text"])

    def test_server_info_names_the_pulsar_file_it_runs_and_returns_its_contract(self):
        session = Session(self, plan={"contract --json": {"json": CONTRACT}})
        session.initialize()
        result = session.call("pulsar_mcp_server_info")
        self.assertIs(result["isError"], False)
        answer = session.checked_object(result)
        self.assertEqual(list(answer), [*RESULT_FIELDS, "cli"])
        self.assertEqual(answer["cli"], str(session.double))
        self.assertEqual((answer["operation"], answer["output_form"], answer["output"]),
                         ("contract", "envelope", CONTRACT))
        self.assertEqual(session.calls(), [["contract", "--json"]])


class Usage(unittest.TestCase):
    """`pulsar mcp` without a client. None of these starts a server; if one did
    start, its double could run nothing."""

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.home / "runtime").mkdir(mode=0o700)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_"))}
        self.env.update(PULSAR_MCP_CLI="/bin/false", XDG_RUNTIME_DIR=str(self.home / "runtime"),
                        TMPDIR=str(self.home), COLUMNS="80", PYTHONDONTWRITEBYTECODE="1")

    def run_mcp(self, *args, stdin=subprocess.DEVNULL):
        process = subprocess.Popen([str(PULSAR), *args], stdin=stdin, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=self.env, cwd=self.home, text=True,
                                   start_new_session=True)
        try:
            out, err = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail(f"pulsar {' '.join(args)} did not exit")
        self.assertEqual(list((self.home / "runtime").iterdir()), [], "no server was to start")
        return process.returncode, out, err

    def test_help_prints_the_usage_and_exits_0_with_or_without_json(self):
        for args in (["mcp", "--help"], ["mcp", "-h"], ["mcp", "--help", "--json"], ["mcp", "--json", "--help"]):
            with self.subTest(args=args):
                status, out, err = self.run_mcp(*args)
                self.assertEqual((status, err), (0, ""))
                self.assertTrue(out.startswith("usage: pulsar mcp [--help]\n\nServe Pulsar's read-only tools"), out)
        self.assertIn("  pulsar mcp\n", self.run_mcp("help")[1])

    def test_json_is_a_usage_mistake_with_the_envelope_every_command_prints(self):
        status, out, err = self.run_mcp("mcp", "--json")
        self.assertEqual((status, err), (2, ""))
        mistake = json.loads(out)
        self.assertEqual(mistake["error"]["code"], "usage_error")
        self.assertIn("see ./pulsar mcp --help", mistake["error"]["message"])
        # The server builds this envelope itself; it has the shape of any other usage mistake.
        other_status, other_out, _ = self.run_mcp("no-such-command", "--json")
        other = json.loads(other_out)
        self.assertEqual(status, other_status)
        self.assertEqual(out.strip(), json.dumps(mistake, sort_keys=True))
        self.assertEqual({key: other[key] for key in ("schema_version", "ok")},
                         {key: mistake[key] for key in ("schema_version", "ok")})
        self.assertEqual(set(mistake), set(other))
        self.assertEqual(set(mistake["error"]), set(other["error"]))
        self.assertEqual(mistake["error"]["code"], other["error"]["code"])
        self.assertEqual([set(detail) for detail in mistake["error"]["details"]],
                         [set(detail) for detail in other["error"]["details"]])
        self.assertEqual(mistake["error"]["details"][0]["field"], other["error"]["details"][0]["field"])
        self.assertEqual(mistake["error"]["message"],
                         f"{mistake['error']['details'][0]['field']}: {mistake['error']['details'][0]['message']}")
        self.assertEqual(other["error"]["message"],
                         f"{other['error']['details'][0]['field']}: {other['error']['details'][0]['message']}")

    def test_an_argument_is_a_usage_mistake(self):
        for args in (["mcp", "serve"], ["mcp", "--grant", "read"]):
            with self.subTest(args=args):
                status, out, err = self.run_mcp(*args)
                self.assertEqual((status, out), (2, ""))
                self.assertEqual(err, f"error: pulsar mcp takes no arguments, and got {args[1]}; "
                                      "see ./pulsar mcp --help\n")

    def test_on_a_terminal_it_is_a_usage_mistake_and_never_waits_for_input(self):
        terminal, person = pty.openpty()
        self.addCleanup(os.close, terminal)
        try:
            status, out, err = self.run_mcp("mcp", stdin=person)
        finally:
            os.close(person)
        self.assertEqual((status, out), (2, ""))
        self.assertEqual(err, "error: pulsar mcp is started by an agent harness, which talks to it over standard "
                              "input and output; it is not for a terminal. See ./pulsar mcp --help\n")

    def test_a_grace_period_that_is_no_number_is_a_usage_mistake(self):
        self.env["PULSAR_MCP_GRACE_SECONDS"] = "soon"
        status, out, err = self.run_mcp("mcp")
        self.assertEqual((status, out), (2, ""))
        self.assertIn("PULSAR_MCP_GRACE_SECONDS must be a number of seconds", err)


if __name__ == "__main__":
    unittest.main()
