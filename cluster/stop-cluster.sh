#!/usr/bin/env bash
# Fail-closed teardown for an exact N-rank profile or all stack-managed ranks.
#   cluster/stop-cluster.sh <model-name|--all>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"

ARG="${1:-}"
[ -n "$ARG" ] || { echo "usage: $0 <model-name|--all>" >&2; exit 2; }

if [ "$ARG" = --all ]; then
  load_cluster_topology || exit 1
  [ "$CLUSTER_TOPOLOGY_COUNT" -gt 1 ] || {
    echo "[stop] ERROR: no confirmed remote cluster ranks" >&2
    exit 1
  }
  echo "[stop] removing stack-managed containers across $CLUSTER_TOPOLOGY_COUNT confirmed ranks"

  # Prove every Docker endpoint before the first mutation.
  if ! list_managed_container_ids_local >/dev/null; then
    echo "[stop] ERROR: rank 0 Docker unavailable" >&2
    echo "[stop] No containers were removed." >&2
    exit 1
  fi

  for ((rank = 1; rank < CLUSTER_TOPOLOGY_COUNT; rank++)); do
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    if ! list_managed_container_ids_remote "$host" >/dev/null; then
      echo "[stop] ERROR: rank $rank unreachable or Docker error on $host" >&2
      echo "[stop] No containers were removed." >&2
      exit 1
    fi
  done

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
      echo "[stop] clean — no stack-managed containers remain"
      exit 0
      ;;
    2)
      echo "[stop] WARNING: some candidates were refused (unknown conf or rank placement)" >&2
      ;;
    *)
      echo "[stop] WARNING: managed cleanup reported an operational error" >&2
      ;;
  esac
  exit 1
fi

load_conf "$ARG"
if [ "$NODES" -le 1 ]; then
  echo "[stop] ERROR: $ARG is a single-node profile; use scripts/down.sh $ARG" >&2
  exit 1
fi
require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" \
  || exit 1

EXACT=$(container_name_for "$ARG" "$NODES")
echo "[stop] stopping exact profile=$ARG · $NODES ranks · container=$EXACT"
rc=0
remove_stack_owned_cluster "$ARG" "$EXACT" "$NODES" || rc=$?
if [ "$rc" -eq 2 ]; then
  echo "[stop] refused: ownership not proven for every existing rank of $EXACT" >&2
  exit 1
fi
if [ "$rc" -ne 0 ]; then
  echo "[stop] failed (operational error; initial probe failed without fallback)" >&2
  exit 1
fi

left=0
probe_rc=0
container_ownership_inspect_local "$EXACT" >/dev/null 2>&1 && left=$((left + 1)) || {
  probe_rc=$?
  if [ "$probe_rc" -eq 1 ]; then
    echo "[stop] WARNING: local Docker error verifying rank 0" >&2
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
        echo "[stop] WARNING: rank $rank error verifying $EXACT is gone" >&2
        exit 1
      fi
    }
done

if [ "$left" -eq 0 ]; then
  echo "[stop] clean — $EXACT absent from all $NODES active ranks"
  exit 0
fi
echo "[stop] WARNING: $EXACT remains on $left rank(s)" >&2
exit 1
