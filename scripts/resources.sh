#!/usr/bin/env bash
# Read-only node/container sampling, including the interval before model launch.
set -euo pipefail
SCRIPT_NAME=resources
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
SERVICE_ID="" SPEC_FILE="" NODE_SELECTOR="" INTERVAL=0.25 OVERRIDE_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --service-id|--spec-file|--node|--interval|--override-file)
      case "${2:-}" in ""|-*) die "$1 requires a value" 2 ;; esac
      case "$1" in
        --service-id) SERVICE_ID="$2" ;;
        --spec-file) SPEC_FILE="$2" ;;
        --node) NODE_SELECTOR="$2" ;;
        --interval) INTERVAL="$2" ;;
        --override-file) OVERRIDE_FILE="$2" ;;
      esac
      shift ;;
    --jsonl) ;;
    --help|-h)
      echo 'usage: pulsar resources (--service-id ID | --spec-file FILE [--node NODE] [--override-file FILE]) [--interval SECONDS] --jsonl'
      echo 'Samples confirmed nodes before launch; container metrics remain unavailable until an exact owned recipe appears.'
      exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done
[ -n "$SERVICE_ID" ] || [ -n "$SPEC_FILE" ] || die 'select a service or spec' 2
[ -z "$SERVICE_ID" ] || [ -z "$SPEC_FILE$OVERRIDE_FILE$NODE_SELECTOR" ] || die 'service and spec selectors are exclusive' 2
python3 - "$INTERVAL" <<'PY'
import math,sys
value=float(sys.argv[1])
if not math.isfinite(value) or not 0.1<=value<=60: raise SystemExit('interval must be finite and between 0.1 and 60 seconds')
PY
work=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-resources.XXXXXX")
declare -a pids=()
cleanup() {
  local pid
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -n "$SERVICE_ID" ]; then
  python3 "$REPO_DIR/scripts/service_state.py" locate --state-root "$PULSAR_MODEL_LIBRARY_DIR" --service-id "$SERVICE_ID" >"$work/context.json"
  load_cluster_topology || die 'confirmed topology required'
  mapfile -t locator < <(python3 - "$work/context.json" <<'PY'
import json,sys
plan=json.load(open(sys.argv[1]))
print(plan['topology_id']);print(plan['container_name'])
for rank in plan['ranks']: print(rank['node_id'])
PY
)
  [ "$CLUSTER_TOPOLOGY_ID" = "${locator[0]}" ] || die 'confirmed service topology differs'
  for ((rank=0;rank<${#locator[@]}-2;rank++)); do
    index=$(model_physical_rank "${locator[$((rank+2))]}")
    if [ "$index" = 0 ]; then
      "$PULSAR_DOCKER" inspect --format '{{json .}}' "${locator[1]}" >"$work/container-$rank.json"
    else
      command=$(shell_join_q docker inspect --format '{{json .}}' "${locator[1]}")
      ssh_node "$index" "$command" >"$work/container-$rank.json"
    fi
  done
  python3 "$REPO_DIR/scripts/service_state.py" actual --state-root "$PULSAR_MODEL_LIBRARY_DIR" --plan "$work/context.json" --observations "$work" >"$work/actual.json"
  mv "$work/actual.json" "$work/context.json"
  python3 - "$work/context.json" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);value=json.loads(p.read_text())
for rank in value['ranks']: rank['rank_label']='single' if len(value['ranks'])==1 else str(rank['rank'])
p.write_text(json.dumps(value))
PY
else
  unset PULSAR_OVERRIDE_FILE PULSAR_EFFECTIVE_SPEC_ID
  [ -z "$OVERRIDE_FILE" ] || export PULSAR_OVERRIDE_FILE="$OVERRIDE_FILE"
  export PULSAR_SPEC_FILE="$SPEC_FILE"
  NAME=$(python3 - "$SPEC_FILE" <<'PY'
import sys
from release_spec.serving import load_spec
print(load_spec(sys.argv[1])['spec_id'])
PY
)
  load_conf "$NAME"
  require_spec_platform_admission "$NAME"
  load_cluster_topology || die 'confirmed topology required'
  if [ "$NODES" = 1 ]; then
    NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
    resolve_single_node_placement "$NODE_SELECTOR" || die 'confirmed placement required'
    indexes=("$SINGLE_NODE_INDEX")
  else
    [ -z "$NODE_SELECTOR" ] || die '--node only applies to one-node specs' 2
    require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || die 'confirmed recipe geometry unavailable'
    indexes=()
    for ((i=0;i<NODES;i++)); do indexes+=("$i"); done
  fi
  node_ids=()
  for index in "${indexes[@]}"; do node_ids+=("${CLUSTER_NODE_IDS[$index]}"); done
  python3 - "$work/context.json" "$NAME" "$PULSAR_EFFECTIVE_SPEC_ID" "$CLUSTER_TOPOLOGY_ID" "$(container_name_for "$NAME" "$NODES")" "${node_ids[@]}" <<'PY'
import json,sys
from pathlib import Path
from scripts.container_runtime import service_identifier
path,selected,effective,topology,name,*nodes=sys.argv[1:]
Path(path).write_text(json.dumps(dict(schema_version=1,kind='pulsar-resource-context',selected_spec_id=selected,
    spec_id=effective,topology_id=topology,container_name=name,service_id=service_identifier(selected,topology,nodes),
    ranks=[dict(rank=i,rank_label='single' if len(nodes)==1 else str(i),node_id=node) for i,node in enumerate(nodes)])))
PY
fi
python3 - "$work/context.json" "$INTERVAL" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
print(json.dumps(dict(schema_version=1,kind='pulsar-resource-stream',service_id=value['service_id'],
    spec_id=value['spec_id'],ranks=[r['rank_label'] for r in value['ranks']],interval_seconds=sys.argv[2])),flush=True)
PY
count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["ranks"]))' "$work/context.json")
token=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
for ((rank=0;rank<count;rank++)); do
  setsid bash "$REPO_DIR/scripts/resource-rank.sh" "$work/context.json" "$rank" "$INTERVAL" "$token" "$work/relay.lock" &
  pids+=("$!")
done
wait -n "${pids[@]}" || true
python3 "$REPO_DIR/scripts/resource_relay.py" --lock "$work/relay.lock" --error sampler_ended
exit 3
