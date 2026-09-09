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
from release_spec.schema import ReleaseSpecError
from scripts.terminal_format import TerminalWriter


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise serving.SpecValidationError("arguments", message)


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


def failure(exc, *, json_output: bool, code="invalid_spec", exit_code=2):
    details = [{"field": getattr(exc, "field", "$"),
                "message": getattr(exc, "reason", str(exc))}]
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
    freeze = subs.add_parser("freeze", help="freeze a JSON draft and verified source manifest")
    freeze.add_argument("--draft", required=True)
    freeze.add_argument("--manifest", required=True)
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
            result = serving.example(args.nodes)
        elif args.command == "freeze":
            result = serving.freeze(serving.load_json(args.draft), serving.load_json(args.manifest))
        elif args.command == "compare":
            result = serving.compare(serving.load_spec(args.before), serving.load_spec(args.after))
        else:
            result = serving.load_spec(args.file, historical=getattr(args, "historical", False))
        emit(result, json_output=json_output)
        return 0
    except (ReleaseSpecError, OSError, ValueError) as exc:
        code = "unsupported_spec_version" if getattr(exc, "field", None) == "schema_version" else "invalid_spec"
        return failure(exc, json_output=json_output, code=code)


if __name__ == "__main__":
    raise SystemExit(main())
