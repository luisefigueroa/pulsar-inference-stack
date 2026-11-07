#!/usr/bin/env bash
# Shared Gum menu and prompt helpers for home, catalog, topology and archive
# menus. Source only.
# shellcheck shell=bash

if [ -n "${_PULSAR_SCRIPTS_UI:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
_PULSAR_SCRIPTS_UI=1

# Expect REPO_DIR from caller (lib.sh or home/wizard bootstrap).
if [ -z "${REPO_DIR:-}" ]; then
  _ui_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_DIR="$(cd "$_ui_dir/.." && pwd)"
fi

# ---------------------------------------------------------------------------
# One renderer: Gum. Every menu action is also a command.
# ---------------------------------------------------------------------------
# Menus and prompts draw only with Gum. Gum runs when stdin and stderr are
# terminals (PULSAR_FORCE_GUM=1 skips that check for tests), TERM is set and
# not dumb, and an executable is found: GUM_BIN, the vendored build on
# aarch64 Linux, or gum on PATH. Otherwise a menu entry's require_gum names
# the equivalent commands and exits 2; there is no plain-text copy of any
# menu.
#
# Color: never rely on Gum defaults (Charm pink/purple). With color, every
# call passes the full blue palette:
#   PULSAR_ACCENT (default bright blue 12) — choose cursor/header/selected
#     and confirm prompt
#   confirm selected button — blue bg 4 + bright fg 15
#   Ordinary list items: no colored backgrounds.
# NO_COLOR or PULSAR_COLOR=never passes no color flags and runs Gum with
# NO_COLOR=1, so its defaults draw no color either. page_output runs the
# command it captures with PULSAR_COLOR=always (unless never), so a page keeps
# the styles scripts/terminal_format.py gives a terminal.

PULSAR_ACCENT="${PULSAR_ACCENT:-12}"
_PULSAR_CONFIRM_SELECTED_FG="${PULSAR_CONFIRM_SELECTED_FG:-15}"
_PULSAR_CONFIRM_SELECTED_BG="${PULSAR_CONFIRM_SELECTED_BG:-4}"

pulsar_color_enabled() {
  if [ -n "${NO_COLOR:-}" ]; then
    return 1
  fi
  case "${PULSAR_COLOR:-}" in
    never|0|no|off|false) return 1 ;;
  esac
  return 0
}

# ---------------------------------------------------------------------------
# Gum discovery (interactive terminal; then GUM_BIN / vendored / system)
# ---------------------------------------------------------------------------
VENDORED_GUM="${VENDORED_GUM:-$REPO_DIR/third_party/gum/linux-arm64/gum}"
GUM_CMD=""
have_gum=0

_ui_resolve_gum() {
  GUM_CMD=""
  have_gum=0
  case "${TERM:-}" in
    dumb|"") return 0 ;;
  esac
  # Gum draws on stderr and reads keys from stdin. Without both terminals a
  # hidden prompt would wait for keystrokes, so no menu opens at all.
  if [ "${PULSAR_FORCE_GUM:-0}" != 1 ]; then
    if ! [ -t 0 ] || ! [ -t 2 ]; then
      return 0
    fi
  fi
  if [ -n "${GUM_BIN:-}" ]; then
    if [ -x "$GUM_BIN" ]; then
      GUM_CMD="$GUM_BIN"
    else
      printf 'warning: GUM_BIN is not executable: %s\n' "$GUM_BIN" >&2
    fi
  elif [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = aarch64 ] \
      && [ -x "$VENDORED_GUM" ]; then
    GUM_CMD="$VENDORED_GUM"
  elif command -v gum >/dev/null 2>&1; then
    GUM_CMD=$(command -v gum)
  fi
  [ -z "$GUM_CMD" ] || have_gum=1
}

_ui_resolve_gum

# gum_available: whether menus and prompts can draw here.
gum_available() {
  [ "$have_gum" = 1 ]
}

# elapsed_text SECONDS — a duration for an outcome line, such as "2m 5s".
elapsed_text() {
  local seconds="$1"
  if [ "$seconds" -ge 3600 ]; then printf '%dh %dm' $((seconds / 3600)) $((seconds % 3600 / 60))
  elif [ "$seconds" -ge 60 ]; then printf '%dm %ds' $((seconds / 60)) $((seconds % 60))
  else printf '%ds' "$seconds"; fi
}

# report_outcome ACTION LABEL SUBJECT STATUS ELAPSED [--preview|--recorded]
# Prints an operation's outcome worded by what its exit status means, so a
# "no" (1) and "couldn't tell" (3) read differently. Callers report
# interruptions themselves.
report_outcome() {
  PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m scripts.menu_outcome \
    --action "$1" --label "$2" --subject "$3" --status "$4" --elapsed "$5" "${@:6}"
}

# require_gum WHAT [COMMAND WHAT_IT_DOES]...
# A menu or guided flow opens only with Gum. Otherwise print the equivalent
# commands, one complete ./pulsar command per line with what it does as a
# shell comment, so any line can be pasted as it is, and exit 2 instead of
# opening a degraded copy.
require_gum() {
  ! gum_available || return 0
  local what="$1" width=0 i
  shift
  local -a steps=("$@")
  printf 'error: %s needs an interactive terminal with Gum. Run the same steps as commands:\n' "$what" >&2
  for ((i = 0; i < ${#steps[@]}; i += 2)); do [ "${#steps[$i]}" -le "$width" ] || width=${#steps[$i]}; done
  while [ $# -ge 2 ]; do
    printf '  %-*s  # %s\n' "$width" "$1" "$2" >&2
    shift 2
  done
  exit 2
}

# A menu shows output it captured on this terminal: the home status, a page,
# a preview. Its commands style that output as for a terminal
# (PULSAR_COLOR=always, scripts/terminal_format.py) unless the operator turned
# styles off; NO_COLOR still removes the colors. Call after require_gum.
ui_keep_styles() {
  [ -t 1 ] || return 0
  case "${PULSAR_COLOR:-}" in
    never|0|no|off|false) ;;
    *) export PULSAR_COLOR=always ;;
  esac
}

# Every primitive below draws with Gum only. Menu entries call require_gum
# first; this refusal only stops a prompt that would otherwise wait unseen.
_ui_require_gum() {
  ! gum_available || return 0
  printf 'error: this prompt needs an interactive terminal with Gum\n' >&2
  return 2
}

# _ui_gum ARGS... runs Gum; without color it runs with NO_COLOR=1.
_ui_gum() {
  if pulsar_color_enabled; then
    "$GUM_CMD" "$@"
  else
    NO_COLOR=1 "$GUM_CMD" "$@"
  fi
}

# Style flags: layout always; color only when color is enabled, and then the
# complete palette.
_ui_gum_choose_style_args() {
  GUM_CHOOSE_STYLE_ARGS=()
  if pulsar_color_enabled; then
    GUM_CHOOSE_STYLE_ARGS+=(
      --cursor.foreground="$PULSAR_ACCENT"
      --header.foreground="$PULSAR_ACCENT"
      --selected.foreground="$PULSAR_ACCENT"
    )
  fi
  GUM_CHOOSE_STYLE_ARGS+=(--padding "1 0")
}

_ui_gum_confirm_style_args() {
  GUM_CONFIRM_STYLE_ARGS=()
  if pulsar_color_enabled; then
    GUM_CONFIRM_STYLE_ARGS+=(
      --prompt.foreground="$PULSAR_ACCENT"
      --selected.foreground="$_PULSAR_CONFIRM_SELECTED_FG"
      --selected.background="$_PULSAR_CONFIRM_SELECTED_BG"
    )
  fi
  GUM_CONFIRM_STYLE_ARGS+=(--padding "1 0")
}

_ui_gum_style_frame_args() {
  GUM_STYLE_FRAME_ARGS=(--border rounded)
  if pulsar_color_enabled; then
    GUM_STYLE_FRAME_ARGS+=(--border-foreground "$PULSAR_ACCENT")
  fi
  GUM_STYLE_FRAME_ARGS+=(--padding "0 1" --margin "1 0")
}

# The pager draws its own border; search matches use the accent palette.
_ui_gum_pager_style_args() {
  GUM_PAGER_STYLE_ARGS=(--show-line-numbers=false)
  if pulsar_color_enabled; then
    GUM_PAGER_STYLE_ARGS+=(
      --match.foreground="$PULSAR_ACCENT"
      --match-highlight.foreground="$_PULSAR_CONFIRM_SELECTED_FG"
      --match-highlight.background="$_PULSAR_CONFIRM_SELECTED_BG"
    )
  fi
}

_ui_gum_input_style_args() {
  GUM_INPUT_STYLE_ARGS=()
  if pulsar_color_enabled; then
    GUM_INPUT_STYLE_ARGS+=(
      --prompt.foreground="$PULSAR_ACCENT"
      --header.foreground="$PULSAR_ACCENT"
      --placeholder.foreground="8"
    )
  fi
  GUM_INPUT_STYLE_ARGS+=(--padding "1 0")
}

# emit_frame
# Reads stdin and prints it in a rounded border (accent-colored with color).
emit_frame() {
  _ui_require_gum || return
  _ui_gum_style_frame_args
  _ui_gum style "${GUM_STYLE_FRAME_ARGS[@]}"
}

# emit_error
# Reads stdin and prints it in bold on stderr (red with color).
emit_error() {
  _ui_require_gum || return
  local -a style=(--bold)
  if pulsar_color_enabled; then
    style=(--foreground 1 --bold)
  fi
  echo >&2
  _ui_gum style "${style[@]}" >&2
}

# prompt_input HEADER [PLACEHOLDER]
# Prints the entered line on stdout. Returns 1 on Esc/empty input, 130 on Ctrl-C.
prompt_input() {
  local header="$1" placeholder="${2:-}" out rc
  _ui_require_gum || return
  _ui_gum_input_style_args
  set +e
  out=$(_ui_gum input \
    "${GUM_INPUT_STYLE_ARGS[@]}" \
    --header "$header" \
    --placeholder "$placeholder")
  rc=$?
  set -e
  if [ "$rc" -eq 130 ]; then
    return 130
  fi
  if [ "$rc" -ne 0 ] || [ -z "${out:-}" ]; then
    return 1
  fi
  printf '%s\n' "$out"
}

# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
# _ui_terminal_rows
# Prints the terminal's height. Gum draws on stderr while callers capture
# stdout, so the size is read from stderr; $LINES is the fallback.
_ui_terminal_rows() {
  local size rows
  size=$(stty size <&2 2>/dev/null) || size=""
  rows=${size%% *}
  [[ "$rows" =~ ^[1-9][0-9]*$ ]] || rows=${LINES:-}
  [[ "$rows" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$rows"
}

# _ui_terminal_columns
# Prints the terminal's width, read like its height; $COLUMNS is the fallback.
_ui_terminal_columns() {
  local size columns
  size=$(stty size <&2 2>/dev/null) || size=""
  columns=${size##* }
  [[ "$columns" =~ ^[1-9][0-9]*$ ]] || columns=${COLUMNS:-}
  [[ "$columns" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$columns"
}

# _ui_card_page ROWS OPTION... — how many options of a card menu one page
# shows. Gum's --height counts options, not lines, and pages in fixed blocks
# (page 2 holds options N+1 to 2N), so every block of N options must fit: in
# ROWS - 5 lines (padding and the two-line header) when one page holds them
# all, else in ROWS - 6, since Gum then adds a blank line and the page dots.
# Gum cuts a taller view from the top, padding and title first; the limits
# were measured with the bundled Gum (tests/test_ui_confirm.py checks them).
# A page must also not start with a model's later spec, whose model name would
# be on the page before: given after the gutter strip, only such an option
# starts with a space. Of the sizes that fit and keep groups whole, the one
# with the fewest pages and then the fewest options per page wins, so pages
# come out even and a last page rarely holds one option alone. When no size
# keeps every group whole, as for a model taller than a page, the largest
# size that fits wins.
_ui_card_page() {
  local rows="$1" option newlines total=0 size start end sum i fits whole pages
  local largest=1 best=0 fewest=0
  shift
  local -a heights=() later=()
  for option in "$@"; do
    newlines=${option//[!$'\n']/}
    heights+=($((${#newlines} + 1)))
    total=$((total + ${#newlines} + 1))
    if [[ "$option" == " "* ]]; then later+=(1); else later+=(0); fi
  done
  if [ "$total" -le $((rows - 5)) ]; then
    printf '%s\n' "$#"
    return 0
  fi
  for ((size = 1; size <= $#; size++)); do
    fits=1 whole=1
    for ((start = 0; start < $# && fits; start += size)); do
      sum=0 end=$((start + size < $# ? start + size : $#))
      for ((i = start; i < end; i++)); do sum=$((sum + heights[i])); done
      [ "$sum" -le $((rows - 6)) ] || fits=0
      [ "$start" -eq 0 ] || [ "${later[start]}" = 0 ] || whole=0
    done
    [ "$fits" = 1 ] || continue
    largest=$size
    [ "$whole" = 1 ] || continue
    pages=$((($# + size - 1) / size))
    if [ "$best" = 0 ] || [ "$pages" -lt "$fewest" ]; then
      best=$size fewest=$pages
    fi
  done
  printf '%s\n' "$((best > 0 ? best : largest))"
}

# choose_index HEADER OPTION...
# Prints the selected zero-based option index. Gum shows each label and
# returns its hidden index, so a label's whole row is available to its text.
# Gum maps each label to one value, so a repeated label gets a " (2)" suffix
# and stays selectable. The list is as tall as its options and the terminal
# allow, so no option waits on a second page.
# An option with several lines, a card such as a spec under its model's name
# in the catalog list, is one cursor stop. Card lines come at their screen
# columns and may carry styles; a menu with cards pages so its title stays on
# screen and each page starts with a model's name or a single-line choice
# where it can (see _ui_card_page). Single-line menus are drawn as before.
# PULSAR_CHOOSE_DEFAULT=N starts the cursor on option N, except in a menu with
# cards. Returns 1 on Esc or cancel and 130 on Ctrl-C, so a nested menu can
# step back on Esc and leave entirely on Ctrl-C.
choose_index() {
  local header="$1"
  shift
  if [ "$#" -eq 0 ]; then
    return 1
  fi
  _ui_require_gum || return
  local entries=() labels=() option label candidate suffix out rc selected rows gap input=""
  local index=1 height=$# default="${PULSAR_CHOOSE_DEFAULT:-}" cards=0
  local -a initial=() card_args=()
  local -A seen=()
  for option in "$@"; do
    [[ "$option" != *$'\n'* ]] || cards=1
  done
  for option in "$@"; do
    label=${option//$'\t'/ } gap=""
    if [ "$cards" = 1 ]; then
      # Gum draws its two-column cursor gutter before an option's first line
      # only and prints later lines as given, so a first line given at screen
      # columns gives its two leading spaces to the gutter.
      [[ "$label" != "  "* ]] || label=${label:2}
      # A card that ends a group ends with a newline, a blank line on screen;
      # a " (2)" suffix goes on its last line of text.
      [[ "$label" != *$'\n' ]] || { gap=$'\n'; label=${label%$'\n'}; }
    fi
    candidate=$label suffix=2
    while [ -n "${seen["x$candidate"]+x}" ]; do
      candidate="$label ($suffix)"
      suffix=$((suffix + 1))
    done
    seen["x$candidate"]=1
    labels+=("$candidate$gap")
    entries+=("$candidate$gap"$'\t'"$index")
    index=$((index + 1))
  done
  # Gum's own key hint names neither Esc nor what it does, so it is replaced by
  # Pulsar's under the title. PULSAR_CHOOSE_ESC says what Esc does (default back).
  local hint="↑/↓ move · enter choose · esc ${PULSAR_CHOOSE_ESC:-back}"
  if [ "$cards" = 1 ]; then
    # Options are separated by the record separator, so they may hold
    # newlines; a trailing separator would be an empty option, which Gum
    # rejects. Gum strips styles from options unless told not to.
    card_args=(--input-delimiter=$'\x1e' --no-strip-ansi)
    for option in "${entries[@]}"; do input+="${input:+$'\x1e'}$option"; done
    if rows=$(_ui_terminal_rows); then
      height=$(_ui_card_page "$rows" "${labels[@]}")
    fi
  else
    if [[ "$default" =~ ^[0-9]+$ ]] && [ "$default" -lt "$#" ]; then
      # Gum splits --selected at commas; a backslash keeps one in the label.
      initial=(--selected="${labels[$default]//,/\\,}")
    fi
    # Gum adds 5 rows around the list: padding and the two-line header.
    if rows=$(_ui_terminal_rows) && [ $((rows - 5)) -ge 3 ] && [ "$height" -gt $((rows - 5)) ]; then
      height=$((rows - 5))
    fi
  fi
  _ui_gum_choose_style_args
  set +e
  # Capture the selection on stdout only. Do NOT redirect Gum stderr: the
  # interactive TUI is drawn on stderr; silencing it makes the menu invisible.
  out=$(if [ "$cards" = 1 ]; then printf '%s' "$input"; else printf '%s\n' "${entries[@]}"; fi | _ui_gum choose \
    "${GUM_CHOOSE_STYLE_ARGS[@]}" "${initial[@]}" "${card_args[@]}" \
    --label-delimiter=$'\t' --height="$height" --no-show-help --header "$header"$'\n'"$hint")
  rc=$?
  set -e
  if [ "$rc" -eq 130 ]; then
    return 130
  fi
  if [ "$rc" -ne 0 ] || [ -z "${out:-}" ]; then
    return 1
  fi
  # Gum prints the index alone; a test double may echo the whole entry.
  selected=${out##*$'\t'}
  if ! [[ "$selected" =~ ^[0-9]+$ ]] \
      || [ "$selected" -lt 1 ] || [ "$selected" -gt "$#" ]; then
    return 1
  fi
  printf '%s\n' "$((selected - 1))"
}

# confirm MSG [yes|no]
# Returns 0 if affirmed, 1 if declined, 130 on Ctrl-C.
confirm() {
  local msg="$1"
  local default="${2:-no}" rc
  _ui_require_gum || return
  _ui_gum_confirm_style_args
  set +e
  # Leave stderr open — Gum confirm draws its TUI on stderr.
  # Gum defaults to Yes when --default is omitted; pass both values explicitly.
  if [ "$default" = yes ]; then
    _ui_gum confirm \
      "${GUM_CONFIRM_STYLE_ARGS[@]}" \
      --default=true "$msg"
  else
    _ui_gum confirm \
      "${GUM_CONFIRM_STYLE_ARGS[@]}" \
      --default=false "$msg"
  fi
  rc=$?
  set -e
  return "$rc"
}

# spin TITLE CMD...
# Shows a spinner while a short, silent step runs, then replays CMD's stdout
# (capturable by the caller) and stderr and returns CMD's status. Output is
# saved inside the spinner so no Gum version can drop it. Do not wrap commands
# that report progress themselves.
spin() {
  local title="$1" out err rc=0
  shift
  _ui_require_gum || return
  local -a style=()
  if pulsar_color_enabled; then
    style=(--spinner.foreground="$PULSAR_ACCENT" --title.foreground="$PULSAR_ACCENT")
  fi
  out=$(mktemp) || return 1
  err=$(mktemp) || { rm -f "$out"; return 1; }
  _ui_gum spin "${style[@]}" --title "$title" -- \
    bash -c 'out="$1" err="$2"; shift 2; "$@" >"$out" 2>"$err"' _ "$out" "$err" "$@" || rc=$?
  cat "$out"
  cat "$err" >&2
  rm -f "$out" "$err"
  return "$rc"
}

# ---------------------------------------------------------------------------
# Output the next menu must not bury
# ---------------------------------------------------------------------------
# Output longer than this opens in the pager; shorter output prints in place,
# where it stays readable above the next menu.
_UI_PAGER_LINES=8

# page_output TITLE COMMAND...
# Shows what a read-only command printed, its errors kept in order with its
# output. Up to _UI_PAGER_LINES lines print in place; longer output opens in
# Gum's pager (↑/↓ scroll, / searches, q or Esc returns). A nonempty TITLE
# shows a spinner while a slow command runs. COMMAND must be an executable,
# not a shell function: it runs with COLUMNS set to the pager's text width.
# Returns the command's status, or 130 after Ctrl-C while it runs or in the
# pager.
page_output() {
  local title="$1" out columns rc=0 pager_rc=0
  shift
  _ui_require_gum || return
  local -a command=("$@") page_env=()
  # The pager's border and padding take four columns.
  if columns=$(_ui_terminal_columns) && [ "$columns" -gt 4 ]; then
    page_env+=(COLUMNS=$((columns - 4)))
  fi
  # The captured output is shown on this terminal, so it keeps a terminal's
  # styles (PULSAR_COLOR=always) unless the operator turned them off.
  # NO_COLOR still removes the colors.
  if [ -t 1 ]; then
    case "${PULSAR_COLOR:-}" in
      never|0|no|off|false) ;;
      *) page_env+=(PULSAR_COLOR=always) ;;
    esac
  fi
  [ "${#page_env[@]}" -eq 0 ] || command=(env "${page_env[@]}" "$@")
  out=$(mktemp) || return 1
  if [ -n "$title" ]; then
    spin "$title" bash -c '"$@" 2>&1' _ "${command[@]}" >"$out" || rc=$?
  else
    "${command[@]}" >"$out" 2>&1 || rc=$?
  fi
  if [ "$rc" -eq 130 ]; then
    rm -f -- "$out"
    return 130
  fi
  if [ "$(wc -l <"$out")" -le "$_UI_PAGER_LINES" ]; then
    cat -- "$out"
  else
    _ui_gum_pager_style_args
    _ui_gum pager "${GUM_PAGER_STYLE_ARGS[@]}" <"$out" || pager_rc=$?
    # A pager that could not open must not hide the output.
    [ "$pager_rc" -eq 0 ] || [ "$pager_rc" -eq 130 ] || cat -- "$out"
  fi
  rm -f -- "$out"
  [ "$pager_rc" -ne 130 ] || return 130
  return "$rc"
}

# wait_continue [HEADER]
# Holds the screen until the operator chooses Continue, so an operation's
# output and outcome stay readable before a menu redraws. Esc also continues;
# Ctrl-C returns 130.
wait_continue() {
  local rc=0
  choose_index "${1:-Review the output above}" "Continue" >/dev/null || rc=$?
  [ "$rc" -ne 130 ] || return 130
  return 0
}

# confirm_or_review QUESTION ACTION_LABEL PREVIEW_COMMAND...
# Prints QUESTION, then offers "Yes, <action label>", "Show full preview" and
# "No, change nothing", with No selected first. Gum cuts a long header, so the
# question prints above the choice. Show full preview runs PREVIEW_COMMAND
# through page_output and asks again. Returns 0 only for yes, 1 for no or Esc,
# and 130 after Ctrl-C at the choice or in the pager.
confirm_or_review() {
  local question="$1" action="$2" choice rc
  shift 2
  _ui_require_gum || return
  printf '\n%s\n' "$question" | python3 "$REPO_DIR/scripts/terminal_format.py"
  while true; do
    choice=$(PULSAR_CHOOSE_DEFAULT=2 choose_index "$action" "Yes, ${action,,}" "Show full preview" \
      "No, change nothing") || return
    case "$choice" in
      0) return 0 ;;
      1) page_output "" "$@" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      *) return 1 ;;
    esac
  done
}
