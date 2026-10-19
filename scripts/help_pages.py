#!/usr/bin/env python3
"""Help pages: `pulsar help` and its topics, `terms` and `workflow`.

The menu's Help runs the same commands. The terms page reads the Terms table
in docs/OPERATIONS.md, so the menu and the docs show one list; a table that
cannot be read is an error, never an empty page. The workflow page names each
step with the catalog menu's own labels.

Pages mark their text for the shared terminal styles: `literal` for
commands, options and paths, **choice** for menu choices and other on-screen
names, and *term* for words the terms page explains. Off a terminal they
print plainly.
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

# The command reference: each group's commands with one line on what they do.
REFERENCE = [
    ("Guides", [
        ("pulsar help terms", "What the words mean: spec, recipe, home, prepared copies and more."),
        ("pulsar help workflow", "How serving works, from download to purge, one step at a time.")]),
    ("Set up this checkout", [
        ("pulsar", "Open the menu on a terminal with Gum: cluster setup, read-only catalog details, "
                   "diagnostics and **Help**. Full operations follow membership and SSH setup; archives are optional."),
        ("pulsar topology setup", "First-use membership and SSH enrollment with separate confirmations."),
        ("pulsar topology show|check|detect|configure",
         "Inspect saved membership, check nodes, discover or explicitly configure."),
        ("pulsar ssh-trust --help", "Inspect or explicitly enroll SSH trust."),
        ("pulsar configure archive-root", "Select an existing archive location when archives are needed."),
        ("pulsar doctor", "Run explicit host and topology diagnostics.")]),
    ("Catalog and model files", [
        ("pulsar models", "Browse catalog and saved storage observations."),
        ("pulsar models menu --read-only",
         "Select published specs and inspect saved details without offering operations."),
        ("pulsar models show SPEC", "Show recipe, known copies, pins and blockers."),
        ("pulsar models results SPEC", "Inspect published run dates, workloads and policy outcomes for this spec."),
        ("pulsar models check SPEC", "Explicitly check the selected managed files and archive."),
        ("pulsar release show|verify SPEC", "Print a catalog spec as JSON, or its state and review."),
        ("pulsar model --help", "Acquire, restore, move, prepare, pin, purge and archive.")]),
    ("Serve", [
        ("pulsar image check|stage SPEC", "Inspect or explicitly stage the exact image on selected serving ranks."),
        ("pulsar start SPEC", "Recheck prerequisites and start the exact selected spec."),
        ("pulsar guarded template|validate|run|stop|reconcile",
         "Own a bounded trial and reconcile its completed locator."),
        ("pulsar status SPEC", "Observe the live service on all participating nodes."),
        ("pulsar stop SPEC", "Stop the owned service; retain model files and pins."),
        ("pulsar stop --all --yes",
         "Stop every Stack-managed service on all confirmed nodes; retain model files and pins."),
        ("pulsar inventory", "Inspect service ownership and current resource use; read-only."),
        ("pulsar inventory menu", "Select a running catalog service for live status or a confirmed stop.")]),
    ("Contributors and integrations", [
        ("pulsar contract", "Show the versioned stack/workbench integration contract."),
        ("pulsar spec example|freeze|verify|compare", "Author and inspect a complete JSON serving spec."),
        ("pulsar memory verify --file FILE --spec-file FILE",
         "Validate an explicit estimate of resident weights for the exact spec."),
        ("pulsar observe --service-id ID [--full] [--verification-jobs N] --json",
         "Verify every rank; reuse file verification unless `--full` requests hashing."),
        ("pulsar resources --service-id ID|--spec-file FILE --jsonl",
         "Sample confirmed nodes before launch; wait for the matching owned container."),
        ("pulsar policy show baseline-v1|baseline-v2", "Print a supported baseline policy and its digest."),
        ("pulsar evidence measurement|evaluate|verify|summary",
         "Build and assess compact evidence for a spec; never a catalog gate."),
        ("pulsar contribution verify --package DIR",
         "Validate a contribution package, including publication privacy."),
        ("pulsar privacy check|commits", "Check files or commit metadata for private information before publication."),
        ("pulsar selftest [--checks-only]", "Run this checkout's deterministic tests and checks; nothing reaches a node."),
        ("pulsar release list", "Deprecated; use `pulsar models list`. Removed in CLI contract 2.")]),
]
REFERENCE_NOTE = (
    "`SPEC` is a catalog spec ID. People may type a unique prefix of at least 12 characters; the command "
    "prints the complete ID it selected. `--json`, `--spec-file`, `image` commands, `release show|verify`, "
    "`model purge` and `model remove` require the complete 64-character ID. Candidate files are accepted "
    "only through explicit `--spec-file`. Review status, prepared files and a running service are separate. "
    "Most commands accept `--help`. See `docs/OPERATIONS.md`.")


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


def _marked(cell: str) -> str:
    """A Markdown cell as page text: links keep their words, and code and
    bold stay as the page's literal and choice marks."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)


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
        result.append((_marked(cells[0]).replace("`", "").replace("**", ""), _marked(cells[1])))
    return result


def workflow() -> list[tuple[str, str]]:
    """How serving works, as (step, one marked sentence), in lifecycle order."""
    return [
        (f"{LABELS['acquire']} or {LABELS['restore']}",
         "Fetch the spec's exact model files to one node, where they become its *home*; "
         f"**{LABELS['restore']}** copies them from an *archive* instead."),
        (LABELS["prepare"],
         "Copy the files from the *home* to every serving node as *prepared copies*, reusing matching "
         "ones; preparing does not start a *service*."),
        (LABELS["start"],
         "Recheck the model files, *serving image*, nodes and memory, then launch the *service*; "
         "it is ready once it answers a test request."),
        ("Connect",
         "Send requests to the API URL with the served model name, both shown on the **Connect** card "
         f"after **{LABELS['start']}** and in **{LABELS['status']}**."),
        (LABELS["stop"], "Remove the *service*'s containers from every node; model files, *pins* and "
                         "*archives* are kept."),
        (LABELS["purge"],
         "Remove the spec's *prepared copies* to free space; pinned or in-use copies are refused, "
         "and the *home* and *archives* are kept."),
    ]


def emit_reference(writer: TerminalWriter) -> None:
    writer.emit("Pulsar Inference Stack", role="heading")
    writer.emit("Catalog, model storage, recovery and exact-spec serving.")
    for heading, rows in REFERENCE:
        writer.blank()
        writer.emit(heading, role="heading")
        for command, description in rows:
            writer.emit(f"`{command}`", initial_indent="  ", markup=True)
            writer.emit(description, initial_indent="    ", markup=True)
    writer.blank()
    writer.emit(REFERENCE_NOTE, markup=True)


def emit_terms(writer: TerminalWriter, rows: list[tuple[str, str]]) -> None:
    writer.emit("What the words mean", role="heading")
    writer.emit("Menus and commands use these words. The same list is under Terms in `docs/OPERATIONS.md`.",
                markup=True)
    writer.blank()
    for term, meaning in rows:
        writer.emit(term, initial_indent="  ", role="heading")
        writer.emit(meaning, initial_indent="    ", markup=True)


def emit_workflow(writer: TerminalWriter) -> None:
    writer.emit("How serving works", role="heading")
    writer.emit("**Set up cluster membership and SSH trust** first. Then choose a spec in **Catalog and "
                "storage**; the **Lifecycle** line in its frame marks the step it has reached.", markup=True)
    writer.blank()
    for number, (step, sentence) in enumerate(workflow(), 1):
        writer.emit(f"{number}. {step}", initial_indent="  ", role="heading")
        writer.emit(sentence, initial_indent="     ", markup=True)
    writer.blank()
    writer.emit("The same steps as commands: `pulsar model acquire` or `model restore`, `model prepare`, "
                "`start`, `status`, `stop` and `model purge`. Most commands accept `--help`.", markup=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulsar help", description="Print a Help page.")
    parser.add_argument("topic", choices=("reference", "terms", "workflow"))
    # Tests read a fixture document instead of this checkout's guide.
    parser.add_argument("--file", type=Path, default=OPERATIONS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    writer = TerminalWriter()
    if args.topic == "reference":
        emit_reference(writer)
        return 0
    if args.topic == "workflow":
        emit_workflow(writer)
        return 0
    try:
        rows = terms(args.file)
    except GuideError as error:
        error_line(error, tag="help-pages")
        return 1
    emit_terms(writer, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
