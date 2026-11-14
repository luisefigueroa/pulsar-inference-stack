#!/usr/bin/env bash
# Read-only preflight for an exact configured multi-node profile.
#   cluster/preflight.sh SPEC_ID
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/topology-probes.sh"

PROFILE="${1:-}"
if [ -n "$PROFILE" ]; then
  shift
  load_conf "$PROFILE"
  EXPECTED_NODES="$NODES"
else
  load_cluster_topology || exit 1
  EXPECTED_NODES="$CLUSTER_TOPOLOGY_COUNT"
  # Without a recipe, every saved node: on a ring, every cable around it.
  TOPOLOGY_CLASS=roce-full-mesh
  [ "$CLUSTER_TOPOLOGY_CLASS" != roce-ring ] || TOPOLOGY_CLASS=roce-ring
  MIN_RAILS_PER_PAIR=1
fi
PLACEMENT_NODES=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --placement-nodes)
      [ -n "${2:-}" ] && [ -n "$PROFILE" ] || { echo "error: --placement-nodes needs a spec and a node list" >&2; exit 2; }
      PLACEMENT_NODES="$2"; shift ;;
    --weight-source|--weight-mode)
      refuse_removed_weight_mode_flag
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$EXPECTED_NODES" -gt 1 ] || {
  echo "error: spec must require more than one node" >&2
  exit 1
}
# The recipe's nodes in role order: those --placement-nodes names, as Start
# resolves them, or the default order. ROLE_POSITION[r] is role rank r's saved
# rank; every check below runs on those nodes only and names role ranks.
SERVING_NODE_INDEXES=()
if [ -n "$PROFILE" ]; then
  resolve_serving_placement "" "$PLACEMENT_NODES" || exit 1
fi
ROLE_POSITION=()
declare -A ROLE_OF=()
for ((role = 0; role < EXPECTED_NODES; role++)); do
  ROLE_POSITION[role]="${SERVING_NODE_INDEXES[$role]:-$role}"
  ROLE_OF[${ROLE_POSITION[role]}]=$role
done
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
ok "$EXPECTED_NODES active ranks · topology ${CLUSTER_TOPOLOGY_ID:0:12} · exact spec"
# Rows name role ranks; NODE_EXEC and the saved arrays take saved ranks.
for ((rank = 0; rank < EXPECTED_NODES; rank++)); do
  position="${ROLE_POSITION[$rank]}"
  host="${CLUSTER_NODE_SSH_HOSTS[$position]}"
  control_ip="${CLUSTER_NODE_CONTROL_IPS[$position]}"
  control_if="${CLUSTER_NODE_CONTROL_IFS[$position]}"
  if [ "$position" = 0 ]; then
    ok "rank $rank local · ${CLUSTER_NODE_HOSTNAMES[$position]} · control $control_ip/$control_if"
  elif node_exec "$position" true >/dev/null 2>&1; then
    ok "rank $rank SSH $host · ${CLUSTER_NODE_HOSTNAMES[$position]} · control $control_ip/$control_if"
  else
    bad "rank $rank key-based SSH failed: $host"
  fi
done

# Verify every link among the recipe's ranks in both directions on every
# preflight, each ping bound to the source's own interface on that link, then
# each pair's links against what the recipe needs. On a ring only neighbours
# have links; require_profile_topology already refused any pair without them.
# A recipe that passes data around a loop (roce-ring) uses only each rank and
# the next, and the last and the first: only those pairs are pinged and judged.
while IFS=$'\t' read -r kind first second third fourth _interface; do
  # The helper names saved ranks; the rows name role ranks.
  [ "$kind" = pair ] || [ "$kind" = ok ] || [ "$kind" = fail ] || continue
  first="${ROLE_OF[$first]}" second="${ROLE_OF[$second]}"
  case "$kind" in
    ok) ok "rank $first → rank $second · $third · $fourth" ;;
    fail) bad "rank $first cannot reach rank $second · $third · $fourth" ;;
    pair)
      [ "$first" -lt "$second" ] || { swap="$first"; first="$second"; second="$swap"; }
      if [ "$third" -ge "$MIN_RAILS_PER_PAIR" ]; then
        ok "ranks $first/$second: $third of $fourth links answered both ways"
      else
        bad "ranks $first/$second: $third of $fourth links answered both ways; the spec requires $MIN_RAILS_PER_PAIR"
      fi
      ;;
  esac
done < <(topology_ping_links "$CLUSTER_TOPOLOGY_FILE" "$EXPECTED_NODES" "$([ "$TOPOLOGY_CLASS" != roce-ring ] || echo loop)" \
  "$([ -z "$PLACEMENT_NODES" ] || echo "${ROLE_POSITION[*]}")")

echo "[preflight] per-node readiness"
# check_node ROLE — the node of role rank ROLE; its rows name ROLE.
check_node() {
  local rank="$1" position gpu runtime avail_kb control_ip control_if command
  local probe_file probe_json
  position="${ROLE_POSITION[$rank]}"
  control_ip="${CLUSTER_NODE_CONTROL_IPS[$position]}"
  control_if="${CLUSTER_NODE_CONTROL_IFS[$position]}"

  probe_file=$(mktemp "${TMPDIR:-/tmp}/pulsar-preflight-probe.XXXXXX")
  if probe_node_json_for_rank "$position" >"$probe_file" 2>/dev/null \
      && probe_json=$(python3 "$REPO_DIR/scripts/launch_plan.py" \
        probe-from-node "$probe_file" --rank "$position"); then
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

  # The adapters this launch gives the rank must be ACTIVE RDMA ports: a ping
  # answers over IP even when the RDMA driver is not loaded.
  local selected active total missing
  selected="${CLUSTER_PROFILE_HCAS[$position]:-}"
  IFS=$'\t' read -r active total missing < <(
    node_exec "$position" "rdma link show 2>/dev/null" 2>/dev/null | topology_adapters_active "$selected")
  if [ "$total" -gt 0 ] && [ "$active" = "$total" ]; then
    ok "rank $rank: $active of $total selected RDMA adapters ACTIVE"
  elif [ "$total" -gt 0 ]; then
    bad "rank $rank: $active of $total selected RDMA adapters ACTIVE; not ACTIVE: ${missing//,/, }"
  else
    bad "rank $rank: no RDMA adapter is selected for this rank"
  fi

  node_exec "$position" "test -e $(printf '%q' "$PULSAR_RDMA_VERBS_DEVICE")" \
    2>/dev/null \
    && ok "rank $rank: /dev/infiniband present" \
    || bad "rank $rank: /dev/infiniband missing"

  command="ip -4 -o addr show dev $(printf '%q' "$control_if")"
  command+=" | awk '{print \$4}' | cut -d/ -f1 | grep -Fxq $(printf '%q' "$control_ip")"
  node_exec "$position" "$command" 2>/dev/null \
    && ok "rank $rank: control binding $control_ip on $control_if" \
    || bad "rank $rank: control IP/interface mismatch"

  avail_kb=$(node_exec "$position" \
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
  stale=$(node_exec "${ROLE_POSITION[$rank]}" \
    "docker ps -a --format '{{.Names}}' | grep -c '^vllm-cluster-'" \
    2>/dev/null || true)
  if [ "${stale:-0}" -eq 0 ]; then
    ok "rank $rank: no stale vllm-cluster containers"
  else
    bad "rank $rank: $stale stale vllm-cluster container(s)"
  fi
done

if [ -n "$PROFILE" ]; then
  echo "[preflight] spec ${PROFILE:0:12}"
  HFDIR="models--${MODEL//\//--}"
  for ((rank = 0; rank < EXPECTED_NODES; rank++)); do
    image_command="docker image inspect $(printf '%q' "$IMAGE") >/dev/null 2>&1"
    node_exec "${ROLE_POSITION[$rank]}" "$image_command" \
      && ok "rank $rank: image $IMAGE" \
      || bad "rank $rank: image $IMAGE not present"
  done
  placement=()
  [ -z "$PLACEMENT_NODES" ] || placement=(--placement-nodes "$PLACEMENT_NODES")
  if "$REPO_DIR/scripts/check-weights.sh" "$PROFILE" "${placement[@]}" >/dev/null; then
    ok "model files: prepared on every rank for ${PROFILE:0:12}"
  else
    bad "model files are not prepared — run: ./pulsar model prepare $PROFILE ${placement[*]:+${placement[*]} }--yes"
  fi
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "[preflight] ALL CHECKS PASSED"
else
  echo "[preflight] FAILURES — fix before starting"
fi
exit "$FAIL"
