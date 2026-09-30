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
# NO_COLOR=1, so its defaults draw no color either.

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

# require_gum WHAT COMMANDS
# A menu or guided flow opens only with Gum. Otherwise print one line naming
# the equivalent commands and exit 2 instead of opening a degraded copy.
require_gum() {
  ! gum_available || return 0
  printf 'error: %s needs an interactive terminal with Gum; use: %s\n' "$1" "$2" >&2
  exit 2
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
# Prints the entered line on stdout. Returns 1 on cancel or empty input.
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
  if [ "$rc" -ne 0 ] || [ -z "${out:-}" ]; then
    return 1
  fi
  printf '%s\n' "$out"
}

# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
# choose_index HEADER OPTION...
# Prints the selected zero-based option index. Display text is not used as
# identity, so duplicate or truncated labels remain selectable.
# PULSAR_CHOOSE_DEFAULT=N starts the cursor on option N. Returns 1 on Esc or
# cancel and 130 on Ctrl-C, so a nested menu can step back on Esc and leave
# entirely on Ctrl-C.
choose_index() {
  local header="$1"
  shift
  if [ "$#" -eq 0 ]; then
    return 1
  fi
  _ui_require_gum || return
  local indexed=() option out rc selected index=1 default="${PULSAR_CHOOSE_DEFAULT:-}"
  local -a initial=()
  for option in "$@"; do
    indexed+=("${index}"$'\t'"${option}")
    index=$((index + 1))
  done
  if [[ "$default" =~ ^[0-9]+$ ]] && [ "$default" -lt "$#" ]; then
    initial=(--selected="${indexed[$default]}")
  fi
  _ui_gum_choose_style_args
  set +e
  # Capture the selection on stdout only. Do NOT redirect Gum stderr: the
  # interactive TUI is drawn on stderr; silencing it makes the menu invisible.
  out=$(printf '%s\n' "${indexed[@]}" | _ui_gum choose \
    "${GUM_CHOOSE_STYLE_ARGS[@]}" "${initial[@]}" \
    --header "$header")
  rc=$?
  set -e
  if [ "$rc" -eq 130 ]; then
    return 130
  fi
  if [ "$rc" -ne 0 ] || [[ "${out:-}" != *$'\t'* ]]; then
    return 1
  fi
  selected=${out%%$'\t'*}
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
  if [ "$default" = yes ]; then
    _ui_gum confirm \
      "${GUM_CONFIRM_STYLE_ARGS[@]}" \
      --default=true "$msg"
  else
    _ui_gum confirm \
      "${GUM_CONFIRM_STYLE_ARGS[@]}" \
      "$msg"
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
