#!/usr/bin/env python3
"""Help guide pages: `pulsar help terms` and `pulsar help workflow`.

The menu's Help runs the same commands. The terms page reads the Terms table
in docs/OPERATIONS.md, so the menu and the docs show one list; a table that
cannot be read is an error, never an empty page. The workflow page names each
step with the catalog menu's own labels.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog_menu import LABELS  # noqa: E402
from scripts.terminal_format import TerminalWriter, error_line  # noqa: E402

OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
TERMS_HEADING = "## Terms"
# A cell boundary: a pipe not escaped as \|.
_CELL = re.compile(r"(?<!\\)\|")


class GuideError(Exception):
    """The guide's source document could not be read as a Terms table."""


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [cell.strip().replace("\\|", "|") for cell in _CELL.split(inner)]


def _plain(cell: str) -> str:
    """A Markdown cell as terminal text: links keep their words, and code and
    bold lose their markers."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text.replace("`", "")


def terms(path: Path = OPERATIONS) -> list[tuple[str, str]]:
    """The Terms table's rows as (term, meaning), in the document's order.

    The table follows the `## Terms` heading, its header reads `Term |
    Meaning`, and every row has exactly those two cells.
    """
    shown = _shown(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise GuideError(f"cannot read {shown}: {getattr(error, 'strerror', None) or error}") from error
    if TERMS_HEADING not in lines:
        raise GuideError(f"{shown} has no '{TERMS_HEADING}' section")
    table: list[tuple[int, str]] = []
    for number in range(lines.index(TERMS_HEADING) + 1, len(lines)):
        line = lines[number]
        if line.startswith("#") or (table and not line.startswith("|")):
            break
        if line.startswith("|"):
            table.append((number + 1, line))
    if len(table) < 3:
        raise GuideError(f"{shown}: the Terms section has no table rows")
    (header_number, header), (separator_number, separator), *rows = table
    if _cells(header) != ["Term", "Meaning"]:
        raise GuideError(f"{shown} line {header_number}: the Terms table's header must read 'Term | Meaning'")
    if len(_cells(separator)) != 2 or not all(re.fullmatch(r":?-+:?", cell) for cell in _cells(separator)):
        raise GuideError(f"{shown} line {separator_number}: expected the Terms table's separator row")
    result = []
    for number, line in rows:
        cells = _cells(line)
        if len(cells) != 2:
            raise GuideError(f"{shown} line {number}: a Terms row needs 2 cells, a term and its meaning; "
                             f"found {len(cells)}")
        if not all(cells):
            raise GuideError(f"{shown} line {number}: a Terms row has an empty {'meaning' if cells[0] else 'term'}")
        result.append((_plain(cells[0]), _plain(cells[1])))
    return result


def workflow() -> list[tuple[str, str]]:
    """How serving works, as (step, one sentence), in lifecycle order."""
    return [
        (f"{LABELS['acquire']} or {LABELS['restore']}",
         "Fetch the spec's exact model files to one node, where they become its home; "
         f"{LABELS['restore']} copies them from an archive instead."),
        (LABELS["prepare"],
         "Copy the files from the home to every serving node, reusing matching copies; "
         "prepared files do not start a service."),
        (LABELS["start"],
         "Recheck the model files, serving image, nodes and memory, then launch the service; "
         "it is ready once it answers a test request."),
        ("Connect",
         "Send requests to the API URL with the served model name, both shown on the Connect card "
         f"after {LABELS['start']} and in {LABELS['status']}."),
        (LABELS["stop"], "Remove the service's containers from every node; model files, pins and archives are kept."),
        (LABELS["purge"],
         "Remove the spec's prepared copies to free space; pinned or in-use copies are refused, "
         "and the home and archives are kept."),
    ]


def emit_terms(writer: TerminalWriter, rows: list[tuple[str, str]]) -> None:
    writer.emit("What the words mean")
    writer.emit("Menus and commands use these words. The same list is under Terms in docs/OPERATIONS.md.")
    writer.blank()
    for term, meaning in rows:
        writer.emit(term, initial_indent="  ")
        writer.emit(meaning, initial_indent="    ")


def emit_workflow(writer: TerminalWriter) -> None:
    writer.emit("How serving works")
    writer.emit("Set up cluster membership and SSH trust first. Then choose a spec in Catalog and "
                "storage; the Lifecycle line in its frame marks the step it has reached.")
    writer.blank()
    for number, (step, sentence) in enumerate(workflow(), 1):
        writer.emit(f"{number}. {step}", initial_indent="  ")
        writer.emit(sentence, initial_indent="     ")
    writer.blank()
    writer.emit("The same steps as commands: pulsar model acquire or model restore, model prepare, "
                "start, status, stop and model purge. Most commands accept --help.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulsar help", description="Print a Help guide page.")
    parser.add_argument("topic", choices=("terms", "workflow"))
    # Tests read a fixture document instead of this checkout's guide.
    parser.add_argument("--file", type=Path, default=OPERATIONS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    writer = TerminalWriter()
    if args.topic == "workflow":
        emit_workflow(writer)
        return 0
    try:
        rows = terms(args.file)
    except GuideError as error:
        error_line(error, tag="help-guide")
        return 1
    emit_terms(writer, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
