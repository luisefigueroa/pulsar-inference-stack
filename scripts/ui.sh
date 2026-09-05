#!/usr/bin/env bash
# Shared Gum / plain-menu UI helpers for home and wizard. Source only.
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
# Color / UI mode policy
# ---------------------------------------------------------------------------
# Two modes only (deterministic — never run Gum with empty style flags):
#
#   1) Plain Bash menus (uncolored): GUM=0, or NO_COLOR set, or
#      PULSAR_COLOR=never, or TERM=dumb (or empty TERM). Same path as GUM=0.
#      Fake/real Gum is never invoked; no Charm pink/purple fallback possible.
#
#   2) Color-enabled Gum: only when Gum is available AND color is allowed.
#      Always pass full blue palette overrides (never rely on Gum defaults):
#        PULSAR_ACCENT (default bright blue 12) — choose cursor/header/selected
#          and confirm prompt
#        confirm selected button — blue bg 4 + bright fg 15
#      Ordinary list items: no colored backgrounds.
#
# Do not emit raw ANSI when Gum is unavailable.

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
  case "${TERM:-}" in
    dumb|"") return 1 ;;
  esac
  return 0
}

# ---------------------------------------------------------------------------
# Gum discovery (GUM=0 / no-color → plain; else GUM_BIN / vendored / system)
# ---------------------------------------------------------------------------
VENDORED_GUM="${VENDORED_GUM:-$REPO_DIR/third_party/gum/linux-arm64/gum}"
GUM_CMD=""
have_gum=0

_ui_resolve_gum() {
  GUM_CMD=""
  have_gum=0
  if [ "${GUM:-1}" = 0 ]; then
    return 0
  fi
  # Force plain uncolored menus — never call Gum without blue overrides
  # (empty style arrays would fall back to Charm pink/purple defaults).
  if ! pulsar_color_enabled; then
    return 0
  fi
  # Gum needs a real controlling interaction path. When stdin or stderr is not
  # a terminal, use the EOF-safe plain menu instead of leaving a hidden TUI
  # waiting for keystrokes. PULSAR_FORCE_GUM is a test/debug escape hatch.
  if [ "${PULSAR_FORCE_GUM:-0}" != 1 ]; then
    if ! [ -t 0 ] || ! [ -t 2 ]; then
      return 0
    fi
  fi
  if [ -n "${GUM_BIN:-}" ]; then
    if [ -x "$GUM_BIN" ]; then
      GUM_CMD="$GUM_BIN"
    else
      if declare -F warn >/dev/null 2>&1; then
        warn "GUM_BIN is not executable: $GUM_BIN"
      fi
    fi
  elif [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = aarch64 ] \
      && [ -x "$VENDORED_GUM" ]; then
    GUM_CMD="$VENDORED_GUM"
  elif command -v gum >/dev/null 2>&1; then
    GUM_CMD=$(command -v gum)
  fi
  [ -n "$GUM_CMD" ] && have_gum=1
}

_ui_resolve_gum

# Style flags always applied on the Gum path (have_gum=1 implies color enabled).
# Never leave these empty while invoking Gum.
_ui_gum_choose_style_args() {
  GUM_CHOOSE_STYLE_ARGS=(
    --cursor.foreground="$PULSAR_ACCENT"
    --header.foreground="$PULSAR_ACCENT"
    --selected.foreground="$PULSAR_ACCENT"
  )
}

_ui_gum_confirm_style_args() {
  GUM_CONFIRM_STYLE_ARGS=(
    --prompt.foreground="$PULSAR_ACCENT"
    --selected.foreground="$_PULSAR_CONFIRM_SELECTED_FG"
    --selected.background="$_PULSAR_CONFIRM_SELECTED_BG"
  )
}

# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
# choose HEADER OPTION...
# Prints selected option on stdout. Returns 1 on cancel/ESC/EOF/empty.
choose() {
  local header="$1"
  shift
  if [ "$#" -eq 0 ]; then
    return 1
  fi
  if [ "$have_gum" = 1 ]; then
    local out rc
    _ui_gum_choose_style_args
    set +e
    # Capture selection on stdout only. Do NOT redirect Gum stderr — the
    # interactive TUI is drawn on stderr; silencing it makes the menu invisible.
    out=$(printf '%s\n' "$@" | "$GUM_CMD" choose \
      "${GUM_CHOOSE_STYLE_ARGS[@]}" \
      --header "$header")
    rc=$?
    set -e
    if [ "$rc" -ne 0 ] || [ -z "${out:-}" ]; then
      return 1
    fi
    printf '%s\n' "$out"
    return 0
  fi

  # Plain Bash menu (uncolored). Numbered select; EOF / empty cancel → return 1.
  echo "$header" >&2
  local PS3="Select number: "
  local opt
  # shellcheck disable=SC2034 # opt used by select
  select opt in "$@"; do
    if [ -n "${opt:-}" ]; then
      printf '%s\n' "$opt"
      return 0
    fi
    if [ -z "${REPLY:-}" ]; then
      return 1
    fi
    echo "Invalid selection; enter a listed number (or EOF to cancel)." >&2
  done
  return 1
}

# choose_index HEADER OPTION...
# Prints the selected zero-based option index. Unlike choose(), display text is
# not used as identity, so duplicate or truncated labels remain selectable.
choose_index() {
  local header="$1"
  shift
  if [ "$#" -eq 0 ]; then
    return 1
  fi
  if [ "$have_gum" = 1 ]; then
    local indexed=() option out rc selected index=1
    for option in "$@"; do
      indexed+=("${index}"$'\t'"${option}")
      index=$((index + 1))
    done
    _ui_gum_choose_style_args
    set +e
    out=$(printf '%s\n' "${indexed[@]}" | "$GUM_CMD" choose \
      "${GUM_CHOOSE_STYLE_ARGS[@]}" \
      --header "$header")
    rc=$?
    set -e
    if [ "$rc" -ne 0 ] || [[ "${out:-}" != *$'\t'* ]]; then
      return 1
    fi
    selected=${out%%$'\t'*}
    if ! [[ "$selected" =~ ^[0-9]+$ ]] \
        || [ "$selected" -lt 1 ] || [ "$selected" -gt "$#" ]; then
      return 1
    fi
    printf '%s\n' "$((selected - 1))"
    return 0
  fi

  echo "$header" >&2
  local PS3="Select number: "
  local opt
  # shellcheck disable=SC2034 # opt is populated and checked by select
  select opt in "$@"; do
    if [ -n "${opt:-}" ] && [[ "${REPLY:-}" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$((10#$REPLY - 1))"
      return 0
    fi
    if [ -z "${REPLY:-}" ]; then
      return 1
    fi
    echo "Invalid selection; enter a listed number (or EOF to cancel)." >&2
  done
  return 1
}

# confirm MSG [yes|no]
# Returns 0 if affirmed, 1 if declined/cancel.
confirm() {
  local msg="$1"
  local default="${2:-no}"
  if [ "$have_gum" = 1 ]; then
    _ui_gum_confirm_style_args
    set +e
    # Leave stderr open — Gum confirm draws its TUI on stderr.
    if [ "$default" = yes ]; then
      "$GUM_CMD" confirm \
        "${GUM_CONFIRM_STYLE_ARGS[@]}" \
        --default=true "$msg"
    else
      "$GUM_CMD" confirm \
        "${GUM_CONFIRM_STYLE_ARGS[@]}" \
        "$msg"
    fi
    local rc=$?
    set -e
    return "$rc"
  fi

  local prompt="[y/N]"
  [ "$default" = yes ] && prompt="[Y/n]"
  local a=""
  if ! read -r -p "$msg $prompt " a; then
    return 1
  fi
  case "$a" in
    y|Y|yes|YES) return 0 ;;
    n|N|no|NO) return 1 ;;
    "") [ "$default" = yes ] ;;
    *) return 1 ;;
  esac
}

# spin TITLE CMD...
spin() {
  local title="$1"
  shift
  if [ "$have_gum" = 1 ]; then
    "$GUM_CMD" spin --title "$title" --show-output -- "$@"
  else
    if declare -F log >/dev/null 2>&1; then
      log "$title"
    else
      printf '%s\n' "$title" >&2
    fi
    "$@"
  fi
}
