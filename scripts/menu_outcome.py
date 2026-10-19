#!/usr/bin/env python3
"""One outcome per menu operation, worded by what its exit status means.

Exit statuses follow the CLI contract (docs/CONTRACT.md): 1 is a known "no",
2 is a request rejected before any action, and 3 is an outcome Stack could
not establish. "No" and "couldn't tell" must not read alike: Live status exit
1 means starting is safe, while exit 3 means it is not. The command's own
output, printed above this outcome, holds the reasons. An interruption has
its own report: the menu cannot confirm cleanup or partial effects, even when
a concurrent completion returned exit 0.
"""

from __future__ import annotations

import argparse
import sys

from scripts.terminal_format import TerminalWriter

INSPECTIONS = {"status", "check", "readiness", "image-check", "verify", "budget"}
# The inspection that establishes an action's effect when its outcome is unknown.
RECHECK = {"start": "Live status", "stop": "Live status",
           "image-stage": "Launch options → Check serving image"}
STORAGE_RECHECK = "Check now"
REJECTED = "the reason is above; if it names an invalid argument, please report a menu bug"

# A "no" from each action: (glyph, headline, detail).
NO = {
    "status": ("▲", "{subject} is not running.",
               "no node runs it, so starting it won't conflict"),
    "readiness": ("✗", "{subject} is not ready to start.", "the blockers and their fixes are above"),
    "image-check": ("✗", "The serving image for {subject} is not ready on every node.",
                    "each node's state is above; Launch options → Stage serving image can add it"),
    "check": ("✗", "{label} failed for {subject} without saving an observation.",
              "the reason is above"),
    "verify": ("✗", "{label} found a problem for {subject}.", "the findings are above"),
    "start": ("✗", "Start was refused or failed for {subject}.", "the reasons are above"),
    "stop": ("✗", "Stop was refused or failed for {subject}.",
             "the reasons are above; Live status shows what still runs"),
}
NO_DEFAULT = ("✗", "{label} was refused or failed for {subject}.", "the reasons are above")

STORAGE = ("acquire", "restore", "prepare", "move", "archive", "pin", "unpin", "purge", "remove")
# What to inspect after an interruption, before retrying the operation.
INTERRUPTED_NEXT = {
    **dict.fromkeys(STORAGE, "Use Check now to inspect current files before retrying. "
                             "Retained staging may be recoverable by the same operation."),
    **dict.fromkeys(("start", "stop"), "Use Live status to inspect the service before retrying Start or Stop."),
    "image-stage": "Use Launch options → Check serving image before retrying staging.",
}
INTERRUPTED_INSPECTION = "Rerun this inspection when ready; its partial output may be incomplete."


def outcome(action: str, label: str, subject: str, status: int, elapsed: str,
            *, preview: bool = False, recorded: bool = False) -> tuple[str, str | None]:
    """Return the headline and an optional detail line for one operation."""
    if preview:
        return _preview(label, subject, status)
    if status == 0:
        return f"✓ {label} finished for {subject} in {elapsed}", None
    after = f"exit {status} after {elapsed}"
    if recorded:
        found = ("some files could not be observed, so their state is unknown" if status == 3
                 else "missing or changed files are above")
        return (f"▲ {label} saved an observation with findings for {subject} in {elapsed}.",
                f"exit {status} · {found}")
    if status == 1:
        glyph, headline, detail = NO.get(action, NO_DEFAULT)
    elif status == 2:
        glyph, headline, detail = (
            "✗", "{label} was rejected before it ran for {subject}; nothing changed.",
            REJECTED)
    elif status == 3 and action == "status":
        glyph, headline, detail = (
            "?", "Whether {subject} is running is unknown: a node could not be observed.",
            "don't start it until Live status observes every node")
    elif status == 3 and action in INSPECTIONS:
        glyph, headline, detail = ("?", "{label} could not finish for {subject}, so its result is unknown.",
                                   "the reason is above")
    elif status == 3:
        glyph, headline, detail = (
            "?", "The outcome of {label} for {subject} is not established.",
            f"use {RECHECK.get(action, STORAGE_RECHECK)} before retrying")
    else:
        glyph, headline, detail = ("✗", "{label} failed for {subject}.", "details are above")
    return (f"{glyph} " + headline.format(label=label, subject=subject),
            f"{after} · {detail}")


def interruption(action: str, label: str, subject: str, status: int,
                 elapsed: str) -> tuple[str, list[str]]:
    """Return the headline and detail lines for an interrupted operation."""
    return (f"Interruption requested: {label} for {subject} after {elapsed}.",
            [f"exit {status} · this menu has not confirmed cleanup or partial effects; "
             "review the command output above",
             INTERRUPTED_NEXT.get(action, INTERRUPTED_INSPECTION)])


def _preview(label: str, subject: str, status: int) -> tuple[str, str]:
    """A --plan preview changes nothing, whatever its status."""
    if status == 1:
        return f"✗ {label} is not possible for {subject} now; nothing changed.", "exit 1 · the reason is above"
    if status == 2:
        return (f"✗ The {label} preview was rejected before it ran; nothing changed.",
                f"exit 2 · {REJECTED}")
    if status == 3:
        return (f"? The {label} preview could not finish for {subject}; nothing changed.",
                "exit 3 · the reason is above")
    return f"✗ The {label} preview failed for {subject}; nothing changed.", f"exit {status} · details are above"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a menu operation's outcome.")
    parser.add_argument("--action", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--status", required=True, type=int)
    parser.add_argument("--elapsed", default="")
    parser.add_argument("--preview", action="store_true", help="the operation's --plan preview failed")
    parser.add_argument("--recorded", action="store_true", help="a check saved an observation")
    parser.add_argument("--interrupted", action="store_true", help="the operation was interrupted")
    args = parser.parse_args(argv)
    if args.interrupted:
        headline, details = interruption(args.action, args.label, args.subject, args.status, args.elapsed)
    else:
        headline, detail = outcome(args.action, args.label, args.subject, args.status, args.elapsed,
                                   preview=args.preview, recorded=args.recorded)
        details = [detail] if detail else []
    writer = TerminalWriter()
    # The leading ✓ or ✗ takes the pass or fail style.
    role = {"✓": "pass", "✗": "fail"}.get(headline[:1])
    if role and headline[1:2] == " ":
        writer.emit(headline[2:], initial_indent=headline[:2], subsequent_indent="  ", prefix_role=role)
    else:
        writer.emit(headline, subsequent_indent="  ")
    for detail in details:
        writer.emit(detail, initial_indent="  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
