#!/usr/bin/env bash
# Stop containers by proven ownership and immutable ID. Always retain files.
set -euo pipefail
SCRIPT_NAME=down
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
TARGET="${1:?spec id or --all required}"; shift
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
load_cluster_topology || die "confirmed topology is required for safe stop"
[ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || die "confirmed topology is required for safe stop"
if [ "$TARGET" = --all ]; then
  [ -z "$NODE_SELECTOR" ] || die "--node cannot be used with --all" 2
  if [ "$CLUSTER_TOPOLOGY_COUNT" -gt 1 ]; then
    exec "$REPO_DIR/cluster/stop-cluster.sh" --all
  fi
  remove_all_stack_managed_local
else
  [[ "$TARGET" =~ ^[0-9a-f]{64}$ ]] || die "stop requires an exact spec id" 2
  stop_named_service_by_labels "$TARGET" "$NODE_SELECTOR"
fi
log "Stopped. Model files, pins, archives and experiment evidence are retained."
