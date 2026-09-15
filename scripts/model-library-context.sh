#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

local_node() { printf '%s' "$1" | "${PULSAR_NODE_PYTHON:-python3}" -m model_library.node; }

resolve_selected() {
  local request result
  request=$(model_json operation resolve spec_id "$SPEC_ID" spec_file "$SPEC_FILE" manifest_file "$MANIFEST_FILE" snapshot "$SNAPSHOT")
  result=$(model_ctl "$request") || return 2
  MANIFEST_JSON=$(json_fields "$result" manifest)
  MANIFEST_ID=$(json_fields "$MANIFEST_JSON" manifest_id)
  SPEC_JSON=$(json_fields "$result" spec)
  SNAPSHOTS_JSON=$(json_fields "$result" snapshots)
  if [ "$SPEC_JSON" != null ] && [ -n "$SPEC_JSON" ] && [ "$(json_fields "$SPEC_JSON" schema_version)" = 3 ]; then VIEW_SCHEMA=2; fi
  MODEL_ID=$(json_fields "$MANIFEST_JSON" model_id)
  REVISION=$(json_fields "$MANIFEST_JSON" snapshot_revision)
  if [ -n "$SPEC_JSON" ] && [ "$SPEC_JSON" != null ]; then
    SPEC_ID=$(json_fields "$SPEC_JSON" spec_id)
    [ -z "$SPEC_FILE" ] || export PULSAR_SPEC_FILE="$SPEC_FILE"
    load_conf "$SPEC_ID"
    NODE=$(spec_overlay_node_selector "$NODE")
  fi
}

load_home() {
  local result
  result=$(model_ctl "$(model_json operation home snapshot_manifest_id "$MANIFEST_ID")") || return 2
  HOME_JSON=$(json_fields "$result" home)
  [ -n "$HOME_JSON" ] || HOME_JSON=null
}

require_home() { load_home || return 2; [ "$HOME_JSON" != null ] || die "no home is registered; acquire or restore the exact snapshot"; }

selected_nodes() {
  local rank
  require_cluster_nodes "${NODES:-1}" >/dev/null || die "confirmed topology is required"
  SELECTED_RANKS=() SELECTED_IDS=()
  if [ "${NODES:-1}" -eq 1 ]; then
    resolve_single_node_placement "$NODE" || die "cannot resolve selected placement"
    SELECTED_RANKS=("$SINGLE_NODE_INDEX")
  else
    [ -z "$NODE" ] || die "--node selects a one-node placement only"
    for ((rank=0; rank<NODES; rank++)); do SELECTED_RANKS+=("$rank"); done
  fi
  for rank in "${SELECTED_RANKS[@]}"; do SELECTED_IDS+=("${CLUSTER_NODE_IDS[$rank]}"); done
  NODE_IDS_JSON=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${SELECTED_IDS[@]}")
}

all_observations() {
  local rank value temp
  require_cluster_nodes 1 >/dev/null || die "confirmed topology is required"
  temp=$(mktemp)
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    if ! value=$(model_container_observation "$rank"); then rm -f "$temp"; return 255; fi
    printf '%s\n' "$value" >>"$temp"
  done
  python3 -c 'import json,sys; print(json.dumps([json.loads(x) for x in open(sys.argv[1]) if x.strip()]))' "$temp" || { local rc=$?; rm -f "$temp"; return "$rc"; }
  rm -f "$temp"
}

all_node_ids() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${CLUSTER_NODE_IDS[@]}"
}

verify_record() {
  local record="$1" full="${2:-$FULL}" rank result
  if [ -n "${PREPARE_VERIFICATION_DIR:-}" ]; then
    local cached
    cached=$(printf '%s' "$record" | python3 -m model_library.preparation_verification lookup "$PREPARE_VERIFICATION_DIR") || return 2
    # Reuse a result from this invocation when available; otherwise keep the
    # requested mode and let the node validate the record's existing stamp.
    if [ "$cached" != null ]; then record="$cached"; full=0; fi
  fi
  select_record_manifest "$record" || return 2
  rank=$(model_physical_rank "$(json_fields "$record" node_id)") || return 255
  result=$(model_node "$rank" "$(model_node_request verify for_runtime: true path "$(json_fields "$record" path)" stamp: "$(json_fields "$record" verification)" full: "$([ "$full" = 1 ] && echo true || echo false)")") || return $?
  local refreshed
  refreshed=$(printf '%s' "$record" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["verification"]=json.loads(sys.argv[1])["verification"]; from model_library.state import now; d["verified_at"]=now() if d["verification"]["method"]=="sha256" else d["verified_at"]; print(json.dumps(d))' "$result") || return 2
  # A hash performed during planning is persisted only once execution begins,
  # even when its subsequent filesystem check used matching metadata.
  if [ "${PLAN:-0}" -eq 0 ] && { [ "$(json_fields "$refreshed" verification.method)" = sha256 ] || [ "$(json_fields "$refreshed" verified_at)" != "$(json_fields "$1" verified_at)" ]; }; then
    model_ctl "$(model_json operation refresh-verification record: "$refreshed")" >/dev/null || return 2
    if [ "$(json_fields "$refreshed" kind)" = pulsar-prepared-view ]; then
      model_node "$rank" "$(model_node_request refresh-view view: "$refreshed")" >/dev/null || return $?
    fi
  fi
  remember_preparation_verification "$refreshed" || return 2
  printf '%s\n' "$refreshed"
}

remember_preparation_verification() {
  [ -n "${PREPARE_VERIFICATION_DIR:-}" ] || return 0
  printf '%s' "$1" | python3 -m model_library.preparation_verification remember "$PREPARE_VERIFICATION_DIR"
}

prepared_snapshot_info() {
  local views rank node row verified temp home_node found=0
  require_home
  selected_nodes
  home_node=$(json_fields "$HOME_JSON" node_id)
  for node in "${SELECTED_IDS[@]}"; do [ "$node" != "$home_node" ] || found=1; done
  [ "$found" -eq 1 ] || { echo 'home is outside selected serving nodes; explicitly move it first' >&2; return 1; }
  HOME_JSON=$(verify_record "$HOME_JSON") || return $?
  views=$(model_ctl "$(model_json operation views spec_id "$SPEC_ID")") || return 2
  temp=$(mktemp)
  for ((rank=0; rank<${#SELECTED_IDS[@]}; rank++)); do
    node="${SELECTED_IDS[$rank]}"
    row=$(printf '%s' "$views" | python3 -c '
import json,sys
rows=[r for r in json.load(sys.stdin) if r["node_id"]==sys.argv[1] and r["rank"]==int(sys.argv[2]) and r["topology_id"]==sys.argv[3] and r["snapshot_manifest_id"]==sys.argv[4]]
if len(rows)!=1: raise SystemExit(1)
print(json.dumps(rows[0]))
' "$node" "$rank" "$CLUSTER_TOPOLOGY_ID" "$MANIFEST_ID") || { rm -f "$temp"; echo 'required prepared copy is missing or placement changed' >&2; return 1; }
    if [ "$node" = "$home_node" ]; then
      if [ "$(json_fields "$row" is_home_view)" != true ] || [ "$(json_fields "$row" path)" != "$(json_fields "$HOME_JSON" path)" ] || [ "$(json_fields "$row" hub_path)" != "$(json_fields "$HOME_JSON" hub_path)" ]; then
        rm -f "$temp"; echo 'home node must use its verified home directly' >&2; return 2
      fi
      verified=$(printf '%s' "$row" | python3 -c 'import json,sys; r=json.load(sys.stdin); h=json.loads(sys.argv[1]); r["verification"]=h["verification"]; r["verified_at"]=h["verified_at"]; print(json.dumps(r))' "$HOME_JSON") || { rm -f "$temp"; return 2; }
    else
      [ "$(json_fields "$row" is_home_view)" != true ] || { rm -f "$temp"; echo 'non-home node requires a verified working copy' >&2; return 2; }
      verified=$(verify_record "$row") || { local rc=$?; rm -f "$temp"; return "$rc"; }
    fi
    printf '%s\n' "$verified" >>"$temp"
  done
  python3 -c '
import json,sys
rows=[json.loads(x) for x in open(sys.argv[1])]
print(json.dumps({"schema_version":1,"kind":"pulsar-prepared-set","spec_id":sys.argv[2],"snapshot_manifest_id":sys.argv[3],"topology_id":sys.argv[4],"home_node_id":sys.argv[5],"revision":sys.argv[6],"home":json.loads(sys.argv[7]),"ranks":rows}))
' "$temp" "$SPEC_ID" "$MANIFEST_ID" "$CLUSTER_TOPOLOGY_ID" "$home_node" "$REVISION" "$HOME_JSON" || { local rc=$?; rm -f "$temp"; return "$rc"; }
  rm -f "$temp"
}

require_archive_root() {
  [ -n "${PULSAR_COLD_ROOT:-}" ] || die "archive location is not configured; use ./pulsar configure archive-root"
  [ -d "$PULSAR_COLD_ROOT" ] || die "configured archive directory must already exist"
}

archive_snapshot_verify() {
  require_archive_root
  local_node "$(model_node_request archive-verify archive_root "$PULSAR_COLD_ROOT")"
}

save_home_result() {
  local result="$1" old="${2:-null}" registered_home
  registered_home=$(json_fields "$result" home)
  model_ctl "$(model_json operation save-home manifest: "$MANIFEST_JSON" home: "$registered_home" expected_home: "$old")"
}

emit_result() {
  if [ "$JSON" -eq 1 ]; then
    printf '%s' "$1" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert isinstance(value,dict), "operation returned no result object"; print(json.dumps(value,sort_keys=True))'
  else
    printf '%s' "$1" | python3 -m model_library.render --operation "$OP" --archive-action "$ARCHIVE_ACTION"
  fi
}


select_snapshot() {
  local name="$1"
  MANIFEST_JSON=$(printf '%s' "$SNAPSHOTS_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]["snapshot_manifest"]))' "$name") || return 2
  MANIFEST_ID=$(json_fields "$MANIFEST_JSON" manifest_id)
  MODEL_ID=$(json_fields "$MANIFEST_JSON" model_id)
  REVISION=$(json_fields "$MANIFEST_JSON" snapshot_revision)
}

snapshot_names() {
  printf '%s' "$SNAPSHOTS_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("\n".join(["target"]+sorted(k for k in d if k!="target")))'
}

select_record_manifest() {
  local record="$1" selected
  # During per-manifest recovery a complete recipe need not have been selected.
  [ -n "$SNAPSHOTS_JSON" ] && [ "$SNAPSHOTS_JSON" != null ] || return 0
  selected=$(printf '%s' "$SNAPSHOTS_JSON" | python3 -c 'import json,sys; r=json.loads(sys.argv[1]); print(next(k for k,v in json.load(sys.stdin).items() if v["snapshot_manifest"]["manifest_id"]==r["snapshot_manifest_id"]))' "$record") || return 2
  select_snapshot "$selected"
}

prepared_info() {
  if [ "$VIEW_SCHEMA" != 2 ]; then prepared_snapshot_info; return; fi
  selected_nodes || return 2
  local name value tmp
  tmp=$(mktemp)
  while IFS= read -r name <&3; do
    select_snapshot "$name" || { rm -f "$tmp"; return 2; }
    value=$(prepared_snapshot_info) || { local rc=$?; rm -f "$tmp"; echo "required snapshot $name is not ready" >&2; return "$rc"; }
    printf '%s\n' "$(model_json name "$name" prepared: "$value")" >>"$tmp"
  done 3< <(snapshot_names)
  python3 - "$tmp" "$SPEC_ID" "$CLUSTER_TOPOLOGY_ID" <<'PYCODE'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1])]
print(json.dumps({'schema_version':2,'kind':'pulsar-prepared-set','spec_id':sys.argv[2],
                  'topology_id':sys.argv[3],'snapshots':{r['name']:r['prepared'] for r in rows}}))
PYCODE
  local rc=$?
  rm -f "$tmp"
  select_snapshot "${SNAPSHOT:-target}"
  return "$rc"
}

archive_verify() {
  if [ "$VIEW_SCHEMA" != 2 ] || [ -n "$SNAPSHOT" ]; then archive_snapshot_verify; return; fi
  local name result tmp
  tmp=$(mktemp)
  while IFS= read -r name <&3; do
    select_snapshot "$name" || { rm -f "$tmp"; return 2; }
    result=$(archive_snapshot_verify) || { rm -f "$tmp"; echo "archive for required snapshot $name did not verify" >&2; return 1; }
    printf '%s\n' "$(model_json name "$name" verification: "$result")" >>"$tmp"
  done 3< <(snapshot_names)
  python3 - "$tmp" "$SPEC_ID" <<'PYCODE'
import json,sys
print(json.dumps({'schema_version':2,'kind':'pulsar-archive-verification','spec_id':sys.argv[2],
 'snapshots':{r['name']:r['verification'] for r in map(json.loads,open(sys.argv[1]))},'verified':True}))
PYCODE
  local rc=$?
  rm -f "$tmp"
  select_snapshot target
  return "$rc"
}
