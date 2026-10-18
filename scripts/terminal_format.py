#!/usr/bin/env python3
"""Shared width-aware formatting for Pulsar's human-facing terminal views."""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from typing import TextIO


DEFAULT_WIDTH = 80
MIN_WIDTH = 32
MAX_WIDTH = 100


def error_line(message: object, *, tag: str, stream: TextIO | None = None) -> None:
    """One `error: …` line, as scripts/lib.sh prints it: the message leads, and
    PULSAR_VERBOSE=1 adds the internal tool name for debugging."""
    prefix = f"[{tag}] " if os.environ.get("PULSAR_VERBOSE") == "1" else ""
    print(f"{prefix}error: {message}", file=stream or sys.stderr)


def terminal_width(
    fallback: int = DEFAULT_WIDTH,
    minimum: int = MIN_WIDTH,
    maximum: int = MAX_WIDTH,
) -> int:
    """Return a practical output width from COLUMNS/the active terminal."""
    width = shutil.get_terminal_size((fallback, 24)).columns
    return max(minimum, min(width, maximum))


_KEEP = "\u00a0"  # no-break space: textwrap never breaks at it

# Styles for human output, after clig.dev, NO_COLOR (no-color.org) and
# man-pages(7): headings in bold and the menu accent, menu choices in bold,
# repo terms in the accent, commands and other literal text in cyan, and
# placeholders such as SPEC underlined, as man shows them on a terminal.
# Color is never the only cue (WCAG 1.4.1); each styled part also reads
# plainly. A terminal (TERM set and not dumb) gets styles. PULSAR_COLOR=always
# styles other output too, such as a page the menu's pager shows;
# PULSAR_COLOR=never prints no escape sequences. NO_COLOR removes the colors
# and keeps bold and underline.
_NEVER = {"never", "0", "no", "off", "false"}
_MARKUP = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\*([^*\s][^*]*)\*")
# Inside literal text, a word in capitals standing alone is a placeholder:
# SPEC, NODE, ID|..., [N]. Capitals within a name, such as PULSAR_COLOR or
# docs/OPERATIONS.md, are literal.
_PLACEHOLDER = re.compile(r"(?<![^\s|\[(,])[A-Z]+(?![^\s|\]),])")
_WRAP_WHITESPACE = re.compile(r"[\t\n\x0b\x0c\r]")


def styles_enabled(stream: TextIO | None = None) -> bool:
    setting = os.environ.get("PULSAR_COLOR", "")
    if setting in _NEVER:
        return False
    if setting == "always":
        return True
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    try:
        return (stream or sys.stdout).isatty()
    except (AttributeError, ValueError):
        return False


def colors_enabled(stream: TextIO | None = None) -> bool:
    return styles_enabled(stream) and not os.environ.get("NO_COLOR")


def _accent() -> str:
    """PULSAR_ACCENT, as the menus use it, in SGR form. The 16 basic colors
    keep the terminal theme's own values."""
    value = os.environ.get("PULSAR_ACCENT", "12").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return "38;2;" + ";".join(str(int(value[i:i + 2], 16)) for i in (1, 3, 5))
    index = int(value) if value.isdigit() else 12
    if index < 8:
        return str(30 + index)
    if index < 16:
        return str(90 + index - 8)
    return f"38;5;{index}" if index < 256 else "94"


def _sgr(colors: bool) -> dict[str, str]:
    accent = _accent()
    if not colors:
        return {"heading": "1", "choice": "1", "term": "", "literal": "", "placeholder": "4"}
    return {"heading": f"1;{accent}", "choice": "1", "term": accent, "literal": "36", "placeholder": "4;36"}


def _marked(text: str, room: int) -> tuple[str, list[str | None]]:
    """Text with `literal`, **choice** and *term* marks as plain text and each
    character's role. A literal that fits in ROOM never wraps inside."""
    plain: list[str] = []
    roles: list[str | None] = []
    end = 0
    for match in _MARKUP.finditer(text):
        plain.append(text[end:match.start()])
        roles.extend([None] * (match.start() - end))
        literal, choice, term = match.groups()
        if literal is not None:
            kinds: list[str | None] = ["literal"] * len(literal)
            for placeholder in _PLACEHOLDER.finditer(literal):
                kinds[placeholder.start():placeholder.end()] = ["placeholder"] * len(placeholder.group())
            plain.append(literal.replace(" ", _KEEP) if len(literal) <= room else literal)
            roles.extend(kinds)
        else:
            words = choice if choice is not None else term
            plain.append(words)
            roles.extend(["choice" if choice is not None else "term"] * len(words))
        end = match.end()
    plain.append(text[end:])
    roles.extend([None] * (len(text) - end))
    return "".join(plain), roles


class TerminalWriter:
    """Render labeled fields and hanging-indented text without line overflow."""

    def __init__(self, width: int | None = None, stream: TextIO | None = None):
        self.width = (
            terminal_width()
            if width is None
            else max(MIN_WIDTH, min(width, MAX_WIDTH))
        )
        self.stream = stream or sys.stdout
        self.styles = styles_enabled(self.stream)
        self.sgr = _sgr(colors_enabled(self.stream))

    def emit(
        self,
        text: object = "",
        initial_indent: str = "",
        subsequent_indent: str | None = None,
        break_on_hyphens: bool = False,
        *,
        role: str | None = None,
        markup: bool = False,
    ) -> None:
        """Print TEXT wrapped to the width. ROLE styles all of it (heading,
        choice, term, literal); MARKUP reads `literal`, **choice** and *term*
        marks inside it. Lines wrap on the visible text, so styled and plain
        output break at the same places."""
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
        if role is None and not markup:
            for line in wrapper.wrap(str(text)):
                print(line, file=self.stream)
            return
        # One character of whitespace stays one, so each wrapped line maps back
        # to its characters' roles.
        text = _WRAP_WHITESPACE.sub(" ", str(text))
        if markup:
            plain, roles = _marked(text, self.width - max(len(initial_indent), len(subsequent_indent)))
        else:
            plain, roles = text, [role] * len(text)
        lines = wrapper.wrap(plain)
        cursor = 0
        for number, line in enumerate(lines):
            indent = initial_indent if number == 0 else subsequent_indent
            content = line[len(indent):]
            while cursor < len(plain) and plain[cursor] == " " and not content.startswith(" "):
                cursor += 1
            if plain[cursor:cursor + len(content)] != content:
                # Never misplace a style: the rest prints plainly.
                for rest in lines[number:]:
                    print(rest.replace(_KEEP, " "), file=self.stream)
                return
            print(indent + self._styled(content, roles[cursor:cursor + len(content)]), file=self.stream)
            cursor += len(content)

    def _styled(self, content: str, roles: list[str | None]) -> str:
        pieces = []
        start = 0
        for index in range(1, len(content) + 1):
            if index == len(content) or roles[index] != roles[start]:
                run = content[start:index].replace(_KEEP, " ")
                params = self.sgr.get(roles[start] or "", "") if self.styles else ""
                pieces.append(f"\033[{params}m{run}\033[0m" if params and run.strip() else run)
                start = index
        return "".join(pieces)

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
