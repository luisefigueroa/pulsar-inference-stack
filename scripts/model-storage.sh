#!/usr/bin/env bash
# Browse saved state; only selected actions invoke storage or serving services.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# lib.sh supplies process/.env configuration; loading it performs no live checks.
# shellcheck source=lib.sh
. "$REPO_DIR/scripts/lib.sh"
export PULSAR_MODEL_LIBRARY_DIR

catalog() { python3 -m model_library.catalog "$@"; }

select_node() {
  require_cluster_nodes 1 >/dev/null || return 2
  local index
  index=$(choose_index "Select a confirmed physical node" "${CLUSTER_NODE_IDS[@]}") || return 1
  printf '%s\n' "${CLUSTER_NODE_IDS[$index]}"
}

browse() {
  # shellcheck source=ui.sh
  . "$REPO_DIR/scripts/ui.sh"
  local result index spec action node selected nodes
  local -a ids=() labels=() args=()
  result=$(catalog list --json) || return $?
  mapfile -t ids < <(printf '%s' "$result" | python3 -c 'import json,sys;print("\n".join(r["spec_id"] for r in json.load(sys.stdin)["entries"]))')
  if [ "${#ids[@]}" -eq 0 ] || [ -z "${ids[0]:-}" ]; then catalog list; return; fi
  mapfile -t labels < <(printf '%s' "$result" | python3 -c '
import json,sys,shutil
width=max(32,min(100,shutil.get_terminal_size((80,24)).columns))-6
for row in json.load(sys.stdin)["entries"]:
 review=row.get("review") or {}
 suffix=" ["+row["spec_id"][:8]+"] "+(review.get("status") or "not specified")
 model=row["model_id"]; available=max(4,width-len(suffix))
 if len(model)>available:model=model[:available-3]+"..."
 print(model+suffix)
')
  index=$(choose_index "Select a catalog recipe" "${labels[@]}") || return 0
  spec="${ids[$index]}"
  nodes=$(printf '%s' "$result" | python3 -c 'import json,sys;print(json.load(sys.stdin)["entries"][int(sys.argv[1])]["geometry"]["nodes"])' "$index")
  catalog show "$spec"
  selected=$(choose_index "Choose one operation" \
    "Check now" "Download" "Restore" "Move home" "Prepare" \
    "Pin prepared copies" "Unpin prepared copies" "Purge prepared copies" \
    "Remove home" "Create archive" "Verify archive" "Start" "Stop" "Live status" "Back") || return 0
  case "$selected" in
    0) action=check ;;
    10) "$REPO_DIR/scripts/model-library.sh" archive verify "$spec"; return ;;
    13) action=status ;;
    14) return 0 ;;
    1) action=acquire ;;
    2) action=restore ;;
    3) action=move ;;
    4) action=prepare ;;
    5) action=pin ;;
    6) action=unpin ;;
    7) action=purge ;;
    8) action=remove ;;
    9) action=archive ;;
    11) action=start ;;
    12) action=stop ;;
  esac
  case "$action" in
    acquire|restore|move)
      node=$(select_node) || return 0
      args+=(--node "$node") ;;
    prepare|start|stop|status|check)
      if [ "$nodes" -eq 1 ]; then
        node=$(select_node) || return 0
        args+=(--node "$node")
      fi ;;
  esac
  case "$action" in
    check) "$REPO_DIR/scripts/model-library.sh" check "$spec" "${args[@]}"; return ;;
    status) "$REPO_DIR/scripts/status.sh" "$spec" "${args[@]}"; return ;;
  esac
  confirm "Perform $action for this exact recipe?" no || return 0
  case "$action" in
    start) "$REPO_DIR/scripts/up.sh" "$spec" "${args[@]}" ;;
    stop) "$REPO_DIR/scripts/down.sh" "$spec" "${args[@]}" ;;
    archive) "$REPO_DIR/scripts/model-library.sh" archive create "$spec" --yes ;;
    *) "$REPO_DIR/scripts/model-library.sh" "$action" "$spec" "${args[@]}" --yes ;;
  esac
}

command="${1:-}"
[ $# -eq 0 ] || shift
case "$command" in
  "") if [ -t 0 ]; then browse; else catalog list; fi ;;
  menu) browse ;;
  list|show) catalog "$command" "$@" ;;
  check) exec "$REPO_DIR/scripts/model-library.sh" check "$@" ;;
  --json) catalog list --json "$@" ;;
  --help|-h|help) catalog --help ;;
  *) echo 'usage: pulsar models [list|show SPEC|check SPEC|menu] [--json]' >&2; exit 2 ;;
esac
