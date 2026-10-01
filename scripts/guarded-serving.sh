#!/usr/bin/env bash
# Bounded, foreground serving through the normal spec/preparation contracts.
set -euo pipefail
SCRIPT_NAME=guarded-serving
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
spec="" spec_id="" output="" approved=0 placement_nodes=""
declare -a admission=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --spec-file) spec="${2:?spec required}"; shift ;;
    --spec-id) spec_id="${2:?reviewed spec ID required}"; shift ;;
    --output-dir) output="${2:?fresh output directory required}"; shift ;;
    --memory-estimate-file|--memory-estimate-id) admission+=("$1" "${2:?value required}"); shift ;;
    --placement-nodes) placement_nodes="${2:?ordered node list required}"; admission+=("$1" "$placement_nodes"); shift ;;
    --accept-memory-warn) admission+=("$1") ;;
    --yes) approved=1 ;;
    --help|-h)
      cat <<'HELP'
usage: pulsar guarded run --spec-file FILE --spec-id SHA256
       --output-dir NEW_DIR --yes [--json]
       [--memory-estimate-file FILE --memory-estimate-id ID]
       [--accept-memory-warn] [--placement-nodes NODE_ID,NODE_ID]

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
prerequisite_rc=0
PULSAR_LAUNCH_PLAN_OUT="$output/plan.json" bash "$repo/scripts/up.sh" "$spec_id" \
  --spec-file "$output/spec.json" --dry-run "${admission[@]}" \
  >"$output/prerequisites.stdout" 2>"$output/prerequisites.stderr" || prerequisite_rc=$?
if [ "$prerequisite_rc" != 0 ]; then
  python3 -m serving_guard.controller fail-prerequisites --output "$output" --returncode "$prerequisite_rc"
  exit "$prerequisite_rc"
fi
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
serving_indexes=()
expected_nodes=()
if [ -n "$placement_nodes" ]; then
  IFS=, read -r -a selectors <<<"$placement_nodes"
  [ "${#selectors[@]}" = "$serving_ranks" ] || die 'guarded placement count differs'
  for selector in "${selectors[@]}"; do
    node_id=$(resolve_single_node_placement "$selector" >/dev/null && printf '%s' "$SINGLE_NODE_ID") || die 'guarded placement is not confirmed'
    expected_nodes+=("$node_id")
  done
else
  expected_nodes=("${CLUSTER_NODE_IDS[@]:0:$serving_ranks}")
fi
for ((rank=0;rank<serving_ranks;rank++)); do
  [ "${expected_nodes[$rank]}" = "${binding[$((rank+2))]}" ] || die 'guarded rank placement differs'
  index=$(model_physical_rank "${binding[$((rank+2))]}") || die 'guarded rank placement differs from confirmed membership'
  serving_indexes+=("$index")
done
batch_pid="" finalized=0
phase() {
  local action="$1" rank rc=0
  local -a MODEL_NODE_COMMAND=() options=() ready_args=()
  for ((rank=0;rank<serving_ranks;rank++)); do
    model_node_command "${serving_indexes[$rank]}" || return $?
    ready_args=()
    [ "${serving_indexes[$rank]}" != 0 ] || ready_args=(--local-head)
    python3 -m serving_guard.controller task --output "$output" --phase "$action" --rank "$rank" "${ready_args[@]}" -- \
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
  python3 -m serving_guard.controller finish --output "$output" --state-root "$PULSAR_MODEL_LIBRARY_DIR"
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
