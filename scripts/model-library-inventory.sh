#!/usr/bin/env bash
# Read-only model inventory. No lifecycle lock or model-library record writes.
show_files() (
  local work rank request files containers observation rc error container_error result
  require_cluster_nodes 1 >/dev/null || die "model-file inventory requires confirmed topology"
  work=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-model-files.XXXXXX") || return 2
  trap 'rm -rf -- "$work"' EXIT
  python3 -m model_library.file_inventory context --repo-root "$REPO_DIR" \
    --state-root "$PULSAR_MODEL_LIBRARY_DIR" \
    --overlay "${PULSAR_OVERLAY_PATH:-$REPO_DIR/.pulsar-overlay.json}" >"$work/context.json" || return 2
  : >"$work/observations.jsonl"
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    phase "$((rank+1))" "$CLUSTER_TOPOLOGY_COUNT" "inspecting model files on $(human_node_name "$rank")"
    request=$(python3 -m model_library.file_inventory request --context "$work/context.json" \
      --node-id "${CLUSTER_NODE_IDS[$rank]}") || return 2
    rc=0; error=""; container_error=""
    files=$(model_node "$rank" "$(model_node_request inventory-files node_id "${CLUSTER_NODE_IDS[$rank]}" \
      homes: "$(json_fields "$request" homes)" views: "$(json_fields "$request" views)" \
      home_roots: "$(json_fields "$request" home_roots)")" 2>"$work/files.err") || rc=$?
    case "$rc" in 129|130|143) return "$rc" ;; esac
    if [ "$rc" -ne 0 ]; then
      files=null
      error="node files not observed (exit $rc): $(cat "$work/files.err")"
    fi
    rc=0
    containers=$(model_container_observation "$rank" 2>"$work/containers.err") || rc=$?
    case "$rc" in 129|130|143) return "$rc" ;; esac
    if [ "$rc" -eq 0 ]; then containers=$(json_fields "$containers" containers)
    else
      containers=null
      container_error="container references not observed (exit $rc): $(cat "$work/containers.err")"
    fi
    observation=$(model_json node_id "${CLUSTER_NODE_IDS[$rank]}" hostname "$(human_node_name "$rank")" \
      files: "$files" containers: "$containers" error "$error" container_error "$container_error") || return 2
    printf '%s\n' "$observation" >>"$work/observations.jsonl"
  done
  result=$(python3 -m model_library.file_inventory combine --context "$work/context.json" \
    --observations "$work/observations.jsonl") || return 2
  emit_result "$result"
)
