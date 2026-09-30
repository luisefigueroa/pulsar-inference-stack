#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

local_node() { model_node_program local "$1"; }

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

# Acquire, restore and move place a home on NODE: the operator's --node or, for
# a one-node spec without it, the deployment overlay's placement. Resolve it
# once, by the rules prepare and start use (the hostname that output shows,
# node ID, SSH host, control IP, rank key or rank number), to the node_id that
# home records store. model_physical_rank then matches NODE exactly as it
# matches a record. A value naming no node, or several, is refused before any
# node is contacted; the resolver's warning says which.
resolve_node_selector() {
  local node_id source="--node '$NODE'" fix="use a hostname or node ID from ./pulsar topology show"
  [ -n "$NODE" ] || return 0
  if [ "$NODE" = "${OVERLAY_PLACEMENT_NODE_ID:-}" ]; then
    source="overlay placement.node_id '$NODE'" fix="correct the placement in the deployment overlay"
  fi
  # The subshell leaves this shell's SINGLE_NODE_* placement variables untouched.
  node_id=$(resolve_single_node_placement "$NODE" >/dev/null && printf '%s\n' "$SINGLE_NODE_ID") || node_id=""
  [ -n "$node_id" ] || die "$source does not select exactly one confirmed node; $fix"
  NODE="$node_id"
}

selected_nodes() {
  resolve_serving_placement "$NODE" "${PLACEMENT_NODES:-}" || die "cannot resolve confirmed serving placement"
  SELECTED_RANKS=("${SERVING_NODE_INDEXES[@]}") SELECTED_IDS=("${SERVING_NODE_IDS[@]}")
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
  refresh_verified_record "$1" "$record" "$result"
}

refresh_verified_record() {
  local original="$1" record="$2" result="$3" completed="${4:-}" rank refreshed
  select_record_manifest "$record" || return 2
  rank=$(model_physical_rank "$(json_fields "$record" node_id)") || return 255
  refreshed=$(printf '%s' "$record" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["verification"]=json.loads(sys.argv[1])["verification"]; from model_library.state import now; d["verified_at"]=(sys.argv[2] or now()) if d["verification"]["method"]=="sha256" else d["verified_at"]; print(json.dumps(d))' "$result" "$completed") || return 2
  # A hash performed during planning is persisted only once execution begins,
  # even when its subsequent filesystem check used matching metadata.
  if [ "${PLAN:-0}" -eq 0 ] && { [ "$(json_fields "$refreshed" verification.method)" = sha256 ] || [ "$(json_fields "$refreshed" verified_at)" != "$(json_fields "$original" verified_at)" ]; }; then
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
  python3 -m model_library.inspection member "$PREPARED_INSPECTION_DIR" "${CHECK_SNAPSHOT:-target}"
}

require_archive_root() {
  [ -n "${PULSAR_COLD_ROOT:-}" ] || die "archive location is not configured; use ./pulsar configure archive-root"
  [ -d "$PULSAR_COLD_ROOT" ] || die "configured archive location must already exist"
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

# phase STEP TOTAL TEXT
# Progress for long operations, on stderr only. Previews (--plan) stay quiet;
# stdout results, including JSON, are unchanged.
phase() {
  [ "$PLAN" -eq 0 ] || return 0
  local name="$OP"
  [ "$OP" != archive ] || name="archive $ARCHIVE_ACTION"
  printf '[%s %s/%s] %s\n' "$name" "$1" "$2" "$3" >&2
}

# source_summary SOURCE_JSON — "MODEL @ COMMIT (N files, X GiB)" for progress.
source_summary() {
  printf '%s' "$1" | python3 -c '
import json,sys
s=json.load(sys.stdin); files=s.get("files") or []
sizes=[f.get("size") for f in files if isinstance(f,dict)]
size=f", {sum(sizes)/1024**3:.1f} GiB" if sizes and all(type(x) is int for x in sizes) else ""
model=s.get("model_id","model"); commit=str(s.get("snapshot_revision",""))[:8]
print(f"{model} @ {commit} ({len(files)} files{size})")'
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

prepared_info() (
  local work rc=0
  work=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-inspection.XXXXXX")
  trap 'rm -rf "$work"' EXIT
  inspect_prepared "$work" || rc=$?
  if [ "$rc" -ne 0 ]; then
    [ ! -f "$work/members.json" ] || python3 -m model_library.inspection errors "$work"
    return "$rc"
  fi
  cat "$work/prepared.json"
)

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
