"""Run the MCP server under test against a double of the pulsar file.

Every test starts the server through Session, which refuses to run without a
double: a server left to its default would run this checkout's real pulsar
commands. A test that runs `pulsar mcp` where no server is to start, such as
on a terminal, takes the same environment() and so the same refusal. The
doubles print synthetic JSON only.

Two styles of double, written into the test's own directory:

- python_double: the wrapper style. Like scripts/public_cli.py it can handle
  SIGTERM itself and print a `cancelled` error envelope. A steps file says what
  it does for each command line.
- shell_double: the direct shell style. A Bash script with no trap that starts
  one child and waits for it.

A double says "I am ready" with a marker file. It writes the marker only after
its signal handler is installed, or after its child was started, so a test
that waits for the marker never sends a signal too early.
"""
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
PULSAR = ROOT / "pulsar"
# Generous: a test that waits this long has failed. Under load a step takes much longer than alone.
PATIENCE = 60.0

PYTHON_DOUBLE = r'''#!/usr/bin/env python3
"""A stand-in for the pulsar file: it does what steps.json says for its command line."""
import json
import os
import signal
import sys
import time

home = os.environ["MCP_DOUBLE_DIR"]
argv = sys.argv[1:]
with open(os.path.join(home, "calls.jsonl"), "a") as calls:
    calls.write(json.dumps(argv) + "\n")
with open(os.path.join(home, "steps.json")) as steps:
    step = json.load(steps).get(" ".join(argv))
if step is None:
    print("double: no step for: " + " ".join(argv), file=sys.stderr)
    sys.exit(64)
name = step.get("name", "double")


def mark(suffix, text=""):
    """A marker appears whole: written beside its place, then moved there."""
    path = os.path.join(home, name + "." + suffix)
    with open(path + ".part", "w") as marker:
        marker.write(text)
    os.replace(path + ".part", path)


def on_sigterm(_signum, _frame):
    mark("sigterm")
    if step["sigterm"] == "envelope":
        # What scripts/public_cli.py prints when it is cancelled.
        print(json.dumps({"schema_version": 1, "ok": False, "error": {
            "code": "cancelled", "message": "Stack command cancelled; local command and tracked node workers reaped",
            "details": [{"field": "$", "message": "fixture"}]}}, sort_keys=True), flush=True)
        os._exit(128 + signal.SIGTERM)


if step.get("sigterm"):  # "envelope", or "ignore": note it and go on.
    signal.signal(signal.SIGTERM, on_sigterm)
if step.get("started"):
    mark("started", "%d %d\n" % (os.getpid(), os.getppid()))
if step.get("release"):  # Wait until the test lets go; give up after two minutes.
    release = os.path.join(home, name + ".release")
    deadline = time.monotonic() + 120
    while not os.path.exists(release):
        if time.monotonic() > deadline:
            sys.exit(70)
        time.sleep(0.01)
if step.get("report"):
    actions = {signal.SIG_DFL: "default", signal.SIG_IGN: "ignored"}
    step["stdout"] = json.dumps({
        "kind": "fixture-report", "argv": argv, "stdin": os.readlink("/proc/self/fd/0"),
        "pid": os.getpid(), "parent": os.getppid(), "group": os.getpgrp(), "session": os.getsid(0),
        # As the command found them: Python changes neither of these two when it starts.
        "sigterm": actions.get(signal.getsignal(signal.SIGTERM), "handled"),
        "sighup": actions.get(signal.getsignal(signal.SIGHUP), "handled"),
        "sigint_ignored": signal.getsignal(signal.SIGINT) is signal.SIG_IGN,
        "blocked": sorted(int(signum) for signum in signal.pthread_sigmask(signal.SIG_BLOCK, [])),
        "descriptors": sorted(int(name) for name in os.listdir("/proc/self/fd"))[:-1]})
sys.stderr.write(step.get("stderr", ""))
sys.stderr.flush()
# A lone low surrogate in a step stands for one byte that is not UTF-8.
sys.stdout.buffer.write(step.get("stdout", "").encode("utf-8", "surrogateescape"))
sys.stdout.flush()
sys.exit(step.get("exit", 0))
'''

SHELL_DOUBLE = r'''#!/usr/bin/env bash
# A stand-in for a pulsar command that is a plain shell script: no trap, one
# child, then wait. Run as `models show SPEC --json`; SPEC names the marker.
# The marker is written with a redirection, never by a child process, and only
# after the child was started: it holds this script's process ID, its parent's
# and the child's.
sleep 120 &
child=$!
printf '%s %s %s\n' "$$" "$PPID" "$child" > "$MCP_DOUBLE_DIR/$3.started"
wait "$child"
printf '{"kind": "fixture-shell", "ended": "by itself"}\n'
'''

# A document, an envelope and their parts, with made-up names and documentation addresses only.
TOPOLOGY = {"kind": "pulsar-topology-observation", "schema_version": 1, "status": "ready",
            "nodes": [{"hostname": "fixture-node-1", "address": "192.0.2.11"},
                      {"hostname": "fixture-node-2", "address": "192.0.2.12"}]}
CONTRACT = {"schema_version": 1, "ok": True,
            "result": {"kind": "pulsar-stack-integration-contract", "schema_version": 2,
                       "operations": ["contract", "topology.show", "models.show"]}}
SPEC = "ab" * 32


def spec(number: int) -> str:
    """A made-up spec ID, different for each number."""
    return f"{number:064x}"


def envelope(result=None, *, code=None) -> dict:
    if code is None:
        return {"schema_version": 1, "ok": True, "result": result if result is not None else {"fixture": True}}
    return {"schema_version": 1, "ok": False,
            "error": {"code": code, "message": f"fixture {code}", "details": [{"field": "$", "message": "fixture"}]}}


def process_state(pid: int):
    """(state, parent, group, start time) of a process from /proc, or None when it is gone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields[0].decode(), int(fields[1]), int(fields[2]), fields[19].decode()


def running(pid: int, started: str | None = None) -> bool:
    """Whether PID still runs. A process that has ended and only waits to be
    collected is gone, and so is one whose number went to a later process."""
    state = process_state(pid)
    return state is not None and state[0] != "Z" and (started is None or state[3] == started)


def checked_double(double) -> Path:
    """DOUBLE as a path, or AssertionError: no test runs `pulsar mcp` without a
    double, and nothing inside the checkout, such as its pulsar file, is one."""
    if not double:
        raise AssertionError("refusing to run pulsar mcp without a double of the pulsar file")
    path = Path(double).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"the CLI double {path} is not an executable file")
    if os.path.commonpath([path, ROOT.resolve()]) == str(ROOT.resolve()):
        raise AssertionError(f"refusing to run pulsar mcp: {path} is inside the checkout, not a double")
    return path


def environment(home, double) -> dict:
    """The environment of every `pulsar mcp` a test runs. HOME is the test's own directory.

    PULSAR_MCP_CLI names the double. The runtime and temporary directories are
    the test's own, so no server under test looks at the person's. If the real
    pulsar were reached after all, the other doubles would stop it at its first step.
    """
    home = Path(home)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_"))}
    env.update(
        PULSAR_MCP_CLI=str(checked_double(double)), MCP_DOUBLE_DIR=str(home),
        XDG_RUNTIME_DIR=str(home / "runtime"), TMPDIR=str(home / "tmp"), PYTHONDONTWRITEBYTECODE="1",
        PULSAR_SSH="/bin/false", PULSAR_DOCKER="/bin/false",
        CLUSTER_TOPOLOGY_FILE=str(home / "absent-topology.json"),
        PULSAR_MODEL_LIBRARY_DIR=str(home / "absent-library"))
    return env


def make_home(test) -> Path:
    """A directory for one test, outside the checkout, with the directories environment() names."""
    home = Path(tempfile.mkdtemp(prefix="mcp-test-"))
    test.addCleanup(shutil.rmtree, home, ignore_errors=True)
    for name in ("runtime", "tmp", "work"):
        (home / name).mkdir(mode=0o700)
    (home / "steps.json").write_text("{}")
    return home


def write_double(home, name="pulsar-double.py", text=None) -> Path:
    path = Path(home) / name
    path.write_text(PYTHON_DOUBLE if text is None else text)
    path.chmod(0o700)
    return path


class Session:
    """One server process and the test's end of its standard input and output."""

    def __init__(self, test, double=None, *, steps=None, grace=None, base=None, launcher=None, start=True):
        self.test = test
        self.home = base.home if base else make_home(test)
        self.runtime = self.home / "runtime"
        if double == "shell":
            double = write_double(self.home, "pulsar-shell-double.sh", SHELL_DOUBLE)
        elif double is None and base is None:
            double = write_double(self.home)
        elif double is None:
            double = base.double
        self.double = checked_double(double)
        if steps is not None:
            self.steps(steps)
        self.env = environment(self.home, self.double)
        if grace is not None:
            self.env["PULSAR_MCP_GRACE_SECONDS"] = str(grace)
        self.command = [sys.executable, "-B", str(launcher)] if launcher else [str(PULSAR), "mcp"]
        self.process = None
        self.raw = b""  # Every byte the server wrote to standard output.
        self.messages = []  # Every message read so far, in order.
        self._unread = b""
        self._next_id = 0
        descriptor, path = tempfile.mkstemp(prefix="server-", suffix=".stderr", dir=self.home)
        os.close(descriptor)
        self.stderr_path = Path(path)
        test.addCleanup(self.stop)
        if start:
            self.start()

    def steps(self, added: dict) -> None:
        """What the python double does for each command line, such as "topology show --json"."""
        written = json.loads((self.home / "steps.json").read_text())
        for line, step in added.items():
            if "json" in step:
                step = {**step, "stdout": json.dumps(step["json"]) + "\n"}
                del step["json"]
            written[line] = step
        path = self.home / "steps.json.part"
        path.write_text(json.dumps(written))
        path.replace(self.home / "steps.json")

    def calls(self) -> list:
        """The command lines the python double was run with, in order."""
        path = self.home / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # -- the process

    def start(self):
        if self.env.get("PULSAR_MCP_CLI") != str(checked_double(self.double)):
            raise AssertionError("refusing to start the server without PULSAR_MCP_CLI pointing at a double")
        with open(self.stderr_path, "wb") as errors:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=errors, env=self.env, cwd=self.home / "work",
                                            start_new_session=True)
        return self

    def stderr(self) -> str:
        return self.stderr_path.read_text(errors="replace")

    def session_processes(self) -> list:
        """Every running process that this session's server started, or the server itself."""
        tag = f"MCP_DOUBLE_DIR={self.home}".encode()
        found = []
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            try:
                environment = Path(f"/proc/{name}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if tag in environment and running(int(name)):
                found.append(int(name))
        return found

    def stop(self):
        """Leave nothing running: the server's group, then whatever it started."""
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait()
        for _ in range(50):
            left = self.session_processes()
            if not left:
                break
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.02)

    def fail(self, message):
        """Stop everything, then fail with the server's standard error."""
        self.stop()
        self.test.fail(f"{message}\nserver standard error:\n{self.stderr()}")

    def wait_exit(self, timeout=PATIENCE) -> int:
        """The server's exit status. A server that does not exit is killed with its whole group."""
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.fail("the server did not exit")

    def kill(self):
        """Kill the server outright, and only the server."""
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait()

    def close_input(self):
        self.process.stdin.close()

    # -- the conversation

    def send_line(self, line: bytes):
        self.process.stdin.write(line + b"\n")
        self.process.stdin.flush()

    def send(self, message: dict):
        self.send_line(json.dumps(message).encode())

    def read_message(self, timeout=PATIENCE):
        """The next message from the server, or None at the end of its output."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self._unread:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.process.stdout], [], [], left)[0]:
                self.fail("no message from the server in time")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                self.test.assertEqual(self._unread, b"", "the output ended in the middle of a line")
                return None
            self.raw += chunk
            self._unread += chunk
        line, self._unread = self._unread.split(b"\n", 1)
        message = json.loads(line)
        self.messages.append(message)
        return message

    def read_to_end(self, timeout=PATIENCE) -> list:
        """Every message until the server closes its output."""
        read = []
        while (message := self.read_message(timeout)) is not None:
            read.append(message)
        return read

    def response(self, request_id, timeout=PATIENCE) -> dict:
        """The response to REQUEST_ID; anything read on the way stays in messages."""
        for message in self.messages:
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
        while True:
            message = self.read_message(timeout)
            if message is None:
                self.fail(f"the server's output ended before the response to {request_id!r}")
            if message.get("id") == request_id:
                return message

    def post(self, method, params=None) -> int:
        """Send a request and return its id without waiting for the response."""
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self._next_id

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(self, method, params=None) -> dict:
        return self.response(self.post(method, params))

    def initialize(self, version="2025-11-25") -> dict:
        response = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                               "clientInfo": {"name": "fixture-client", "version": "0"}})
        self.notify("notifications/initialized")
        return response

    def post_call(self, tool, arguments=None) -> int:
        return self.post("tools/call", {"name": tool, "arguments": arguments or {}})

    def call(self, tool, arguments=None) -> dict:
        """Call a tool and return the result of the call: content, structuredContent and isError."""
        response = self.response(self.post_call(tool, arguments))
        self.test.assertNotIn("error", response, response)
        return response["result"]

    def structured(self, tool, arguments=None) -> dict:
        """Call a tool and return its one object, after checking that the text block holds the same JSON."""
        result = self.call(tool, arguments)
        return self.checked_object(result)

    def checked_object(self, result: dict) -> dict:
        self.test.assertEqual([block["type"] for block in result["content"]], ["text"], result)
        self.test.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    # -- markers

    def marker(self, name, timeout=PATIENCE) -> str:
        """Wait for a double's marker file and return what it holds."""
        path = self.home / name
        deadline = time.monotonic() + timeout
        while True:
            try:
                text = path.read_text()
                if text.endswith("\n") or not name.endswith(".started"):
                    return text  # The shell double's marker is complete once its line has ended.
            except FileNotFoundError:
                pass
            if time.monotonic() > deadline:
                self.fail(f"the double never wrote {name}")
            time.sleep(0.005)

    def release(self, name="double"):
        (self.home / f"{name}.release").write_text("")

    def private_directories(self) -> list:
        return sorted(path for path in self.runtime.iterdir() if path.name.startswith("pulsar-mcp-"))

    def wait_until(self, condition, what, timeout=PATIENCE):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting until {what}")
            time.sleep(0.005)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
