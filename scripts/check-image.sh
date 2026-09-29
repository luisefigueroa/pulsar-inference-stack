#!/usr/bin/env bash
# Read-only inspection of the pinned spec image on every selected physical node.
# Exit: 0 pass · 1 the condition failed · 3 the check could not run.
set -euo pipefail
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then echo "Usage: check-image.sh SPEC [--spec-file FILE] [--node NODE] [--json]"; exit 0; fi
SCRIPT_NAME=check-image
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
check_exit_convention
[ -n "${1:-}" ] || die "spec id required" 3
NAME="$1"; shift
JSON=0 NODE_SELECTOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --node) [ -n "${2:-}" ] || die "--node requires a node" 3; NODE_SELECTOR="$2"; shift ;;
    --spec-file) [ -n "${2:-}" ] || die "--spec-file requires a file" 3; export PULSAR_SPEC_FILE="$2"; shift ;;
    *) die "unknown argument: $1" 3 ;;
  esac
  shift
done
load_conf "$NAME"
node_indices=() states=()
topology_ready=1
if ! require_cluster_nodes "$NODES" >/dev/null 2>&1; then topology_ready=0; fi
if [ "$NODES" = 1 ] && [ "$topology_ready" = 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" || die "selected node is not confirmed" 3
  node_indices=("$SINGLE_NODE_INDEX")
else
  [ -z "$NODE_SELECTOR" ] || die "--node applies only to one-node specs" 3
  for ((rank=0;rank<NODES;rank++)); do node_indices+=("$rank"); done
fi
verify_image_json() {
  python3 -c '
import json,sys
try:
 d=json.load(sys.stdin)
 if isinstance(d,list) and len(d)==1:d=d[0]
 if not isinstance(d,dict) or not d.get("Id"):raise ValueError("image identity missing")
 if not any(value.endswith("@"+sys.argv[1]) for value in d.get("RepoDigests",[])):raise ValueError("image digest differs")
 if d.get("Architecture")!="arm64":raise ValueError("image architecture is not arm64")
except (ValueError,TypeError,AttributeError) as exc:
 print(str(exc),file=sys.stderr);raise SystemExit(1)
' "${IMAGE##*@}"
}
for ((rank=0;rank<NODES;rank++)); do
  if [ "$topology_ready" != 1 ]; then states+=(need-topology); continue; fi
  physical="${node_indices[$rank]}"
  if [ "$physical" = 0 ]; then
    if ! "$PULSAR_DOCKER" info >/dev/null 2>&1; then states+=(docker-error); continue; fi
    if raw=$("$PULSAR_DOCKER" image inspect --format '{{json .}}' "$IMAGE" 2>/dev/null); then
      if printf '%s' "$raw" | verify_image_json 2>/dev/null; then states+=(ok); else states+=(docker-error); fi
    else states+=(missing); fi
  else
    if ! ssh_node "$physical" true >/dev/null 2>&1; then states+=(unreachable); continue; fi
    if ! ssh_node "$physical" 'docker info >/dev/null 2>&1'; then states+=(docker-error); continue; fi
    if raw=$(ssh_node "$physical" "$(shell_join_q docker image inspect --format '{{json .}}' "$IMAGE")" 2>/dev/null); then
      if printf '%s' "$raw" | verify_image_json 2>/dev/null; then states+=(ok); else states+=(docker-error); fi
    else states+=(missing); fi
  fi
done
report=$(python3 - "$REPO_DIR" "$NAME" "$IMAGE" "$NODES" "${node_indices[*]}" "${states[@]}" <<'PY'
import json,sys
sys.path.insert(0,sys.argv[1])
from scripts.launch_plan import rank_image_aggregate_state
name,image,nodes,indices=sys.argv[2:6];states=sys.argv[6:];indices=indices.split()
state=rank_image_aggregate_state(states)
if state=='missing-on-rank' and states[0]=='missing':
 state='missing-both' if 'missing' in states[1:] else 'missing-on-head'
print(json.dumps(dict(schema_version=1,kind='pulsar-image-check',model=name,image=image,nodes=int(nodes),state=state,head_ok=states[0]=='ok',worker_ok=len(states)>1 and states[1]=='ok',ranks=[dict(rank=i,topology_index=int(indices[i]),state=value,ok=value=='ok') for i,value in enumerate(states)])))
PY
)
state=$(printf '%s' "$report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
if [ "$JSON" = 1 ]; then printf '%s\n' "$report"; else
  if [ "$state" = ok ]; then print_hanging 'PASS  image  ' "Pinned arm64 image verified on all $NODES serving ranks."; else
    print_hanging 'FAIL  image  ' "$state; inspect the required nodes before image staging."
  fi
fi
[ "$state" = ok ] || check_result 1
