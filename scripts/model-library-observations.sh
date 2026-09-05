#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

check_model() {
  local result rc=0 details local_state=unknown home_state=unknown archive_state=unknown prepared=0 required="${NODES:-1}" observation error_file
  error_file=$(mktemp)
  result=$(prepared_info 2>"$error_file") || rc=$?
  if [ "$rc" -eq 0 ]; then
    local_state=ready; home_state=verified; prepared="$required"
  else
    load_home || true
    if [ "$HOME_JSON" = null ]; then
      local_state=missing; home_state=missing
    elif [ "$rc" -eq 255 ]; then
      local_state=unknown
    elif [ "$rc" -eq 1 ]; then
      local_state=missing
    else
      local_state=changed
    fi
  fi
  details=$(python3 -c 'import json,sys; s=open(sys.argv[1]).read().strip(); print(json.dumps([s] if s else []))' "$error_file")
  rm -f "$error_file"
  if [ -z "${PULSAR_COLD_ROOT:-}" ]; then
    archive_state=not-configured
  elif [ "$FULL" -eq 1 ]; then
    if result=$(archive_verify 2>/dev/null); then
      archive_state=verified
      model_ctl "$(model_json operation archive-record snapshot_manifest_id "$MANIFEST_ID" root "$PULSAR_COLD_ROOT" result: "$result")" >/dev/null
    else archive_state=unavailable; fi
  else
    archive_state=$(python3 -c '
import json,sys
from pathlib import Path
from model_library.local import location,payload
from model_library.integrity import read_json
m=json.loads(sys.argv[2]); root=Path(sys.argv[1])
try:
 if not root.is_dir(): print("unavailable")
 else:
  hub=location(root,m,archive=True)
  if not hub.exists() and not hub.is_symlink(): print("missing")
  elif read_json(hub/"manifest.json")!=m or not payload(hub,m).is_dir(): print("unavailable")
  else: print("present")
except (ValueError,OSError): print("unavailable")
' "$PULSAR_COLD_ROOT" "$MANIFEST_JSON")
  fi
  observation=$(model_json local_state "$local_state" home "$home_state" prepared: "$(model_json verified: "$prepared" required: "$required")" blockers: "$details" archive_state "$archive_state")
  model_ctl "$(model_json operation save-observation spec_id "$SPEC_ID" observation: "$observation")" >/dev/null || die "could not save catalog observation"
  emit_result "$(model_json spec_id "$SPEC_ID" observation: "$observation")"
  return "$rc"
}

show_budget() {
  local rank roots space result='[]'
  require_cluster_nodes 1 >/dev/null || die "budget inspection requires confirmed topology"
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    roots=$(model_node "$rank" "$(model_node_request roots)") || die "node storage is unobservable"
    space=$(model_node "$rank" "$(model_node_request space path "$(json_fields "$roots" view_root)")") || die "node storage usage is unobservable"
    result=$(printf '%s' "$result" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"node_id":sys.argv[1],**json.loads(sys.argv[2])}); print(json.dumps(a))' "${CLUSTER_NODE_IDS[$rank]}" "$space")
  done
  emit_result "$(model_json kind pulsar-storage-budget nodes: "$result")"
}
