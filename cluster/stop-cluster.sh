#!/usr/bin/env bash
# Fail-closed teardown for an exact N-rank profile or all stack-managed ranks.
#   cluster/stop-cluster.sh SPEC_ID|--all
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
SCRIPT_NAME=stop
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"

ARG="${1:-}"
[ -n "$ARG" ] || { echo "usage: $0 SPEC_ID|--all" >&2; exit 2; }
acquire_model_library_lifecycle_lock exclusive

if [ "$ARG" = --all ]; then
  load_cluster_topology || exit 1
  [ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] || {
    echo "error: no confirmed cluster ranks" >&2
    exit 1
  }
  log "removing stack-managed containers across $CLUSTER_TOPOLOGY_COUNT confirmed ranks"

  # Prove every Docker endpoint before the first mutation.
  if ! list_managed_container_ids_local >/dev/null; then
    echo "error: stop --all did not run: Docker is unavailable on $(human_node_name 0) (rank 0), so no container was removed. Next: ./pulsar doctor, then rerun ./pulsar stop --all --yes" >&2
    exit 1
  fi

  for ((rank = 1; rank < CLUSTER_TOPOLOGY_COUNT; rank++)); do
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    if ! list_managed_container_ids_remote "$host" >/dev/null; then
      echo "error: stop --all did not run: $(human_node_name "$rank") (rank $rank) is unreachable or its Docker failed, so no container was removed. Next: ./pulsar topology check, then rerun ./pulsar stop --all --yes" >&2
      exit 1
    fi
  done

  # First each ordinary service recorded off the default node order, on the
  # nodes its record names and with the same proofs as stop SPEC. Guarded
  # trials keep their own path: scripts/down.sh refuses --all while one is
  # recorded off the default order.
  if ! recorded=$(python3 - "$PULSAR_MODEL_LIBRARY_DIR" "${CLUSTER_NODE_IDS[@]}" <<'PY_RECORDED'
import sys
from model_library.state import Store
from scripts.service_state import off_default_order, recorded_bindings
for item in recorded_bindings(Store(sys.argv[1])):
    if not item['guarded'] and off_default_order(item['node_ids'], sys.argv[2:]):
        print(item['selected_spec_id'], item['plan_id'], sep='\t')
PY_RECORDED
  ); then
    echo "error: stop --all did not run: the saved service records cannot be read, so no container was removed. Next: ./pulsar inventory" >&2
    exit 1
  fi
  while IFS=$'\t' read -r recorded_spec recorded_plan; do
    [ -n "$recorded_plan" ] || continue
    if ! stop_recorded_service "$recorded_spec" "$recorded_plan"; then
      echo "error: stop --all stopped before removing the other services' containers, because spec ${recorded_spec:0:12}'s service could not be stopped on its recorded nodes. Next: ./pulsar stop $(spec_reference "$recorded_spec"), which says what it needs, then rerun ./pulsar stop --all --yes" >&2
      exit 1
    fi
  done <<<"$recorded"

  rc=0
  for ((rank = CLUSTER_TOPOLOGY_COUNT - 1; rank >= 1; rank--)); do
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    step=0
    remove_all_stack_managed_remote "$host" "$rank" || step=$?
    rc=$(lifecycle_merge_rc "$rc" "$step")
  done
  step=0
  remove_all_stack_managed_local || step=$?
  rc=$(lifecycle_merge_rc "$rc" "$step")

  case "$rc" in
    0)
      retire_stopped_service_indexes --all
      log "clean — no stack-managed containers remain"
      exit 0
      ;;
    2)
      warn "some candidates were refused (unknown spec or rank placement)"
      ;;
    *)
      warn "managed cleanup reported an operational error"
      ;;
  esac
  exit 1
fi

load_conf "$ARG"
if [ "$NODES" -le 1 ]; then
  echo "error: $ARG is a single-node spec; use ./pulsar stop $ARG" >&2
  exit 1
fi
# Stop needs the confirmed nodes, never the fabric: a running or leftover
# service stays stoppable whatever the saved cluster's shape.
require_cluster_nodes "$NODES" || exit 1

EXACT=$(container_name_for "$ARG" "$NODES")
log "stopping spec ${ARG:0:12} · $NODES nodes · container=$EXACT"
rc=0
remove_stack_owned_cluster "$ARG" "$EXACT" "$NODES" || rc=$?
if [ "$rc" -eq 2 ]; then
  error_line "refused: ownership not proven for every existing rank of $EXACT"
  exit 1
fi
if [ "$rc" -ne 0 ]; then
  error_line "stop failed (operational error; initial probe failed without fallback)"
  exit 1
fi

left=0
probe_rc=0
container_ownership_inspect_local "$EXACT" >/dev/null 2>&1 && left=$((left + 1)) || {
  probe_rc=$?
  if [ "$probe_rc" -eq 1 ]; then
    warn "local Docker error verifying rank 0"
    exit 1
  fi
}
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  probe_rc=0
  container_ownership_inspect_remote "$host" "$EXACT" >/dev/null 2>&1 \
    && left=$((left + 1)) || {
      probe_rc=$?
      if [ "$probe_rc" -eq 1 ]; then
        warn "rank $rank error verifying $EXACT is gone"
        exit 1
      fi
    }
done

if [ "$left" -eq 0 ]; then
  retire_stopped_service_indexes "$ARG"
  log "clean — $EXACT absent from all $NODES active ranks"
  exit 0
fi
warn "$EXACT remains on $left rank(s)"
exit 1
