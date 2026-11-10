#!/usr/bin/env python3
"""Why will a model not start?: Start's own checks, without starting, in plain words.

    python3 -m scripts.start_reasons screen --document FILE --rc N --spec-id SPEC [--width N] < CATALOG_JSON
    python3 -m scripts.start_reasons choices --document FILE --rc N

The document is what `./pulsar start SPEC --dry-run --json` printed: its
`checks` (scripts/start_checks.py), and on a refusal the blocker records in
`error.details` (scripts/start_blockers.py). Each check shows its outcome; a
check the dry run did not reach reads "not run", never as passed. Each
blocker is said in plain words with its fix, and the fix is an intent the
menu offers, not a single operation: **Serve a model…** (it plans the files,
the copies and the serving image, and stops what is in the way), **Running
services…**, **Check the cluster now** or **Run diagnostics**. The records'
own `fix` and `note` texts are not shown here: they name command flags that
only an explicit choice may add. `choices` prints `ACTION<TAB>LABEL` lines,
one per fix, then Check again and Back. Nothing here contacts a node.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.terminal_format import TerminalWriter, terminal_width  # noqa: E402

# Start's checks by what they look at, in its order (scripts/start_checks.py CHECK_IDS).
CHECKS = {"spec-review": "recipe review", "recipe": "recipe", "topology": "nodes", "fabric": "links between nodes",
          "image": "serving image", "service": "existing service", "model-files": "model files",
          "memory": "free memory", "port": "port", "preflight": "multi-node preflight", "plan": "launch plan"}
MARKS = {"pass": "✓", "fail": "✗", "info": "•", "not_run": "–"}

# The words this screen prints about a check or the whole answer. Each has a
# row under Catalog labels in docs/OPERATIONS.md (Why: …); tests/test_help.py checks it.
STATUS_WORDS = {
    "not-run": "not run: an earlier blocker ended the checks",
    "passed": "Nothing blocks it",
    "could-not-run": "Start's checks could not run",
}

# Each blocker in plain words, and the intent that resolves it.
REASONS = {
    "model_files_not_ready": ("The model files are not on every serving node yet.", "serve"),
    "image_missing": ("The serving image is missing on a serving node.", "serve"),
    "port_in_use": ("Its port is already in use on a serving node.", "serve"),
    "memory_insufficient": ("There is not enough free memory on a serving node for it.", "services"),
    "memory_warning": ("Free memory is below the recommended headroom.", "services"),
    "service_exists": ("A service of this recipe already exists.", "services"),
    "topology_incomplete": ("The cluster has fewer nodes than this recipe needs.", "cluster"),
    "fabric_incomplete": ("The links between the nodes do not meet this recipe's needs.", "cluster"),
    "node_unreachable": ("A serving node did not answer over SSH.", "cluster"),
    "docker_unavailable": ("Docker is unavailable on a serving node, or could not confirm the serving image.",
                           "diagnostics"),
    "image_check_failed": ("The serving image check could not finish.", "diagnostics"),
    "model_files_check_failed": ("The model files check could not finish.", "diagnostics"),
    "memory_check_failed": ("The memory check could not finish.", "diagnostics"),
    "preflight_failed": ("The multi-node preflight failed.", "diagnostics"),
    "guard_unsupported": ("This recipe needs a serving guard, which ordinary Start cannot enforce.", None),
    "historical_spec": ("This is a historical recipe that this Stack reads but cannot start.", None),
}
# Each fix: its choice and what it does, said where the blocker is.
FIXES = {
    "serve": ("Serve a model…", "plans what the recipe still needs: its files, the copies to every serving node "
                                "and the serving image, and a Stop of a Pulsar service in its way, after one approval"),
    "services": ("Running services…", "shows what runs on the nodes, with its health and Stop"),
    "cluster": ("Check the cluster now", "finds which node or link, with its fix"),
    "diagnostics": ("Run diagnostics", "checks Docker, the GPUs and every node's prerequisites"),
}
NO_FIX = "No menu choice resolves it: this Stack cannot start this recipe."


def clean(value: Any) -> str:
    return " ".join(str(value).split())


def read(document: Any, rc: int) -> tuple[list[dict], list[dict], str | None]:
    """(checks, blocker records, why the checks could not run) from the dry run's document."""
    if not isinstance(document, dict):
        return [], [], f"its answer was not readable (exit {rc})"
    if document.get("ok") is True and isinstance(document.get("result"), dict):
        checks = document["result"].get("checks") or []
        return [check for check in checks if isinstance(check, dict)], [], None
    error = document.get("error") if isinstance(document.get("error"), dict) else {}
    records = [record for record in error.get("details") or [] if isinstance(record, dict) and record.get("blocker")]
    checks = [check for check in error.get("checks") or [] if isinstance(check, dict)]
    if not records:
        return checks, [], clean(error.get("message") or f"exit {rc}")
    return checks, records, None


def check_lines(checks: list[dict]) -> list[tuple[str, str]]:
    """(mark, text) per check, its worst outcome across nodes, in Start's order."""
    severity = {"not_run": 0, "pass": 1, "info": 2, "fail": 3}
    worst: dict[str, dict] = {}
    for check in checks:
        name = check.get("id")
        outcome = check.get("outcome")
        if name is None or outcome not in severity:
            continue
        if name not in worst or severity[outcome] > severity[worst[name]["outcome"]]:
            worst[name] = check
    lines = []
    for name, check in worst.items():
        words = CHECKS.get(name, clean(name))
        outcome = check["outcome"]
        if outcome == "pass":
            lines.append((MARKS[outcome], words))
        elif outcome == "not_run":
            lines.append((MARKS[outcome], f"{words}: {STATUS_WORDS['not-run']}"))
        else:
            message = clean(check.get("message") or "")
            lines.append((MARKS[outcome], f"{words}: {message}" if message else words))
    return lines


def blockers(records: list[dict]) -> list[tuple[str, str | None]]:
    """(sentence, fix action) per distinct blocker, in Start's order."""
    found, seen = [], set()
    for record in records:
        code = record.get("blocker")
        reason, action = REASONS.get(code, (f"Start reported: {clean(record.get('message') or code)}.",
                                            "diagnostics"))
        where = clean(record.get("node") or "")
        detail = clean(record.get("detail") or "")
        extra = "; ".join(part for part in (f"on {where}" if where else "", detail) if part)
        sentence = reason[:-1] + f" ({extra})." if extra else reason
        if (sentence, action) in seen:
            continue
        seen.add((sentence, action))
        found.append((sentence, action))
    return found


def screen(document: Any, rc: int, title: str, *, width: int | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    out.emit(title, role="heading")
    checks, records, failure = read(document, rc)
    if failure is not None and not checks:
        out.emit(f"{STATUS_WORDS['could-not-run']}: {failure}. **Run diagnostics** checks Docker, the GPUs and "
                 "every node's prerequisites.", "✗ ", "  ", prefix_role="fail", markup=True)
        return output.getvalue()
    out.emit("Start's checks, run just now without starting:")
    for mark, text in check_lines(checks):
        out.emit(text, f"  {mark} ", "    ", prefix_role="fail" if mark == "✗" else None)
    out.blank()
    if failure is not None:
        out.emit(f"{STATUS_WORDS['could-not-run']} to the end: {failure}.", "✗ ", "  ", prefix_role="fail")
    elif not records:
        out.emit(f"{STATUS_WORDS['passed']}: every check passed. **Serve a model…** starts it, after its plan.",
                 "✓ ", "  ", markup=True)
    else:
        for sentence, action in blockers(records):
            fix = (f"Fix: **{FIXES[action][0]}** {FIXES[action][1]}." if action else NO_FIX)
            out.emit(f"{sentence} {fix}", "Blocked: ", "  ", prefix_role="fail", markup=True)
        if any(check.get("outcome") == "not_run" for check in checks):
            out.emit("Checks after the first blocker did not run; after the fix, **Check again** runs them.",
                     markup=True)
    return output.getvalue()


def choices(document: Any, rc: int) -> list[tuple[str, str]]:
    checks, records, failure = read(document, rc)
    found: list[tuple[str, str]] = []
    actions = [action for _, action in blockers(records) if action]
    if failure is not None:
        actions.append("diagnostics")
    elif not records:
        actions.append("serve")
    for action in dict.fromkeys(actions):
        found.append((action, FIXES[action][0]))
    return found + [("again", "Check again"), ("back", "Back")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("screen", "choices"):
        command = sub.add_parser(name)
        command.add_argument("--document", required=True)
        command.add_argument("--rc", type=int, default=0)
        command.add_argument("--width", type=int)
    sub.choices["screen"].add_argument("--spec-id", required=True)
    args = parser.parse_args(argv)
    try:
        document = json.loads(Path(args.document).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = None
    if args.command == "screen":
        try:
            entries = json.load(sys.stdin).get("entries") or []
        except (ValueError, AttributeError):
            entries = []
        model = next((clean(row["model_id"]) for row in entries if row.get("spec_id") == args.spec_id), "it")
        sys.stdout.write(screen(document, args.rc, f"Why will {model} not start?",
                                width=args.width or terminal_width()))
    else:
        for action, label in choices(document, args.rc):
            print(f"{action}\t{label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
