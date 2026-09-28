#!/usr/bin/env bash
# Verify prepared files on every exact serving rank; no acquisition or fallback.
# Exit: 0 pass · 1 files are not ready · 3 the check could not run.
set -euo pipefail
SCRIPT_NAME=check-weights
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
check_exit_convention
[ -n "${1:-}" ] || die "spec id required" 3
NAME="$1"; shift
NODE_SELECTOR="" JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --node) [ -n "${2:-}" ] || die "--node requires a node" 3; NODE_SELECTOR="$2"; shift ;;
    --spec-file) [ -n "${2:-}" ] || die "--spec-file requires a file" 3; export PULSAR_SPEC_FILE="$2"; shift ;;
    --json) JSON=1 ;;
    --full) export PULSAR_OBSERVE_FULL=1 ;;
    *) die "unknown argument: $1" 3 ;;
  esac
  shift
done
acquire_model_library_lifecycle_lock shared
acquire_model_library_hot_lock shared
load_conf "$NAME"
if [ "$NODES" = 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" || die "placement is not confirmed" 3
  load_cluster_topology || die "confirmed topology required" 3
else
  [ -z "$NODE_SELECTOR" ] || die "--node only applies to one-node specs" 3
  require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || die "required topology unavailable" 3
fi
# model-library info: 0 ready, 1 no ready copy, 2 a copy failed verification,
# 255 a serving node is unreachable over SSH.
rc=0
info=$(library_hot_info_for_profile "$NAME") || rc=$?
case "$rc" in
  0) ;;
  1|2)
    printf 'FAIL  model files\n      Prepared files are missing or changed on at least one serving rank.\n'
    check_result 1 ;;
  255) die "a serving node is unreachable, so model files could not be checked" 3 ;;
  *) die "model-file inspection failed (exit $rc)" 3 ;;
esac
if ! output=$(printf '%s' "$info" | python3 "$REPO_DIR/scripts/runtime_binding.py" prepared-shell \
    --spec "$CONF_PATH" --topology-id "${CLUSTER_TOPOLOGY_ID:-${SINGLE_NODE_TOPOLOGY_ID:-}}"); then
  printf 'FAIL  model files\n      Prepared model records do not match the selected spec; prepare it again.\n'
  check_result 1
fi
eval "$output"
PULSAR_PREPARED_SET_JSON="$info"
if [ "$JSON" = 1 ]; then printf '%s\n' "$PULSAR_PREPARED_SET_JSON"; else
  printf 'PASS  model files\n      Exact snapshot verified on all %s serving ranks.\n' "$NODES"
fi
