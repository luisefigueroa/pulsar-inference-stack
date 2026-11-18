"""The Model Context Protocol as this server speaks it: messages and the handshake.

One JSON-RPC 2.0 message per line of UTF-8 on standard input and output. The
server speaks the handshake form only: `initialize`, then
`notifications/initialized`, `ping`, `tools/list`, `tools/call` and
`notifications/cancelled`. Every other method, `server/discover` among them,
is answered with "Method not found", which is what makes a client that also
speaks the later form without a handshake fall back to `initialize`.

Standard library only.
"""
from __future__ import annotations

import json
import math
import os
import select
import signal

# The standard JSON-RPC error codes. The server uses no other code.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

# Protocol revisions this server agrees to, newest first. A client that asks
# for another one is answered with the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")

SERVER_INFO = {"name": "pulsar", "version": "1"}

INSTRUCTIONS = (
    "Every tool of this server is read-only: it inspects, checks or previews, and changes no model files, "
    "service, membership or configuration. Make changes with the pulsar command line. "
    "Read a result by its output_form. For envelope, read output.ok and output.error.code with exit_status: "
    "1 means the answer is no, and 3 means it could not be established. For document, read the document's "
    "own state, such as status, result or ok: those commands do not follow these exit statuses "
    "(docs/CONTRACT.md, Output forms). A result with state running carries a command_id: pass it to "
    "pulsar_command_get to wait for the result, or to pulsar_command_cancel to cancel the command."
)

# The signals that end the session, as a closed standard input does.
END_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def result(request_id, body: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": body}


def error(request_id, code: int, message: str) -> dict:
    """An error response. REQUEST_ID is None when the request's id could not be
    read; the response then carries no id, as the protocol's schema has it."""
    body = {"code": code, "message": message}
    if request_id is None:
        return {"jsonrpc": "2.0", "error": body}
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def valid_id(value) -> bool:
    """A request id is a string or a number, never null and never a boolean.
    A number too large to keep is refused: it could not be written back as JSON."""
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def id_key(request_id) -> str:
    """One key per request id: the string "1" and the number 1 are different ids."""
    return json.dumps(request_id)


def _not_json(name):
    """NaN and Infinity are not JSON, though Python's reader would take them."""
    raise ValueError(name)


def parse(text: str):
    """Read one received line.

    Returns (message, None) for a request or a notification, with `params`
    always an object, or (None, response) for anything else; the response is
    None when nothing is to be sent back.
    """
    try:
        message = json.loads(text, parse_constant=_not_json)
    except (ValueError, RecursionError):  # Not JSON, or nested deeper than this program can read.
        return None, error(None, PARSE_ERROR, "Parse error: the line is not JSON")
    if not isinstance(message, dict):
        return None, error(None, INVALID_REQUEST, "Invalid Request: a message is one JSON object")
    is_request = "id" in message
    if is_request and not valid_id(message["id"]):
        return None, error(None, INVALID_REQUEST, "Invalid Request: id must be a string or a number")
    request_id = message.get("id")
    if not isinstance(message.get("method"), str):
        if "result" in message or "error" in message:
            return None, None  # A response. This server sends no requests, so it expects none.
        return None, error(request_id, INVALID_REQUEST, "Invalid Request: method must be a string")
    params = message.get("params", {})
    if not isinstance(params, dict):
        if is_request:
            return None, error(request_id, INVALID_PARAMS, "Invalid params: params must be an object")
        return None, None  # A notification never gets a response.
    return {**message, "params": params}, None


def initialize_result(params: dict) -> dict:
    """The answer to `initialize`: the client's revision when this server knows it, else the newest."""
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
        "capabilities": {"tools": {}},
        "serverInfo": dict(SERVER_INFO),
        "instructions": INSTRUCTIONS,
    }


def tool_result(payload: dict, *, is_error: bool) -> dict:
    """A tool's one object, as structured content and as the same JSON in a text block."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), allow_nan=False)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def refusal(message: str) -> dict:
    """A call the server refused before any command ran: a tool result, not a JSON-RPC error."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


class ClientGone(Exception):
    """Standard output is closed: nobody reads the answers any more."""


class SignalEndedWrite(Exception):
    """SIGINT, SIGTERM or SIGHUP arrived during a write of Output.send: the
    write was given up, and the session is over."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# Whether a write of Output.send is in progress: Output sets the mark, and the
# handler that Input installs reads it. The mark is the module's and not an
# object's because Input is made before there is an Output, and a process has
# one protocol stream and one handler for each signal. So neither class has to
# know the other.
_write_in_progress = False


def _end_write(signum, _frame) -> None:
    """The handler of the signals that end the session.

    It does nothing, except during a write of Output.send: the loop cannot
    take a signal while that write waits for the client. Then it ends the
    write. It clears the mark first, so it raises at most once, and never in
    the cleanup that follows.
    """
    global _write_in_progress
    if _write_in_progress:
        _write_in_progress = False
        raise SignalEndedWrite(signum)


class Output:
    """The protocol stream: one message per line, and nothing else, ever.

    The stream is moved to a descriptor of its own and descriptor 1 is pointed
    at standard error, so a stray print in this process cannot reach the client.
    """

    def __init__(self):
        self._fd = os.dup(1)
        os.dup2(2, 1)

    def send(self, message: dict) -> None:
        global _write_in_progress
        # ensure_ascii keeps the message one line of ASCII: no newline can be inside it.
        # allow_nan=False: nothing that is not JSON is ever written.
        data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
        try:
            # The write waits for as long as the client keeps the stream open and
            # does not read it. It goes on until the client reads, closes its end
            # (then ClientGone) or sends SIGINT, SIGTERM or SIGHUP during the
            # write: the handler then raises SignalEndedWrite. A closed standard
            # input does not end the write, and neither does a signal that came
            # before the write began: both wait for the loop.
            _write_in_progress = True
            while data:  # A signal can cut a long write short.
                data = data[os.write(self._fd, data):]
        except BrokenPipeError as gone:
            raise ClientGone() from gone
        finally:
            _write_in_progress = False


class Input:
    """Standard input as whole lines, with signals as one more thing to wait for.

    Python only notes a signal when it arrives, by writing its number to a
    pipe. wait() reads that pipe, so a signal is taken at one point of the
    loop, between two lines. One case is taken sooner: SIGINT, SIGTERM or
    SIGHUP during a write of Output.send ends that write, and the session with
    it, because the write may wait for a client that reads nothing. So an
    answer is cut only when the client was not reading it and a signal ended
    the session; what was read and not answered by then gets no answer.
    """

    def __init__(self):
        self._pending = b""
        self._ended = False
        self._wake, noted = os.pipe()
        os.set_blocking(self._wake, False)
        os.set_blocking(noted, False)
        signal.set_wakeup_fd(noted, warn_on_full_buffer=False)
        # SIGCHLD only wakes the loop, so that a command that ended is seen at once.
        signal.signal(signal.SIGCHLD, lambda _signum, _frame: None)
        for signum in END_SIGNALS:
            signal.signal(signum, _end_write)

    def wait(self, timeout: float | None):
        """Wait up to TIMEOUT seconds (None: as long as it takes).

        Returns (lines, end): the lines that arrived, as text, and why the
        session ends: None while it goes on, "input" when standard input
        closed, or the number of the signal that ended it.
        """
        sources = [self._wake] if self._ended else [0, self._wake]
        readable, _, _ = select.select(sources, [], [], timeout)
        if self._wake in readable:
            for signum in os.read(self._wake, 4096):
                if signum in END_SIGNALS:
                    return [], signum
        if 0 not in readable:
            return [], None
        chunk = os.read(0, 65536)
        if chunk:
            *lines, self._pending = (self._pending + chunk).split(b"\n")
            return [decode(line) for line in lines], None
        self._ended = True
        last, self._pending = self._pending, b""  # A last line may come without its newline.
        return ([decode(last)] if last.strip() else []), "input"

    def drain_signals(self, timeout: float) -> None:
        """Sleep up to TIMEOUT seconds, or until a signal arrives; which one is not looked at."""
        readable, _, _ = select.select([self._wake], [], [], timeout)
        if readable:
            os.read(self._wake, 4096)


def decode(line: bytes) -> str:
    """The text of one received line. Bytes that are not UTF-8 make the line fail to parse as JSON."""
    return line.decode("utf-8", errors="backslashreplace").rstrip("\r")
