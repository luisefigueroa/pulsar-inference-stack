#!/usr/bin/env bash
# Stop containers by proven ownership and immutable ID. Always retain files.
set -euo pipefail
SCRIPT_NAME=down
down_usage() {
  python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/terminal_format.py" <<'HELP'
usage: pulsar stop SPEC_ID [--node NODE] [--spec-file FILE]
       pulsar stop --all

Stop the owned service for the exact spec on every participating node.
Model files, pins, archives and evidence are always retained. NODE is a
confirmed node's hostname or node ID.
HELP
}
case "${1:-}" in
  -h|--help) down_usage; exit 0 ;;
  "") down_usage >&2; exit "${PULSAR_USAGE_EXIT:-2}" ;;
esac
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
TARGET="$1"; shift
NODE_SELECTOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --node) [ -n "${2:-}" ] || usage_die "--node requires a node"; NODE_SELECTOR="$2"; shift ;;
    --spec-file) [ -n "${2:-}" ] || usage_die "--spec-file requires a file"; export PULSAR_SPEC_FILE="$2"; shift ;;
    --retain-weights)
      warn "pulsar stop --retain-weights is deprecated: stop always retains model files. It is removed in CLI contract 2." ;;
    --pin-weights|--purge-hot) usage_die "stop retains files; use an explicit model pin or purge command" ;;
    *) usage_die "unknown argument: $1" ;;
  esac
  shift
done
if [ "$TARGET" = --all ]; then
  [ -z "$NODE_SELECTOR" ] || usage_die "--node cannot be used with --all"
  # The delegated command acquires the lock once for all confirmed ranks.
  exec "$REPO_DIR/cluster/stop-cluster.sh" --all
fi
[[ "$TARGET" =~ ^[0-9a-f]{64}$ ]] || usage_die "stop requires the complete 64-character spec id (./pulsar models list --json)"
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
