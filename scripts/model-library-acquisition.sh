#!/usr/bin/env bash
# Internal operation helpers; sourced by model-library.sh only.

find_source_homes() {
  local rank result tmp known_homes
  known_homes=$(model_ctl "$(model_json operation homes model_id "$MODEL_ID" snapshot_revision "$REVISION")") || return 2
  tmp=$(mktemp)
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    result=$(model_node "$rank" "$(model_node_request find-source model_id "$MODEL_ID" snapshot_revision "$REVISION" node_id "${CLUSTER_NODE_IDS[$rank]}" known_homes: "$known_homes" full: "$([ "$FULL" = 1 ] && echo true || echo false)")") \
      || { local rc=$?; rm -f "$tmp"; return "$rc"; }
    printf '%s\n' "$result" >>"$tmp"
  done
  local found known state record known_rank
  found=$(python3 -c 'import json,sys; print(json.dumps([h for line in open(sys.argv[1]) for h in json.loads(line)["homes"]]))' "$tmp")
  rm -f "$tmp"
  if [ -n "$MANIFEST_ID" ]; then
    known=$(model_ctl "$(model_json operation home snapshot_manifest_id "$MANIFEST_ID")") || return 2
    record=$(json_fields "$known" home)
    if [ -n "$record" ] && [ "$record" != null ]; then
      known_rank=$(model_physical_rank "$(json_fields "$record" node_id)") || return 255
      state=$(model_node "$known_rank" "$(model_node_request path-state path "$(json_fields "$record" path)")") || return $?
      if [ "$(json_fields "$state" state)" = present ]; then
        # Discovery already verified copies under the configured home root.
        # Only verify the registered path separately when it lies elsewhere.
        if ! printf '%s' "$found" | python3 -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if any((v["home"]["node_id"],v["home"]["path"])==(r["node_id"],r["path"]) for v in json.load(sys.stdin)) else 1)' "$record"; then
          record=$(verify_record "$record") || return $?
          found=$(printf '%s' "$found" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"home":json.loads(sys.argv[1]),"manifest":json.loads(sys.argv[2])}); print(json.dumps(a))' "$record" "$MANIFEST_JSON")
        fi
      fi
    fi
  fi
  printf '%s\n' "$found"
}

acquire_model() {
  local rank source existing count candidate result stage checked registered original_manifest="$MANIFEST_JSON"
  require_cluster_nodes 1 >/dev/null || die "acquisition requires confirmed topology"
  rank=$(model_physical_rank "${NODE:-0}")
  if [ -n "$SPEC_JSON" ] && [ "$SPEC_JSON" != null ] && [ "${NODES:-1}" -gt 1 ] && [ "$rank" -ge "$NODES" ]; then die "selected home node is outside exact serving geometry"; fi
  [ -n "$MODEL_ID" ] && [ -n "$REVISION" ] || die "acquire needs a spec or --model-id and --revision"
  if [ "$PLAN" -eq 0 ]; then
    [[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || die "download requires the exact commit printed by --plan"
  fi
  # A supplied spec/retained manifest already supplies expected identity.
  # Reuse matching bytes offline, without consulting Hugging Face again.
  if [ -n "$MANIFEST_JSON" ]; then
    existing=$(find_source_homes) || die "known model locations are unobservable"
    count=$(printf '%s' "$existing" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
    [ "$count" -le 1 ] || die "multiple managed homes match; use explicit movement or cleanup"
    if [ "$count" -eq 1 ]; then
      candidate=$(printf '%s' "$existing" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[0]))')
      HOME_JSON=$(json_fields "$candidate" home)
      printf '%s' "$candidate" | python3 -c 'import json,sys; assert json.load(sys.stdin)["manifest"]==json.loads(sys.argv[1]), "home differs from selected manifest"' "$MANIFEST_JSON" || die "existing home has different bytes"
      if [ -n "$NODE" ] && [ "$(json_fields "$HOME_JSON" node_id)" != "${CLUSTER_NODE_IDS[$rank]}" ]; then die "home is on another node; use explicit move"; fi
      if [ "$PLAN" -eq 1 ]; then emit_result "$(model_json kind pulsar-acquisition-plan action reuse manifest: "$MANIFEST_JSON" home: "$HOME_JSON")"; return; fi
      [ "$YES" -eq 1 ] || die "registering verified reuse requires --yes"
      result=$(save_home_result "$(model_json home: "$HOME_JSON")") || die "verified home registration failed"
      if [ -n "$MANIFEST_OUT" ]; then
        printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; from pathlib import Path; from model_library.integrity import atomic_json; atomic_json(Path(sys.argv[1]).absolute(),json.load(sys.stdin),replace=False)' "$MANIFEST_OUT" || die "manifest output cannot be written"
      fi
      emit_result "$(model_json kind pulsar-acquisition-result reused: true manifest: "$MANIFEST_JSON" home: "$(json_fields "$result" home)")"
      return
    fi
  fi
  source=$(source_inventory_on_rank "$rank" "$MODEL_ID" "$REVISION") || die "cannot resolve selected source on selected node"
  REVISION=$(json_fields "$source" snapshot_revision)
  if [ -n "$MANIFEST_JSON" ]; then
    printf '%s' "$source" | python3 -c 'import json,sys; from model_library.source import compare_inventory_to_manifest; compare_inventory_to_manifest(json.load(sys.stdin),json.loads(sys.argv[1]))' "$MANIFEST_JSON" || die "source does not match selected spec"
  fi
  existing=$(find_source_homes) || die "home presence could not be checked on every confirmed node"
  count=$(printf '%s' "$existing" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
  [ "$count" -le 1 ] || die "multiple managed homes match; resolve the duplicate before acquisition"
  if [ "$PLAN" -eq 1 ]; then
    emit_result "$(model_json kind pulsar-acquisition-plan model_id "$MODEL_ID" snapshot_revision "$REVISION" selected_node "${CLUSTER_NODE_IDS[$rank]}" source: "$source" existing_homes: "$existing")"
    return
  fi
  [ "$YES" -eq 1 ] || die "acquisition requires --yes after reviewing the exact commit and selected node"
  if [ "$count" -eq 1 ]; then
    phase 1 1 "reusing the recorded home; checking it against the upstream file list"
    candidate=$(printf '%s' "$existing" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[0]))')
    HOME_JSON=$(json_fields "$candidate" home)
    if [ -n "$NODE" ] && [ "$(json_fields "$HOME_JSON" node_id)" != "${CLUSTER_NODE_IDS[$rank]}" ]; then
      die "home is on another node; use an explicit move instead of downloading again"
    fi
    rank=$(model_physical_rank "$(json_fields "$HOME_JSON" node_id)")
    registered=$(model_ctl "$(model_json operation home snapshot_manifest_id "$(json_fields "$HOME_JSON" snapshot_manifest_id)")") || die "registered home is unavailable"
    if printf '%s' "$registered" | python3 -c 'import json,sys; old=json.load(sys.stdin)["home"]; new=json.loads(sys.argv[1]); fields=("snapshot_manifest_id","node_id","hub_path","path"); sys.exit(0 if isinstance(old,dict) and all(old[k]==new[k] for k in fields) else 1)' "$HOME_JSON"; then
      MANIFEST_JSON=$(json_fields "$candidate" manifest)
      printf '%s' "$source" | python3 -c 'import json,sys; from model_library.source import compare_inventory_to_manifest; compare_inventory_to_manifest(json.load(sys.stdin),json.loads(sys.argv[1]))' "$MANIFEST_JSON" || die "source differs from the registered manifest"
    else
      checked=$(model_node "$rank" "$(model_node_request source-verify path "$(json_fields "$HOME_JSON" path)" source: "$source")") || die "existing bytes do not match the complete upstream inventory"
      MANIFEST_JSON=$(json_fields "$checked" manifest)
    fi
    result=$(model_json home: "$HOME_JSON")
  else
    phase 1 4 "staging on $(human_node_name "$rank")"
    stage=$(model_node "$rank" "$(model_node_request begin-source source: "$source")") || die "could not create private same-filesystem staging"
    phase 2 4 "downloading $(source_summary "$source") to $(human_node_name "$rank")"
    source_download_on_rank "$rank" "$(json_fields "$stage" stage)" "$source" || die "download incomplete; no home was published"
    phase 3 4 "verifying SHA-256 of every downloaded file"
    checked=$(model_node "$rank" "$(model_node_request source-verify path "$(json_fields "$stage" path)" source: "$source")") || die "download verification failed; no home was published"
    MANIFEST_JSON=$(json_fields "$checked" manifest)
    existing=$(find_source_homes) || die "cannot recheck all-node home absence before publication"
    [ "$(printf '%s' "$existing" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" -eq 0 ] || die "another home appeared; staged download was not published"
    phase 4 4 "publishing the home on $(human_node_name "$rank")"
    result=$(model_node "$rank" "$(model_node_request publish-home stage "$(json_fields "$stage" stage)" node_id "${CLUSTER_NODE_IDS[$rank]}")") || die "home publication failed"
  fi
  if [ -n "$original_manifest" ]; then
    printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); b=json.loads(sys.argv[1]); assert a==b, "verified download differs from selected spec"' "$original_manifest" || die "verified files differ from selected spec"
  fi
  MANIFEST_ID=$(json_fields "$MANIFEST_JSON" manifest_id)
  result=$(save_home_result "$result") || die "home files exist but registration failed; inspect before retrying"
  if [ -n "$MANIFEST_OUT" ]; then
    printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; from pathlib import Path; from model_library.integrity import atomic_json; atomic_json(Path(sys.argv[1]).absolute(),json.load(sys.stdin),replace=False)' "$MANIFEST_OUT" || die "manifest output exists or cannot be written"
  fi
  emit_result "$(model_json kind pulsar-acquisition-result manifest: "$MANIFEST_JSON" home: "$(json_fields "$result" home)")"
}
