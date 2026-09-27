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
catalog_menu() { python3 -m model_library.catalog_menu "$@"; }

# lib.sh has already applied the process value, then .env, for the archive root.
archive_location() {
  if [ -z "${PULSAR_COLD_ROOT+x}" ]; then echo not-configured
  elif [ -z "$PULSAR_COLD_ROOT" ]; then echo disabled
  else echo configured; fi
}

select_node() {
  require_cluster_nodes 1 >/dev/null || return 2
  local index rc
  local -a names=()
  for index in "${!CLUSTER_NODE_IDS[@]}"; do names+=("$(human_node_name "$index")"); done
  # Show hostnames; operations receive the stable node_id.
  index=$(choose_index "Select a confirmed physical node" "${names[@]}") || { rc=$?; return "$rc"; }
  printf '%s\n' "${CLUSTER_NODE_IDS[$index]}"
}

# The last operation that succeeded for each recipe in this menu session. The
# saved check cannot show it, so it informs the suggested next step.
declare -A LAST_SUCCESS=()

elapsed_text() {
  local seconds="$1"
  if [ "$seconds" -ge 3600 ]; then printf '%dh %dm' $((seconds / 3600)) $((seconds % 3600 / 60))
  elif [ "$seconds" -ge 60 ]; then printf '%dm %ds' $((seconds / 60)) $((seconds % 60))
  else printf '%ds' "$seconds"; fi
}

# run_operation ACTION LABEL SPEC MODEL COMMAND...
# Runs one explicit operation and reports its outcome. Ctrl-C reaches the
# operation, which stops through its own cleanup; the menu then continues.
run_operation() {
  local action="$1" label="$2" spec="$3" model="$4" started=$SECONDS rc interrupted=0
  shift 4
  printf '\n→ %s %s. Ctrl-C stops it and returns to this menu.\n' "$label" "$model"
  trap 'interrupted=1' INT
  set +e
  "$@"
  rc=$?
  set -e
  trap - INT
  local took
  took=$(elapsed_text $((SECONDS - started)))
  if [ "$rc" -eq 0 ]; then
    printf '✓ %s finished for %s in %s\n' "$label" "$model" "$took"
    case "$action" in status|verify) ;; *) LAST_SUCCESS[$spec]="$action" ;; esac
  elif [ "$interrupted" = 1 ] || [ "$rc" -eq 130 ]; then
    printf '✗ %s stopped by Ctrl-C for %s after %s; details above\n' "$label" "$model" "$took"
  else
    printf '✗ %s failed for %s (exit %d) after %s; details above\n' "$label" "$model" "$rc" "$took"
  fi
  return 0
}

# plan_and_confirm ACTION LABEL SPEC OPERATION_ARGS...
# Shows the operation's own --plan preview, then asks a question that names
# the model, placement and consequence. Returns 0 only after an explicit yes.
plan_and_confirm() {
  local action="$1" label="$2" spec="$3" plan question rc
  shift 3
  local -a render_args=(--operation "$1")
  [ "$1" != archive ] || render_args+=(--archive-action create)
  plan=$(mktemp)
  if ! spin "Planning ${label,,}…" "$REPO_DIR/scripts/model-library.sh" "$@" --plan --json >"$plan"; then
    rm -f "$plan"
    printf '✗ Planning %s failed; nothing changed.\n' "${label,,}"
    return 1
  fi
  echo
  python3 -m model_library.render "${render_args[@]}" <"$plan" || true
  echo
  set +e
  question=$(printf '%s' "$ROW_JSON" | catalog_menu confirm --spec-id "$spec" --action "$action" \
    --plan-file "$plan" ${MENU_SNAPSHOT:+--snapshot "$MENU_SNAPSHOT"} ${MENU_NODE:+--node "$MENU_NODE"})
  rc=$?
  set -e
  rm -f "$plan"
  if [ "$rc" -eq 3 ]; then
    printf '✗ The plan is blocked; nothing changed.\n'
    return 1
  fi
  [ "$rc" -eq 0 ] || return 1
  # Gum Ctrl-C (130) at the question leaves the menu, as at any other prompt.
  confirm "$question" no || { rc=$?; echo 'Nothing changed.'; [ "$rc" -eq 130 ] && return 130; return 1; }
}

# perform ACTION LABEL SPEC — gathers snapshot and node, previews, confirms, runs.
perform() {
  local action="$1" label="$2" spec="$3" rc question choice
  local -a args=() choices=()
  MENU_SNAPSHOT="" MENU_NODE=""
  mapfile -t choices < <(printf '%s' "$VIEW" | awk -F'\t' -v a="$action" '$1 == "snapshot" && $2 == a {print $3}')
  if [ "${#choices[@]}" -eq 1 ]; then
    MENU_SNAPSHOT="${choices[0]}"
  elif [ "${#choices[@]}" -gt 1 ]; then
    choice=$(choose_index "Select a required snapshot" "${choices[@]}") || { rc=$?; return "$rc"; }
    MENU_SNAPSHOT="${choices[$choice]}"
  fi
  [ -z "$MENU_SNAPSHOT" ] || args+=(--snapshot "$MENU_SNAPSHOT")
  case "$action" in
    acquire|restore|move) MENU_NODE=$(select_node) || { rc=$?; return "$rc"; } ;;
    prepare|start|stop|status|check)
      if [ "$RECIPE_NODES" -eq 1 ]; then MENU_NODE=$(select_node) || { rc=$?; return "$rc"; }; fi ;;
  esac
  [ -z "$MENU_NODE" ] || args+=(--node "$MENU_NODE")
  case "$action" in
    check) run_operation check "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" check "$spec" "${args[@]}" ;;
    status) run_operation status "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/status.sh" "$spec" "${args[@]}" ;;
    verify) run_operation verify "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" archive verify "$spec" ;;
    start|stop)
      question=$(printf '%s' "$ROW_JSON" | catalog_menu confirm --spec-id "$spec" --action "$action" ${MENU_NODE:+--node "$MENU_NODE"}) || return 0
      confirm "$question" no || { rc=$?; echo 'Nothing changed.'; [ "$rc" -ne 130 ] || return 130; return 0; }
      if [ "$action" = start ]; then
        run_operation start "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/up.sh" "$spec" "${args[@]}"
      else
        run_operation stop "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/down.sh" "$spec" "${args[@]}"
      fi ;;
    archive)
      plan_and_confirm archive "$label" "$spec" archive create "$spec" "${args[@]}" || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
      run_operation archive "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" archive create "$spec" "${args[@]}" --yes ;;
    *)
      plan_and_confirm "$action" "$label" "$spec" "$action" "$spec" "${args[@]}" || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
      run_operation "$action" "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" "$action" "$spec" "${args[@]}" --yes ;;
  esac
}

# recipe_menu SPEC — loops over one recipe's operations until Back.
recipe_menu() {
  local spec="$1" kind a b c index rc default group
  local -a header=() main=() main_labels=() storage=() storage_labels=() labels=()
  while true; do
    ROW_JSON=$(catalog show "$spec" --json) || return 0
    VIEW=$(printf '%s' "$ROW_JSON" | catalog_menu view --spec-id "$spec" \
      --archive-location "$(archive_location)" ${LAST_SUCCESS[$spec]:+--after "${LAST_SUCCESS[$spec]}"}) || return 0
    header=(); main=(); main_labels=(); storage=(); storage_labels=(); default=""
    while IFS=$'\t' read -r kind a b c; do
      case "$kind" in
        recipe) RECIPE_NODES="$a"; RECIPE_MODEL="$b" ;;
        header) header+=("$a") ;;
        option)
          if [ "$a" = main ]; then main+=("$b"); main_labels+=("$c")
          else storage+=("$b"); storage_labels+=("$c"); fi ;;
        suggest)
          for index in "${!main[@]}"; do [ "${main[$index]}" != "$a" ] || default="$index"; done ;;
      esac
    done <<<"$VIEW"
    echo
    printf '%s\n' "${header[@]}" | emit_frame
    labels=("${main_labels[@]}")
    [ "${#storage[@]}" -eq 0 ] || labels+=("Storage and archive…")
    labels+=("Show details" "Back")
    index=$(PULSAR_CHOOSE_DEFAULT="$default" choose_index "Choose one operation" "${labels[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    if [ "$index" -lt "${#main[@]}" ]; then
      perform "${main[$index]}" "${main_labels[$index]% (suggested)}" "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
      continue
    fi
    group="${labels[$index]}"
    case "$group" in
      "Storage and archive…")
        index=$(choose_index "Storage and archive" "${storage_labels[@]}" "Back") \
          || { rc=$?; [ "$rc" -ne 130 ] || return 130; continue; }
        [ "$index" -lt "${#storage[@]}" ] || continue
        perform "${storage[$index]}" "${storage_labels[$index]}" "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      "Show details") catalog show "$spec" || true ;;
      *) return 0 ;;
    esac
  done
}

browse() {
  # shellcheck source=ui.sh
  . "$REPO_DIR/scripts/ui.sh"
  require_gum "the catalog menu" "pulsar models list | show SPEC | check SPEC"
  local result index rc
  local -a ids=() labels=()
  while true; do
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
    index=$(choose_index "Select a catalog spec" "${labels[@]}" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    [ "$index" -lt "${#ids[@]}" ] || return 0
    recipe_menu "${ids[$index]}" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
  done
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
