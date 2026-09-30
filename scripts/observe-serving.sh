#!/usr/bin/env bash
# Private all-rank observation used only by explicit qualification/diagnostics.
set -euo pipefail
SCRIPT_NAME=observe-serving
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
NAME="" SERVICE_ID="" NODE_SELECTOR="" PLACEMENT_NODES="" OBSERVE_FULL=0 OBSERVE_VERIFICATION_JOBS=3
while [ $# -gt 0 ]; do
  case "$1" in
    --service-id) case "${2:-}" in ""|-*) usage_die "--service-id requires a service ID" ;; esac; SERVICE_ID="$2"; shift ;;
    --node) case "${2:-}" in ""|-*) usage_die "--node requires a node" ;; esac; NODE_SELECTOR="$2"; shift ;;
    --spec-file)
      case "${2:-}" in ""|-*) usage_die "--spec-file requires a file" ;; esac
      warn "pulsar observe --spec-file is deprecated: it is ignored because the recorded spec is authoritative. It is removed in CLI contract 2."
      shift ;;
    --json) ;;
    --full) OBSERVE_FULL=1 ;;
    --verification-jobs) OBSERVE_VERIFICATION_JOBS="${2:-}"; [ $# -lt 2 ] || shift ;;
    --*) usage_die "unknown argument: $1" ;;
    *) [ -z "$NAME" ] || usage_die "unexpected argument: $1"; NAME="$1" ;;
  esac
  shift
done
[[ "$OBSERVE_VERIFICATION_JOBS" =~ ^[1-9][0-9]*$ ]] || usage_die "--verification-jobs requires a positive integer"
acquire_model_library_lifecycle_lock shared
acquire_model_library_hot_lock shared
OBS=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-observe.XXXXXX")
trap 'rm -rf "$OBS"' EXIT
chmod 700 "$OBS"
locator=(--selected-spec-id "$NAME")
[ -z "$SERVICE_ID" ] || locator=(--service-id "$SERVICE_ID")
python3 "$REPO_DIR/scripts/service_state.py" locate --state-root "$PULSAR_MODEL_LIBRARY_DIR" "${locator[@]}" >"$OBS/plan.json"
python3 - "$OBS/plan.json" "$OBS/selected-spec.json" "$OBS/overlay.json" <<'PYCODE'
import json,sys
from pathlib import Path
plan=json.loads(Path(sys.argv[1]).read_text())
Path(sys.argv[2]).write_text(json.dumps(plan['selected_spec']))
# The same private overlay reaches the model-file verification subprocess.
# Today's overlay describes future launches, not this recorded service.
Path(sys.argv[3]).write_text(json.dumps(dict(schema_version=1,kind='pulsar-deployment-overlay',
    defaults=dict(port=plan['port'],served_name=plan['served_name'],cache_root=None,placement=None),specs={})))
PYCODE
NAME=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["selected_spec_id"])' "$OBS/plan.json")
unset PULSAR_OVERRIDE_FILE PULSAR_EFFECTIVE_SPEC_ID
export PULSAR_SPEC_FILE="$OBS/selected-spec.json"
export PULSAR_OVERLAY_PATH="$OBS/overlay.json"
load_conf "$NAME"
require_spec_platform_admission "$NAME"
recorded_node=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ranks"][0]["node_id"])' "$OBS/plan.json")
if [ "$NODES" = 1 ]; then
  if [ -n "$NODE_SELECTOR" ]; then
    # --node accepts a hostname, SSH alias, position or node ID, as start does;
    # the resolver's warning says why a selector matches no single node.
    resolve_single_node_placement "$NODE_SELECTOR" \
      || usage_die "--node '$NODE_SELECTOR' does not select exactly one confirmed node; use a hostname or node ID from ./pulsar topology show"
    if [ "${SINGLE_NODE_ID:-}" != "$recorded_node" ]; then
      resolve_single_node_placement "$recorded_node" >/dev/null 2>&1 \
        || die "the service for this spec runs on node $recorded_node, which is no longer in the confirmed topology"
      die "the service for this spec runs on $SINGLE_NODE_HOSTNAME, not on $NODE_SELECTOR"
    fi
  fi
  NODE_SELECTOR="$recorded_node"
fi
PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$OBS/plan.json")
if [ "$NODES" = 1 ]; then
  resolve_single_node_placement "$NODE_SELECTOR" || die "placement is not confirmed"
  load_cluster_topology || die "confirmed topology required"
  API_URL=$(single_node_api_base_url "$PORT")
else
  [ -z "$NODE_SELECTOR" ] || usage_die "--node only applies to one-node specs"
  PLACEMENT_NODES=$(python3 -c 'import json,sys; print(",".join(row["node_id"] for row in json.load(open(sys.argv[1]))["ranks"]))' "$OBS/plan.json")
  resolve_serving_placement "" "$PLACEMENT_NODES" || die "recorded placement is no longer confirmed"
  require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || die "required topology unavailable"
  API_URL="http://$(url_host "${CLUSTER_NODE_CONTROL_IPS[${SERVING_NODE_INDEXES[0]:-0}]}"):$PORT"
fi
[ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || die "confirmed topology is required"
[ "$CLUSTER_TOPOLOGY_ID" = "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["topology_id"])' "$OBS/plan.json")" ] || die "confirmed service topology changed"
CONTAINER=$(container_name_for "$NAME" "$NODES")
for ((rank=0; rank<NODES; rank++)); do
  index="${SERVING_NODE_INDEXES[$rank]:-$rank}"; [ "$NODES" != 1 ] || index="$SINGLE_NODE_INDEX"
  command=$(shell_join_q docker inspect --format '{{json .}}' "$CONTAINER")
  if [ "$index" = 0 ]; then
    "$PULSAR_DOCKER" inspect --format '{{json .}}' "$CONTAINER" >"$OBS/container-$rank.json"
    actual_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["Image"])' "$OBS/container-$rank.json")
    "$PULSAR_DOCKER" image inspect --format '{{json .}}' "$actual_image" >"$OBS/image-$rank.json"
  else
    ssh_node "$index" "$command" >"$OBS/container-$rank.json"
    actual_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["Image"])' "$OBS/container-$rank.json")
    command=$(shell_join_q docker image inspect --format '{{json .}}' "$actual_image")
    ssh_node "$index" "$command" >"$OBS/image-$rank.json"
  fi
  if ! runtime_context_for_rank "$index" >"$OBS/context-$rank.json"; then
    printf '{"available":false}\n' >"$OBS/context-$rank.json"
  fi
done
PULSAR_OBSERVE_FULL="$OBSERVE_FULL" PULSAR_OBSERVE_VERIFICATION_JOBS="$OBSERVE_VERIFICATION_JOBS" resolve_library_hot_for_profile "$NAME"
python3 "$REPO_DIR/scripts/service_state.py" actual --state-root "$PULSAR_MODEL_LIBRARY_DIR" --plan "$OBS/plan.json" --observations "$OBS" >"$OBS/actual-plan.json"
mv "$OBS/actual-plan.json" "$OBS/plan.json"
printf '%s\n' "$PULSAR_PREPARED_SET_JSON" >"$OBS/prepared.json"
PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$OBS/plan.json")
SERVED_NAME=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["served_name"])' "$OBS/plan.json")
if [ "$NODES" = 1 ]; then
  API_URL=$(single_node_api_base_url "$PORT")
else
  API_URL="http://$(url_host "${CLUSTER_NODE_CONTROL_IPS[${SERVING_NODE_INDEXES[0]:-0}]}"):$PORT"
fi
for ((rank=0; rank<NODES; rank++)); do
  index="${SERVING_NODE_INDEXES[$rank]:-$rank}"; [ "$NODES" != 1 ] || index="$SINGLE_NODE_INDEX"
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
