#!/usr/bin/env bash
# Cluster… (home menu): the cluster's one place. A frame from saved records
# only (scripts/cluster_status.py frame): the nodes, when they last answered,
# and the changes noticed with their age. Opening it probes nothing. Choices:
#   Check the cluster now              scripts/cluster-overview.sh: one report
#   Review what changed…               scripts/cluster-review.sh; shown when the
#                                      saved changes hold something to review
#   Settings…                          archive location, and where an API key is set
#   Start over: forget this cluster…   ./pulsar topology forget: its plan, then a
#                                      question that starts on No
# Each choice runs as a child and returns here; Back returns home. After a
# forget, or when membership is no longer valid, the menu returns home, which
# then offers setup again.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
# The locations its Python helpers read, as ./pulsar commands read them (.env included).
# shellcheck source=menu-settings.sh
. "$REPO_DIR/scripts/menu-settings.sh"
require_gum "the Cluster menu" \
  "./pulsar topology show" "saved membership; no probes" \
  "./pulsar topology detect" "discover the machines and save the changes noticed" \
  "./pulsar topology check" "check saved nodes and fabric" \
  "./pulsar ssh-trust check" "check the nodes' recorded SSH identities" \
  "./pulsar doctor" "diagnostics with every node's prerequisites" \
  "./pulsar topology configure" "save a change to the cluster; asks first" \
  "./pulsar configure archive-root" "archive location" \
  "./pulsar topology forget" "start over: forget this cluster; asks first"
ui_keep_styles

# with_effect LABEL EFFECT: "LABEL (EFFECT)" where the whole choice fits the
# menu's width, else LABEL, so Gum never cuts the effect in half.
with_effect() {
  local columns text="$1 ($2)"
  columns=$(_ui_terminal_columns) || columns=84
  if [ "${#text}" -le "$((columns - 2))" ]; then printf '%s\n' "$text"; else printf '%s\n' "$1"; fi
}

# status COMMAND...: a cluster_status screen fitted to the terminal; the frame's
# border and padding take four more columns (FRAME_MARGIN). The width is read
# once, before any capture: a captured stderr hides the terminal.
COLUMNS_SEEN=$(_ui_terminal_columns) || COLUMNS_SEEN=""
status() {
  local width=""
  [ -z "$COLUMNS_SEEN" ] || width=$((COLUMNS_SEEN > 36 ? COLUMNS_SEEN - ${FRAME_MARGIN:-0} : 32))
  if [ -n "$width" ]; then
    python3 -m scripts.cluster_status "$@" --repo-root "$REPO_DIR" --width "$width"
  else
    python3 -m scripts.cluster_status "$@" --repo-root "$REPO_DIR"
  fi
}

settings_menu() {
  local choice rc
  while true; do
    choice=$(choose_index "Settings" "Archive location…" "API key" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "$choice" in
      0) "$REPO_DIR/pulsar" configure archive-root menu || rc=$? ;;
      1) echo; status api-key || rc=$? ;;
      *) return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
  done
}

shown=""
while true; do
  # Membership forgotten or no longer readable: home offers setup instead.
  output=$(FRAME_MARGIN=4 status frame 2>/dev/null) || exit 0
  header=${output%%$'\n'*}
  key=${header#key$'\t'}; review=${key#*$'\t'}; key=${key%%$'\t'*}
  if [ "$key" != "$shown" ]; then
    # The frame is drawn on entry and again only when a record changed, so it
    # never buries what a choice printed.
    printf '%s\n' "${output#*$'\n'}" | emit_frame
    shown=$key
  fi
  labels=("Check the cluster now") actions=(check)
  if [ "$review" = review ]; then labels+=("Review what changed…"); actions+=(review); fi
  labels+=("$(with_effect "Settings…" "archive location, API key")" "Start over: forget this cluster…" "Back")
  actions+=(settings forget back)
  choice=$(choose_index "Cluster" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  rc=0
  case "${actions[$choice]}" in
    check) "$REPO_DIR/scripts/cluster-overview.sh" || rc=$? ;;
    review) "$REPO_DIR/scripts/cluster-review.sh" || rc=$? ;;
    settings) settings_menu || rc=$? ;;
    forget) "$REPO_DIR/pulsar" topology forget || rc=$? ;;
    *) exit 0 ;;
  esac
  [ "$rc" -ne 130 ] || exit 130
done
