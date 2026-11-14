#!/usr/bin/env bash
# Check the cluster now (Cluster menu): runs discovery, the membership check,
# the SSH identity check, diagnostics with every node's prerequisites, the
# service inventory and the storage budget in turn, each under a spinner
# naming it, then shows one report built from their JSON
# (scripts/cluster_overview.py) through the pager when it is long. Discovery
# saves the changes noticed; the report's findings are saved beside them, so
# home and the Cluster menu show them later without a probe. Nothing runs
# until the operator chooses it. The sources only read the nodes; model budget
# takes the shared storage lock, so it runs last.
#
# Under the report: Review what changed… when discovery noticed a change (the
# fix this menu can run; it returns to the Cluster menu), Show the report
# again, Check again and Back.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "the cluster check" \
  "./pulsar topology detect" "discover the machines and save the changes noticed" \
  "./pulsar topology check" "check saved nodes and fabric" \
  "./pulsar ssh-trust check" "check the nodes' recorded SSH identities" \
  "./pulsar doctor" "diagnostics with every node's prerequisites" \
  "./pulsar inventory" "see what runs on each node" \
  "./pulsar model budget" "disk headroom on every node"
ui_keep_styles

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-cluster-check.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
STEPS=6

# source_json STEP NAME TITLE COMMAND...: one source's JSON, errors and exit status.
source_json() {
  local step="$1" name="$2" title="$3" rc=0
  shift 3
  # Gum draws the spinner on stderr, so only the command's own stderr is kept.
  spin "$title ($step/$STEPS)…" bash -c 'err="$1"; shift; "$@" 2>"$err"' _ "$WORK/$name.err" \
    "$REPO_DIR/pulsar" "$@" --json >"$WORK/$name.json" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  printf '%s\n' "$rc" >"$WORK/$name.rc"
}

# save_check: the report's findings, kept beside the changes discovery saved.
# Tests save only where PULSAR_CLUSTER_CHANGES_FILE points, as discovery does.
# Saving never changes what the operator sees, except for one warning.
save_check() {
  [ "${PULSAR_SELFTEST:-0}" != 1 ] || [ -n "${PULSAR_CLUSTER_CHANGES_FILE:-}" ] || return 0
  python3 "$REPO_DIR/scripts/cluster_overview.py" "$WORK" --check-record 2>/dev/null \
    | python3 -m scripts.saved_observations save-cluster-check --repo-root "$REPO_DIR" || true
}

STARTED="" FINISHED=""
observe() {
  STARTED=$(date +%s)
  source_json 1 discovery "Looking for the machines" topology detect || return
  source_json 2 topology "Checking nodes and fabric" topology check || return
  source_json 3 identities "Checking SSH identities" ssh-trust check || return
  source_json 4 diagnostics "Checking every node's prerequisites" doctor || return
  source_json 5 inventory "Listing services and memory" inventory || return
  source_json 6 budget "Reading storage budgets" model budget || return
  FINISHED=$(date +%s)
  save_check
  show
}

show() {
  # The sources' failures show in the report; a failure to render it is a
  # Stack defect, so it is held on screen before the menu redraws over it.
  local rc=0
  page_output "" python3 "$REPO_DIR/scripts/cluster_overview.py" "$WORK" --started "$STARTED" \
    --finished "$FINISHED" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  if [ "$rc" -ne 0 ]; then
    printf '\n✗ The cluster check could not be displayed; review the error above.\n  exit %s\n' "$rc"
    wait_continue "Cluster check error" || return
    return "$rc"
  fi
}

# changed: whether this check's discovery noticed a change to review.
changed() {
  python3 -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: raise SystemExit(1)
raise SystemExit(0 if (d.get("changes_noticed") or {}).get("result") in ("changed", "not-checked") else 1)' \
    "$WORK/discovery.json"
}

observe || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; }
while true; do
  labels=() actions=()
  if changed; then labels+=("Review what changed…"); actions+=(review); fi
  labels+=("Show the report again" "Check again" "Back"); actions+=(show check back)
  choice=$(choose_index "Cluster check" "${labels[@]}") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  case "${actions[$choice]}" in
    review)
      # The review's Check the cluster now (exit 10) checks again here;
      # anything else returns to the Cluster menu, which shows what changed.
      rc=0
      _PULSAR_REVIEW_FROM_CHECK=1 "$REPO_DIR/scripts/cluster-review.sh" || rc=$?
      [ "$rc" -ne 130 ] || exit 130
      [ "$rc" -eq 10 ] || exit 0
      observe || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; } ;;
    show) show || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; } ;;
    check) observe || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit "$rc"; } ;;
    *) exit 0 ;;
  esac
done
