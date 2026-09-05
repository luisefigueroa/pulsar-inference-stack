#!/usr/bin/env python3
"""Shared width-aware formatting for Pulsar's human-facing terminal views."""

from __future__ import annotations

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
            break_on_hyphens=False,
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
