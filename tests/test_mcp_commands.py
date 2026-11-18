"""Running commands of the MCP server: cancel, the end of a session, a killed server,
the limit, the private directory and the size rule.

Every server here runs a double of the pulsar file; see tests/support/mcp_session.py.
A test sends a signal only after the double's marker says it is ready for it.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server import commands
from tests.support.mcp_session import (PATIENCE, Session, envelope, mode, process_state, running, spec)

CANCELLED = {"schema_version": 1, "ok": False, "error": {
    "code": "cancelled", "message": "Stack command cancelled; local command and tracked node workers reaped",
    "details": [{"field": "$", "message": "fixture"}]}}

# The server as shipped, except for one number: how long it waits, after the
# grace period, before it kills a supervisor that has not exited. The shipped
# two seconds would make the test that needs this wait take longer than that.
SHORT_BACKSTOP = 0.2
SHORT_BACKSTOP_LAUNCHER = """\
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {root!r})
from mcp_server import __main__ as server, commands
commands.BACKSTOP_SECONDS = {seconds!r}
sys.exit(server.main([]))
"""


def wrapper(number=1, **step):
    """A step in the wrapper style: it handles SIGTERM itself and prints the cancelled envelope."""
    return {f"models show {spec(number)} --json": {"name": f"wrapper-{number}", "started": True, "release": True,
                                                    "sigterm": "envelope", "json": envelope({"number": number}),
                                                    **step}}


class Running(unittest.TestCase):
    """Helpers shared by the classes below."""

    def start_wrapper(self, session, number=1):
        """Start a wrapper-style command, wait until it handles SIGTERM, and return its command_id."""
        answer = session.structured("pulsar_models_show", {"spec_id": spec(number), "wait_seconds": 0})
        self.assertEqual(answer["state"], "running")
        session.marker(f"wrapper-{number}.started")
        return answer["command_id"]

    def start_shell(self, session, number=1):
        """Start a shell-style command and return (command_id, script, supervisor, child), the last
        three as (process ID, start time)."""
        answer = session.structured("pulsar_models_show", {"spec_id": spec(number), "wait_seconds": 0})
        self.assertEqual(answer["state"], "running")
        pids = [int(word) for word in session.marker(f"{spec(number)}.started").split()]
        identities = [(pid, process_state(pid)[3]) for pid in pids]
        self.assertTrue(all(running(*identity) for identity in identities))
        # The script and its child are in the supervisor's process group and session.
        script, supervisor, child = pids
        for pid in (script, child):
            self.assertEqual(process_state(pid)[2], supervisor)
            self.assertEqual(os.getsid(pid), supervisor)
        self.assertNotEqual(os.getsid(supervisor), os.getsid(session.process.pid))
        return (answer["command_id"], *identities)

    def assert_gone(self, session, *identities):
        session.wait_until(lambda: not any(running(*identity) for identity in identities),
                           "the command's processes are gone")

    def assert_nothing_left(self, session, but=()):
        session.wait_until(lambda: sorted(session.session_processes()) == sorted(but),
                           "no process of the session is left")


class Cancel(Running):
    def test_a_cancelled_request_ends_its_command_and_gets_no_answer(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        cancelled = session.post_call("pulsar_models_show", {"spec_id": spec(1)})
        session.marker("wrapper-1.started")
        session.notify("notifications/cancelled", {"requestId": cancelled, "reason": "fixture"})
        session.marker("wrapper-1.sigterm")  # The command got SIGTERM.
        # Nobody was given a command_id, so the server drops the command when it has ended.
        session.wait_until(lambda: session.structured("pulsar_command_list") == {"commands": []},
                           "the cancelled command has ended")
        session.notify("notifications/cancelled", {"requestId": cancelled})  # A second one is ignored.
        self.assertEqual(session.request("ping")["result"], {})
        session.close_input()
        session.read_to_end()
        self.assertEqual(session.wait_exit(), 0)
        self.assertEqual([message for message in session.messages if message.get("id") == cancelled], [])
        self.assert_nothing_left(session)

    def test_cancel_returns_what_a_wrapper_style_command_printed_when_it_got_sigterm(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        command_id = self.start_wrapper(session)
        started = time.monotonic()
        result = session.call("pulsar_command_cancel", {"command_id": command_id})
        self.assertLess(time.monotonic() - started, commands.GRACE_SECONDS)
        self.assertIs(result["isError"], True)
        answer = session.checked_object(result)
        self.assertEqual((answer["state"], answer["exit_status"], answer["command_id"]),
                         ("cancelled", 128 + signal.SIGTERM, None))
        self.assertEqual(answer["output"], CANCELLED)  # The command's own last output, unchanged.
        self.assertEqual(answer["diagnostics"], "")
        self.assertEqual(session.marker("wrapper-1.sigterm"), "")
        self.assertEqual(session.structured("pulsar_command_list"), {"commands": []})
        self.assert_nothing_left(session, but=[session.process.pid])

    def test_cancel_ends_a_shell_style_command_and_its_child_within_the_grace_period(self):
        session = Session(self, double="shell")
        session.initialize()
        command_id, script, supervisor, child = self.start_shell(session)
        started = time.monotonic()
        answer = session.structured("pulsar_command_cancel", {"command_id": command_id})
        self.assertLess(time.monotonic() - started, commands.GRACE_SECONDS)
        # The script has no trap: SIGTERM ended it, and nothing had to be killed.
        self.assertEqual((answer["state"], answer["exit_status"]), ("cancelled", 128 + signal.SIGTERM))
        self.assertEqual((answer["output"], answer["text"], answer["diagnostics"]), (None, None, ""))
        self.assert_gone(session, script, supervisor, child)
        self.assert_nothing_left(session, but=[session.process.pid])

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period_and_the_result_says_so(self):
        session = Session(self, steps=wrapper(sigterm="ignore"), grace=0.3)
        session.initialize()
        command_id = self.start_wrapper(session)
        started = time.monotonic()
        answer = session.structured("pulsar_command_cancel", {"command_id": command_id})
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(session.marker("wrapper-1.sigterm"), "")  # It got SIGTERM first.
        self.assertEqual((answer["state"], answer["exit_status"], answer["output"]),
                         ("cancelled", 128 + signal.SIGKILL, None))
        self.assertEqual(answer["diagnostics"], "pulsar mcp: the command still had running processes 0.3 seconds "
                                                "after SIGTERM; they were killed\n")
        self.assert_nothing_left(session, but=[session.process.pid])

    def test_the_server_kills_a_command_whose_supervisor_does_not_exit(self):
        self.assertEqual(commands.BACKSTOP_SECONDS, 2.0)  # What ships; this test's server waits less.
        launcher = Path(self.enterContext(tempfile.TemporaryDirectory())) / "launcher.py"
        launcher.write_text(SHORT_BACKSTOP_LAUNCHER.format(root=str(ROOT), seconds=SHORT_BACKSTOP))
        grace = 0.2
        session = Session(self, launcher=launcher, steps=wrapper(sigterm="ignore"), grace=grace)
        session.initialize()
        command_id = self.start_wrapper(session)
        supervisor = int(session.marker("wrapper-1.started").split()[1])
        os.kill(supervisor, signal.SIGSTOP)  # A supervisor that does nothing any more.
        started = time.monotonic()
        answer = session.structured("pulsar_command_cancel", {"command_id": command_id})
        # The server left the supervisor its grace period first, then its own wait.
        self.assertGreaterEqual(time.monotonic() - started, grace + SHORT_BACKSTOP)
        self.assertEqual((answer["state"], answer["exit_status"]), ("cancelled", 128 + signal.SIGKILL))
        self.assertIn("the server killed the command's process group", answer["diagnostics"])
        self.assert_nothing_left(session, but=[session.process.pid])

    def test_a_command_cancelled_in_its_first_moments_ends_at_once_and_has_no_output(self):
        # No marker is waited for: SIGTERM may arrive before the supervisor or the command has started.
        session = Session(self, steps={f"models show {spec(1)} --json": {"name": "early", "release": True}})
        session.initialize()
        started = time.monotonic()
        command_id = session.structured("pulsar_models_show", {"spec_id": spec(1), "wait_seconds": 0})["command_id"]
        result = session.call("pulsar_command_cancel", {"command_id": command_id})
        # Whenever it arrived, the signal was not lost: nothing waited for the kill after the grace period.
        self.assertLess(time.monotonic() - started, commands.GRACE_SECONDS)
        self.assertIs(result["isError"], True)
        answer = session.checked_object(result)
        self.assertEqual((answer["state"], answer["exit_status"]), ("cancelled", 128 + signal.SIGTERM))
        # It printed nothing, and that is no fault of its own: there is no text and no note.
        self.assertEqual((answer["output"], answer["text"], answer["diagnostics"]), (None, None, ""))
        self.assert_nothing_left(session, but=[session.process.pid])

    def test_a_request_cancelled_in_the_same_write_as_its_call_ends_its_command_at_once(self):
        session = Session(self, steps={f"models show {spec(1)} --json": {"name": "early", "release": True}})
        session.initialize()
        call = {"jsonrpc": "2.0", "id": "early", "method": "tools/call",
                "params": {"name": "pulsar_models_show", "arguments": {"spec_id": spec(1)}}}
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "early"}}
        started = time.monotonic()
        session.send_line(json.dumps(call).encode() + b"\n" + json.dumps(cancel).encode())
        session.wait_until(lambda: session.structured("pulsar_command_list") == {"commands": []},
                           "the cancelled command has ended")
        self.assertLess(time.monotonic() - started, commands.GRACE_SECONDS)
        self.assertEqual([message for message in session.messages if message.get("id") == "early"], [])
        self.assert_nothing_left(session, but=[session.process.pid])

    def test_a_cancel_for_a_call_that_already_answered_running_is_ignored(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        first = session.post_call("pulsar_models_show", {"spec_id": spec(1), "wait_seconds": 0})
        command_id = session.checked_object(session.response(first)["result"])["command_id"]
        session.marker("wrapper-1.started")
        session.notify("notifications/cancelled", {"requestId": first})
        # A cancelled call that only waits for the command does not end it either, and gets no answer.
        waiting = session.post_call("pulsar_command_get", {"command_id": command_id})
        session.notify("notifications/cancelled", {"requestId": waiting})
        self.assertEqual(session.structured("pulsar_command_list")["commands"][0]["state"], "running")
        session.release("wrapper-1")
        answer = session.structured("pulsar_command_get", {"command_id": command_id})
        self.assertEqual((answer["state"], answer["exit_status"], answer["output"]),
                         ("finished", 0, envelope({"number": 1})))
        self.assertFalse((session.home / "wrapper-1.sigterm").exists())
        self.assertEqual([message for message in session.messages if message.get("id") == waiting], [])

    def test_a_command_cancelled_by_the_cancel_tool_still_answers_the_call_that_waits_for_it(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        original = session.post_call("pulsar_models_show", {"spec_id": spec(1)})
        session.marker("wrapper-1.started")
        command_id = session.structured("pulsar_command_list")["commands"][0]["command_id"]
        cancel = session.post_call("pulsar_command_cancel", {"command_id": command_id})
        # The original request was not cancelled, its command was: both calls are answered.
        for request in (original, cancel):
            result = session.response(request)["result"]
            self.assertIs(result["isError"], True)
            answer = session.checked_object(result)
            self.assertEqual((answer["state"], answer["exit_status"], answer["output"]),
                             ("cancelled", 128 + signal.SIGTERM, CANCELLED))

    def test_cancel_of_a_command_that_already_ended_returns_its_result_as_finished(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        command_id = self.start_wrapper(session)
        session.release("wrapper-1")
        session.wait_until(lambda: session.structured("pulsar_command_list")["commands"][0]["state"] == "finished",
                           "the command has ended")
        answer = session.structured("pulsar_command_cancel", {"command_id": command_id})
        self.assertEqual((answer["state"], answer["exit_status"]), ("finished", 0))


class SessionEnd(Running):
    def end_with(self, end, status):
        session = Session(self, steps={**wrapper(1), **wrapper(2)})
        session.initialize()  # The server answers only after its signal handlers are installed.
        for number in (1, 2):
            self.start_wrapper(session, number)
        waiting = session.post_call("pulsar_command_get", {"command_id": "command-1"})
        directory, = session.private_directories()
        end(session)
        for number in (1, 2):
            self.assertEqual(session.marker(f"wrapper-{number}.sigterm"), "")
        self.assertEqual(session.read_to_end(), [])  # No answer is written once the session is over.
        self.assertEqual(session.wait_exit(), status)
        self.assertEqual([message for message in session.messages if message.get("id") == waiting], [])
        self.assert_nothing_left(session)
        self.assertFalse(directory.exists())
        self.assertEqual(session.stderr(), "")

    def test_closing_standard_input_cancels_every_running_command_then_the_server_exits(self):
        self.end_with(lambda session: session.close_input(), 0)

    def test_sigint_with_standard_input_open_cancels_every_running_command_then_the_server_exits(self):
        self.end_with(lambda session: os.kill(session.process.pid, signal.SIGINT), 128 + signal.SIGINT)

    def test_sigterm_with_standard_input_open_cancels_every_running_command_then_the_server_exits(self):
        self.end_with(lambda session: os.kill(session.process.pid, signal.SIGTERM), 128 + signal.SIGTERM)

    def test_sighup_with_standard_input_open_cancels_every_running_command_then_the_server_exits(self):
        self.end_with(lambda session: os.kill(session.process.pid, signal.SIGHUP), 128 + signal.SIGHUP)

    def test_the_end_of_a_session_ends_a_shell_style_command_and_its_child(self):
        session = Session(self, double="shell")
        session.initialize()
        _, script, supervisor, child = self.start_shell(session)
        started = time.monotonic()
        session.close_input()
        self.assertEqual(session.wait_exit(), 0)
        self.assert_gone(session, script, supervisor, child)
        self.assertLess(time.monotonic() - started, commands.GRACE_SECONDS)  # Ended by SIGTERM, not killed.
        self.assert_nothing_left(session)

    def test_a_closed_standard_output_ends_the_session_like_a_closed_input(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        self.start_wrapper(session)
        session.process.stdout.close()
        session.post("ping")  # The answer cannot be written.
        self.assertEqual(session.wait_exit(), 0)
        self.assertEqual(session.marker("wrapper-1.sigterm"), "")
        self.assert_nothing_left(session)


class KilledServer(Running):
    def test_a_killed_server_ends_a_wrapper_style_command_with_sigterm(self):
        session = Session(self, steps=wrapper())
        session.initialize()
        self.start_wrapper(session)
        double, supervisor = (int(word) for word in session.marker("wrapper-1.started").split())
        identities = [(pid, process_state(pid)[3]) for pid in (double, supervisor)]
        session.kill()
        self.assertEqual(session.marker("wrapper-1.sigterm"), "")  # The double received SIGTERM.
        self.assert_gone(session, *identities)
        self.assert_nothing_left(session)

    def test_a_killed_server_ends_a_shell_style_command_and_its_child(self):
        session = Session(self, double="shell")
        session.initialize()
        _, script, supervisor, child = self.start_shell(session)
        started = time.monotonic()
        session.kill()
        self.assert_gone(session, child, script, supervisor)
        # The supervisor passed SIGTERM to the whole group; nothing waited for the kill after the grace period.
        self.assertLess(time.monotonic() - started, commands.GRACE_SECONDS)
        self.assert_nothing_left(session)

    def test_a_starting_server_removes_a_killed_servers_directory_and_leaves_a_running_ones(self):
        first = Session(self, steps=wrapper())
        first.initialize()
        kept, = first.private_directories()
        killed = Session(self, base=first)
        killed.initialize()
        self.start_wrapper(killed)
        left, = set(first.private_directories()) - {kept}
        self.assertTrue(any(left.iterdir()))  # It holds the command's output files.
        killed.kill()
        self.assertEqual(first.private_directories(), sorted([kept, left]))  # A killed server removes nothing.
        third = Session(self, base=first)
        third.initialize()
        own, = set(first.private_directories()) - {kept, left}
        self.assertEqual(first.private_directories(), sorted([kept, own]))
        self.assertFalse(left.exists())
        # The running server's directory still works.
        first.steps({"contract --json": {"json": envelope()}})
        self.assertEqual(first.structured("pulsar_contract")["output"], envelope())
        for session in (third, first):
            session.close_input()
            self.assertEqual(session.wait_exit(), 0)
        self.assertEqual(first.private_directories(), [])


    def test_servers_that_start_at_the_same_moment_each_keep_a_directory_of_their_own(self):
        first = Session(self, steps={"contract --json": {"json": envelope()}}, start=False)
        sessions = [first, *(Session(self, base=first, start=False) for _ in range(7))]
        for session in sessions:
            session.start()  # None waits for another: each makes its directory while the others look for stale ones.
        for session in sessions:
            self.assertEqual(session.request("ping")["result"], {})
        self.assertEqual(len(first.private_directories()), len(sessions))
        for session in sessions:
            session.initialize()
            # Its directory is still there: the command's output files are made in it.
            self.assertEqual(session.structured("pulsar_contract")["output"], envelope())
        for session in sessions:
            session.close_input()
        for session in sessions:
            self.assertEqual(session.wait_exit(), 0)
            self.assertEqual(session.stderr(), "")
        self.assertEqual(first.private_directories(), [])


class Supervisor(unittest.TestCase):
    """supervisor.py by itself, with this test as its server."""

    def supervise(self, *command, server=None, grace=5):
        argv = [sys.executable, "-B", str(ROOT / "mcp_server/supervisor.py"),
                str(os.getpid() if server is None else server), str(grace), *command]
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, text=True)
        try:
            out, err = process.communicate(timeout=PATIENCE)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("the supervisor did not exit")
        return process.returncode, out, err

    def test_it_exits_with_the_commands_status_and_prints_nothing_of_its_own(self):
        for status in (0, 1, 3, 64):
            with self.subTest(status=status):
                self.assertEqual(self.supervise(sys.executable, "-c", f"print('out'); raise SystemExit({status})"),
                                 (status, "out\n", ""))

    def test_a_command_ended_by_a_signal_is_128_plus_the_signal(self):
        self.assertEqual(self.supervise("/bin/sh", "-c", "kill -KILL $$"), (128 + signal.SIGKILL, "", ""))

    def test_a_command_that_cannot_be_run_is_127_or_126_with_the_reason(self):
        status, out, err = self.supervise("/nonexistent/fixture/pulsar", "contract")
        self.assertEqual((status, out), (127, ""))
        self.assertEqual(err, "pulsar mcp: cannot run /nonexistent/fixture/pulsar: No such file or directory\n")
        status, out, err = self.supervise("/dev/null")
        self.assertEqual((status, out), (126, ""))
        self.assertIn("pulsar mcp: cannot run /dev/null: ", err)

    def test_it_starts_nothing_when_its_server_is_already_gone(self):
        marker = Path(self.enterContext(tempfile.TemporaryDirectory())) / "ran"
        # Process 1 is not this test: for the supervisor, its server ended before it could ask for the signal.
        status, out, err = self.supervise("/bin/sh", "-c", f": > {marker}", server=1)
        self.assertEqual((status, out, err), (128 + signal.SIGTERM, "", ""))
        self.assertFalse(marker.exists())

    def test_sigterm_at_any_moment_of_a_start_ends_the_command_without_waiting_for_the_kill(self):
        grace = 20
        # From before the supervisor's first instruction to after the command's: the signal is never lost.
        for delay in (0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.06, 0.1):
            with self.subTest(delay=delay):
                argv = [sys.executable, "-B", str(ROOT / "mcp_server/supervisor.py"), str(os.getpid()), str(grace),
                        sys.executable, "-c", "import time; time.sleep(60)"]
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.PIPE, start_new_session=True, text=True)
                self.addCleanup(process.stderr.close)  # This round's pipe, not the last round's.
                time.sleep(delay)
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                try:
                    status = process.wait(PATIENCE)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    self.fail("the supervisor did not exit")
                self.assertLess(time.monotonic() - started, grace)
                # Ended by the signal itself before its handler was there, or after it passed the signal on.
                self.assertIn(status, (-signal.SIGTERM, 128 + signal.SIGTERM))
                self.assertEqual(process.stderr.read(), "")
                # No process is left in the supervisor's group, which had its process ID.
                self.assertFalse([pid for pid in map(int, filter(str.isdigit, os.listdir("/proc")))
                                  if (state := process_state(pid)) and state[2] == process.pid and state[0] != "Z"])

    def test_a_wrong_command_line_is_a_usage_mistake(self):
        for arguments in ((), ("x", "5", "true")):
            argv = [sys.executable, "-B", str(ROOT / "mcp_server/supervisor.py"), *arguments]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=PATIENCE)
            self.assertEqual((result.returncode, result.stdout), (2, ""))
            self.assertIn("usage: supervisor.py SERVER_PID GRACE_SECONDS COMMAND", result.stderr)


class Limits(Running):
    def test_a_fifth_command_is_refused_and_names_the_four(self):
        steps = {}
        for number in range(1, 6):
            steps.update(wrapper(number))
        session = Session(self, steps=steps)
        session.initialize()
        ids = [self.start_wrapper(session, number) for number in range(1, 5)]
        self.assertEqual(ids, ["command-1", "command-2", "command-3", "command-4"])
        for tool, arguments in (("pulsar_models_show", {"spec_id": spec(5)}), ("pulsar_contract", {}),
                                ("pulsar_mcp_server_info", {})):
            with self.subTest(tool=tool):
                response = session.response(session.post_call(tool, arguments))
                self.assertNotIn("error", response)  # A tool result, not a protocol error.
                refused = response["result"]
                self.assertIs(refused["isError"], True)
                self.assertNotIn("structuredContent", refused)
                self.assertEqual(refused["content"][0]["text"], (
                    f"{tool} was not run: 4 commands are already running, the most this server runs at once: "
                    "command-1 (models.show), command-2 (models.show), command-3 (models.show), "
                    "command-4 (models.show). Wait for one with pulsar_command_get or cancel one with "
                    "pulsar_command_cancel."))
        self.assertEqual(len(session.calls()), 4)  # The refused ones never ran.
        # The tools that start nothing still answer.
        self.assertEqual(len(session.structured("pulsar_command_list")["commands"]), 4)
        self.assertEqual(session.structured("pulsar_command_get", {"command_id": "command-2", "wait_seconds": 0})
                         ["state"], "running")
        # One ends; then there is room again.
        session.release("wrapper-3")
        self.assertEqual(session.structured("pulsar_command_get", {"command_id": "command-3"})["state"], "finished")
        self.assertEqual(self.start_wrapper(session, 5), "command-5")

    def test_a_command_reads_dev_null_and_runs_in_a_session_apart_from_the_server(self):
        session = Session(self, steps={"topology show --json": {"report": True}})
        session.initialize()
        report = session.structured("pulsar_topology_show")["output"]
        self.assertEqual(report["stdin"], "/dev/null")
        self.assertEqual(report["argv"], ["topology", "show", "--json"])
        # The command's parent is its supervisor, which leads the session and the process group.
        self.assertEqual((report["session"], report["group"]), (report["parent"], report["parent"]))
        self.assertNotIn(session.process.pid, (report["parent"], report["pid"]))
        self.assertNotEqual(report["session"], os.getsid(session.process.pid))
        # The server and the supervisor hold signals back while they start a process. The command
        # itself starts with none blocked and none ignored, so SIGTERM ends it.
        self.assertEqual((report["sigterm"], report["sighup"], report["sigint_ignored"], report["blocked"]),
                         ("default", "default", False, []))
        # It inherits its three standard streams and nothing else: not the server's lock, pipes or files.
        self.assertEqual(report["descriptors"], [0, 1, 2])


class PrivateDirectory(Running):
    def test_the_private_directory_is_outside_the_checkout_owner_only_and_gone_after_exit(self):
        session = Session(self, steps=wrapper())
        self.assertEqual(session.request("ping")["result"], {})
        directory, = session.private_directories()  # Made before the first answer.
        self.assertEqual((mode(directory), directory.stat().st_uid), (0o700, os.geteuid()))
        checkout = ROOT.resolve()
        self.assertNotEqual(os.path.commonpath([directory.resolve(), checkout]), str(checkout))
        self.assertEqual(directory.parent, session.runtime)
        session.initialize()
        self.start_wrapper(session)
        files = sorted(directory.iterdir())
        self.assertEqual([path.name for path in files], ["command-1.stderr", "command-1.stdout"])
        self.assertEqual({mode(path) for path in files}, {0o600})
        session.close_input()
        self.assertEqual(session.wait_exit(), 0)
        self.assertEqual(session.private_directories(), [])
        self.assertEqual(sorted(path.name for path in (session.home / "work").iterdir()), [])
        self.assertEqual(sorted(path.name for path in (session.home / "tmp").iterdir()), [])

    def test_a_result_above_the_size_limit_is_saved_to_the_private_directory_with_its_outline(self):
        rows = [{"spec_id": spec(number), "home": "fixture-node-1", "address": "192.0.2.11"} for number in range(400)]
        large = {"kind": "pulsar-model-catalog", "schema_version": 1, "specs": rows, "nodes": ["fixture-node-1"],
                 "summary": {"rows": [1, 2, 3]}, "complete": True}
        printed = json.dumps(large, indent=1) + "\n"
        self.assertGreater(len(printed), commands.SIZE_LIMIT_BYTES)
        small = {"kind": "pulsar-model-catalog", "specs": rows[:10]}
        session = Session(self, steps={
            f"models show {spec(1)} --json": {"stdout": printed, "exit": 1, "stderr": "a note\n"},
            f"models show {spec(2)} --json": {"json": small},
            f"models show {spec(3)} --json": {"stdout": "word " * 8000}})
        session.initialize()
        directory, = session.private_directories()
        result = session.call("pulsar_models_show", {"spec_id": spec(1)})
        self.assertIs(result["isError"], True)  # The exit status still decides.
        answer = session.checked_object(result)
        saved = Path(answer["saved_to"])
        self.assertEqual((saved.parent, saved.name, mode(saved)), (directory, "command-1.json", 0o600))
        self.assertEqual(saved.read_text(), printed)  # The complete output, as printed.
        self.assertEqual((answer["output"], answer["text"], answer["bytes"]), (None, None, len(printed)))
        self.assertEqual(answer["outline"], {
            "type": "object", "keys": ["kind", "schema_version", "specs", "nodes", "summary", "complete"],
            "list_lengths": {"specs": 400, "nodes": 1}})
        self.assertEqual((answer["exit_status"], answer["diagnostics"]), (1, "a note\n"))
        self.assertLess(len(json.dumps(result)), 2000)
        # Below the limit a result stays in the answer and nothing is kept.
        answer = session.structured("pulsar_models_show", {"spec_id": spec(2)})
        self.assertEqual((answer["output"], answer["saved_to"], answer["bytes"], answer["outline"]),
                         (small, None, None, None))
        # Large output that is not JSON is saved too, and is still a fault.
        result = session.call("pulsar_models_show", {"spec_id": spec(3)})
        self.assertIs(result["isError"], True)
        answer = session.checked_object(result)
        self.assertEqual((Path(answer["saved_to"]).name, answer["outline"], answer["text"]),
                         ("command-3.txt", {"type": "text", "lines": 1}, None))
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["command-1.json", "command-3.txt"])
        session.close_input()
        self.assertEqual(session.wait_exit(), 0)
        self.assertFalse(saved.exists())

    def test_outline_lists_the_top_level_keys_and_the_length_of_each_list(self):
        self.assertEqual(commands.outline({"ok": True, "result": {"rows": [1, 2]}, "rows": [], "name": "x"}),
                         {"type": "object", "keys": ["ok", "result", "rows", "name"], "list_lengths": {"rows": 0}})
        self.assertEqual(commands.outline([1, 2, 3]), {"type": "list", "length": 3})
        for value, kind in (("x", "string"), (3, "number"), (0.5, "number"), (True, "boolean"), (None, "null")):
            self.assertEqual(commands.outline(value), {"type": kind})

    def test_a_directory_is_removed_only_when_its_server_no_longer_holds_its_lock(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        running_server = commands.PrivateDirectory(str(base))
        dead_server = commands.PrivateDirectory(str(base))
        Path(dead_server.path, "command-1.stdout").write_text("fixture-node-1\n")
        Path(dead_server.path, "nested").mkdir()
        # What a starting server must leave alone, whatever its name says.
        other = base / "another-program"
        other.mkdir()
        (base / "pulsar-mcp-afile123").write_text("not a directory\n")
        (base / "pulsar-mcp-alink123").symlink_to(other)
        (base / "pulsar-mcp-open1234").mkdir(mode=0o755)  # Not owner-only: no server made it.
        (base / "pulsar-mcp-longer-name").mkdir(mode=0o700)  # Not a name mkdtemp gives.
        self.assertTrue(Path(running_server.path).is_dir() and Path(dead_server.path).is_dir())
        os.close(dead_server._lock)  # The kernel does this when a server is killed.
        starting = commands.PrivateDirectory(str(base))
        self.assertEqual(sorted(path.name for path in base.iterdir()),
                         sorted(["another-program", "pulsar-mcp-afile123", "pulsar-mcp-alink123",
                                 "pulsar-mcp-open1234", "pulsar-mcp-longer-name",
                                 Path(running_server.path).name, Path(starting.path).name]))
        for directory in (starting, running_server):
            self.assertEqual(mode(directory.path), 0o700)
            directory.remove()
            self.assertFalse(Path(directory.path).exists())

    def test_no_private_directory_is_made_inside_the_checkout(self):
        inside = str(ROOT / "tests")  # An existing directory; nothing is written to it.
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": inside}), \
                mock.patch("tempfile.gettempdir", return_value=inside):
            with self.assertRaises(commands.NoPrivateDirectory):
                commands.base_directory()
        outside = self.enterContext(tempfile.TemporaryDirectory())
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": inside}), \
                mock.patch("tempfile.gettempdir", return_value=outside):
            self.assertEqual(commands.base_directory(), os.path.realpath(outside))
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": outside}), \
                mock.patch("tempfile.gettempdir", return_value=inside):
            self.assertEqual(commands.base_directory(), os.path.realpath(outside))


if __name__ == "__main__":
    unittest.main()
