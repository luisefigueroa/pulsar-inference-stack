#!/usr/bin/env bash
# Stop containers by proven ownership and immutable ID. Always retain files.
set -euo pipefail
SCRIPT_NAME=down
down_usage() {
  cat <<'HELP'
usage: pulsar stop SPEC_ID [--node NODE_ID] [--spec-file FILE]
       pulsar stop --all

Stop the owned service for the exact spec on every participating node.
Model files, pins, archives and evidence are always retained.
HELP
}
case "${1:-}" in
  -h|--help) down_usage; exit 0 ;;
  "") down_usage >&2; exit 2 ;;
esac
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
TARGET="$1"; shift
NODE_SELECTOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE_SELECTOR="${2:?node required}"; shift ;;
    --spec-file) export PULSAR_SPEC_FILE="${2:?file required}"; shift ;;
    --retain-weights) ;;
    --pin-weights|--purge-hot) die "stop retains files; use an explicit model pin or purge command" 2 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done
if [ "$TARGET" = --all ]; then
  [ -z "$NODE_SELECTOR" ] || die "--node cannot be used with --all" 2
  # The delegated command acquires the lock once for all confirmed ranks.
  exec "$REPO_DIR/cluster/stop-cluster.sh" --all
fi
[[ "$TARGET" =~ ^[0-9a-f]{64}$ ]] || die "stop requires the complete 64-character spec id (./pulsar models list --json)" 2
acquire_model_library_lifecycle_lock exclusive
load_cluster_topology || die "confirmed topology is required for safe stop"
[ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || die "confirmed topology is required for safe stop"
stop_named_service_by_labels "$TARGET" "$NODE_SELECTOR"
retire_stopped_service_indexes "$TARGET" "$NODE_SELECTOR"
if [ -n "${PULSAR_STOP_RESULT_FILE:-}" ]; then
  stopped=true; [ "${STOP_NAMED_NOTHING_FOUND:-0}" != 1 ] || stopped=false
  printf '{"spec_id": "%s", "stopped": %s}\n' "$TARGET" "$stopped" >"$PULSAR_STOP_RESULT_FILE"
fi
if [ "${STOP_NAMED_NOTHING_FOUND:-0}" = 1 ]; then
  log "Nothing was stopped. Model files, pins, archives and experiment evidence are unchanged."
else
  log "Stopped. Model files, pins, archives and experiment evidence are retained."
fi
