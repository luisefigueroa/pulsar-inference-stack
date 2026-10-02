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

# The last relevant outcome for each recipe in this menu session. A recorded
# check supersedes a prior mutation even when it found missing/changed files.
declare -A LAST_RESULT=()
OPERATION_RC=0 OPERATION_INTERRUPTED=0

elapsed_text() {
  local seconds="$1"
  if [ "$seconds" -ge 3600 ]; then printf '%dh %dm' $((seconds / 3600)) $((seconds % 3600 / 60))
  elif [ "$seconds" -ge 60 ]; then printf '%dm %ds' $((seconds / 60)) $((seconds % 60))
  else printf '%ds' "$seconds"; fi
}

# run_operation ACTION LABEL SPEC MODEL COMMAND...
# Runs one explicit operation and reports its outcome. The command owns
# interruption and cleanup; the menu cannot independently confirm its effects.
run_operation() {
  local action="$1" label="$2" spec="$3" model="$4" started=$SECONDS rc interrupted=0 result="" recovery
  shift 4
  [ "$action" != check ] || result=$(mktemp)
  printf '\n→ %s %s. Ctrl-C requests interruption; this menu returns when the command exits.\n' "$label" "$model" \
    | python3 -m scripts.terminal_format
  trap 'interrupted=1' INT
  set +e
  if [ "$action" = check ]; then
    "$@" --json >"$result"
  else
    "$@"
  fi
  rc=$?
  set -e
  trap - INT
  if [ "$action" = check ]; then
    LAST_RESULT[$spec]=check-failed
    # Check emits this result only after saving the observation. Missing files
    # still return a nonzero status, but their recorded state can guide repair.
    if [ -s "$result" ] && python3 -m model_library.render --operation check <"$result"; then
      LAST_RESULT[$spec]=check
    elif [ "$rc" -eq 0 ]; then
      printf 'error: check did not return a readable saved observation\n' >&2
      rc=2
    fi
    rm -f "$result"
  fi
  local took
  took=$(elapsed_text $((SECONDS - started)))
  if [ "$interrupted" = 1 ] || [ "$rc" -eq 130 ]; then
    interrupted=1
    recovery='Rerun this inspection when ready; its partial output may be incomplete.'
    case "$action" in
      acquire|restore|prepare|move|archive|pin|unpin|purge|remove)
        LAST_RESULT[$spec]=storage-interrupted
        recovery='Use Check now to inspect current files before retrying. Retained staging may be recoverable by the same operation.' ;;
      start|stop)
        LAST_RESULT[$spec]=service-interrupted
        recovery='Use Live status to inspect the service before retrying Start or Stop.' ;;
      image-stage)
        LAST_RESULT[$spec]=image-interrupted
        recovery='Use Launch options → Check pinned image before retrying staging.' ;;
      check) LAST_RESULT[$spec]=check-interrupted ;;
    esac
    {
      printf 'Interruption requested: %s for %s after %s (command exit %s).\n' "$label" "$model" "$took" "$rc"
      printf 'This menu has not confirmed cleanup or partial effects. Review the command output above.\n'
      printf '%s\n' "$recovery"
    } | python3 -m scripts.terminal_format
  elif [ "$rc" -eq 0 ]; then
    printf '✓ %s finished for %s in %s\n' "$label" "$model" "$took"
    case "$action" in status|verify|check|readiness|image-check|image-stage|budget) ;; *) LAST_RESULT[$spec]="$action" ;; esac
  elif [ "$action" = check ] && [ "${LAST_RESULT[$spec]:-}" = check ]; then
    printf '→ %s saved an observation with findings for %s in %s (command exit %s).\n' "$label" "$model" "$took" "$rc"
  else
    printf '✗ %s failed for %s (exit %d) after %s; details above\n' "$label" "$model" "$rc" "$took"
  fi
  OPERATION_RC="$rc" OPERATION_INTERRUPTED="$interrupted"
  return 0
}


# A failed Start already reports its checks. Offer one narrowly scoped retry
# only when the existing structured blocker records contain memory warnings.
start_catalog_spec() {
  local label="$1" spec="$2" blockers question allow=0 rc
  shift 2
  blockers=$(mktemp) || return 1
  run_operation start "$label" "$spec" "$RECIPE_MODEL" env PULSAR_START_BLOCKERS_FILE="$blockers" \
    "$REPO_DIR/pulsar" start "$spec" "$@"
  if [ "$OPERATION_RC" -eq 1 ] && [ "$OPERATION_INTERRUPTED" -eq 0 ] \
      && catalog_menu memory-warning --blockers-file "$blockers"; then
    allow=1
  fi
  rm -f "$blockers"
  [ "$allow" -eq 1 ] || return 0
  question=$(printf '%s' "$ROW_JSON" | catalog_menu confirm --spec-id "$spec" \
    --action start-memory ${MENU_NODE:+--node "$MENU_NODE"}) || return 0
  confirm "$question" no || { rc=$?; echo 'Start was not retried.'; [ "$rc" -ne 130 ] || return 130; return 0; }
  run_operation start 'Start accepting memory warning' "$spec" "$RECIPE_MODEL" \
    "$REPO_DIR/pulsar" start "$spec" "$@" --accept-memory-warn
}


stage_catalog_image() {
  local label="$1" spec="$2" choice plan rc index verb mode_name
  shift 2
  local -a mode=() names=()
  choice=$(choose_index "Stage the catalog's pinned image" "Pull pinned image from registry" \
    "Copy pinned image from this node" "Back") || { rc=$?; return "$rc"; }
  case "$choice" in
    0) mode=(--pull); verb=Pull; mode_name=pull-exact-digest ;;
    1) verb=Copy; mode_name=stream-from-controller ;;
    *) return 1 ;;
  esac
  require_cluster_nodes "$RECIPE_NODES" >/dev/null || return 0
  for index in "${!CLUSTER_NODE_IDS[@]}"; do names+=(--node-name "$index=$(human_node_name "$index")"); done
  plan=$(mktemp) || return 1
  if spin "Planning pinned image staging…" "$REPO_DIR/pulsar" image stage "$spec" "$@" \
      "${mode[@]}" --plan --json >"$plan"; then
    :
  else
    rc=$?
    rm -f "$plan"
    if [ "$rc" -eq 130 ]; then
      printf 'Image preview interrupted; staging was not requested. Review command output for cleanup status.\n'
    else
      printf '✗ Image staging preview failed; nothing was staged.\n'
    fi
    return 0
  fi
  if ! printf '%s' "$ROW_JSON" | catalog_menu image-plan --spec-id "$spec" --plan-file "$plan" \
      --mode "$mode_name" "${names[@]}"; then
    rm -f "$plan"
    return 0
  fi
  rm -f "$plan"
  confirm "$verb the pinned image for $RECIPE_MODEL on the nodes shown? No service starts or is replaced." no \
    || { rc=$?; echo 'Nothing was staged.'; return "$rc"; }
  run_operation image-stage "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/pulsar" image stage "$spec" "$@" "${mode[@]}" --yes
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
  if spin "Planning ${label,,}…" "$REPO_DIR/scripts/model-library.sh" "$@" --plan --json >"$plan"; then
    :
  else
    rc=$?
    rm -f "$plan"
    if [ "$rc" -eq 130 ]; then
      printf 'Preview interrupted; %s was not requested. Review command output for cleanup status.\n' "${label,,}"
    else
      printf '✗ Planning %s failed; nothing changed.\n' "${label,,}"
    fi
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
    prepare|start|stop|status|check|readiness|image-check|image-stage)
      if [ "$RECIPE_NODES" -eq 1 ]; then MENU_NODE=$(select_node) || { rc=$?; return "$rc"; }; fi ;;
  esac
  [ -z "$MENU_NODE" ] || args+=(--node "$MENU_NODE")
  case "$action" in
    budget) run_operation budget 'Inspect storage budget' "$spec" 'all confirmed nodes' "$REPO_DIR/pulsar" model budget ;;
    check) run_operation check "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" check "$spec" "${args[@]}" ;;
    status) run_operation status "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/status.sh" "$spec" "${args[@]}" ;;
    verify) run_operation verify "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" archive verify "$spec" ;;
    readiness) run_operation readiness "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/pulsar" start "$spec" "${args[@]}" --dry-run ;;
    image-check) run_operation image-check "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/pulsar" image check "$spec" "${args[@]}" ;;
    image-stage) stage_catalog_image "$label" "$spec" "${args[@]}" ;;
    start|stop)
      question=$(printf '%s' "$ROW_JSON" | catalog_menu confirm --spec-id "$spec" --action "$action" ${MENU_NODE:+--node "$MENU_NODE"}) || return 0
      confirm "$question" no || { rc=$?; echo 'Nothing changed.'; return "$rc"; }
      if [ "$action" = start ]; then
        start_catalog_spec "$label" "$spec" "${args[@]}"
      else
        run_operation stop "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/down.sh" "$spec" "${args[@]}"
      fi ;;
    archive)
      plan_and_confirm archive "$label" "$spec" archive create "$spec" "${args[@]}" || return $?
      run_operation archive "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" archive create "$spec" "${args[@]}" --yes ;;
    *)
      plan_and_confirm "$action" "$label" "$spec" "$action" "$spec" "${args[@]}" || return $?
      run_operation "$action" "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/scripts/model-library.sh" "$action" "$spec" "${args[@]}" --yes ;;
  esac
}

# Compare two existing catalog specs through the canonical public comparator.
compare_catalog_spec() {
  local spec="$1" result rows entry index rc
  local -a ids=() labels=()
  result=$(catalog list --json) || return 0
  rows=$(printf '%s' "$result" | catalog_menu labels --compare-with "$spec") || return 0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    ids+=("${entry%%$'\t'*}"); labels+=("${entry#*$'\t'}")
  done <<<"$rows"
  if [ "${#ids[@]}" -eq 0 ]; then
    printf 'No other catalog spec supports comparison. Show details remains available.\n'
    return 0
  fi
  index=$(choose_index "Compare the selected spec with another catalog spec" "${labels[@]}" "Back") \
    || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  [ "$index" -lt "${#ids[@]}" ] || return 0
  printf '\nSelected spec: %s\nCompared with: %s\n' "$spec" "${ids[$index]}"
  "$REPO_DIR/pulsar" spec compare --before "$REPO_DIR/releases/$spec.json" \
    --after "$REPO_DIR/releases/${ids[$index]}.json" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
}

# recipe_menu SPEC [READ_ONLY] — loops over one selected spec until Back.
recipe_menu() {
  local spec="$1" read_only="${2:-0}" kind a b c index rc default group title="Choose one operation"
  local -a view_args=()
  if [ "$read_only" = 1 ]; then view_args+=(--read-only); title="Catalog spec (read-only)"; fi
  local -a header=() main=() main_labels=() launch=() launch_labels=() storage=() storage_labels=() labels=()
  while true; do
    ROW_JSON=$(catalog show "$spec" --json) || return 0
    VIEW=$(printf '%s' "$ROW_JSON" | catalog_menu view --spec-id "$spec" \
      --repo-root "$REPO_DIR" --archive-location "$(archive_location)" "${view_args[@]}" \
      ${LAST_RESULT[$spec]:+--after "${LAST_RESULT[$spec]}"}) || return 0
    header=(); main=(); main_labels=(); launch=(); launch_labels=(); storage=(); storage_labels=(); default=""
    while IFS=$'\t' read -r kind a b c; do
      case "$kind" in
        recipe) RECIPE_NODES="$a"; RECIPE_MODEL="$b" ;;
        header) header+=("$a") ;;
        option)
          if [ "$a" = main ]; then main+=("$b"); main_labels+=("$c")
          elif [ "$a" = launch ]; then launch+=("$b"); launch_labels+=("$c")
          else storage+=("$b"); storage_labels+=("$c"); fi ;;
        suggest)
          for index in "${!main[@]}"; do [ "${main[$index]}" != "$a" ] || default="$index"; done ;;
      esac
    done <<<"$VIEW"
    echo
    printf '%s\n' "${header[@]}" | emit_frame
    labels=("${main_labels[@]}")
    [ "${#launch[@]}" -eq 0 ] || labels+=("Launch options…")
    [ "${#storage[@]}" -eq 0 ] || labels+=("Storage and archive…")
    labels+=("Show details" "Published results" "Compare catalog specs" "Back")
    index=$(PULSAR_CHOOSE_DEFAULT="$default" choose_index "$title" "${labels[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    if [ "$index" -lt "${#main[@]}" ]; then
      perform "${main[$index]}" "${main_labels[$index]% (suggested)}" "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
      continue
    fi
    group="${labels[$index]}"
    case "$group" in
      "Launch options…")
        while true; do
          index=$(choose_index "Launch options for the selected catalog spec" "${launch_labels[@]}" "Back") \
            || { rc=$?; [ "$rc" -ne 130 ] || return 130; break; }
          [ "$index" -lt "${#launch[@]}" ] || break
          if perform "${launch[$index]}" "${launch_labels[$index]}" "$spec"; then break
          else
            rc=$?; [ "$rc" -ne 130 ] || return 130
            [ "$rc" -eq 1 ] || break
          fi
        done ;;
      "Storage and archive…")
        while true; do
          index=$(choose_index "Storage and archive" "${storage_labels[@]}" "Back") \
            || { rc=$?; [ "$rc" -ne 130 ] || return 130; break; }
          [ "$index" -lt "${#storage[@]}" ] || break
          if perform "${storage[$index]}" "${storage_labels[$index]}" "$spec"; then break
          else
            rc=$?; [ "$rc" -ne 130 ] || return 130
            [ "$rc" -eq 1 ] || break
          fi
        done ;;
      "Show details") catalog show "$spec" || true ;;
      "Published results") catalog results "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      "Compare catalog specs") compare_catalog_spec "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      *) return 0 ;;
    esac
  done
}

browse() {
  # shellcheck source=ui.sh
  . "$REPO_DIR/scripts/ui.sh"
  require_gum "the catalog menu" "pulsar models list | show SPEC | check SPEC"
  local read_only="${1:-0}" result index rc entry title="Select a catalog spec"
  [ "$read_only" = 0 ] || title+=" (read-only)"
  local -a entries=() ids=() labels=()
  while true; do
    result=$(catalog list --json) || return $?
    # Each entry is "SPEC_ID<tab>label"; labels show the same saved state as models list.
    mapfile -t entries < <(printf '%s' "$result" | catalog_menu labels)
    if [ "${#entries[@]}" -eq 0 ]; then
      catalog list
      [ "$read_only" = 0 ] || return 0
    fi
    ids=(); labels=()
    for entry in "${entries[@]}"; do ids+=("${entry%%$'\t'*}"); labels+=("${entry#*$'\t'}"); done
    [ "$read_only" = 1 ] || labels+=("Storage budget (all nodes)")
    index=$(choose_index "$title" "${labels[@]}" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    if [ "$read_only" = 0 ] && [ "$index" -eq "${#ids[@]}" ]; then
      run_operation budget 'Inspect storage budget' '' 'all confirmed nodes' "$REPO_DIR/pulsar" model budget
      continue
    fi
    [ "$index" -lt "${#ids[@]}" ] || return 0
    recipe_menu "${ids[$index]}" "$read_only" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
  done
}

command="${1:-}"
[ $# -eq 0 ] || shift
case "$command" in
  "") if [ -t 0 ]; then browse; else catalog list; fi ;;
  menu)
    for arg in "$@"; do
      [ "$arg" != --json ] || { echo "error: the catalog menu is interactive; use ./pulsar models list --json" >&2; exit 2; }
    done
    read_only=0
    if [ "${1:-}" = --read-only ]; then read_only=1; shift; fi
    [ $# -eq 0 ] || { echo "error: use ./pulsar models menu [--read-only], or ./pulsar models list --json" >&2; exit 2; }
    browse "$read_only" ;;
  list|show|results) catalog "$command" "$@" ;;
  check) exec "$REPO_DIR/scripts/model-library.sh" check "$@" ;;
  --json) catalog list --json "$@" ;;
  --help|-h|help) catalog --help ;;
  *) echo 'usage: pulsar models list|show SPEC|results SPEC|check SPEC|menu [--read-only]' >&2; exit 2 ;;
esac
