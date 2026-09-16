#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

collect_node_views() {
  local rank result temp request scope="${1:-spec}"
  case "$scope" in
    spec) request=$(model_node_request view-records spec_id "$SPEC_ID") ;;
    snapshot) request=$(model_node_request view-records snapshot_manifest_id "$MANIFEST_ID") ;;
    all) request=$(model_node_request view-records) ;;
    *) return 2 ;;
  esac
  temp=$(mktemp)
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    result=$(model_node "$rank" "$request") || { local rc=$?; rm -f "$temp"; return "$rc"; }
    printf '%s' "$result" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert all(v.get("node_id")==sys.argv[1] for key in ("views","transactions") for v in d[key]), "node record is bound to another physical node"' "${CLUSTER_NODE_IDS[$rank]}" || { rm -f "$temp"; return 2; }
    printf '%s\n' "$result" >>"$temp"
  done
  python3 -c 'import json,sys; rows=[json.loads(x) for x in open(sys.argv[1])]; print(json.dumps({"views":[v for r in rows for v in r["views"]],"transactions":[v for r in rows for v in r["transactions"]]}))' "$temp" || { local rc=$?; rm -f "$temp"; return "$rc"; }
  rm -f "$temp"
}

prepare_snapshot() {
  [ -n "$SPEC_ID" ] && [ -n "$SPEC_JSON" ] || die "prepare requires a frozen spec"
  require_home
  selected_nodes
  local home_node home_rank observations existing transactions ranks_tmp budgets_tmp views_tmp previous row verified result plan rank slot node roots space budget available reserve used limit action stage source_rank source_path source_stamp candidates candidate reuse_view
  home_node=$(json_fields "$HOME_JSON" node_id)
  home_rank=$(model_physical_rank "$home_node")
  case " ${SELECTED_IDS[*]} " in *" $home_node "*) ;; *) die "home is outside the selected serving nodes; move it explicitly first" ;; esac
  HOME_JSON=$(verify_record "$HOME_JSON") || die "home verification failed"
  observations=$(all_observations) || die "all confirmed nodes must be observable before preparation"
  existing=$(merged_views snapshot) || die "node and controller preparation records cannot be reconciled"
  transactions=$(json_fields "$existing" transactions)
  existing=$(json_fields "$existing" views)
  ranks_tmp=$(mktemp); budgets_tmp=$(mktemp); views_tmp=$(mktemp)
  for ((slot=0; slot<${#SELECTED_RANKS[@]}; slot++)); do
    rank="${SELECTED_RANKS[$slot]}"; node="${CLUSTER_NODE_IDS[$rank]}"
    candidates=$(model_ctl "$(model_json operation prepare-candidates spec: "$SPEC_JSON" home: "$HOME_JSON" node_id "$node" views: "$existing" transactions: "$transactions")") || die "prepared candidates are inconsistent"
    row=null; reuse_view=null
    while IFS= read -r candidate; do
      if [ "$(json_fields "$candidate" spec_id)" != "$SPEC_ID" ]; then
        result=$(model_node "$rank" "$(model_node_request path-state path "$(json_fields "$candidate" hub_path)")") || die "prepared candidate is unobservable"
        [ "$(json_fields "$result" state)" != missing ] || continue
        reuse_view="$candidate"
      fi
      row="$candidate"; break
    done < <(printf '%s' "$candidates" | python3 -c 'import json,sys; [print(json.dumps(v)) for v in json.load(sys.stdin)]')
    verified=false
    if [ "$row" != null ] && { [ "$reuse_view" != null ] || [ "$(json_fields "$row" topology_id)" = "$CLUSTER_TOPOLOGY_ID" ]; }; then
      if result=$(verify_record "$row"); then
        verified=true
        # Keep the original pin and ownership metadata; only verification cache changes.
        if [ "$reuse_view" != null ]; then reuse_view="$result"; else printf '%s\n' "$result" >>"$views_tmp"; fi
      fi
    fi
    printf '%s\n' "$(model_json node_id "$node" view_verified: "$verified" reuse_view: "$reuse_view")" >>"$ranks_tmp"
    roots=$(model_node "$rank" "$(model_node_request roots)") || die "node storage roots are unavailable"
    space=$(model_node "$rank" "$(model_node_request space path "$(json_fields "$roots" view_root)")") || die "node disk usage is unavailable"
    budget=$(printf '%s' "$space" | python3 -c 'import json,sys; s=json.load(sys.stdin); r=int(sys.argv[1]) if sys.argv[1] else max(64*1024**3,s["total"]*5//100); limit=int(sys.argv[2]) if sys.argv[2] else max(0,s["available"]+s["used"]-r); assert r>=0 and limit>=0; print(json.dumps({"available":s["available"],"used":s["used"],"reserve":r,"limit":limit}))' "${PULSAR_HOT_RESERVE_BYTES:-}" "${PULSAR_HOT_BUDGET_BYTES:-}") || die "invalid disk budget"
    printf '%s\n' "$(model_json node_id "$node" budget: "$budget")" >>"$budgets_tmp"
  done
  observations=$(printf '%s' "$observations" | python3 -c 'import json,sys; all=json.load(sys.stdin); selected=[json.loads(x) for x in open(sys.argv[1])]; print(json.dumps([{**next(o for o in all if o["node_id"]==v["node_id"]),**v} for v in selected]))' "$ranks_tmp")
  budget=$(python3 -c 'import json,sys; print(json.dumps({r["node_id"]:r["budget"] for r in map(json.loads,open(sys.argv[1]))}))' "$budgets_tmp")
  plan=$(model_ctl "$(model_json operation plan-prepare snapshot "${PREPARE_SNAPSHOT:-target}" views: "$existing" spec: "$SPEC_JSON" home: "$HOME_JSON" node_ids: "$NODE_IDS_JSON" topology_id "$CLUSTER_TOPOLOGY_ID" observations: "$observations" budgets: "$budget")") || die "could not build preparation plan"
  rm -f "$ranks_tmp" "$budgets_tmp"
  if [ "$PLAN" -eq 1 ]; then rm -f "$views_tmp"; emit_result "$plan"; return; fi
  [ "$YES" -eq 1 ] || die "preparation requires --yes after reviewing placement and storage"
  [ "$(json_fields "$plan" eligible)" = true ] || { emit_result "$plan"; rm -f "$views_tmp"; return 1; }
  . "$REPO_DIR/scripts/model-transfer.sh"
  source_rank="$home_rank"; source_path=$(json_fields "$HOME_JSON" path); source_stamp=$(json_fields "$HOME_JSON" verification)
  # Rank 0 is prepared first when a remote home serves multiple ranks. Its
  # required verified working copy supplies later transfers; no extra home.
  for ((slot=0; slot<${#SELECTED_RANKS[@]}; slot++)); do
    rank="${SELECTED_RANKS[$slot]}"; node="${CLUSTER_NODE_IDS[$rank]}"
    action=$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["actions"][int(sys.argv[1])]["action"])' "$slot")
    if [ "$action" = reuse ]; then
      if [ "$rank" -eq 0 ] && [ "$home_rank" -ne 0 ]; then
        source_rank=0
        source_path=$(python3 -c 'import json,sys; print(next(json.loads(x)["path"] for x in open(sys.argv[1]) if json.loads(x)["node_id"]==sys.argv[2]))' "$views_tmp" "$node")
        source_stamp=$(python3 -c 'import json,sys; print(json.dumps(next(json.loads(x)["verification"] for x in open(sys.argv[1]) if json.loads(x)["node_id"]==sys.argv[2])))' "$views_tmp" "$node")
      fi
      continue
    fi
    if [ "$action" = bind ]; then
      row=$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["actions"][int(sys.argv[1])]["source_view"]))' "$slot")
      model_ctl "$(model_json operation protect-shared-copy source_view: "$row")" >/dev/null || die "shared-copy protection could not be recorded"
      result=$(model_node "$rank" "$(model_node_request bind-view source_view: "$row" spec_id "$SPEC_ID" node_id "$node" rank: "$slot" topology_id "$CLUSTER_TOPOLOGY_ID")") || die "prepared-copy binding failed; original files retained"
      row=$(json_fields "$result" view)
    elif [ "$action" = home-view ]; then
      row=$(model_ctl "$(model_json operation record-view view_schema: "$VIEW_SCHEMA" home: "$HOME_JSON" spec_id "$SPEC_ID" topology_id "$CLUSTER_TOPOLOGY_ID" rank: "$slot" is_home_view: true)") || die "home view could not be recorded"
      model_node "$rank" "$(model_node_request save-view view: "$row")" >/dev/null || die "home view node record failed"
    else
      stage=$(model_node "$rank" "$(model_node_request begin-view spec_id "$SPEC_ID" node_id "$node" rank: "$slot" topology_id "$CLUSTER_TOPOLOGY_ID")") || die "prepared-copy staging failed"
      model_transfer "$source_rank" "$source_path" "$rank" "$(json_fields "$stage" path)" "$MANIFEST_JSON" "$source_stamp" publication || die "preparation is incomplete; no all-rank readiness was published"
      result=$(model_node "$rank" "$(model_node_request publish-view stage "$(json_fields "$stage" stage)" spec_id "$SPEC_ID" node_id "$node" rank: "$slot" topology_id "$CLUSTER_TOPOLOGY_ID")") || die "prepared-copy publication failed"
      row=$(json_fields "$result" view)
    fi
    if [ "$rank" -eq 0 ]; then source_rank=0; source_path=$(json_fields "$row" path); source_stamp=$(json_fields "$row" verification); fi
    remember_preparation_verification "$row" || die "could not retain preparation verification"
    printf '%s\n' "$row" >>"$views_tmp"
  done
  existing=$(python3 -c 'import json,sys; print(json.dumps(sorted([json.loads(x) for x in open(sys.argv[1])],key=lambda r:r["rank"])))' "$views_tmp")
  rm -f "$views_tmp"
  # All-rank barrier checks current identity/metadata, rehashing changed copies.
  local verified_tmp
  verified_tmp=$(mktemp)
  while IFS= read -r row; do
    verified=$(verify_record "$row") || { rm -f "$verified_tmp"; die "final all-rank verification failed"; }
    printf '%s\n' "$verified" >>"$verified_tmp"
  done < <(printf '%s' "$existing" | python3 -c 'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)]')
  existing=$(python3 -c 'import json,sys; print(json.dumps([json.loads(x) for x in open(sys.argv[1])]))' "$verified_tmp"); rm -f "$verified_tmp"
  result=$(model_ctl "$(model_json operation save-views manifest: "$MANIFEST_JSON" spec_id "$SPEC_ID" expected_node_ids: "$NODE_IDS_JSON" views: "$existing")") || die "node copies verified but all-rank record publication failed; inspect before retrying"
  emit_result "$result"
}


prepare_model() (
  # Subshells used to capture JSON share this private, invocation-only cache.
  PREPARE_VERIFICATION_DIR=$(mktemp -d) || return 2
  trap 'rm -rf -- "$PREPARE_VERIFICATION_DIR"' EXIT
  if [ "$VIEW_SCHEMA" != 2 ]; then prepare_snapshot; return; fi
  local name tmp result plan saved_plan="$PLAN" saved_json="$JSON" saved_full="$FULL"
  tmp=$(mktemp)
  # Review every snapshot and the combined storage budget before any mutation.
  PLAN=1 JSON=1
  while IFS= read -r name <&3; do
    select_snapshot "$name" || { rm -f "$tmp"; return 2; }
    PREPARE_SNAPSHOT="$name"
    result=$(prepare_snapshot) || { local rc=$?; rm -f "$tmp"; return "$rc"; }
    printf '%s\n' "$result" >>"$tmp"
  done 3< <(snapshot_names)
  plan=$(python3 - "$tmp" "$SPEC_JSON" <<'PYCODE'
import json,sys
from model_library.planning import preparation_set_plan
print(json.dumps(preparation_set_plan(json.loads(sys.argv[2]),[json.loads(x) for x in open(sys.argv[1])])) )
PYCODE
  ) || { rm -f "$tmp"; return 2; }
  rm -f "$tmp"
  PLAN="$saved_plan" JSON="$saved_json"
  if [ "$PLAN" = 1 ]; then emit_result "$plan"; return; fi
  [ "$(json_fields "$plan" eligible)" = true ] || { emit_result "$plan"; return 1; }
  [ "$YES" = 1 ] || die "preparation requires --yes after reviewing the complete snapshot set"
  while IFS= read -r name <&3; do
    select_snapshot "$name"
    PREPARE_SNAPSHOT="$name"
    prepare_snapshot >/dev/null || return $?
  done 3< <(snapshot_names)
  FULL=0
  result=$(prepared_info) || die "preparation incomplete; required snapshot verification failed"
  FULL="$saved_full"
  emit_result "$result"
)
