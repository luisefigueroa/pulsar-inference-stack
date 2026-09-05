#!/usr/bin/env bash
# Verify prepared files on every exact serving rank; no acquisition or fallback.
set -euo pipefail
SCRIPT_NAME=check-weights
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
NAME="${1:?spec id required}"; shift
NODE_SELECTOR="" JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE_SELECTOR="${2:?node required}"; shift ;;
    --spec-file) export PULSAR_SPEC_FILE="${2:?file required}"; shift ;;
    --json) JSON=1 ;;
    --full) export PULSAR_OBSERVE_FULL=1 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done
acquire_model_library_lifecycle_lock shared
acquire_model_library_hot_lock shared
load_conf "$NAME"
if [ "$NODES" = 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" || die "placement is not confirmed"
  load_cluster_topology || die "confirmed topology required"
else
  [ -z "$NODE_SELECTOR" ] || die "--node only applies to one-node specs" 2
  require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || die "required topology unavailable"
fi
resolve_library_hot_for_profile "$NAME"
if [ "$JSON" = 1 ]; then printf '%s\n' "$PULSAR_PREPARED_SET_JSON"; else
  printf 'PASS  model files\n      Exact snapshot verified on all %s serving ranks.\n' "$NODES"
fi
