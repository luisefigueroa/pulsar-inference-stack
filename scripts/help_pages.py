#!/usr/bin/env python3
"""Help pages: `pulsar help` and its topics, `terms`, `labels` and `workflow`.

The menu's Help runs the same commands. The terms and labels pages read the
Terms and Catalog labels tables in docs/OPERATIONS.md, so the menu and the
docs show one list; a table that
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
LABELS_HEADING = "## Catalog labels"
# A cell boundary: a pipe not escaped as \|.
_CELL = re.compile(r"(?<!\\)\|")

# The command reference: each group's commands with one line on what they do.
REFERENCE = [
    ("Guides", [
        ("pulsar help terms", "What the words mean: spec, recipe, home, prepared copies and more."),
        ("pulsar help labels", "What the catalog's status words mean, such as not checked yet, and what to do."),
        ("pulsar help workflow", "How serving works, from download to purge, one step at a time.")]),
    ("Set up this checkout", [
        ("pulsar", "Open the menu on a terminal with Gum. Before setup: **Set up this cluster…**, read-only "
                   "catalog details and **Help and troubleshooting…**. After it: **Serve a model…**, **Running "
                   "services…**, **Models and disk space…**, **Cluster…** and **Help and troubleshooting…**, "
                   "whose **Advanced: single operations…** keeps each single operation. Archives are optional."),
        ("pulsar topology setup", "First-use setup in five steps: this machine, finding the machines, each "
                                  "machine's prerequisites, then membership and SSH identities, each with its "
                                  "own confirmation. It installs nothing."),
        ("pulsar topology show|check|detect|configure",
         "Inspect saved membership, check nodes, discover or explicitly configure; "
         "`configure --remove-missing` saves without a node that did not answer."),
        ("pulsar topology forget [--plan]",
         "Start over: remove saved membership and cluster observations after a private backup copy."),
        ("pulsar ssh-trust --help", "Inspect or explicitly enroll SSH trust."),
        ("pulsar configure archive-root", "Select an existing archive location when archives are needed."),
        ("pulsar doctor", "Run explicit host and topology diagnostics, with every node's prerequisites "
                          "and its hf login.")]),
    ("Catalog and model files", [
        ("pulsar models", "Browse catalog and saved storage observations."),
        ("pulsar models menu --read-only",
         "Select published specs and inspect saved details without offering operations."),
        ("pulsar models show SPEC", "Show recipe, known copies, pins and blockers."),
        ("pulsar models files", "Open Model files and disk space; checks all confirmed nodes."),
        ("pulsar model files", "List managed model snapshots and their placement, including uncataloged downloads."),
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
        ("pulsar logs SPEC", "Print the recent output of the service's containers on every rank; read-only."),
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
        ("pulsar policy show baseline-v1|baseline-v2", "Print a supported baseline policy: its digest, and what each gate requires."),
        ("pulsar evidence measurement|evaluate|verify|summary",
         "Build and assess compact evidence for a spec; never a catalog gate."),
        ("pulsar contribution verify --package DIR",
         "Validate a contribution package, including publication privacy."),
        ("pulsar privacy check|commits", "Check files or commit metadata for private information before publication."),
        ("pulsar selftest [--checks-only] [--jobs N]", "Run this checkout's deterministic tests and checks; nothing reaches a node."),
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


def _table(path: Path, heading: str, name: str, columns: tuple[str, str]) -> list[tuple[str, str]]:
    """The two-column table after HEADING as (key, meaning), in the document's order.

    NAME and COLUMNS (such as "Terms" and ("Term", "Meaning")) word the errors
    and the expected header; every row has exactly those two cells.
    """
    shown = _shown(path)
    noun = columns[0].lower()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise GuideError(f"cannot read {shown}: {getattr(error, 'strerror', None) or error}") from error
    if heading not in lines:
        raise GuideError(f"{shown} has no '{heading}' section")
    table: list[tuple[int, str]] = []
    for number in range(lines.index(heading) + 1, len(lines)):
        line = lines[number]
        if line.startswith("#") or (table and not line.startswith("|")):
            break
        if line.startswith("|"):
            table.append((number + 1, line))
    if len(table) < 3:
        raise GuideError(f"{shown}: the {name} section has no table rows")
    (header_number, header), (separator_number, separator), *rows = table
    if _cells(header) != list(columns):
        raise GuideError(f"{shown} line {header_number}: the {name} table's header must read "
                         f"'{columns[0]} | {columns[1]}'")
    if len(_cells(separator)) != 2 or not all(re.fullmatch(r":?-+:?", cell) for cell in _cells(separator)):
        raise GuideError(f"{shown} line {separator_number}: expected the {name} table's separator row")
    result = []
    for number, line in rows:
        cells = _cells(line)
        if len(cells) != 2:
            raise GuideError(f"{shown} line {number}: a {name} row needs 2 cells, a {noun} and its meaning; "
                             f"found {len(cells)}")
        if not all(cells):
            raise GuideError(f"{shown} line {number}: a {name} row has an empty {'meaning' if cells[0] else noun}")
        result.append((_marked(cells[0]).replace("`", "").replace("**", ""), _marked(cells[1])))
    return result


def terms(path: Path = OPERATIONS) -> list[tuple[str, str]]:
    """The Terms table's rows as (term, meaning): the `## Terms` heading, a
    `Term | Meaning` header, and two cells per row."""
    return _table(path, TERMS_HEADING, "Terms", ("Term", "Meaning"))


def labels(path: Path = OPERATIONS) -> list[tuple[str, str]]:
    """The Catalog labels table's rows as (label, meaning). Each label reads
    `Field: words`, naming where the words appear."""
    return _table(path, LABELS_HEADING, "Catalog labels", ("Label", "Meaning"))


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
         f"after **{LABELS['start']}** and under **Running services… → How to connect**."),
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


def emit_labels(writer: TerminalWriter, rows: list[tuple[str, str]]) -> None:
    """Rows grouped under their field, in the table's order: Files, Archive and so on."""
    writer.emit("What the labels mean", role="heading")
    writer.emit("The catalog list, a spec's frame and `pulsar models list` describe each spec with these words; "
                "the Model files page (`pulsar model files`) describes each model's files. "
                "The same list is under Catalog labels in `docs/OPERATIONS.md`.", markup=True)
    group = None
    for label, meaning in rows:
        field, _, words = label.partition(": ")
        if not words:
            field, words = "", label
        if field != group:
            group = field
            writer.blank()
            if field:
                writer.emit(field, role="heading")
        writer.emit(words, initial_indent="  ", role="choice")
        writer.emit(meaning, initial_indent="    ", markup=True)


def emit_workflow(writer: TerminalWriter) -> None:
    writer.emit("How serving works", role="heading")
    writer.emit("**Set up this cluster…** first. Then choose **Serve a model…** and a model: Pulsar shows a plan "
                "of the steps below that it still needs, with the *serving image* and a Stop of whatever is in "
                "the way between Prepare and Start, and the cursor on **Cancel**. One **Go ahead** runs them in "
                "order; each step checks first, and Pulsar stops and asks again if one would do more than the "
                "plan says. **Running services…** shows what runs and how to connect to it, and restarts, switches "
                "or stops it, after a plan that starts on Cancel or a question that starts on No. **Cluster…** checks the nodes and "
                "saves a change to the cluster. **Help and troubleshooting… → Why will a model not start?** runs "
                "Start's checks without starting and offers each fix. **Help and troubleshooting… → Advanced: "
                "single operations…** runs each step on its own.", markup=True)
    writer.blank()
    for number, (step, sentence) in enumerate(workflow(), 1):
        writer.emit(f"{number}. {step}", initial_indent="  ", role="heading")
        writer.emit(sentence, initial_indent="     ", markup=True)
    writer.blank()
    writer.emit("The same steps as commands: `pulsar model acquire` or `model restore`, `model prepare`, "
                "`image stage`, `start`, `status`, `stop` and `model purge`. Most commands accept `--help`. "
                "The outcome of **Serve a model…** lists the commands it ran under Same as.", markup=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulsar help", description="Print a Help page.")
    parser.add_argument("topic", choices=("reference", "terms", "labels", "workflow"))
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
        rows = labels(args.file) if args.topic == "labels" else terms(args.file)
    except GuideError as error:
        error_line(error, tag="help-pages")
        return 1
    (emit_labels if args.topic == "labels" else emit_terms)(writer, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
