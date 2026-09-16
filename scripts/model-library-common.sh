#!/usr/bin/env bash
# Shared control-plane adapters. Source only; no model/storage actions on import.

model_physical_rank() {
  local selector="${1:?node selector required}" rank found=""
  require_cluster_nodes 1 >/dev/null || return 1
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    if [ "$selector" = "${CLUSTER_NODE_IDS[$rank]}" ] || [ "$selector" = "$rank" ]; then
      [ -z "$found" ] || die "node selector is ambiguous"
      found="$rank"
    fi
  done
  [ -n "$found" ] || die "node is not in confirmed topology"
  printf '%s\n' "$found"
}

model_node() {
  local rank="${1:?physical rank required}" request="${2:?request JSON required}"
  require_cluster_nodes "$((rank+1))" >/dev/null || return 1
  model_node_program "$rank" "$request"
}

model_node_command() {
  local rank="${1:?node required}" bootstrap
  local -a SSH_NODE_COMMAND=()
  bootstrap=$(python3 "$REPO_DIR/scripts/node-bundle.py" --bootstrap) || return 2
  if [ "$rank" = local ] || [ "$rank" = 0 ]; then
    MODEL_NODE_COMMAND=("${PULSAR_NODE_PYTHON:-python3}" -c "$bootstrap")
  else
    require_topology_ssh_trust >/dev/null || return 2
    ssh_node_command "$rank"
    MODEL_NODE_COMMAND=("${SSH_NODE_COMMAND[@]}" "$(shell_join_q "${PULSAR_NODE_PYTHON:-python3}" -c "$bootstrap")")
  fi
}

model_node_program() {
  local rank="${1:?node required}" request="${2:?request JSON required}" program
  local -a MODEL_NODE_COMMAND=()
  program=$(printf '%s' "$request" | python3 "$REPO_DIR/scripts/node-bundle.py" --supervised) || return 2
  model_node_command "$rank" || return $?
  printf '%s' "$program" | python3 -m model_library.verification_process --owner "$$" -- "${MODEL_NODE_COMMAND[@]}"
}

model_container_observation() {
  local rank="${1:?}" raw command
  # Empty ID list is valid only after docker ps itself succeeded.
  command='set -eu; ids=$(docker ps -aq); if [ -z "$ids" ]; then printf "[]\n"; else docker inspect $ids; fi'
  if [ "$rank" -eq 0 ]; then
    local ids
    ids=$("${PULSAR_DOCKER:-docker}" ps -aq) || return 2
    if [ -z "$ids" ]; then raw='[]'; else
      local -a id_array=()
      mapfile -t id_array <<<"$ids"
      raw=$("${PULSAR_DOCKER:-docker}" inspect "${id_array[@]}") || return 2
    fi
  else
    require_topology_ssh_trust >/dev/null || return 2
    raw=$(ssh_node "$rank" "$command" </dev/null) || return $?
  fi
  printf '%s' "$raw" | python3 -c '
import json,sys
node=sys.argv[1]
raw=json.load(sys.stdin)
if not isinstance(raw,list): raise SystemExit("container inspection is not a list")
rows=[]
for item in raw:
 if not isinstance(item,dict) or not isinstance(item.get("Mounts"),list) or not item.get("Id"):
  raise SystemExit("container inspection is incomplete")
 mounts=[]
 for mount in item["Mounts"]:
  if not isinstance(mount,dict) or not isinstance(mount.get("Source"),str): raise SystemExit("mount source is unobservable")
  mounts.append(mount["Source"])
 rows.append({"id":item["Id"],"mounts":mounts,"running":(item.get("State") or {}).get("Running"),"labels":(item.get("Config") or {}).get("Labels") or {}})
print(json.dumps({"node_id":node,"observable":True,"containers":rows}))
' "${CLUSTER_NODE_IDS[$rank]}"
}

model_json() {
  # A key ending ':' takes a JSON value; other values remain exact strings.
  # Shared-copy inventories can exceed the per-argument exec limit. Bash emits
  # NUL-delimited fields on stdin without putting the JSON in Python argv.
  { [ "$#" -eq 0 ] || printf '%s\0' "$@"; } | python3 -c '
import json,sys
args=sys.stdin.read().split("\0")[:-1]
if len(args)%2: raise SystemExit("JSON fields need key/value pairs")
d={}
for key,value in zip(args[::2],args[1::2]):
 if key.endswith(":"): d[key[:-1]]=json.loads(value)
 else: d[key]=value
print(json.dumps(d,separators=(",",":")))
'
}

model_ctl() {
  local request="${1:?controller request required}"
  printf '%s' "$request" | python3 -m model_library.controller \
    --state-root "$PULSAR_MODEL_LIBRARY_DIR" --repo-root "$REPO_DIR"
}

model_node_request() {
  local operation="${1:?}"; shift
  local -a fields=(operation "$operation" view_schema: "${VIEW_SCHEMA:-1}")
  [ -z "${MANIFEST_JSON:-}" ] || fields+=(manifest: "$MANIFEST_JSON")
  if [ -n "${OVERLAY_CACHE_ROOT:-}" ]; then fields+=(home_root "$OVERLAY_CACHE_ROOT")
  elif [ -n "${PULSAR_HOME_ROOT:-}" ]; then fields+=(home_root "$PULSAR_HOME_ROOT"); fi
  [ "${PULSAR_HOT_ROOT_EXPLICIT:-0}" != 1 ] || fields+=(view_root "$PULSAR_HOT_ROOT")
  model_json "${fields[@]}" "$@"
}
