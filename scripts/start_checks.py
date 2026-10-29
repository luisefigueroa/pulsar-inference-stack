#!/usr/bin/env python3
"""Start checks: what `pulsar start` checked, passed or did not reach.

`pulsar start` (and its dry run) records each check it runs through `record`,
beside the human PASS/FAIL line, when PULSAR_START_CHECKS_FILE is set. Before
the first check it records the checks this start expects through `expect`, so
`read` can name the ones a blocker kept it from running as `not_run`.
`start --json` returns `read`'s list as `checks`, in the result or beside the
error's blocker records; the catalog menu prints it as one checklist line.
Recording never probes a node and never changes what start does.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every check start can run, in order. fabric and preflight apply only to
# multi-node specs.
CHECK_IDS = ("spec-review", "recipe", "topology", "fabric", "image", "service", "model-files", "memory",
             "port", "preflight", "plan")
OUTCOMES = ("pass", "fail", "info", "not_run")
NOT_RUN = "not checked: an earlier blocker ended the checks"


def read(path: str | Path) -> list[dict]:
    """Recorded checks in order, then each expected check not recorded as not_run.

    A missing file means start recorded nothing.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    expected: list[str] = []
    checks: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        value = json.loads(line)
        if "expected" in value:
            expected = list(value["expected"])
        else:
            checks.append(value)
    seen = {check["id"] for check in checks}
    return checks + [{"id": check, "outcome": "not_run", "node": None, "message": NOT_RUN}
                     for check in expected if check not in seen]


# The checklist's mark for each outcome; the word after it names the check.
MARKS = {"pass": "✓", "fail": "✗", "info": "•", "not_run": "–"}


def checklist(checks: list[dict], keep: str = " ") -> str:
    """One line such as "✓ recipe  ✓ topology  ✗ memory  – port (not run)".

    A check recorded once per node shows once, with its worst outcome. KEEP
    joins the words of one entry; a no-break space keeps them together when
    the line wraps.
    """
    severity = {"not_run": 0, "pass": 1, "info": 2, "fail": 3}
    outcome: dict[str, str] = {}
    for check in checks:
        if severity[check["outcome"]] > severity[outcome.get(check["id"], "not_run")] or check["id"] not in outcome:
            outcome[check["id"]] = check["outcome"]
    entries = []
    for check, value in outcome.items():
        words = [MARKS[value], *check.split("-")] + (["(not", "run)"] if value == "not_run" else [])
        entries.append(keep.join(words))
    return "  ".join(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    expect = sub.add_parser("expect", help="start a new record with the checks this start expects")
    expect.add_argument("ids", nargs="+", choices=CHECK_IDS)
    record = sub.add_parser("record", help="record one check")
    record.add_argument("outcome", choices=OUTCOMES[:3])
    record.add_argument("id", choices=CHECK_IDS)
    record.add_argument("message")
    record.add_argument("--node")
    show = sub.add_parser("checklist", help="print the recorded checks as one line")
    show.add_argument("--checks-file", required=True)
    args = parser.parse_args(argv)
    if args.command == "checklist":
        keep = "\u00a0"
        line = checklist(read(args.checks_file), keep)
        if line:
            import textwrap
            from scripts.terminal_format import terminal_width
            for row in textwrap.wrap(line, width=terminal_width(), initial_indent="  Checks: ",
                                     subsequent_indent=" " * 10, break_on_hyphens=False):
                print(row.replace(keep, " "))
        return 0
    target = os.environ.get("PULSAR_START_CHECKS_FILE")
    if not target:
        return 0
    if args.command == "expect":
        Path(target).write_text(json.dumps({"expected": args.ids}) + "\n", encoding="utf-8")
        return 0
    # Terminal styling never reaches JSON.
    message = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", args.message)
    value = {"id": args.id, "outcome": args.outcome, "node": args.node or None, "message": " ".join(message.split())}
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
