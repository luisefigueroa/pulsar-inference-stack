"""Public document commands; never loads topology, credentials or Docker."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_spec import serving
from release_spec.immutable_io import ImmutableDescriptorDirectoryError
from release_spec.schema import ReleaseSpecError
from scripts.terminal_format import TerminalWriter

# The public error envelope's codes. Exit statuses are integers, except the
# signal-derived "128+signal" for interrupted actions. docs/CONTRACT.md and
# `pulsar contract --json` publish this table; tests keep them in step.
ERROR_CODES = {
    "usage_error": (2, "The command line is invalid: an unknown command or missing or bad arguments."),
    "file_error": (2, "A file or directory named by the caller could not be read or written."),
    "invalid_spec": (2, "Spec or document content failed validation."),
    "unsupported_spec_version": (2, "The document's schema version is not supported."),
    "invalid_stack_output": (2, "A Stack script produced output that is not JSON. This is a Stack defect."),
    "prerequisite_failed": (3, "A Stack action exited unsuccessfully; message and details hold its diagnostics."),
    "cancelled": ("128+signal", "Interrupted; cleanup of the command and its node workers was confirmed."),
    "cleanup_incomplete": ("128+signal", "Interrupted; worker exit could not be confirmed."),
}
EXIT_STATUSES = {
    "0": "Success.",
    "2": "The request was rejected before any action: usage, file, spec or Stack output error.",
    "3": "A prerequisite or Stack action failed.",
    "128+signal": "Interrupted by the signal; see cancelled and cleanup_incomplete.",
}


class UsageError(serving.SpecValidationError):
    """The command line is invalid; no input was read."""


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError("arguments", message)


def error_code(exc: BaseException) -> str:
    """Envelope code for an input, usage or validation failure."""
    if isinstance(exc, UsageError):
        return "usage_error"
    if isinstance(exc, (OSError, ImmutableDescriptorDirectoryError, serving.InputFileError)):
        return "file_error"
    if getattr(exc, "field", None) == "schema_version":
        return "unsupported_spec_version"
    return "invalid_spec"


def emit(result, *, json_output: bool):
    if json_output:
        print(json.dumps({"schema_version": 1, "ok": True, "result": result}, sort_keys=True))
    else:
        # Full draft/spec JSON stays copyable. Comparison is meant for people.
        if "changes" in result:
            out = TerminalWriter()
            out.emit("Recipe changed" if result["recipe_changed"] else "Recipe unchanged")
            for change in result["changes"]:
                out.emit(change["field"])
                out.emit(f"{change['before']!r} -> {change['after']!r}", initial_indent="  ")
        else:
            print(json.dumps(result, indent=2, sort_keys=True))


def failure(exc, *, json_output: bool, code=None, exit_code=None):
    code = code or error_code(exc)
    if exit_code is None:
        exit_code = ERROR_CODES[code][0]
    details = getattr(exc, "envelope_details", None) or [
        {"field": getattr(exc, "field", "$"), "message": getattr(exc, "reason", str(exc))}]
    if json_output:
        print(json.dumps({"schema_version": 1, "ok": False,
                          "error": {"code": code, "message": str(exc), "details": details}}, sort_keys=True))
    else:
        print(f"error: {exc}", file=sys.stderr)
    return exit_code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    # Permit the output mode anywhere without teaching each subparser a copy.
    argv = [arg for arg in argv if arg != "--json"]
    parser = CommandParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True, parser_class=CommandParser)
    sample = subs.add_parser("example", help="JSON draft with explicit execution defaults")
    sample.add_argument("--nodes", type=int, default=1)
    sample.add_argument("--schema-version", type=int, choices=(1, 2), default=1)
    freeze = subs.add_parser("freeze", help="freeze a JSON draft and verified source manifest")
    freeze.add_argument("--draft", required=True)
    freeze.add_argument("--manifest", required=True, action="append", help="FILE for draft 1; repeated NAME=FILE for draft 2")
    for name in ("verify", "show"):
        show = subs.add_parser(name)
        show.add_argument("--file", required=True)
        if name == "show":
            show.add_argument("--historical", action="store_true")
    compare = subs.add_parser("compare", help="show every changed recipe field")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "example":
            result = serving.example(args.nodes, args.schema_version)
        elif args.command == "freeze":
            draft = serving.load_json(args.draft)
            if draft.get("schema_version") == 1:
                if len(args.manifest) != 1:
                    serving.invalid("manifests", "draft 1 requires one manifest file")
                manifests = serving.load_json(args.manifest[0])
            else:
                manifests = {}
                for item in args.manifest:
                    name, separator, path = item.partition("=")
                    if not separator or not name or not path or name in manifests:
                        serving.invalid("manifests", "use unique NAME=FILE inputs")
                    manifests[name] = serving.load_json(path)
            result = serving.freeze(draft, manifests)
        elif args.command == "compare":
            result = serving.compare(serving.load_spec(args.before), serving.load_spec(args.after))
        else:
            result = serving.load_spec(args.file, historical=getattr(args, "historical", False))
        emit(result, json_output=json_output)
        return 0
    except (ReleaseSpecError, OSError, ValueError) as exc:
        return failure(exc, json_output=json_output)


if __name__ == "__main__":
    raise SystemExit(main())
