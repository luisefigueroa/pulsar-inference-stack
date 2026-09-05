#!/usr/bin/env bash
# Read-only preflight for an exact configured multi-node profile.
#   cluster/preflight.sh <model-name>
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"

PROFILE="${1:-}"
if [ -n "$PROFILE" ]; then
  shift
  load_conf "$PROFILE"
  EXPECTED_NODES="$NODES"
else
  load_cluster_topology || exit 1
  EXPECTED_NODES="$CLUSTER_TOPOLOGY_COUNT"
  TOPOLOGY_CLASS=roce-full-mesh
  MIN_RAILS_PER_PAIR=1
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --weight-source|--weight-mode)
      refuse_removed_weight_mode_flag
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$EXPECTED_NODES" -gt 1 ] || {
  echo "[preflight] profile must require more than one node" >&2
  exit 1
}
require_profile_topology \
  "$EXPECTED_NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || exit 1

FAIL=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; FAIL=1; }
warn() { printf '  warn %s\n' "$1"; }

node_exec() {
  local rank="${1:?rank required}"
  shift
  if [ "$rank" = 0 ]; then
    bash -c "$1"
  else
    local host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$1"
  fi
}

echo "[preflight] topology"
ok "$EXPECTED_NODES active ranks · topology ${CLUSTER_TOPOLOGY_ID:0:12} · exact profile"
for ((rank = 0; rank < EXPECTED_NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  control_ip="${CLUSTER_NODE_CONTROL_IPS[$rank]}"
  control_if="${CLUSTER_NODE_CONTROL_IFS[$rank]}"
  if [ "$rank" = 0 ]; then
    ok "rank 0 local · ${CLUSTER_NODE_HOSTNAMES[$rank]} · control $control_ip/$control_if"
  elif node_exec "$rank" true >/dev/null 2>&1; then
    ok "rank $rank SSH $host · ${CLUSTER_NODE_HOSTNAMES[$rank]} · control $control_ip/$control_if"
  else
    bad "rank $rank key-based SSH failed: $host"
  fi
done

# Verify every selected pair and rail in both directions on every preflight.
# require_profile_topology already guarantees a confirmed manifest here.
while IFS=$'\t' read -r source_rank _source_host target_rank target_ip network; do
  [ -n "$source_rank" ] || continue
  if [ "$source_rank" -ge "$EXPECTED_NODES" ] \
      || [ "$target_rank" -ge "$EXPECTED_NODES" ]; then
    continue
  fi
  command="ping -c1 -W2 $(printf '%q' "$target_ip") >/dev/null 2>&1"
  if node_exec "$source_rank" "$command"; then
    ok "rank $source_rank → rank $target_rank · $target_ip · $network"
  else
    bad "rank $source_rank cannot reach rank $target_rank · $target_ip · $network"
  fi
done < <(python3 "$REPO_DIR/scripts/topology_manifest.py" \
  ping-plan "$CLUSTER_TOPOLOGY_FILE")

echo "[preflight] per-node readiness"
check_node() {
  local rank="$1" rdma gpu runtime avail_kb control_ip control_if command
  local probe_file probe_json
  control_ip="${CLUSTER_NODE_CONTROL_IPS[$rank]}"
  control_if="${CLUSTER_NODE_CONTROL_IFS[$rank]}"

  rdma=$(node_exec "$rank" \
    "rdma link show 2>/dev/null | grep -c 'state ACTIVE'" 2>/dev/null || true)
  if [ "${rdma:-0}" -ge "$MIN_RAILS_PER_PAIR" ]; then
    ok "rank $rank: ${rdma:-0} RDMA links ACTIVE"
  else
    bad "rank $rank: only ${rdma:-0} RDMA links ACTIVE"
  fi

  probe_file=$(mktemp "${TMPDIR:-/tmp}/pulsar-preflight-probe.XXXXXX")
  if probe_node_json_for_rank "$rank" >"$probe_file" 2>/dev/null \
      && probe_json=$(python3 "$REPO_DIR/scripts/launch_plan.py" \
        probe-from-node "$probe_file" --rank "$rank"); then
    gpu=$(printf '%s' "$probe_json" | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("gpu") or "")')
    runtime=$(printf '%s' "$probe_json" | python3 -c \
      'import json,sys; p=json.load(sys.stdin); print("1" if p.get("docker_nvidia") else "0")')
    [ "$gpu" = "$PULSAR_GPU_NAME" ] \
      && ok "rank $rank: GPU $gpu" \
      || bad "rank $rank: GPU '$gpu' (want ${PULSAR_GPU_NAME})"
    [ "$runtime" = 1 ] \
      && ok "rank $rank: Docker NVIDIA ready" \
      || bad "rank $rank: Docker NVIDIA runtime/CDI missing"
  else
    bad "rank $rank: serving probe failed"
  fi
  rm -f "$probe_file"

  node_exec "$rank" "test -e $(printf '%q' "$PULSAR_RDMA_VERBS_DEVICE")" \
    2>/dev/null \
    && ok "rank $rank: /dev/infiniband present" \
    || bad "rank $rank: /dev/infiniband missing"

  command="ip -4 -o addr show dev $(printf '%q' "$control_if")"
  command+=" | awk '{print \$4}' | cut -d/ -f1 | grep -Fxq $(printf '%q' "$control_ip")"
  node_exec "$rank" "$command" 2>/dev/null \
    && ok "rank $rank: control binding $control_ip on $control_if" \
    || bad "rank $rank: control IP/interface mismatch"

  avail_kb=$(node_exec "$rank" \
    "awk '/MemAvailable/{print \$2}' /proc/meminfo" 2>/dev/null || true)
  warn_kb=$(awk -v g="${PULSAR_PREFLIGHT_WARN_AVAILABLE_GIB}" \
    'BEGIN { printf "%.0f", g * 1048576 }')
  if [ "${avail_kb:-0}" -ge "$warn_kb" ]; then
    ok "rank $rank: $((avail_kb / 1048576)) GiB available"
  else
    warn "rank $rank: only $(( ${avail_kb:-0} / 1048576)) GiB available"
  fi
}

for ((rank = 0; rank < EXPECTED_NODES; rank++)); do
  check_node "$rank"
done

echo "[preflight] stale state"
for ((rank = 0; rank < EXPECTED_NODES; rank++)); do
  stale=$(node_exec "$rank" \
    "docker ps -a --format '{{.Names}}' | grep -c '^vllm-cluster-'" \
    2>/dev/null || true)
  if [ "${stale:-0}" -eq 0 ]; then
    ok "rank $rank: no stale vllm-cluster containers"
  else
    bad "rank $rank: $stale stale vllm-cluster container(s)"
  fi
done

if [ -n "$PROFILE" ]; then
  echo "[preflight] model $PROFILE"
  HFDIR="models--${MODEL//\//--}"
  for ((rank = 0; rank < EXPECTED_NODES; rank++)); do
    image_command="docker image inspect $(printf '%q' "$IMAGE") >/dev/null 2>&1"
    node_exec "$rank" "$image_command" \
      && ok "rank $rank: image $IMAGE" \
      || bad "rank $rank: image $IMAGE not present"
  done
  if "$REPO_DIR/scripts/check-weights.sh" "$PROFILE" >/dev/null; then
    ok "model files: ready library staging for $PROFILE"
  else
    bad "model files are not prepared — run: scripts/model-library.sh prepare $PROFILE --yes"
  fi
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "[preflight] ALL CHECKS PASSED"
else
  echo "[preflight] FAILURES — fix before starting"
fi
exit "$FAIL"
