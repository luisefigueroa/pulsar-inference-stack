#!/usr/bin/env bash
# Cluster overview (home menu): runs topology check, inventory and model budget
# in turn, each under a spinner naming it, then shows one screen built from
# their JSON (scripts/cluster_overview.py) through the pager when it is long.
# Nothing runs until the operator chooses it. Show again re-reads the same
# results, Refresh runs all three again and Back returns home. The sources are
# read-only; model budget takes the shared storage lock, so it runs last.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "the cluster overview" "pulsar topology check | inventory | model budget"
ui_keep_styles

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-overview.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT

# source_json STEP NAME TITLE COMMAND...: one source's JSON, errors and exit status.
source_json() {
  local step="$1" name="$2" title="$3" rc=0
  shift 3
  # Gum draws the spinner on stderr, so only the command's own stderr is kept.
  spin "$title ($step/3)…" bash -c 'err="$1"; shift; "$@" 2>"$err"' _ "$WORK/$name.err" \
    "$REPO_DIR/pulsar" "$@" --json >"$WORK/$name.json" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  printf '%s\n' "$rc" >"$WORK/$name.rc"
}

STARTED="" FINISHED=""
observe() {
  STARTED=$(date +%s)
  source_json 1 topology "Checking nodes and fabric" topology check || return
  source_json 2 inventory "Listing services and memory" inventory || return
  source_json 3 budget "Reading storage budgets" model budget || return
  FINISHED=$(date +%s)
  show
}

show() {
  # The sources' failures show in the overview; a failure to render it is a
  # Stack defect, so it is held on screen before home redraws over it.
  local rc=0
  page_output "" python3 "$REPO_DIR/scripts/cluster_overview.py" "$WORK" --started "$STARTED" \
    --finished "$FINISHED" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  if [ "$rc" -ne 0 ]; then
    printf '\n✗ The cluster overview could not be displayed; review the error above.\n  exit %s\n' "$rc"
    wait_continue "Cluster overview error" || return
    return "$rc"
  fi
}

observe || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; }
while true; do
  choice=$(choose_index "Cluster overview" "Show again" "Refresh (checks nodes now)" "Back") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  case "$choice" in
    0) show || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; } ;;
    1) observe || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; } ;;
    *) exit 0 ;;
  esac
done
