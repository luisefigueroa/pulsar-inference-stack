#!/usr/bin/env python3
"""Shared width-aware formatting for Pulsar's human-facing terminal views."""

from __future__ import annotations

import re
import shutil
import sys
import textwrap
from typing import TextIO


DEFAULT_WIDTH = 80
MIN_WIDTH = 32
MAX_WIDTH = 100


def terminal_width(
    fallback: int = DEFAULT_WIDTH,
    minimum: int = MIN_WIDTH,
    maximum: int = MAX_WIDTH,
) -> int:
    """Return a practical output width from COLUMNS/the active terminal."""
    width = shutil.get_terminal_size((fallback, 24)).columns
    return max(minimum, min(width, maximum))


class TerminalWriter:
    """Render labeled fields and hanging-indented text without line overflow."""

    def __init__(self, width: int | None = None, stream: TextIO | None = None):
        self.width = (
            terminal_width()
            if width is None
            else max(MIN_WIDTH, min(width, MAX_WIDTH))
        )
        self.stream = stream or sys.stdout

    def emit(
        self,
        text: object = "",
        initial_indent: str = "",
        subsequent_indent: str | None = None,
        break_on_hyphens: bool = False,
    ) -> None:
        if text is None or text == "":
            print(file=self.stream)
            return
        if subsequent_indent is None:
            subsequent_indent = initial_indent
        wrapper = textwrap.TextWrapper(
            width=self.width,
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent,
            break_long_words=True,
            break_on_hyphens=break_on_hyphens,
        )
        for line in wrapper.wrap(str(text)):
            print(line, file=self.stream)

    def field(
        self,
        label: object,
        value: object,
        indent: int = 0,
        label_width: int = 10,
    ) -> None:
        label_text = str(label)
        # label_width is an alignment target, not permission to concatenate a
        # long label directly with its value.
        effective_width = max(label_width, len(label_text) + 1)
        prefix = f"{' ' * indent}{label_text:<{effective_width}}"
        self.emit(value, prefix, " " * len(prefix))

    def blank(self) -> None:
        print(file=self.stream)


# One help style for every command: usage lines, a sentence on what the
# command does, then options with their descriptions in one column.
_USAGE = re.compile(r"^\s*usage:", re.IGNORECASE)
# An option row: indent, a term (single spaces inside), two or more spaces,
# then its description.
_OPTION = re.compile(r"^( {2,})(\S+(?: \S+)*?) {2,}(\S.*)$")
_BULLET = re.compile(r"^\s*[*-] ")
_KEEP = "\u00a0"  # no-break space: textwrap never breaks at it


def _keep_brackets(text: str) -> str:
    """Join the words of each [...] group so a wrap never splits one."""
    return re.sub(r"\[[^\]]*\]", lambda group: group.group(0).replace(" ", _KEEP), text)


def emit_help(text: str, writer: TerminalWriter | None = None) -> None:
    """Print a help screen in the shared style, fitted to the terminal width.

    Usage lines wrap with a hanging indent and never inside a [...] group.
    Option rows ("  TERM  DESCRIPTION") align their descriptions in one
    column and wrap under it; a deeper-indented line continues the row above.
    When the column would leave too little room, each description moves under
    its term. Other lines are paragraphs, rejoined and rewrapped; a bullet
    starts a new one.
    """
    out = writer or TerminalWriter()

    def wrap(value: str, first: str, rest: str) -> None:
        wrapper = textwrap.TextWrapper(width=out.width, initial_indent=first, subsequent_indent=rest,
                                       break_long_words=True, break_on_hyphens=False)
        for line in wrapper.wrap(value):
            print(line.replace(_KEEP, " "), file=out.stream)

    blocks: list[list] = []  # [kind, indent, items]
    for raw in text.splitlines():
        line = raw.rstrip()
        indent = line[:len(line) - len(line.lstrip())]
        previous = blocks[-1] if blocks else None
        option = _OPTION.match(line)
        if not line:
            if previous and previous[0] != "blank":
                blocks.append(["blank", "", []])
        elif _USAGE.match(line) or (previous and previous[0] == "usage" and indent and not option):
            blocks.append(["usage", indent, [line.strip()]])
        elif option:
            if not previous or previous[0] != "options":
                blocks.append(["options", option.group(1), []])
            blocks[-1][2].append([option.group(2), option.group(3)])
        elif previous and previous[0] == "options" and len(indent) > len(previous[1]):
            previous[2][-1][1] += " " + line.strip()
        elif previous and previous[0] == "text" and previous[1] == indent and not _BULLET.match(line):
            previous[2][0] += " " + line.strip()
        else:
            blocks.append(["text", indent, [line.strip()]])
    while blocks and blocks[-1][0] == "blank":
        blocks.pop()
    for kind, indent, items in blocks:
        if kind == "blank":
            out.blank()
        elif kind == "usage":
            # A wrapped usage line indents past the start of the next usage form.
            continuation = " " * 9 if _USAGE.match(items[0]) else indent + "  "
            wrap(_keep_brackets(items[0]), indent, continuation)
        elif kind == "text":
            wrap(items[0], indent, indent + ("  " if _BULLET.match(items[0]) else ""))
        else:
            column = len(indent) + max(len(term) for term, _ in items) + 2
            if column <= max(24, out.width // 2):
                for term, description in items:
                    wrap(description, (indent + term).ljust(column), " " * column)
            else:
                for term, description in items:
                    wrap(_keep_brackets(term), indent, indent + "  ")
                    wrap(description, indent + "    ", indent + "    ")


if __name__ == "__main__":
    # Scripts pipe a help screen here: python3 scripts/terminal_format.py <<'HELP'
    emit_help(sys.stdin.read())
