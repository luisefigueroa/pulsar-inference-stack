#!/usr/bin/env python3
"""Resolve a shortened catalog spec ID typed by a person.

Human-mode commands accept a unique prefix of at least MIN_PREFIX characters
and always print the complete spec ID they selected. Machine callers keep the
exact-ID contract: --json, --spec-file and destructive model operations
require the complete 64-character spec ID. Resolution reads only catalog
filenames; it never probes nodes or storage.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.terminal_format import TerminalWriter

MIN_PREFIX = 12
SPEC_ID = re.compile(r"^[0-9a-f]{64}$")
HEX = re.compile(r"^[0-9a-f]+$")
FULL_ID_OPERATIONS = {"purge", "remove"}
VALUE_OPTIONS = {
    "--snapshot", "--spec-file", "--manifest", "--model-id", "--model-commit",
    "--revision", "--node", "--manifest-out", "--verification-jobs",
    "--override-file", "--memory-estimate-file", "--memory-estimate-id",
    "--backend", "--transport", "--copy-streams",
}


class SelectionError(ValueError):
    pass


def catalog_ids(repo: Path) -> list[str]:
    releases = Path(repo) / "releases"
    if not releases.is_dir():
        return []
    return sorted(path.stem for path in releases.glob("*.json") if SPEC_ID.match(path.stem))


def matches(repo: Path, prefix: str) -> list[str]:
    return [spec_id for spec_id in catalog_ids(repo) if spec_id.startswith(prefix)]


def resolve(repo: Path, value: str, *, require_full: str | None = None) -> str:
    """Return the complete spec ID for a unique prefix, or raise SelectionError.

    Values that are not lowercase hex shorter than 64 characters are returned
    unchanged so the command reports its own error for them.
    """
    if len(value) >= 64 or not HEX.match(value):
        return value
    found = matches(repo, value)
    if require_full:
        detail = f"; {value} matches {', '.join(found)}" if found else ""
        raise SelectionError(f"{require_full} requires the complete 64-character spec ID{detail}")
    if not found:
        raise SelectionError(f"no catalog spec ID starts with {value}; see ./pulsar models list")
    if len(value) < MIN_PREFIX:
        raise SelectionError(
            f"use at least {MIN_PREFIX} characters of the spec ID; {value} matches {', '.join(found)}")
    if len(found) > 1:
        raise SelectionError(f"{value} matches more than one spec: {', '.join(found)}")
    return found[0]


def _first_positional(args: list[str], start: int = 0) -> int | None:
    index = start
    while index < len(args):
        arg = args[index]
        if arg in VALUE_OPTIONS:
            index += 2
            continue
        if not arg.startswith("-"):
            return index
        index += 1
    return None


def spec_position(command: str, args: list[str]) -> tuple[int | None, str | None]:
    """Locate the spec argument and whether its operation requires the full ID."""
    if command in ("start", "stop", "status"):
        return (0 if args and not args[0].startswith("-") else None), None
    if command == "models":
        return (1 if args[:1] in (["show"], ["check"]) and len(args) > 1 else None), None
    if command == "model" and args:
        operation = args[0]
        start = 2 if operation == "archive" else 1
        required = f"model {operation}" if operation in FULL_ID_OPERATIONS else None
        return _first_positional(args, start), required
    return None, None


def human_mode(args: list[str]) -> bool:
    return not any(arg == "--json" or arg in ("-h", "--help") or arg == "--spec-file"
                   or arg.startswith("--spec-file=") for arg in args)


def main(argv: list[str] | None = None) -> int:
    """Usage: spec_selector.py --repo-root DIR COMMAND [ARG ...]

    Prints "INDEX SPEC_ID" when argument INDEX should be replaced; prints
    nothing when the arguments stay unchanged. Arguments are not parsed as
    options of this helper, so they pass through exactly as typed.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3 or argv[0] != "--repo-root":
        print("usage: spec_selector.py --repo-root DIR COMMAND [ARG ...]", file=sys.stderr)
        return 2
    repo, command, args = Path(argv[1]), argv[2], argv[3:]
    if not human_mode(args):
        return 0
    index, required = spec_position(command, args)
    if index is None:
        return 0
    try:
        resolved = resolve(repo, args[index], require_full=required)
    except SelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if resolved != args[index]:
        TerminalWriter(stream=sys.stderr).emit(f"Using spec {resolved}")
        # The dispatcher replaces only this argument; every other argument is unchanged.
        print(f"{index} {resolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
