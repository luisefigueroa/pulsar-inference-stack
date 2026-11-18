#!/usr/bin/env python3
"""`pulsar mcp`: serve Pulsar's read-only tools to an agent harness.

One loop reads standard input, takes signals and keeps time. No thread is
started, so every command is started by the thread that lives as long as the
server. The loop ends when standard input closes or SIGINT, SIGTERM or SIGHUP
arrives; every running command is then cancelled before the server exits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True  # Importing the package must leave nothing in the checkout.
CHECKOUT = Path(__file__).resolve().parents[1]
if not __package__:
    # Run as a file by the pulsar dispatcher: import the package from its
    # checkout, not its modules from their own directory.
    sys.path[0] = str(CHECKOUT)

from mcp_server import commands as running_commands, protocol  # noqa: E402
from mcp_server.tool_table import TOOLS, WAIT_SECONDS_DEFAULT, Refused  # noqa: E402

HELP = """\
usage: pulsar mcp [--help]

Serve Pulsar's read-only tools to an agent harness over standard input and output, as a Model Context Protocol (MCP) server.

  --help  Print this help

An agent harness starts this command and talks to it; it is not typed at a terminal. Register it in the harness as a local server whose command is this checkout's pulsar file with the argument mcp.

Each tool runs one pulsar command that inspects, checks or previews. No tool changes model files, a service, membership or configuration: make changes with the pulsar commands themselves.

A command that takes long keeps running after its call has answered, and the agent asks for its result later. When the harness ends the session, every command still running is cancelled. Command output is kept in a private directory outside this checkout until then.
"""

CLI_VARIABLE = "PULSAR_MCP_CLI"  # Tests point the server at a double of the pulsar file.
GRACE_VARIABLE = "PULSAR_MCP_GRACE_SECONDS"  # Tests shorten the grace period.
POLL_SECONDS = 0.25  # While a command runs, the loop looks at least this often; a command's end wakes it sooner.
# What pulsar_mcp_server_info runs to return the contract of the Stack it serves.
CONTRACT_COMMAND = ("contract", "--json")


def cli_path() -> str:
    """The pulsar file whose commands the tools run: this checkout's own, unless a test names a double."""
    return os.path.abspath(os.environ.get(CLI_VARIABLE) or CHECKOUT / "pulsar")


class Waiter:
    """A `tools/call` that has not been answered: it waits for a command."""

    def __init__(self, request_id, command, deadline, started_it):
        self.request_id = request_id
        self.command = command
        self.deadline = deadline  # When to answer `running`; None waits until the command ends.
        self.started_it = started_it  # Whether this call started the command, so cancelling it ends the command.


class Server:
    def __init__(self, tools, commands, source, output):
        self.tools = {tool.name: tool for tool in tools}
        self.commands = commands
        self.source = source
        self.output = output
        self.initialized = False
        self.waiters: dict[str, Waiter] = {}

    def serve(self) -> int:
        """Run until the session ends. Returns the exit status: 0 at the end of
        input, 128 plus its number after a signal."""
        while True:
            lines, end = self.source.wait(self._timeout())
            # Look at which commands ended before the lines are handled: a command's end and a call
            # can wake the loop together, and the call is then counted against the commands that
            # still run as of when the server reads it. A command can still end a moment after this
            # look. The calls whose wait is over are answered after the lines, as before: a cancel
            # that arrives together with the end of its call's wait still ends the command.
            self._settle_ended()
            for line in lines:
                self._receive(line)
            self._settle()
            if end is not None:
                return 0 if end == "input" else 128 + end

    def end(self) -> None:
        """The session is over: nothing more is answered, and every running command is cancelled."""
        self.waiters.clear()
        self.commands.end_all(self.source.drain_signals)

    def _timeout(self) -> float | None:
        deadlines = [waiter.deadline for waiter in self.waiters.values() if waiter.deadline is not None]
        backstop = self.commands.next_deadline()
        if backstop is not None:
            deadlines.append(backstop)
        if not self.commands.running():
            return None
        wait = POLL_SECONDS
        if deadlines:
            wait = min(wait, max(0.0, min(deadlines) - time.monotonic()))
        return wait

    def _settle(self) -> None:
        """Answer the calls whose command ended, then those whose wait is over."""
        self._settle_ended()
        now = time.monotonic()
        for waiter in list(self.waiters.values()):
            if waiter.deadline is not None and now >= waiter.deadline:
                waiter.command.handle_returned = True
                self._answer(waiter, protocol.tool_result(self.commands.running_result(waiter.command),
                                                           is_error=False))

    def _settle_ended(self) -> None:
        """Answer the calls whose command ended, and drop an ended command nobody can ask for."""
        for command in self.commands.poll():
            waiters = [waiter for waiter in self.waiters.values() if waiter.command is command]
            if waiters:
                payload, is_error = self.commands.result(command)
                # Forget it first: when the answer arrives, the private directory holds saved results only.
                self.commands.forget(command)
                for waiter in waiters:
                    self._answer(waiter, protocol.tool_result(payload, is_error=is_error))
            elif not command.handle_returned:
                self.commands.forget(command)  # Its call was cancelled, so nobody can ask for the result.

    def _answer(self, waiter: Waiter, body: dict) -> None:
        del self.waiters[protocol.id_key(waiter.request_id)]
        self.output.send(protocol.result(waiter.request_id, body))

    def _receive(self, text: str) -> None:
        if not text.strip():
            return
        message, response = protocol.parse(text)
        if message is not None:
            response = self._request(message) if "id" in message else self._notification(message)
        if response is not None:
            self.output.send(response)

    def _notification(self, message: dict) -> None:
        if message["method"] == "notifications/cancelled":
            request_id = message["params"].get("requestId")
            waiter = self.waiters.pop(protocol.id_key(request_id), None) if protocol.valid_id(request_id) else None
            # The cancelled call gets no answer. If it started the command, no
            # command_id was returned for it yet, so the command ends with it.
            if waiter is not None and waiter.started_it:
                self.commands.cancel(waiter.command)
        return None  # A notification never gets a response; one this server does not know is ignored.

    def _request(self, message: dict):
        request_id, method, params = message["id"], message["method"], message["params"]
        # params may carry _meta, with progressToken or keys of the client's own. None is read.
        if method == "initialize":
            self.initialized = True
            return protocol.result(request_id, protocol.initialize_result(params))
        if method == "ping":
            return protocol.result(request_id, {})
        if method not in ("tools/list", "tools/call"):
            return protocol.error(request_id, protocol.METHOD_NOT_FOUND, f"Method not found: {method}")
        if not self.initialized:
            return protocol.error(request_id, protocol.INVALID_REQUEST,
                                  f"Invalid Request: {method} came before initialize")
        if method == "tools/list":
            if params.get("cursor") is not None:
                return protocol.error(request_id, protocol.INVALID_PARAMS,
                                      "Invalid params: the tool list is one page, so no cursor is valid")
            return protocol.result(request_id, {"tools": [tool.listed() for tool in self.tools.values()]})
        return self._call(request_id, params)

    def _call(self, request_id, params: dict):
        name, arguments = params.get("name"), params.get("arguments")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return protocol.error(request_id, protocol.INVALID_PARAMS, f"Unknown tool: {name}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return protocol.error(request_id, protocol.INVALID_PARAMS, "Invalid params: arguments must be an object")
        if protocol.id_key(request_id) in self.waiters:
            return protocol.error(request_id, protocol.INVALID_REQUEST,
                                  "Invalid Request: this id belongs to a call that is not answered yet")
        try:
            return self._run(tool, tool.checked_arguments(arguments), request_id)
        except Refused as refused:
            # A refusal is a tool result, so that the agent reads it; no command has run.
            return protocol.result(request_id, protocol.refusal(f"{tool.name} was not run: {refused}."))

    def _run(self, tool, values: dict, request_id):
        """Do what TOOL does. Returns the response, or None when the call now waits for a command."""
        wait = values.get("wait_seconds", WAIT_SECONDS_DEFAULT)
        if tool.operation is not None:
            command = self._start(tool.operation, tool.output_form, tool.command_line(values))
            return self._wait(request_id, command, wait, started_it=True)
        if tool.action == "server_info":
            command = self._start("contract", "envelope", list(CONTRACT_COMMAND), extra={"cli": self.commands.cli})
            return self._wait(request_id, command, wait, started_it=True)
        if tool.action == "command_list":
            return protocol.result(request_id, protocol.tool_result({"commands": self.commands.listing()},
                                                                    is_error=False))
        command = self.commands.held.get(values["command_id"])
        if command is None:
            raise Refused(f"this server holds no command {values['command_id']}: it never ran here, or its "
                          "result was already returned. pulsar_command_list shows the commands it holds")
        command.handle_returned = True  # The client named it, so its result is kept for the client.
        if tool.action == "command_cancel":
            self.commands.cancel(command)
            wait = None  # Until it has ended, which the grace period bounds.
        return self._wait(request_id, command, wait, started_it=False)

    def _start(self, operation: str, output_form: str, arguments: list[str], extra: dict | None = None):
        running = self.commands.running()
        if len(running) >= running_commands.RUNNING_LIMIT:
            names = ", ".join(f"{command.command_id} ({command.operation})" for command in running)
            raise Refused(f"{len(running)} commands are already running, the most this server runs at once: "
                          f"{names}. Wait for one with pulsar_command_get or cancel one with "
                          "pulsar_command_cancel")
        try:
            return self.commands.start(operation, output_form, arguments, extra)
        except OSError as failure:
            raise Refused(f"its command could not be started: {failure.strerror or failure}") from failure

    def _wait(self, request_id, command, wait, *, started_it: bool):
        if command.ended:  # Its result waited to be returned.
            payload, is_error = self.commands.result(command)
            self.commands.forget(command)
            return protocol.result(request_id, protocol.tool_result(payload, is_error=is_error))
        deadline = None if wait is None else time.monotonic() + wait
        self.waiters[protocol.id_key(request_id)] = Waiter(request_id, command, deadline, started_it)
        return None


def usage_mistake(message: str, *, json_output: bool) -> int:
    """Report a mistake in the command line as every pulsar command does: exit
    status 2, with an error line for people or, under --json, the error envelope."""
    if json_output:
        print(json.dumps({"schema_version": 1, "ok": False,
                          "error": {"code": "usage_error", "message": f"command: {message}",
                                    "details": [{"field": "command", "message": message}]}}, sort_keys=True))
    else:
        from scripts.terminal_format import error_line
        error_line(message, tag="mcp")
    return 2


def main(argv: list[str] | None = None, tools=TOOLS) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(argument in ("--help", "-h") for argument in argv):
        # --help is for people: help and exit status 0, with or without --json.
        from scripts.terminal_format import emit_help
        emit_help(HELP)
        return 0
    json_output = "--json" in argv
    if json_output:
        return usage_mistake("pulsar mcp prints no JSON result: it is a server that an agent harness talks to; "
                             "see ./pulsar mcp --help", json_output=True)
    if argv:
        return usage_mistake(f"pulsar mcp takes no arguments, and got {argv[0]}; see ./pulsar mcp --help",
                             json_output=False)
    if os.isatty(0):
        # Never sit waiting for JSON that a person would have to type.
        return usage_mistake("pulsar mcp is started by an agent harness, which talks to it over standard input "
                             "and output; it is not for a terminal. See ./pulsar mcp --help", json_output=False)
    grace = running_commands.GRACE_SECONDS
    if os.environ.get(GRACE_VARIABLE):
        try:
            grace = float(os.environ[GRACE_VARIABLE])
            if not 0 < grace <= 3600:
                raise ValueError
        except ValueError:
            return usage_mistake(f"{GRACE_VARIABLE} must be a number of seconds, more than 0", json_output=False)
    source = protocol.Input()  # Takes the signals from here on, so the directory below is always removed.
    try:
        directory = running_commands.PrivateDirectory()
    except (running_commands.NoPrivateDirectory, OSError) as failure:
        from scripts.terminal_format import error_line
        error_line(f"pulsar mcp cannot keep command output: {failure}", tag="mcp")
        return 1
    server = Server(tools, running_commands.Commands(cli_path(), directory.path, grace), source, protocol.Output())
    try:
        return server.serve()
    except protocol.ClientGone:
        return 0  # Standard output closed: the session is over, as when standard input closes.
    except protocol.SignalEndedWrite as ended:
        return 128 + ended.signum  # The signal ended a write: the same exit status as when the loop takes it.
    finally:
        try:
            server.end()
        finally:
            directory.remove()


if __name__ == "__main__":
    sys.exit(main())
