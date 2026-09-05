#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

archive_create() {
  require_cluster_nodes 1 >/dev/null || die "archive creation requires confirmed topology"
  require_archive_root
  require_home
  local rank result stage
  if [ "$PLAN" -eq 1 ]; then
    emit_result "$(model_json kind pulsar-archive-plan snapshot_manifest_id "$MANIFEST_ID" home: "$HOME_JSON" archive_root "$PULSAR_COLD_ROOT")"; return
  fi
  [ "$YES" -eq 1 ] || die "archive creation requires --yes"
  HOME_JSON=$(verify_record "$HOME_JSON" 1) || die "home verification failed"
  rank=$(model_physical_rank "$(json_fields "$HOME_JSON" node_id)")
  # An existing archive is never replaced. Corruption requires explicit inspection.
  local exists
  exists=$(python3 -c 'from pathlib import Path; import sys; from model_library.local import location; import json; p=location(sys.argv[1],json.loads(sys.argv[2]),archive=True); print(int(p.exists() or p.is_symlink()))' "$PULSAR_COLD_ROOT" "$MANIFEST_JSON")
  if [ "$exists" -eq 1 ]; then
    result=$(archive_verify) || die "existing archive did not verify; it was not replaced"
  elif [ "$rank" -eq 0 ]; then
    result=$(local_node "$(model_node_request archive path "$(json_fields "$HOME_JSON" path)" archive_root "$PULSAR_COLD_ROOT")") || die "archive copy failed"
  else
    . "$REPO_DIR/scripts/model-transfer.sh"
    stage=$(local_node "$(model_node_request begin-archive archive_root "$PULSAR_COLD_ROOT")") || die "archive staging failed"
    model_transfer "$rank" "$(json_fields "$HOME_JSON" path)" 0 "$(json_fields "$stage" path)" "$MANIFEST_JSON" || die "archive transfer incomplete"
    result=$(local_node "$(model_node_request publish-archive archive_root "$PULSAR_COLD_ROOT" stage "$(json_fields "$stage" stage)")") || die "archive was not published"
  fi
  result=$(model_ctl "$(model_json operation archive-record snapshot_manifest_id "$MANIFEST_ID" root "$PULSAR_COLD_ROOT" result: "$result")") || return 2
  emit_result "$result"
}

restore_model() {
  require_archive_root
  local rank existing stage result archive_path previous
  require_cluster_nodes 1 >/dev/null || die "restoration requires confirmed topology"
  load_home || die "existing home registration cannot be inspected"
  previous="$HOME_JSON"
  rank=$(model_physical_rank "${NODE:-0}")
  if [ -n "$SPEC_JSON" ] && [ "$SPEC_JSON" != null ] && [ "${NODES:-1}" -gt 1 ] && [ "$rank" -ge "$NODES" ]; then die "selected home node is outside exact serving geometry"; fi
  existing=$(find_source_homes) || die "all-node home presence is unobservable"
  [ "$(printf '%s' "$existing" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" -eq 0 ] || die "a home exists; verify/reuse it instead of replacing it"
  if [ "$PLAN" -eq 1 ]; then
    emit_result "$(model_json kind pulsar-restore-plan snapshot_manifest_id "$MANIFEST_ID" selected_node "${CLUSTER_NODE_IDS[$rank]}" archive_root "$PULSAR_COLD_ROOT")"; return
  fi
  [ "$YES" -eq 1 ] || die "restoration requires --yes"
  archive_verify >/dev/null || die "archive does not match the selected manifest"
  stage=$(model_node "$rank" "$(model_node_request begin-home)") || die "restore staging failed"
  archive_path=$(python3 -c 'from model_library.local import location,payload; import json,sys; m=json.loads(sys.argv[2]); print(payload(location(sys.argv[1],m,archive=True),m))' "$PULSAR_COLD_ROOT" "$MANIFEST_JSON")
  if [ "$rank" -eq 0 ]; then
    python3 -c 'from model_library.local import copy_files; from pathlib import Path; import json,sys; copy_files(Path(sys.argv[1]),Path(sys.argv[2]),json.loads(sys.argv[3]))' "$archive_path" "$(json_fields "$stage" path)" "$MANIFEST_JSON" || die "restore copy failed"
  else
    . "$REPO_DIR/scripts/model-transfer.sh"
    model_transfer 0 "$archive_path" "$rank" "$(json_fields "$stage" path)" "$MANIFEST_JSON" || die "restore transfer failed"
  fi
  existing=$(find_source_homes) || die "cannot recheck all-node home absence"
  [ "$(printf '%s' "$existing" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" -eq 0 ] || die "another home appeared; restore remains staged"
  result=$(model_node "$rank" "$(model_node_request publish-home stage "$(json_fields "$stage" stage)" node_id "${CLUSTER_NODE_IDS[$rank]}")") || die "restore publication failed"
  result=$(save_home_result "$result" "$previous") || die "verified files were published but home registration failed; inspect before retrying"
  emit_result "$result"
}
