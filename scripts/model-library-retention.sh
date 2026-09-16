#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

merged_views() {
  local controller node_records request scope="${1:-spec}"
  case "$scope" in
    spec) request=$(model_json operation views spec_id "$SPEC_ID") ;;
    snapshot) request=$(model_json operation views snapshot_manifest_id "$MANIFEST_ID") ;;
    all) request=$(model_json operation views) ;;
    *) return 2 ;;
  esac
  controller=$(model_ctl "$request") || return 2
  node_records=$(collect_node_views "$scope") || return $?
  printf '%s\0%s' "$controller" "$node_records" | python3 -c '
import json,sys
from model_library.state import reconcile_views
controller,node=map(json.loads,sys.stdin.read().split("\0",1))
print(json.dumps({"views":reconcile_views(controller+node["views"]),"transactions":node["transactions"]}))
'
}

retention_model() {
  local gathered views transactions observations plan row rank result all_views shared_retained=0
  require_cluster_nodes 1 >/dev/null || die "confirmed topology is required"
  gathered=$(merged_views) || die "prepared ownership records are unobservable or inconsistent"
  views=$(json_fields "$gathered" views); transactions=$(json_fields "$gathered" transactions)
  observations=$(all_observations) || die "every confirmed node must be observable before retention changes"
  if [ "$OP" = pin ] || [ "$OP" = unpin ]; then
    if [ "$PLAN" -eq 1 ]; then emit_result "$(model_json kind pulsar-retention-plan action "$OP" views: "$views")"; return; fi
    [ "$YES" -eq 1 ] || die "pin changes require --yes"
    [ "$views" != '[]' ] || die "no known prepared copies"
    local changed temporary flag
    flag=false; [ "$OP" != pin ] || flag=true
    if [ "$OP" = pin ]; then
      while IFS= read -r row; do
        verify_record "$row" >/dev/null || die "cannot pin unverified copies"
      done < <(printf '%s' "$views" | python3 -c 'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)]')
    fi
    temporary=$(mktemp)
    while IFS= read -r row; do
      select_record_manifest "$row" || die "record references an undeclared snapshot"
      rank=$(model_physical_rank "$(json_fields "$row" node_id)")
      changed=$(printf '%s' "$row" | python3 -c 'import json,sys; r=json.load(sys.stdin); r["pinned"]=json.loads(sys.argv[1]); print(json.dumps(r))' "$flag")
      model_node "$rank" "$(model_node_request pin-view view: "$changed")" >/dev/null || die "pin operation incomplete; inspect remaining node records"
      printf '%s\n' "$changed" >>"$temporary"
    done < <(printf '%s' "$views" | python3 -c 'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)]')
    # Controller mirrors every node, including previous placements, not only current ready set.
    python3 - "$PULSAR_MODEL_LIBRARY_DIR" "$temporary" <<'PY'
import json,sys
from model_library.state import Store,view_record_key
s=Store(sys.argv[1])
for line in open(sys.argv[2]):
 r=json.loads(line); s.put('views',view_record_key(r),r)
PY
    rm -f "$temporary"
    emit_result "$(model_json spec_id "$SPEC_ID" pinned: "$flag")"
    return
  fi
  all_views=$(merged_views all) || die "shared-copy dependencies are unobservable"
  all_views=$(json_fields "$all_views" views)
  plan=$(model_ctl "$(model_json operation plan-purge spec_id "$SPEC_ID" views: "$views" all_views: "$all_views" node_ids: "$(all_node_ids)" observations: "$observations")") || die "purge planning failed"
  if [ "$PLAN" -eq 1 ]; then emit_result "$(model_json plan: "$plan" incomplete_preparations: "$transactions")"; return; fi
  [ "$YES" -eq 1 ] || die "purge requires --yes"
  [ "$(json_fields "$plan" eligible)" = true ] || { emit_result "$plan"; return 1; }
  # Include every pending destination in preflight before deleting any completed view.
  while IFS= read -r row; do
    select_record_manifest "$row" || die "staging references an undeclared snapshot"
    "$REPO_DIR/scripts/guard-storage.sh" --node "$(json_fields "$row" node_id)" --path "$(json_fields "$row" stage)" --path "$(json_fields "$row" destination)" --json >/dev/null || die "incomplete preparation has a container reference"
  done < <(printf '%s' "$transactions" | python3 -c 'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)]')
  # Repeat all-node observations before deleting anything; each target guard repeats locally.
  observations=$(all_observations) || die "could not recheck all nodes before purge"
  all_views=$(merged_views all) || die "shared-copy dependencies became unobservable"
  all_views=$(json_fields "$all_views" views)
  plan=$(model_ctl "$(model_json operation plan-purge spec_id "$SPEC_ID" views: "$views" all_views: "$all_views" node_ids: "$(all_node_ids)" observations: "$observations")") || return 2
  [ "$(json_fields "$plan" eligible)" = true ] || { emit_result "$plan"; return 1; }
  while IFS= read -r row; do
    select_record_manifest "$row" || die "record references an undeclared snapshot"
    rank=$(model_physical_rank "$(json_fields "$row" node_id)")
    "$REPO_DIR/scripts/guard-storage.sh" --node "$(json_fields "$row" node_id)" --path "$(json_fields "$row" hub_path)" --json >/dev/null || die "prepared copy acquired a container reference"
    if [ "$(json_fields "$row" is_home_view)" != true ]; then
      result=$(model_node "$rank" "$(model_node_request remove-view view: "$row" all_views: "$all_views")") || die "copy release incomplete; remaining records retained"
      if [ "$(json_fields "$result" retained_bindings)" -gt 0 ]; then shared_retained=$((shared_retained+1)); fi
    fi
    model_ctl "$(model_json operation forget-view view: "$row")" >/dev/null || die "controller record removal incomplete"
    model_node "$rank" "$(model_node_request forget-view view: "$row")" >/dev/null || die "node record removal incomplete; retry purge to finish cleanup"
  done < <(printf '%s' "$views" | python3 -c 'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)]')
  while IFS= read -r row; do
    select_record_manifest "$row" || die "record references an undeclared snapshot"
    rank=$(model_physical_rank "$(json_fields "$row" node_id)")
    "$REPO_DIR/scripts/guard-storage.sh" --node "$(json_fields "$row" node_id)" --path "$(json_fields "$row" stage)" --path "$(json_fields "$row" destination)" --json >/dev/null || die "incomplete preparation has a container reference"
    model_node "$rank" "$(model_node_request remove-staging transaction: "$row")" >/dev/null || die "incomplete preparation could not be safely removed"
  done < <(printf '%s' "$transactions" | python3 -c 'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)]')
  emit_result "$(model_json spec_id "$SPEC_ID" purged: true archive_untouched: true home_untouched: true shared_copies_retained: "$shared_retained")"
}


snapshot_dependencies() {
  merged_views snapshot
}

remove_home() {
  require_home
  local observations archive_ok=false plan rank dependencies
  require_cluster_nodes 1 >/dev/null || die "home removal requires confirmed topology"
  observations=$(all_observations) || die "cannot inspect every node"
  dependencies=$(snapshot_dependencies) || die "snapshot dependencies are unobservable"
  if [ -n "${PULSAR_COLD_ROOT:-}" ] && archive_verify >/dev/null 2>&1; then archive_ok=true; fi
  plan=$(model_ctl "$(model_json operation plan-remove snapshot_manifest_id "$MANIFEST_ID" node_ids: "$(all_node_ids)" observations: "$observations" views: "$(json_fields "$dependencies" views)" transactions: "$(json_fields "$dependencies" transactions)" archive_verified: "$archive_ok" discard_unpromoted: "$([ "$DISCARD" -eq 1 ] && echo true || echo false)")") || die "home removal planning failed"
  if [ "$PLAN" -eq 1 ]; then emit_result "$plan"; return; fi
  [ "$YES" -eq 1 ] || die "home removal requires --yes"
  [ "$(json_fields "$plan" eligible)" = true ] || { emit_result "$plan"; return 1; }
  # The archive must still verify at the destructive boundary; no presence-only shortcut.
  [ "$archive_ok" != true ] || archive_verify >/dev/null || die "archive changed before removal"
  observations=$(all_observations) || die "cannot recheck nodes before home removal"
  dependencies=$(snapshot_dependencies) || die "cannot recheck snapshot dependencies"
  plan=$(model_ctl "$(model_json operation plan-remove snapshot_manifest_id "$MANIFEST_ID" node_ids: "$(all_node_ids)" observations: "$observations" views: "$(json_fields "$dependencies" views)" transactions: "$(json_fields "$dependencies" transactions)" archive_verified: "$archive_ok" discard_unpromoted: "$([ "$DISCARD" -eq 1 ] && echo true || echo false)")") || return 2
  [ "$(json_fields "$plan" eligible)" = true ] || { emit_result "$plan"; return 1; }
  rank=$(model_physical_rank "$(json_fields "$HOME_JSON" node_id)")
  "$REPO_DIR/scripts/guard-storage.sh" --node "$(json_fields "$HOME_JSON" node_id)" --path "$(json_fields "$HOME_JSON" hub_path)" --json >/dev/null || die "home acquired a container reference"
  model_node "$rank" "$(model_node_request remove-home hub_path "$(json_fields "$HOME_JSON" hub_path)" verification: "$(json_fields "$HOME_JSON" verification)")" >/dev/null || die "home removal did not complete"
  model_ctl "$(model_json operation forget-home home: "$HOME_JSON")" >/dev/null || die "home bytes removed but record remains; inspect state"
  emit_result "$(model_json snapshot_manifest_id "$MANIFEST_ID" removed: true archive_untouched: true)"
}

move_home() {
  require_home
  [ -n "$NODE" ] || die "move requires an explicit destination --node"
  local source_rank target_rank target_node views observations plan stage result dependencies source_state previous="$HOME_JSON" existing count route
  require_cluster_nodes 1 >/dev/null || die "move requires confirmed topology"
  source_rank=$(model_physical_rank "$(json_fields "$HOME_JSON" node_id)")
  target_rank=$(model_physical_rank "$NODE"); target_node="${CLUSTER_NODE_IDS[$target_rank]}"
  if [ -n "$SPEC_JSON" ] && [ "$SPEC_JSON" != null ] && [ "$NODES" -gt 1 ] && [ "$target_rank" -ge "$NODES" ]; then die "destination is outside the spec serving nodes"; fi
  dependencies=$(snapshot_dependencies) || die "snapshot dependencies are unobservable"
  views=$(json_fields "$dependencies" views)
  [ "$views" = '[]' ] && [ "$(json_fields "$dependencies" transactions)" = '[]' ] || die "clear dependent prepared copies before moving the home; pins are not bypassed"
  observations=$(all_observations) || die "every node must be observable before movement"
  "$REPO_DIR/scripts/guard-storage.sh" --node "$(json_fields "$HOME_JSON" node_id)" --path "$(json_fields "$HOME_JSON" hub_path)" --json >/dev/null || die "home is referenced"
  route=direct-ssh-roce
  if [ "$source_rank" -eq "$target_rank" ]; then route=already-home; elif [ "$source_rank" -gt 0 ] && [ "$target_rank" -gt 0 ]; then route=controller-stream-relay; fi
  plan=$(model_json kind pulsar-home-move-plan snapshot_manifest_id "$MANIFEST_ID" source_node "$(json_fields "$HOME_JSON" node_id)" destination_node "$target_node" transfer_route "$route")
  if [ "$PLAN" -eq 1 ]; then emit_result "$plan"; return; fi
  [ "$YES" -eq 1 ] || die "move requires --yes after reviewing destination and transfer route"
  source_state=$(model_node "$source_rank" "$(model_node_request path-state path "$(json_fields "$HOME_JSON" path)")") || die "source home is unobservable"
  if [ "$(json_fields "$source_state" state)" = missing ]; then
    existing=$(model_node "$target_rank" "$(model_node_request exists)") || die "target cannot be verified"
    [ "$(json_fields "$existing" exists)" = true ] || die "neither source nor destination has the home; restore from the archive"
    result=$(printf '%s' "$existing" | python3 -c 'import json,sys; from pathlib import Path; from model_library.local import home_record; d=json.load(sys.stdin); print(json.dumps({"home":home_record(json.loads(sys.argv[1]),sys.argv[2],Path(d["hub_path"]),d["verification"])}))' "$MANIFEST_JSON" "$target_node") || return 2
    result=$(save_home_result "$result" "$previous") || die "verified move recovery could not update the home record"
    emit_result "$result"; return
  fi
  HOME_JSON=$(verify_record "$HOME_JSON" 1) || die "home changed or is unavailable"
  previous="$HOME_JSON"
  if [ "$source_rank" -eq "$target_rank" ]; then emit_result "$(model_json home: "$HOME_JSON" moved: false)"; return; fi
  existing=$(model_node "$target_rank" "$(model_node_request exists)") || die "destination cannot be verified"
  if [ "$(json_fields "$existing" exists)" = true ]; then
    result=$(printf '%s' "$existing" | python3 -c 'import json,sys; from pathlib import Path; from model_library.local import home_record; d=json.load(sys.stdin); print(json.dumps({"home":home_record(json.loads(sys.argv[1]),sys.argv[2],Path(d["hub_path"]),d["verification"])}))' "$MANIFEST_JSON" "$target_node")
  else
    stage=$(model_node "$target_rank" "$(model_node_request begin-home)") || die "destination already has files; inspect before reuse or movement"
    . "$REPO_DIR/scripts/model-transfer.sh"
    model_transfer "$source_rank" "$(json_fields "$HOME_JSON" path)" "$target_rank" "$(json_fields "$stage" path)" "$MANIFEST_JSON" || die "move transfer incomplete; original home remains"
    result=$(model_node "$target_rank" "$(model_node_request publish-home stage "$(json_fields "$stage" stage)" node_id "$target_node")") || die "new home was not published"
  fi
  "$REPO_DIR/scripts/guard-storage.sh" --node "$(json_fields "$HOME_JSON" node_id)" --path "$(json_fields "$HOME_JSON" hub_path)" --json >/dev/null || die "source acquired a reference; both verified copies remain for explicit recovery"
  dependencies=$(snapshot_dependencies) || die "cannot recheck snapshot dependencies after transfer"
  [ "$(json_fields "$dependencies" views)" = '[]' ] && [ "$(json_fields "$dependencies" transactions)" = '[]' ] || die "new dependencies appeared; both copies remain"
  # Retire source only after destination full verification. A failed deletion
  # leaves the original record and both copies visible, never a fabricated move.
  model_node "$source_rank" "$(model_node_request remove-home hub_path "$(json_fields "$HOME_JSON" hub_path)" verification: "$(json_fields "$HOME_JSON" verification)")" >/dev/null || die "source retirement failed; inspect both copies before retrying"
  result=$(save_home_result "$result" "$previous") || die "destination is verified but home registration failed; inspect before retrying"
  emit_result "$result"
}
