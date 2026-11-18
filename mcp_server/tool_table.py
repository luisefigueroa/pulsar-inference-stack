"""The tool table of the MCP server, and the rules for a tool's arguments.

A tool runs one operation of the integration contract in one fixed way. The
table is the whole of what an agent can make this server run: the start of
each command line and its fixed flags are literals here, `--json` is added
here and never by the caller, and an argument reaches a command line only
through the rules below. The output forms are literals too, because this
package imports nothing that knows the contract; a test compares them with
`pulsar contract --json`.

Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re

# How long a call waits for its command before it answers `running`, in seconds.
WAIT_SECONDS_DEFAULT = 60
WAIT_SECONDS_MINIMUM = 0
WAIT_SECONDS_MAXIMUM = 240

OUTPUT_FORMS = ("envelope", "document", "text")
ARGUMENT_KINDS = ("spec_id", "path", "integer", "number", "choice", "text", "flag", "command_id")
SPEC_ID = re.compile(r"[0-9a-f]{64}")
COMMAND_ID = re.compile(r"command-[1-9][0-9]*")


class Refused(ValueError):
    """An argument the server refuses. No command has run."""


@dataclass(frozen=True)
class Argument:
    """One argument of a tool: what an agent may pass, and the option it becomes."""
    name: str
    kind: str
    description: str
    required: bool = False
    flag: str | None = None  # The option it becomes on the command line; None for a positional argument.
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in ARGUMENT_KINDS:
            raise ValueError(f"argument {self.name}: unknown kind {self.kind}")
        if self.kind in ("integer", "number") and (self.minimum is None or self.maximum is None
                                                   or not 0 <= self.minimum <= self.maximum):
            # Not below zero: a negative number would start with "-" on a command line.
            raise ValueError(f"argument {self.name}: a number has a range, from zero or more")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"argument {self.name}: a choice lists its values")
        if self.kind == "flag" and self.flag is None:
            raise ValueError(f"argument {self.name}: a flag names its option")

    def schema(self) -> dict:
        """The argument as a JSON Schema property."""
        if self.kind == "spec_id":
            shape = {"type": "string", "pattern": f"^{SPEC_ID.pattern}$"}
        elif self.kind == "command_id":
            shape = {"type": "string", "pattern": f"^{COMMAND_ID.pattern}$"}
        elif self.kind in ("integer", "number"):
            shape = {"type": self.kind, "minimum": self.minimum, "maximum": self.maximum}
        elif self.kind == "choice":
            shape = {"type": "string", "enum": list(self.choices)}
        elif self.kind == "flag":
            shape = {"type": "boolean"}
        else:
            shape = {"type": "string"}
        return {**shape, "description": self.description}

    def checked(self, value):
        """VALUE as this argument accepts it, or Refused."""
        name = self.name
        if self.kind == "flag":
            if not isinstance(value, bool):
                raise Refused(f"argument {name} must be true or false")
            return value
        if self.kind in ("integer", "number"):
            allowed = int if self.kind == "integer" else (int, float)
            if isinstance(value, bool) or not isinstance(value, allowed) \
                    or (isinstance(value, float) and not math.isfinite(value)):
                raise Refused(f"argument {name} must be {'an integer' if self.kind == 'integer' else 'a number'}")
            if not self.minimum <= value <= self.maximum:
                raise Refused(f"argument {name} must be between {self.minimum:g} and {self.maximum:g}")
            return value
        if not isinstance(value, str):
            raise Refused(f"argument {name} must be a string")
        if self.kind == "spec_id":
            if not SPEC_ID.fullmatch(value):
                raise Refused(f"argument {name} must be a complete spec ID: 64 lower-case hexadecimal characters")
        elif self.kind == "command_id":
            if not COMMAND_ID.fullmatch(value):
                raise Refused(f"argument {name} must be a command_id this server returned, such as command-1")
        elif self.kind == "choice":
            if value not in self.choices:
                raise Refused(f"argument {name} must be one of: {', '.join(self.choices)}")
        elif not value or "\0" in value:
            raise Refused(f"argument {name} must be a non-empty string without a null character")
        elif value.startswith("-"):
            raise Refused(f"argument {name} must not start with -")
        elif self.kind == "path" and not os.path.isabs(value):
            raise Refused(f"argument {name} must be an absolute path")
        return value


WAIT_SECONDS = Argument(
    "wait_seconds", "number",
    "How many seconds this call waits for the command. If the command has not ended by then, the answer has "
    f"state running and a command_id for pulsar_command_get. Default {WAIT_SECONDS_DEFAULT}.",
    minimum=WAIT_SECONDS_MINIMUM, maximum=WAIT_SECONDS_MAXIMUM)
COMMAND_ID_ARGUMENT = Argument(
    "command_id", "command_id", "The command_id of a result whose state was running.", required=True)


@dataclass(frozen=True)
class Tool:
    """One entry of the table.

    A tool with an operation runs `pulsar COMMAND ARGUMENTS FIXED_FLAGS`,
    with `--json` at the end unless its output form is text. A tool with an
    action is one of the server's own.
    """
    name: str
    description: str
    operation: str | None = None  # The contract's name for what runs, such as models.show.
    command: tuple[str, ...] = ()  # The fixed start of the command line.
    fixed_flags: tuple[str, ...] = ()
    arguments: tuple[Argument, ...] = ()  # The command's own options that an agent may set.
    output_form: str | None = None  # As the contract's operation_outputs names it.
    saves: str | None = None  # What the command may save; None when it saves nothing.
    action: str | None = None  # For the server's own tools: what the server does.
    own_arguments: tuple[Argument, ...] = ()  # Read by the server; never part of a command line.
    grant: str = "read"  # This delivery has read tools only.

    def __post_init__(self):
        if (self.operation is None) == (self.action is None):
            raise ValueError(f"tool {self.name}: either an operation or an action")
        if self.operation is not None and (not self.command or self.output_form not in OUTPUT_FORMS):
            raise ValueError(f"tool {self.name}: an operation tool has a command and an output form")
        names = [argument.name for argument in self.all_arguments]
        if len(names) != len(set(names)):
            raise ValueError(f"tool {self.name}: two arguments share a name")

    @property
    def all_arguments(self) -> tuple[Argument, ...]:
        return (*self.arguments, *self.own_arguments)

    @property
    def read_only_hint(self) -> bool:
        """True only for a tool that saves nothing and ends nothing. A hint; no rule relies on it."""
        return self.saves is None and self.action != "command_cancel"

    def flags(self) -> set[str]:
        """Every option this tool can put on a command line."""
        produced = {*self.fixed_flags, *(argument.flag for argument in self.arguments if argument.flag)}
        if self.operation is not None and self.output_form != "text":
            produced.add("--json")
        return produced

    def listed(self) -> dict:
        """The tool as `tools/list` shows it."""
        schema = {"type": "object",
                  "properties": {argument.name: argument.schema() for argument in self.all_arguments},
                  "additionalProperties": False}
        required = [argument.name for argument in self.all_arguments if argument.required]
        if required:
            schema["required"] = required
        annotations = {"readOnlyHint": self.read_only_hint}
        if not self.read_only_hint:
            annotations["destructiveHint"] = False  # It ends a read command; it deletes and overwrites nothing.
        return {"name": self.name, "description": self.description, "inputSchema": schema,
                "annotations": annotations}

    def checked_arguments(self, arguments: dict) -> dict:
        """ARGUMENTS as the tool accepts them, by name, or Refused."""
        known = {argument.name: argument for argument in self.all_arguments}
        unexpected = sorted(set(arguments) - set(known))
        if unexpected:
            raise Refused(f"unexpected argument {unexpected[0]}")
        missing = [name for name, argument in known.items() if argument.required and name not in arguments]
        if missing:
            raise Refused(f"argument {missing[0]} is required")
        return {name: known[name].checked(value) for name, value in arguments.items()}

    def command_line(self, values: dict) -> list[str]:
        """The arguments of `pulsar` for VALUES, which checked_arguments returned.

        A list, never a shell string. No value starts with `-`, so none can be
        read as an option.
        """
        positional, options = [], []
        for argument in self.arguments:
            if argument.name not in values:
                continue
            value = values[argument.name]
            if argument.kind == "flag":
                if value:
                    options.append(argument.flag)
                continue
            word = str(value)
            if word.startswith("-"):
                raise Refused(f"argument {argument.name} must not start with -")
            if argument.flag is None:
                positional.append(word)
            else:
                options.extend([argument.flag, word])
        json_flag = [] if self.output_form == "text" else ["--json"]
        return [*self.command, *positional, *options, *self.fixed_flags, *json_flag]


def operation_tool(name, operation, command, output_form, description, *, arguments=(), fixed_flags=(), saves=None):
    """A tool that runs an operation; every such tool takes wait_seconds."""
    return Tool(name=name, description=description, operation=operation, command=tuple(command.split()),
                fixed_flags=tuple(fixed_flags), arguments=tuple(arguments), output_form=output_form, saves=saves,
                own_arguments=(WAIT_SECONDS,))


def tool_name(operation: str, fixed_flags=()) -> str:
    """The name of the tool that runs OPERATION: dots and hyphens become
    underscores, and a preview form is named after its fixed flag."""
    suffix = "_plan" if "--plan" in fixed_flags else "_dry_run" if "--dry-run" in fixed_flags else ""
    return "pulsar_" + re.sub(r"[.-]", "_", operation) + suffix


# The table, in the order `tools/list` shows it: the operation tools, then the
# server's own. Each description says what the tool does, then what it reaches
# and what it may save.
TOOLS = (
    operation_tool(
        "pulsar_contract", "contract", "contract", "envelope",
        "Returns this Stack's integration contract: the operations it supports, each one's output form, the "
        "supported schema versions, the error codes and what each exit status means. "
        "Reaches nothing and saves nothing."),
    operation_tool(
        "pulsar_topology_show", "topology.show", "topology show", "document",
        "Returns the saved cluster membership: the confirmed nodes and how they are connected. Exit status 1 "
        "with a document means its status is missing or invalid. "
        "Reads saved records only: it contacts no node and saves nothing."),
    operation_tool(
        "pulsar_models_show", "models.show", "models show", "document",
        "Returns one catalog spec in detail: identity, image, engine arguments, home, prepared copies and "
        "blockers, with the saved state of its files and archive and the age of each saved check. "
        "Reads saved records only: it contacts no node and saves nothing.",
        arguments=(Argument("spec_id", "spec_id", "The complete spec ID of a catalog spec: 64 lower-case "
                            "hexadecimal characters.", required=True),)),
    Tool(name="pulsar_mcp_server_info", action="server_info",
         description="Names the pulsar file this server runs and returns that Stack's integration contract, so "
                     "that you can confirm it is the Stack you mean to use. Reaches nothing and saves nothing."),
    Tool(name="pulsar_command_get", action="command_get", own_arguments=(COMMAND_ID_ARGUMENT, WAIT_SECONDS),
         description="Waits up to wait_seconds for a running command, then returns its result, or state running "
                     "if it has not ended. A result is returned once; after that its command_id is unknown. "
                     "Starts nothing."),
    Tool(name="pulsar_command_cancel", action="command_cancel", own_arguments=(COMMAND_ID_ARGUMENT,),
         description="Cancels a running command: it gets SIGTERM, and the answer comes when it has ended, with "
                     "state cancelled and what the command printed. A command that does not end within the "
                     "grace period is killed, and diagnostics says so. Every tool of this server only reads, "
                     "so a cancel leaves nothing half changed."),
    Tool(name="pulsar_command_list", action="command_list",
         description="Lists the commands this server holds: those still running, and those that ended and whose "
                     "result pulsar_command_get has not returned yet. Starts nothing."),
)
