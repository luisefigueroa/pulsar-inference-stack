"""Running commands of the MCP server: where their output goes, how they are
started, cancelled and ended, and the result a tool returns.

A running command is one `pulsar` process the server started and still owns.
It runs under supervisor.py, in a session of its own, with standard input at
/dev/null and its output in files of the server's private directory. All
commands are started from the server's one thread: the signal a supervisor
gets when the server dies follows the thread that started it.

Nothing here is kept after the server exits, and no network listener is
opened. Standard library only.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from mcp_server.protocol import END_SIGNALS

CHECKOUT = Path(__file__).resolve().parents[1]
SUPERVISOR = Path(__file__).with_name("supervisor.py")

RUNNING_LIMIT = 4  # The most commands that run at once.
SIZE_LIMIT_BYTES = 32 * 1024  # Output above this is saved to a file and outlined.
GRACE_SECONDS = 10.0  # From a command's SIGTERM to the kill of what is left of it.
BACKSTOP_SECONDS = 2.0  # After the grace period, how long the server waits for a supervisor before it kills.
DIAGNOSTICS_BYTES = 4000  # How much of the end of standard error a result carries.
DIRECTORY_PREFIX = "pulsar-mcp-"
# A private directory as mkdtemp names it. Nothing else is ever removed.
DIRECTORY_NAME = re.compile(re.escape(DIRECTORY_PREFIX) + r"[a-z0-9_]{8}")


class NoPrivateDirectory(Exception):
    """No place outside the checkout for the output of commands."""


def _inside(path: str, parent: str) -> bool:
    return os.path.commonpath([path, parent]) == parent


def base_directory() -> str:
    """Where the private directory is made: the person's runtime directory, else
    the temporary directory. Never inside the checkout."""
    checkout = os.path.realpath(CHECKOUT)

    def usable(candidate: str) -> bool:
        return (os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK)
                and not _inside(os.path.realpath(candidate), checkout))

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and usable(runtime) and os.stat(runtime).st_uid == os.geteuid():
        return os.path.realpath(runtime)
    temporary = tempfile.gettempdir()
    if usable(temporary):
        return os.path.realpath(temporary)
    raise NoPrivateDirectory("no writable runtime or temporary directory outside the checkout; "
                             "set TMPDIR to one")


def _still_at(descriptor: int, path: str) -> bool:
    """Whether PATH still names the directory DESCRIPTOR is open on."""
    try:
        there, held = os.stat(path, follow_symlinks=False), os.fstat(descriptor)
    except OSError:
        return False
    return (there.st_dev, there.st_ino) == (held.st_dev, held.st_ino)


class PrivateDirectory:
    """The server's directory for command output, which names real nodes.

    Made with mkdtemp, so only its owner can read it, and removed when the
    server exits. The server holds a lock on the directory for as long as it
    lives; the kernel drops the lock when the process ends, however it ends.
    A killed server leaves its directory behind, so each starting server
    removes the directories whose lock it can take: their servers no longer
    run. A directory whose lock is held belongs to a running server and is
    left alone.
    """

    def __init__(self, base: str | None = None):
        self.base = base or base_directory()
        self.path, self._lock = self._make()
        self._remove_left_behind()

    def _make(self) -> tuple[str, int]:
        for _ in range(100):
            path = tempfile.mkdtemp(prefix=DIRECTORY_PREFIX, dir=self.base)
            # Until the lock below is held, a server that starts at the same
            # moment takes the new directory for one left behind and removes
            # it. Then make another.
            try:
                lock = os.open(path, os.O_RDONLY | os.O_DIRECTORY)  # Not inherited by any command.
            except OSError:
                continue
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if _still_at(lock, path):
                    return path, lock
            except OSError:
                pass
            os.close(lock)
            try:
                os.rmdir(path)
            except OSError:
                pass
        raise NoPrivateDirectory(f"cannot keep a private directory under {self.base}")

    def _remove_left_behind(self) -> None:
        for name in os.listdir(self.base):
            path = os.path.join(self.base, name)
            if not DIRECTORY_NAME.fullmatch(name) or path == self.path:
                continue
            try:
                lock = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                continue  # Not a directory, not this person's, or already gone.
            try:
                found = os.fstat(lock)
                if found.st_uid != os.geteuid() or stat.S_IMODE(found.st_mode) != 0o700:
                    continue  # Not made the way a server makes its directory.
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Fails while its server runs.
                if _still_at(lock, path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
            finally:
                os.close(lock)

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        os.close(self._lock)


def _default_signals() -> None:
    """Run in a new process before the supervisor replaces it: until then it
    has the server's signal handlers, which write to the server's own loop."""
    for signum in END_SIGNALS:
        signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, END_SIGNALS)


class Command:
    """One command the server started, until its result is returned or dropped."""

    def __init__(self, command_id, operation, output_form, process, stdout, stderr, extra):
        self.command_id = command_id
        self.operation = operation
        self.output_form = output_form
        self.process = process  # The supervisor; it leads the command's session and process group.
        self.stdout = stdout
        self.stderr = stderr
        self.extra = extra  # Fields beside the result's own, never inside `output`.
        self.started = time.monotonic()
        self.cancelled_at = None
        self.backstop_at = None
        self.ended = False
        self.handle_returned = False  # Whether the client was given, or named, its command_id.

    @property
    def state(self) -> str:
        if not self.ended:
            return "running"
        return "finished" if self.cancelled_at is None else "cancelled"


def _refuse_constant(name):
    raise ValueError(f"{name} is not JSON")


def _finite(text):
    number = float(text)
    if number in (float("inf"), float("-inf")):
        raise ValueError(f"{text} is out of range")
    return number


def parse_json(printed: bytes):
    """(True, value) when PRINTED is JSON that can be sent on unchanged, else (False, None).

    Not so: bytes that are not UTF-8, the constants NaN and Infinity, and a
    number too large to keep.
    """
    try:
        return True, json.loads(printed.decode("utf-8"), parse_constant=_refuse_constant, parse_float=_finite)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError.
        return False, None


def outline(value) -> dict:
    """A mechanical outline of a saved result: the top-level keys, and the length of each list."""
    if isinstance(value, dict):
        return {"type": "object", "keys": list(value),
                "list_lengths": {key: len(item) for key, item in value.items() if isinstance(item, list)}}
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    kind = ("null" if value is None else "boolean" if isinstance(value, bool)
            else "string" if isinstance(value, str) else "number")
    return {"type": kind}


class Commands:
    """The commands one server holds: running ones, and ended ones whose result waits to be returned."""

    def __init__(self, cli: str, directory: str, grace: float = GRACE_SECONDS):
        self.cli = cli
        self.directory = directory
        self.grace = grace
        self.held: dict[str, Command] = {}
        self._started = 0

    def running(self) -> list[Command]:
        return [command for command in self.held.values() if not command.ended]

    def start(self, operation: str, output_form: str, arguments: list[str], extra: dict | None = None) -> Command:
        """Start `pulsar ARGUMENTS` under a supervisor. The caller has checked the limit."""
        self._started += 1
        command_id = f"command-{self._started}"
        paths = [os.path.join(self.directory, f"{command_id}.{stream}") for stream in ("stdout", "stderr")]
        files = [os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) for path in paths]
        # -I and -S: the supervisor needs only the standard library, whatever the person's Python settings are.
        argv = [sys.executable, "-B", "-I", "-S", str(SUPERVISOR), str(os.getpid()), repr(self.grace),
                self.cli, *arguments]
        # Hold the end signals back while the new process still has this
        # server's handlers; it takes the default action before it goes on.
        held = signal.pthread_sigmask(signal.SIG_BLOCK, END_SIGNALS)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=files[0], stderr=files[1],
                                       start_new_session=True, preexec_fn=_default_signals)
        except OSError:
            for path in paths:
                os.unlink(path)
            raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
            for descriptor in files:
                os.close(descriptor)
        command = Command(command_id, operation, output_form, process, paths[0], paths[1], extra or {})
        self.held[command_id] = command
        return command

    def cancel(self, command: Command) -> None:
        """Ask a running command to end: one SIGTERM to its supervisor, which
        passes it to the command's whole process group and kills what is left
        after the grace period."""
        if command.ended or command.cancelled_at is not None or command.process.poll() is not None:
            return  # Already cancelled, or it ended by itself first: then it is finished, not cancelled.
        command.cancelled_at = time.monotonic()
        command.backstop_at = command.cancelled_at + self.grace + BACKSTOP_SECONDS
        command.process.send_signal(signal.SIGTERM)  # Never signals a process that was already collected.

    def poll(self) -> list[Command]:
        """The commands that ended since the last call."""
        ended = []
        for command in self.running():
            if command.process.poll() is not None:
                command.ended = True
                ended.append(command)
            elif command.backstop_at is not None and time.monotonic() >= command.backstop_at:
                # The supervisor should have exited by now. It leads the group and
                # was not collected, so its process ID still names that group.
                command.backstop_at = None
                try:
                    os.killpg(command.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                with open(command.stderr, "a", encoding="utf-8") as diagnostics:
                    diagnostics.write("pulsar mcp: the command's supervisor did not exit after the grace "
                                      "period; the server killed the command's process group\n")
        return ended

    def next_deadline(self) -> float | None:
        deadlines = [command.backstop_at for command in self.running() if command.backstop_at is not None]
        return min(deadlines) if deadlines else None

    def running_result(self, command: Command) -> dict:
        return {"operation": command.operation, "state": "running", "exit_status": None,
                "output_form": command.output_form, "output": None, "text": None, "diagnostics": None,
                "saved_to": None, "bytes": None, "outline": None, "command_id": command.command_id,
                **command.extra}

    def result(self, command: Command) -> tuple[dict, bool]:
        """The result of an ended command and whether it is an error.

        What the command printed is passed on unchanged: as `output` when it is
        the JSON its output form promises, else as `text`. Output above the
        size limit stays in the private directory, with an outline.
        """
        returncode = command.process.returncode
        exit_status = returncode if returncode >= 0 else 128 - returncode
        with open(command.stdout, "rb") as stream:
            printed = stream.read()
        text = printed.decode("utf-8", errors="replace")
        expects_json = command.output_form != "text"
        is_json, value = parse_json(printed) if expects_json and printed.strip() else (False, None)
        payload = {"operation": command.operation, "state": command.state, "exit_status": exit_status,
                   "output_form": command.output_form, "output": None, "text": None,
                   "diagnostics": self._diagnostics(command), "saved_to": None, "bytes": None, "outline": None,
                   "command_id": None, **command.extra}
        if len(printed) > SIZE_LIMIT_BYTES:
            saved = os.path.join(self.directory, f"{command.command_id}.{'json' if is_json else 'txt'}")
            os.replace(command.stdout, saved)
            payload.update(saved_to=saved, bytes=len(printed),
                           outline=outline(value) if is_json else {"type": "text", "lines": len(text.splitlines())})
        elif is_json:
            payload["output"] = value
        elif printed:
            payload["text"] = text
        # Something other than JSON where JSON was promised is a fault, whatever the exit status.
        return payload, exit_status != 0 or (expects_json and not is_json)

    @staticmethod
    def _diagnostics(command: Command) -> str:
        with open(command.stderr, "rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - DIAGNOSTICS_BYTES))
            return stream.read().decode("utf-8", errors="replace")

    def forget(self, command: Command) -> None:
        """Drop a command whose result was returned or will never be asked for. A saved result stays."""
        for path in (command.stdout, command.stderr):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        del self.held[command.command_id]

    def listing(self) -> list[dict]:
        now = time.monotonic()
        return [{"command_id": command.command_id, "operation": command.operation, "state": command.state,
                 "elapsed_seconds": round(now - command.started, 1)} for command in self.held.values()]

    def end_all(self, sleep) -> None:
        """The session is over: cancel every running command and wait until all have ended.

        SLEEP(seconds) waits, and returns early when a command ends. The wait is
        bounded: each supervisor kills what is left of its command after the
        grace period, and poll() kills a supervisor that outlives that.
        """
        for command in self.running():
            self.cancel(command)
        while self.running():
            self.poll()
            if self.running():
                sleep(0.05)
