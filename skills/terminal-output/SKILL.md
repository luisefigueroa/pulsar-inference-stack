---
name: terminal-output
description: Word, build or change what a Pulsar operator reads in a terminal (status words, menu frames, help screens, check lines, previews and pages) with plain words explained in place, the shared TerminalWriter styles, narrow-width layout and unchanged plain output. Use when adding, rewording or editing human output, menus or help in the public stack.
---

# Terminal output

Use this skill in the public inference stack for any text a person reads in a
terminal. The repository's `AGENTS.md` sets the content rules: lead with the
action, affected object and blocker, use the
[glossary](../../docs/OPERATIONS.md#terms), and keep JSON separate. This skill
covers how that text is worded, laid out and styled. What operators see is described
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

## Words

The rules follow the usability heuristics
[NN/g #2, #6 and #10](https://www.nngroup.com/articles/ten-usability-heuristics/):
speak the reader's language, don't make them remember an explanation from
another screen, and put help where it is needed. The decision and its reasons
are under [Plain status words](../../docs/DECISIONS.md#plain-status-words-explained-in-place).

- Explain a status word in this order: a word that explains itself, then a
  short hint where the word appears, then a row in the legend. The legend is
  the reference, not the fix for an unclear word.
- Name what the reader can see and do, not the system's internals. Write `not
  checked yet`, not `never checked` or `unknown: never checked`, and `every
  serving node`, not `every rank`, in status lines. Use the
  [glossary](../../docs/OPERATIONS.md#terms) words; define a new noun there.
- Keep "unknown" for a check that could not observe something. A missing
  saved check is `not checked yet`; neither one means absent.
- Hints go on the value's own line, after a semicolon, and name the choice
  that resolves it, as `Running?` does: `not checked here; choose Live
  status`. Show a hint only where that choice is offered, and never give one
  a frame line of its own: a short terminal drops frame lines, and a hint
  that wraps at 44 columns should still read as part of its value.
- Every status word the catalog can print has a row under
  [Catalog labels](../../docs/OPERATIONS.md#catalog-labels), written as
  `Field: words` with what it means and what to do. `pulsar help labels`
  shows that table, and `tests/test_help.py` fails when a word has no row.
- A table's cells use words when they fit; short codes are only a narrow-width
  fallback, and the key beside the table explains just the words or codes on
  screen. Where a name must be cut to fit, cut it where names do not differ and
  keep the end and any commit, so rows stay distinct. The Model files page shows
  both.
- Maintainer metadata says so where it is shown: State is a maintainer label,
  not a test result.
- Change words only in human output. JSON fields and values, container labels
  and persisted records keep their names (see `AGENTS.md`).

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
- `gum style` strips escape codes, so the recipe frame's text stays plain; only
  its border takes the accent.
- `spin` draws on stderr. Capture a spun command's stderr inside the command
  (`spin TITLE bash -c '"$@" 2>FILE' _ CMD...`), never `spin`'s own, or the
  spinner disappears and its frames land in the captured error.
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
- For new or reworded status words, ask someone who has not used Pulsar to
  read the screen and say what each word means and what they would do next.
  Reword what they cannot answer.
- Check the pager path with `BundledGum.drive` (`tests/test_ui_confirm.py`).
  It runs on arm64 Linux and is skipped elsewhere. Never send keys into a real
  catalog menu.
- For review, render the ANSI output to HTML in a dark and a light theme. The
  terminal's own theme supplies the actual colors.
- Before committing, run the checks `AGENTS.md` requires, including the staged
  privacy check.
