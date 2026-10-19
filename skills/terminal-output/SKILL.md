---
name: terminal-output
description: Build or change what a Pulsar operator reads in a terminal (menu frames, help screens, check lines, previews and pages) with the shared TerminalWriter styles, narrow-width layout and unchanged plain output. Use when adding or editing human output, menus or help in the public stack.
---

# Terminal output

Use this skill in the public inference stack for any text a person reads in a
terminal. The repository's `AGENTS.md` sets the content rules: lead with the
action, affected object and blocker, use the
[glossary](../../docs/OPERATIONS.md#terms), and keep JSON separate. This skill
covers how that text is laid out and styled. What operators see is described
in [output and messages](../../docs/OPERATIONS.md#output-and-messages); the
role-to-style map lives in `scripts/terminal_format.py`. Change it there, not
here.

The rules follow [clig.dev](https://clig.dev/#output) (bold headings, color
with intention, no escape codes off a terminal),
[NO_COLOR](https://no-color.org/) (it removes color, not bold or underline),
man-pages(7) (literal text versus replaceable arguments) and
[WCAG 1.4.1](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html)
(color is never the only cue). Menus use the accent from `scripts/ui.sh`; never
Gum's default pink.

## Build

- Print through `TerminalWriter`, `emit_help` or `emit_checks`, and don't
  write escape codes by hand. A label takes `field(..., label_role=...)`, and
  one line that must not wrap takes `paint(text, roles)`.
- In Bash, print check sections with `scripts/terminal_format.py --checks
  TITLE [LEVEL TEXT]...` and a verdict with `--status LEVEL WORD TEXT`. A
  single row goes through `status_line` or `print_hanging`, which style a
  leading WARN, FAIL, BLOCKED or READY from the same map. `warn` and
  `error_line` style their labels.
- Give styled text a role instead of a color:
  - `heading`: titles, section headings and entry headings.
  - `choice`: menu choices and other on-screen names, which the docs also bold.
  - `term`: glossary words in running text. Mark them yourself; never detect
    them automatically, or every "spec" and "node" lights up.
  - Literal: commands, options and paths. Capitals standing alone inside a
    literal are placeholders and get underlined.
  - Status: `warn`, `fail` and `pass` (READY) words. Leave `ok` plain: color
    marks what needs attention, and thirty green rows mean nothing.
- With `markup=True`, write `` `literal` ``, `**choice**` and `*term*` marks.
  Use `role="command"` for a usage line or option term, and `role="option"` for
  an aligned `  TERM  DESCRIPTION` row.
- Help screens use `emit_help`'s layout: a usage line, one sentence, then
  option rows with at least two spaces between a term and its description.
  Only a text with a usage line is styled. Questions and the SSH help also pass
  through `emit_help` and must stay plain.
- Long output shown from a menu goes through `page_output`. A menu calls
  `ui_keep_styles` after `require_gum`, which exports `PULSAR_COLOR=always`
  on a terminal, so output it captures and shows later keeps its styles.
- Group check rows by the object they describe, such as one section per node
  named by its hostname, instead of repeating it on every row. Count warnings
  and failures in the closing verdict.
- Add no colors beyond the roles. JSON is never styled: keep its messages,
  and let marks and human wording differ only in what people read.

## Gotchas

- Wrap the plain text first, then add styles. `textwrap` counts escape codes
  as width.
- A literal span that fits on a line never breaks inside it. A longer one wraps
  like prose.
- Capitals inside a name are literal: `PULSAR_COLOR=never`,
  `docs/OPERATIONS.md`. In usage lines and option terms, any standalone capital
  word is a metavariable: `SPEC_ID`, `SHA256`, `ORG/NAME`.
- A row with one space before its description is read as a paragraph, not an
  option row.
- A value can contain a backtick. Give JSON its own exact message and put marks
  only in the text people read; never strip marks from a string that carries a
  value. `lit` in `doctor.sh` leaves such a value unmarked.
- `gum style` strips escape codes, so the recipe frame's text stays plain; only
  its border takes the accent.
- Styles go in free text only. Shell parses Gum labels and prefixes such as
  `header\t`, and parses captured stderr (`lib.sh` strips `error: `). So error
  and warning labels are styled only on a real terminal, never for `always`.

## Verify

- Off a terminal, output must be unchanged unless you meant to change it.
  Existing tests compare that plain text.
- With `PULSAR_COLOR=always`, stripping the escape codes must give back exactly
  the plain output. With `NO_COLOR=1`, only bold and underline remain. See the
  `Styles` and `Renderer` tests in `tests/test_help.py`.
- Check 44 columns, and 32 for help screens.
- Check the pager path with `BundledGum.drive` (`tests/test_ui_confirm.py`).
  It runs on arm64 Linux and is skipped elsewhere. Never send keys into a real
  catalog menu.
- For review, render the ANSI output to HTML in a dark and a light theme. The
  terminal's own theme supplies the actual colors.
- Before committing, run the checks `AGENTS.md` requires, including the staged
  privacy check.
