#!/usr/bin/env bash
# Read-only inspection of the pinned spec image on every selected physical node.
# Exit: 0 pass · 1 the condition failed · 2 a usage mistake · 3 the check could not run.
set -euo pipefail
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then echo "Usage: pulsar image check SPEC [--spec-file FILE] [--node NODE | --placement-nodes LIST] [--json]"; exit 0; fi
SCRIPT_NAME=check-image
PLACEMENT_NODES=""
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
case "${1:-}" in ""|-*) usage_die "image check requires a spec ID; see ./pulsar image --help" ;; esac
NAME="$1"; shift
JSON=0 NODE_SELECTOR=""
# The operator's selection, repeated in a suggested image stage command.
selection_flags=()
while [ $# -gt 0 ]; do
  case "$1" in
    --placement-nodes) [ -n "${2:-}" ] || usage_die "--placement-nodes requires an ordered node list"; PLACEMENT_NODES="$2"; selection_flags+=("$1" "$2"); shift ;;
    --json) JSON=1 ;;
    --node) case "${2:-}" in ""|-*) usage_die "--node requires a node" ;; esac; NODE_SELECTOR="$2"; selection_flags+=("$1" "$2"); shift ;;
    --spec-file) case "${2:-}" in ""|-*) usage_die "--spec-file requires a file" ;; esac; export PULSAR_SPEC_FILE="$2"; selection_flags+=("$1" "$2"); shift ;;
    *) usage_die "unknown argument: $1" ;;
  esac
  shift
done
# From here an unexpected exit is a check that could not run; usage_die keeps its status.
check_exit_convention keep-usage
load_conf "$NAME"
[ "$NODES" != 1 ] || [ -z "$PLACEMENT_NODES" ] || usage_die "--placement-nodes requires a multi-node spec; use --node"
node_indices=() states=() present=()
topology_ready=1
if ! require_cluster_nodes "$NODES" >/dev/null 2>&1; then topology_ready=0; fi
if [ "$NODES" = 1 ] && [ "$topology_ready" = 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" || die "selected node is not confirmed" 3
  node_indices=("$SINGLE_NODE_INDEX")
else
  resolve_serving_placement "$NODE_SELECTOR" "$PLACEMENT_NODES" || die "selected serving nodes are not confirmed" 3
  node_indices=("${SERVING_NODE_INDEXES[@]}")
fi
verify_image_json() {
  python3 -c '
import json,sys
try:
 d=json.load(sys.stdin)
 if isinstance(d,list) and len(d)==1:d=d[0]
 if not isinstance(d,dict) or not d.get("Id"):raise ValueError("image identity missing")
 if sys.argv[1] not in d.get("RepoDigests",[]):raise ValueError("image repository/digest reference differs")
 if d.get("Architecture")!="arm64":raise ValueError("image architecture is not arm64")
except (ValueError,TypeError,AttributeError) as exc:
 print(str(exc),file=sys.stderr);raise SystemExit(1)
' "$IMAGE"
}
# The pinned content under another repository name: where the image store
# makes the image ID the manifest digest (the containerd store), the digest
# finds it. Prints the name it is present as.
verify_digest_json() {
  python3 -c '
import json,sys
try:
 d=json.load(sys.stdin)
 if isinstance(d,list) and len(d)==1:d=d[0]
 if not isinstance(d,dict) or d.get("Id")!=sys.argv[1]:raise ValueError("image identity differs")
 if d.get("Architecture")!="arm64":raise ValueError("image architecture is not arm64")
 names=[n for n in (d.get("RepoTags") or [])+(d.get("RepoDigests") or []) if isinstance(n,str) and n and not n.startswith("<none>")]
 print(names[0] if names else "an untagged image")
except (ValueError,TypeError,AttributeError) as exc:
 print(str(exc),file=sys.stderr);raise SystemExit(1)
' "$DIGEST"
}
DIGEST=""
[[ "$IMAGE" != *@sha256:* ]] || DIGEST="${IMAGE##*@}"
# image_on PHYSICAL ARGS... — docker on this node or over management SSH.
image_on() {
  local physical="$1"
  shift
  if [ "$physical" = 0 ]; then "$PULSAR_DOCKER" "$@"; else ssh_node "$physical" "$(shell_join_q docker "$@")"; fi
}
for ((rank=0;rank<NODES;rank++)); do
  present+=("")
  if [ "$topology_ready" != 1 ]; then states+=(need-topology); continue; fi
  physical="${node_indices[$rank]}"
  if [ "$physical" = 0 ]; then
    if ! "$PULSAR_DOCKER" info >/dev/null 2>&1; then states+=(docker-error); continue; fi
  else
    if ! ssh_node "$physical" true >/dev/null 2>&1; then states+=(unreachable); continue; fi
    if ! ssh_node "$physical" 'docker info >/dev/null 2>&1'; then states+=(docker-error); continue; fi
  fi
  if raw=$(image_on "$physical" image inspect --format '{{json .}}' "$IMAGE" 2>/dev/null); then
    if printf '%s' "$raw" | verify_image_json 2>/dev/null; then states+=(ok); else states+=(docker-error); fi
  elif [ -n "$DIGEST" ] && raw=$(image_on "$physical" image inspect --format '{{json .}}' "$DIGEST" 2>/dev/null) \
      && name=$(printf '%s' "$raw" | verify_digest_json 2>/dev/null); then
    states+=(other-name); present[rank]="$name"
  else states+=(missing); fi
done
report=$(python3 - "$REPO_DIR" "$NAME" "$IMAGE" "$NODES" "${node_indices[*]}" "${states[@]}" -- "${present[@]}" <<'PY'
import json,sys
sys.path.insert(0,sys.argv[1])
from scripts.launch_plan import rank_image_aggregate_state
name,image,nodes,indices=sys.argv[2:6];rest=sys.argv[6:];indices=indices.split()
states,present=rest[:rest.index('--')],rest[rest.index('--')+1:]
state=rank_image_aggregate_state(states)
# A rank holding the content under another name lacks the pinned reference.
lacking=('missing','other-name')
if state=='missing-on-rank' and states[0] in lacking:
 state='missing-both' if any(value in lacking for value in states[1:]) else 'missing-on-head'
def rank(i,value):
 row=dict(rank=i,topology_index=int(indices[i]),state=value,ok=value=='ok')
 if value=='other-name':row['present_as']=present[i]
 return row
print(json.dumps(dict(schema_version=1,kind='pulsar-image-check',model=name,image=image,nodes=int(nodes),state=state,head_ok=states[0]=='ok',worker_ok=len(states)>1 and states[1]=='ok',ranks=[rank(i,value) for i,value in enumerate(states)])))
PY
)
state=$(printf '%s' "$report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
# image_failure — what the check found, by node, and one next step. Start runs
# this check too and gives its own next step in its blocker lines.
image_failure() {
  local rank where missing="" other="" unreachable="" docker="" topology=0 controller=0 next="" message="" problem
  local -a problems=() stage=(${selection_flags[@]+"${selection_flags[@]}"})
  for ((rank = 0; rank < NODES; rank++)); do
    [ "${states[$rank]}" != need-topology ] || { topology=1; continue; }
    where="$(human_node_name "${node_indices[$rank]}") (rank $rank)"
    case "${states[$rank]}" in
      missing) missing+="${missing:+, }$where"; [ "${node_indices[$rank]}" != 0 ] || controller=1 ;;
      other-name) other+="${other:+; }$where as ${present[$rank]}" ;;
      unreachable) unreachable+="${unreachable:+, }$where" ;;
      docker-error) docker+="${docker:+, }$where" ;;
    esac
  done
  if [ "$topology" = 1 ]; then
    problems+=("the confirmed topology has fewer nodes than this spec needs"); next="./pulsar topology setup"
  fi
  if [ -n "$unreachable" ]; then
    problems+=("$unreachable could not be reached, so the image there is unknown"); next="${next:-./pulsar topology check}"
  fi
  if [ -n "$docker" ]; then
    problems+=("on $docker, Docker is unavailable or holds an image other than the pinned arm64 digest")
    next="${next:-./pulsar doctor}"
  fi
  [ -z "$missing" ] || problems+=("the pinned image is missing on $missing")
  # A copy from this node can drop registry references; a pull records the
  # pinned name and reuses the layers already there.
  [ -z "$other" ] || problems+=("the pinned content is present on $other, but the pinned name is not recorded; staging with --pull adds the pinned name, and the content is already here")
  if [ -n "$missing$other" ]; then
    # Staging copies from this node, so when it lacks the image only a pull works.
    [ "$controller" = 0 ] && [ -z "$other" ] || stage+=(--pull)
    next="${next:-./pulsar image stage $(python3 -c 'import shlex,sys; print(shlex.join(sys.argv[1:]))' "$NAME" ${stage[@]+"${stage[@]}"}) --plan}"
  fi
  for problem in "${problems[@]}"; do message+="${message:+; }$problem"; done
  print_hanging 'FAIL  image  ' "$message"
  # The command stays whole on its own line so it can be copied.
  [ -n "${PULSAR_RUN_BY_START:-}" ] || [ -z "$next" ] || printf '             Next: %s\n' "$next"
}
if [ "$JSON" = 1 ]; then printf '%s\n' "$report"; else
  if [ "$state" = ok ]; then print_hanging 'PASS  image  ' "Pinned arm64 image verified on all $NODES serving ranks."; else
    image_failure
  fi
fi
[ "$state" = ok ] || check_result 1
