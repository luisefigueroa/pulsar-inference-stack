#!/usr/bin/env bash
# Bounded, foreground serving through the normal spec/preparation contracts.
set -euo pipefail
SCRIPT_NAME=guarded-serving
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
spec="" spec_id="" output="" approved=0
declare -a admission=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --spec-file) spec="${2:?spec required}"; shift ;;
    --spec-id) spec_id="${2:?reviewed spec ID required}"; shift ;;
    --output-dir) output="${2:?fresh output directory required}"; shift ;;
    --memory-estimate-file|--memory-estimate-id) admission+=("$1" "${2:?value required}"); shift ;;
    --accept-memory-warn) admission+=("$1") ;;
    --yes) approved=1 ;;
    --help|-h)
      cat <<'HELP'
usage: pulsar guarded run --spec-file FILE --spec-id SHA256
       --output-dir NEW_DIR --yes [--json]
       [--memory-estimate-file FILE --memory-estimate-id ID]
       [--accept-memory-warn]

Foreground lease owner. No pulls, replacement or acquisition.
Stop through guarded stop with the exact run ID.
HELP
      exit 0 ;;
    *) echo "guarded serving: unsupported argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$approved" = 1 ] && [ -n "$spec$spec_id$output" ] && [ -n "$spec" ] && [ -n "$spec_id" ] && [ -n "$output" ] || { echo 'explicit reviewed inputs and --yes required' >&2; exit 2; }
output=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).absolute())' "$output")
python3 -m serving_guard.controller initialize --spec-file "$spec" --spec-id "$spec_id" --output "$output"
. "$repo/scripts/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
acquire_model_library_lifecycle_lock shared
acquire_model_library_hot_lock shared
# Normal prerequisites and full prepared-file checks remain authoritative.
PULSAR_LAUNCH_PLAN_OUT="$output/plan.json" bash "$repo/scripts/up.sh" "$spec_id" \
  --spec-file "$output/spec.json" --dry-run "${admission[@]}" \
  >"$output/prerequisites.stdout" 2>"$output/prerequisites.stderr"
load_cluster_topology || die 'confirmed topology required'
python3 -m serving_guard.controller activate --output "$output" --owner "$$"
mapfile -t binding < <(python3 - "$output/active-plan.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]));print(p['topology_id']);print(len(p['ranks']))
for row in p['ranks']: print(row['node_id'])
PY
)
[ "$CLUSTER_TOPOLOGY_ID" = "${binding[0]}" ] && [ "$CLUSTER_TOPOLOGY_COUNT" -ge "${binding[1]}" ] || die 'guard requires unchanged complete confirmed membership'
serving_ranks="${binding[1]}"
for ((rank=0;rank<serving_ranks;rank++)); do
  [ "${CLUSTER_NODE_IDS[$rank]}" = "${binding[$((rank+2))]}" ] || die 'guarded rank placement differs'
done
batch_pid="" finalized=0
phase() {
  local action="$1" rank rc=0
  local -a MODEL_NODE_COMMAND=() options=()
  for ((rank=0;rank<serving_ranks;rank++)); do
    model_node_command "$rank" || return $?
    python3 -m serving_guard.controller task --output "$output" --phase "$action" --rank "$rank" -- \
      python3 -m model_library.verification_process --owner "$$" -- "${MODEL_NODE_COMMAND[@]}" || return $?
  done
  python3 -m serving_guard.controller tasks --output "$output" --phase "$action" || return $?
  [ "$action" != cleanup ] || options+=(--keep-going)
  setsid python3 -m model_library.verification_process batch --tasks "$output/$action/tasks.json" \
    --directory "$output/$action" --jobs "$serving_ranks" "${options[@]}" &
  batch_pid=$!
  wait "$batch_pid" || rc=$?
  batch_pid=""
  [ "$rc" = 0 ] || return "$rc"
  python3 -m serving_guard.controller check --output "$output" --phase "$action"
}
cancel_batch() {
  if [ -n "$batch_pid" ]; then
    kill -TERM -- "-$batch_pid" 2>/dev/null || true
    wait "$batch_pid" 2>/dev/null || true
    batch_pid=""
  fi
}
finish() {
  [ "$finalized" = 0 ] || return 0
  finalized=1
  trap '' INT TERM HUP
  cancel_batch
  phase cleanup || true
  python3 -m serving_guard.controller finish --output "$output"
}
stop_signal() {
  cancel_batch
  if [ -f "$output/stop-request.json" ]; then
    finish
    exit $?
  fi
  finish || true
  exit 143
}
trap 'finish || true' EXIT
trap stop_signal INT TERM HUP
phase preflight
persist_launch_plan_file "$output/active-plan.json"
phase execute
finish
