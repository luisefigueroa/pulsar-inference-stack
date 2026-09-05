#!/usr/bin/env bash
# Private all-rank observation used only by explicit qualification/diagnostics.
set -euo pipefail
SCRIPT_NAME=observe-serving
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
NAME="${1:?spec id required}"; shift
NODE_SELECTOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE_SELECTOR="${2:?node required}"; shift ;;
    --spec-file) export PULSAR_SPEC_FILE="${2:?file required}"; shift ;;
    --json) ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done
acquire_model_library_lifecycle_lock shared
acquire_model_library_hot_lock shared
load_conf "$NAME"
require_spec_platform_admission "$NAME"
if [ "$NODES" = 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" || die "placement is not confirmed"
  load_cluster_topology || die "confirmed topology required"
  API_URL=$(single_node_api_base_url "$PORT")
else
  [ -z "$NODE_SELECTOR" ] || die "--node only applies to one-node specs" 2
  require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || die "required topology unavailable"
  API_URL="http://${CLUSTER_NODE_CONTROL_IPS[0]}:$PORT"
fi
[ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || die "confirmed topology is required"
resolve_spec_decode auto
LAUNCH_CONTRACT_ID=$(loaded_launch_contract_id)
# Inspect boot before and after full file verification so restarts during hashing
# cannot create a misleading observation of one continuous serving attempt.
OBS=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-observe.XXXXXX")
trap 'rm -rf "$OBS"' EXIT
chmod 700 "$OBS"
CONTAINER=$(container_name_for "$NAME" "$NODES")
for ((rank=0; rank<NODES; rank++)); do
  index="$rank"; [ "$NODES" != 1 ] || index="$SINGLE_NODE_INDEX"
  command=$(shell_join_q docker inspect --format '{{json .}}' "$CONTAINER")
  if [ "$index" = 0 ]; then
    "$PULSAR_DOCKER" inspect --format '{{json .}}' "$CONTAINER" >"$OBS/container-$rank.json"
    "$PULSAR_DOCKER" image inspect --format '{{json .}}' "$IMAGE" >"$OBS/image-$rank.json"
  else
    ssh_node "$index" "$command" >"$OBS/container-$rank.json"
    command=$(shell_join_q docker image inspect --format '{{json .}}' "$IMAGE")
    ssh_node "$index" "$command" >"$OBS/image-$rank.json"
  fi
done
PULSAR_OBSERVE_FULL=1 resolve_library_hot_for_profile "$NAME"
write_launch_plan_file "$OBS/plan.json" dry-run
for ((rank=0; rank<NODES; rank++)); do
  index="$rank"; [ "$NODES" != 1 ] || index="$SINGLE_NODE_INDEX"
  command=$(shell_join_q docker inspect --format '{{json .}}' "$CONTAINER")
  if [ "$index" = 0 ]; then
    "$PULSAR_DOCKER" inspect --format '{{json .}}' "$CONTAINER" >"$OBS/after-$rank.json"
  else
    ssh_node "$index" "$command" >"$OBS/after-$rank.json"
  fi
  python3 - "$OBS/container-$rank.json" "$OBS/after-$rank.json" <<'PY'
import json,sys
before,after=(json.load(open(path)) for path in sys.argv[1:])
if before.get('Id') != after.get('Id') or before.get('State',{}).get('StartedAt') != after.get('State',{}).get('StartedAt') or after.get('State',{}).get('Running') is not True:
    raise SystemExit('serving rank changed or stopped during full verification')
PY
  mv "$OBS/after-$rank.json" "$OBS/container-$rank.json"
done
python3 "$REPO_DIR/scripts/runtime_binding.py" observe --spec "$CONF_PATH" --plan "$OBS/plan.json" --observations "$OBS" --api-url "$API_URL" --served-name "$SERVED_NAME"
