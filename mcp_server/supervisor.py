#!/usr/bin/env python3
"""The small process each command of the MCP server runs under.

usage: supervisor.py SERVER_PID GRACE_SECONDS COMMAND [ARGUMENT ...]

The server starts this program in a session of its own. It starts COMMAND in
that same session and process group, waits for it and exits with its exit
status, or 128 plus the number of the signal that ended it.

It asks the kernel for SIGTERM when the server dies, because that signal
reaches one process and not a group. On SIGTERM, from a cancel or from the
death of the server, it sends SIGTERM once to the whole group, waits until the
group is empty or GRACE_SECONDS have passed, kills what is left and exits. So
a killed server cancels each command the way a closed terminal would. A
supervisor that is itself killed outright leaves its command running.

Nothing is written to standard output, which is the command's. Linux only.
Standard library only.
"""
from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import sys
import time

END_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
PR_SET_PDEATHSIG = 1  # <linux/prctl.h>
POLL_SECONDS = 0.05  # How often the group is looked at while it ends.
# Exit statuses for a command that never ran, as a shell reports them.
CANNOT_RUN, NOT_FOUND = 126, 127


def say(message: str) -> None:
    """One line at the end of the command's standard error."""
    print(f"pulsar mcp: {message}", file=sys.stderr, flush=True)


def ask_for_sigterm_when_the_parent_dies() -> None:
    """The setting belongs to the thread that started this process, the server's only one."""
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")


def default_signals() -> None:
    """Run in the new process before the command replaces it.

    Until then the process still has this program's signal handlers, which only
    note a signal. With the default action back, a SIGTERM for the group ends
    it, whenever it arrives.
    """
    for signum in END_SIGNALS:
        signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, END_SIGNALS)


def group_members(group: int, own: int) -> list[int]:
    """The processes of GROUP that still run, without this one. A process that
    has ended and only waits to be collected counts as gone."""
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == own:
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as stat:
                # pid (name) state parent group session ...; the name may hold spaces and brackets.
                state, _parent, process_group = stat.read().rsplit(b")", 1)[1].split()[:3]
        except (OSError, ValueError, IndexError):
            continue  # It ended while this was reading.
        if state != b"Z" and int(process_group) == group:
            members.append(int(name))
    return members


def kill_members(group: int, own: int) -> list[int]:
    """Kill every process left in GROUP but this one, including any they start meanwhile."""
    killed = []
    for _ in range(100):
        members = group_members(group, own)
        if not members:
            break
        for pid in members:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            if pid not in killed:
                killed.append(pid)
        time.sleep(0.01)
    return killed


def exit_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


def main(argv: list[str]) -> int:
    try:
        server, grace, command = int(argv[0]), float(argv[1]), argv[2:]
        if not command:
            raise ValueError
    except (IndexError, ValueError):
        say("usage: supervisor.py SERVER_PID GRACE_SECONDS COMMAND [ARGUMENT ...]")
        return 2
    wake, noted = os.pipe()
    os.set_blocking(wake, False)
    os.set_blocking(noted, False)
    signal.set_wakeup_fd(noted, warn_on_full_buffer=False)
    # A handler, never "ignore": an ignored signal would stay ignored in the command.
    for signum in (*END_SIGNALS, signal.SIGCHLD):
        signal.signal(signum, lambda _signum, _frame: None)
    try:
        ask_for_sigterm_when_the_parent_dies()
    except (OSError, AttributeError) as failure:
        say(f"the command was not started: cannot ask for a signal at the server's end ({failure})")
        return CANNOT_RUN
    if os.getppid() != server:
        return 128 + signal.SIGTERM  # The server ended before the request above; nothing was started.
    # Hold the end signals back while the command starts, so that one arriving
    # between the new process and the command's first instruction is not lost.
    held = signal.pthread_sigmask(signal.SIG_BLOCK, END_SIGNALS)
    try:
        child = subprocess.Popen(command, preexec_fn=default_signals)
    except OSError as failure:
        say(f"cannot run {command[0]}: {failure.strerror or failure}")
        return NOT_FOUND if isinstance(failure, FileNotFoundError) else CANNOT_RUN
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)
    group, own = os.getpgrp(), os.getpid()
    kill_at = None  # Set once SIGTERM was sent to the group.
    while True:
        returncode = child.poll()
        if kill_at is None:
            if returncode is not None:
                return exit_status(returncode)
            timeout = None
        else:
            if returncode is not None and not group_members(group, own):
                return exit_status(returncode)
            if time.monotonic() >= kill_at:
                kill_members(group, own)
                if returncode is None:
                    child.kill()  # Also a command that left the group.
                say(f"the command still had running processes {grace:g} seconds after SIGTERM; they were killed")
                return exit_status(child.wait())
            timeout = max(0.0, min(POLL_SECONDS, kill_at - time.monotonic()))
        readable, _, _ = select.select([wake], [], [], timeout)
        if readable and any(signum in END_SIGNALS for signum in os.read(wake, 4096)) and kill_at is None:
            kill_at = time.monotonic() + grace
            os.killpg(group, signal.SIGTERM)  # Reaches this process too; its handler only notes it.


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
