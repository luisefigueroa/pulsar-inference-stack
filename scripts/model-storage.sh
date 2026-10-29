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
# The menu captures what catalog_menu prints, so Python can't see the
# terminal; it gets the width ui.sh reads from it.
catalog_menu() {
  local columns
  if columns=$(_ui_terminal_columns); then
    COLUMNS=$columns python3 -m model_library.catalog_menu "$@"
  else
    python3 -m model_library.catalog_menu "$@"
  fi
}

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
# LAST_AT holds when a successful operation set it, as a Unix time.
declare -A LAST_RESULT=() LAST_AT=()
OPERATION_RC=0 OPERATION_INTERRUPTED=0

# Output to read before the menu redraws: an operation's outcome, or a preview
# that ended without a question. The action that produced it releases it with
# one Continue when it returns.
HOLD_SCREEN=0
hold_screen() {
  [ "$HOLD_SCREEN" = 1 ] || return 0
  HOLD_SCREEN=0
  wait_continue
}

# What this session observed about services: a service inventory captured only
# on request, and each spec's latest Live status, Start or Stop outcome as
# ACTION:STATUS:EPOCH[:NODE]. The labels and frame show their age.
LIVE_FILE="" CATALOG_WORK=""
declare -A SERVICE_OUTCOME=()
LIVE_ARGS=()
live_args() {
  local spec
  LIVE_ARGS=()
  [ -z "$LIVE_FILE" ] || LIVE_ARGS+=(--live "$LIVE_FILE")
  for spec in "${!SERVICE_OUTCOME[@]}"; do LIVE_ARGS+=(--service-outcome "$spec:${SERVICE_OUTCOME[$spec]}"); done
}

catalog_work() {
  [ -z "$CATALOG_WORK" ] || return 0
  CATALOG_WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-catalog-menu.XXXXXX") || return 1
  trap 'rm -rf -- "$CATALOG_WORK"' EXIT
}

# check_running_services CATALOG_JSON — one service inventory, kept for this
# session. A failed or unreadable capture keeps the earlier one.
check_running_services() {
  local catalog_json="$1" captured rc=0
  catalog_work || return 0
  captured="$CATALOG_WORK/inventory.next.json"
  spin "Checking running services on every node…" "$REPO_DIR/pulsar" inventory --json >"$captured" || rc=$?
  [ "$rc" -ne 130 ] || { rm -f -- "$captured"; return 130; }
  if [ "$rc" -eq 0 ] && printf '%s' "$catalog_json" | catalog_menu labels --live "$captured" >/dev/null; then
    mv -f -- "$captured" "$CATALOG_WORK/inventory.json"
    LIVE_FILE="$CATALOG_WORK/inventory.json"
    return 0
  fi
  rm -f -- "$captured"
  local detail=""
  [ -z "$LIVE_FILE" ] || detail="the earlier service inventory is still shown, with its age"
  if [ "$rc" -eq 0 ]; then
    printf '✗ The service inventory could not be read; running services are not shown.\n'
  else
    printf '✗ Running services could not be checked; the reason is above.\n'
    detail="exit $rc${detail:+ · $detail}"
  fi
  [ -z "$detail" ] || printf '  %s\n' "$detail"
  HOLD_SCREEN=1
  return 0
}

# run_operation ACTION LABEL SPEC MODEL COMMAND...
# Runs one explicit operation and reports its outcome, held on screen until
# the caller's Continue. The command owns interruption and cleanup; the menu
# cannot independently confirm its effects.
run_operation() {
  local action="$1" label="$2" spec="$3" model="$4" started=$SECONDS rc interrupted=0 result="" outcome
  shift 4
  [ "$action" != check ] || result=$(mktemp)
  case "$action" in start|status) set -- "$REPO_DIR/scripts/with-connect-card.sh" "$@" ;; esac
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
      # Unreadable Stack output leaves the result unknown: the contract's exit 3.
      printf 'error: check did not return a readable saved observation\n' >&2
      rc=3
    fi
    rm -f "$result"
  fi
  local took
  took=$(elapsed_text $((SECONDS - started)))
  # The contract reports a command interrupted by a signal as 128+signal.
  if [ "$interrupted" = 1 ] || { [ "$rc" -gt 128 ] && [ "$rc" -lt 160 ]; }; then
    interrupted=1
    case "$action" in
      acquire|restore|prepare|move|archive|pin|unpin|purge|remove) LAST_RESULT[$spec]=storage-interrupted ;;
      start|stop) LAST_RESULT[$spec]=service-interrupted ;;
      image-stage) LAST_RESULT[$spec]=image-interrupted ;;
      check) LAST_RESULT[$spec]=check-interrupted ;;
    esac
    report_outcome "$action" "$label" "$model" "$rc" "$took" --interrupted
  elif [ "$rc" -ne 0 ] && [ "$action" = check ] && [ "${LAST_RESULT[$spec]:-}" = check ]; then
    report_outcome check "$label" "$model" "$rc" "$took" --recorded
  else
    if [ "$rc" -eq 0 ]; then
      case "$action" in
        status|verify|check|readiness|image-check|image-stage|budget) ;;
        *) LAST_RESULT[$spec]="$action" LAST_AT[$spec]=$(date +%s) ;;
      esac
    elif [ "$rc" -ne 2 ]; then
      # A failed storage change may have partial effects, so an earlier
      # success no longer describes the files. Exit 2 changed nothing.
      case "$action" in
        acquire|restore|prepare|move|archive|pin|unpin|purge|remove) LAST_RESULT[$spec]=storage-failed ;;
      esac
    fi
    report_outcome "$action" "$label" "$model" "$rc" "$took"
  fi
  if [ -n "$spec" ]; then
    case "$action" in
      start|stop|status)
        # MENU_NODE scopes a one-node recipe's command; a Stop there leaves
        # other nodes unobserved.
        if [ "$interrupted" = 1 ]; then outcome=interrupted; else outcome="$rc"; fi
        SERVICE_OUTCOME[$spec]="$action:$outcome:$(date +%s)${MENU_NODE:+:$MENU_NODE}" ;;
    esac
  fi
  OPERATION_RC="$rc" OPERATION_INTERRUPTED="$interrupted" HOLD_SCREEN=1
  return 0
}


# A failed Start already reports its checks. Offer one narrowly scoped retry
# only when the existing structured blocker records contain memory warnings;
# otherwise offer the menu operations that address the recorded blockers.
start_catalog_spec() {
  local label="$1" spec="$2" blockers question allow=0 rc
  shift 2
  blockers=$(mktemp) || return 1
  run_operation start "$label" "$spec" "$RECIPE_MODEL" env PULSAR_START_BLOCKERS_FILE="$blockers" \
    "$REPO_DIR/pulsar" start "$spec" "$@"
  if [ "$OPERATION_RC" -eq 1 ] && [ "$OPERATION_INTERRUPTED" -eq 0 ] \
      && catalog_menu memory-warning --blockers-file "$blockers"; then
    allow=1
  elif [ "$OPERATION_RC" -ne 0 ] && [ "$OPERATION_INTERRUPTED" -eq 0 ]; then
    rc=0
    start_choices "$spec" "$blockers" "" "$@" || rc=$?
    rm -f "$blockers"
    return "$rc"
  fi
  rm -f "$blockers"
  [ "$allow" -eq 1 ] || return 0
  question=$(printf '%s' "$ROW_JSON" | catalog_menu confirm --spec-id "$spec" \
    --action start-memory ${MENU_NODE:+--node "$MENU_NODE"}) || return 0
  # The question keeps the first outcome on screen; no Continue before it.
  HOLD_SCREEN=0
  confirm "$question" no || { rc=$?; echo 'Start was not retried.'; [ "$rc" -ne 130 ] || return 130; return 0; }
  run_operation start 'Start accepting memory warning' "$spec" "$RECIPE_MODEL" \
    "$REPO_DIR/pulsar" start "$spec" "$@" --accept-memory-warn
}

# check_launch_prerequisites LABEL SPEC START_ARGS... — the existing dry run;
# when it records blockers, the same choices as a refused Start follow.
check_launch_prerequisites() {
  local label="$1" spec="$2" blockers rc=0
  shift 2
  blockers=$(mktemp) || return 1
  run_operation readiness "$label" "$spec" "$RECIPE_MODEL" env PULSAR_START_BLOCKERS_FILE="$blockers" \
    "$REPO_DIR/pulsar" start "$spec" "$@" --dry-run
  if [ "$OPERATION_RC" -ne 0 ] && [ "$OPERATION_INTERRUPTED" -eq 0 ]; then
    start_choices "$spec" "$blockers" --dry-run "$@" || rc=$?
  fi
  rm -f "$blockers"
  return "$rc"
}

# start_choices SPEC BLOCKERS_FILE [--dry-run] START_ARGS...
# After a refused or failed Start (or dry run), offers the menu operations that
# address its recorded blockers, from the codes rather than the printed text.
# Each runs with its own selection, preview and confirmation, and none passes
# --replace or --pull-image. The choice stands in for the Continue hold. Read-only
# choices return here; a declined one returns here too. Returns 130 on Ctrl-C.
start_choices() {
  local spec="$1" blockers="$2" dry_run="$3" title="" line kind label choice rc
  shift 3
  local -a kinds=() labels=()
  while true; do
    title=""; kinds=(); labels=()
    # Each line is KIND, a tab and a nonempty label.
    while IFS= read -r line; do
      kind=${line%%$'\t'*}; label=${line#*$'\t'}
      [ -n "$kind" ] && [ "$kind" != "$line" ] || continue
      if [ "$kind" = title ]; then title=$label; else kinds+=("$kind"); labels+=("$label"); fi
    done < <(printf '%s' "$ROW_JSON" | catalog_menu start-choices --spec-id "$spec" \
      --archive-location "$(archive_location)" --blockers-file "$blockers" ${dry_run:+--dry-run} || true)
    [ "${#kinds[@]}" -gt 0 ] || return 0
    HOLD_SCREEN=0
    choice=$(choose_index "$title" "${labels[@]}" "Back") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    [ "$choice" -lt "${#kinds[@]}" ] || return 0
    rc=0
    case "${kinds[$choice]}" in
      inventory) "$REPO_DIR/pulsar" inventory menu || rc=$? ;;
      topology-menu) "$REPO_DIR/pulsar" topology menu || rc=$? ;;
      topology-check) page_output "Checking saved nodes and fabric…" "$REPO_DIR/pulsar" topology check || rc=$? ;;
      doctor) page_output "Running host diagnostics…" "$REPO_DIR/pulsar" doctor || rc=$? ;;
      # Read-only like the diagnostics: whatever it finds, the choices return.
      logs) page_output "Reading service logs…" "$REPO_DIR/pulsar" logs "$spec" || rc=$? ;;
      verbose)
        # A fresh dry run replaces the recorded blockers, and the choices follow it.
        : >"$blockers"
        run_operation readiness 'Check launch prerequisites with full diagnostics' "$spec" "$RECIPE_MODEL" \
          env PULSAR_START_BLOCKERS_FILE="$blockers" "$REPO_DIR/pulsar" start "$spec" "$@" --dry-run --verbose
        [ "$OPERATION_INTERRUPTED" -eq 0 ] || return 0
        [ "$OPERATION_RC" -ne 0 ] || return 0
        dry_run=--dry-run ;;
      *)
        perform_action "${kinds[$choice]}" "${labels[$choice]}" "$spec" || rc=$?
        [ "$rc" -eq 0 ] && [ "$HOLD_SCREEN" = 1 ] && return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
  done
}

stage_catalog_image() {
  local label="$1" spec="$2" choice plan rc index verb mode_name
  shift 2
  local -a mode=() names=()
  choice=$(choose_index "Stage the serving image (exact digest)" "Pull exact digest from registry" \
    "Copy serving image from this node" "Back") || { rc=$?; return "$rc"; }
  case "$choice" in
    0) mode=(--pull); verb=Pull; mode_name=pull-exact-digest ;;
    1) verb=Copy; mode_name=stream-from-controller ;;
    *) return 1 ;;
  esac
  require_cluster_nodes "$RECIPE_NODES" >/dev/null || return 0
  for index in "${!CLUSTER_NODE_IDS[@]}"; do names+=(--node-name "$index=$(human_node_name "$index")"); done
  plan=$(mktemp) || return 1
  if spin "Planning serving image staging…" "$REPO_DIR/pulsar" image stage "$spec" "$@" \
      "${mode[@]}" --plan --json >"$plan"; then
    :
  else
    rc=$?
    rm -f "$plan"
    if [ "$rc" -eq 130 ]; then
      printf 'Image preview interrupted; staging was not requested. Review command output for cleanup status.\n'
    else
      report_outcome image-stage "$label" "$RECIPE_MODEL" "$rc" "" --preview
    fi
    HOLD_SCREEN=1
    return 0
  fi
  if ! printf '%s' "$ROW_JSON" | catalog_menu image-plan --spec-id "$spec" --plan-file "$plan" \
      --mode "$mode_name" "${names[@]}"; then
    rm -f "$plan"
    HOLD_SCREEN=1
    return 0
  fi
  rm -f "$plan"
  confirm "$verb the serving image for $RECIPE_MODEL on the nodes shown? No service starts or is replaced." no \
    || { rc=$?; echo 'Nothing was staged.'; return "$rc"; }
  run_operation image-stage "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/pulsar" image stage "$spec" "$@" "${mode[@]}" --yes
}

# FULL_PREVIEW PLAN RENDER_ARGS... runs the whole rendered plan, for page_output.
FULL_PREVIEW=(bash -c 'python3 -m model_library.render "${@:2}" <"$1"' _)

# blocked_plan PLAN RENDER_ARGS... — choices after a blocked plan: the full
# preview, Stop for a Pulsar service whose container mounts the files (asked
# first), Live service inventory, and Back. Returns 130 on Ctrl-C, else 1.
blocked_plan() {
  local plan="$1" choice rc kind spec model label question line field
  shift
  local -a kinds=() specs=() models=() labels=()
  # Each line is KIND, SPEC, MODEL and LABEL; the inventory line leaves SPEC
  # and MODEL empty. IFS=$'\t' read would merge those empty fields, so split on
  # each of the first three tabs; the label is the rest of the line.
  while IFS= read -r line; do
    for field in kind spec model; do
      if [[ "$line" == *$'\t'* ]]; then
        printf -v "$field" '%s' "${line%%$'\t'*}"; line=${line#*$'\t'}
      else
        printf -v "$field" '%s' "$line"; line=""
      fi
    done
    label=$line
    [ -n "$kind" ] || continue
    kinds+=("$kind"); specs+=("$spec"); models+=("$model"); labels+=("$label")
  done < <(catalog list --json | catalog_menu blocked-choices --plan-file "$plan" || true)
  while true; do
    choice=$(choose_index "Blocked plan" "Show full preview" "${labels[@]}" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 1; }
    if [ "$choice" -eq 0 ]; then
      page_output "" "${FULL_PREVIEW[@]}" "$plan" "$@" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
      continue
    fi
    choice=$((choice - 1))
    [ "$choice" -lt "${#kinds[@]}" ] || return 1
    case "${kinds[$choice]}" in
      stop)
        question=$(catalog show "${specs[$choice]}" --json \
          | catalog_menu confirm --spec-id "${specs[$choice]}" --action stop) || continue
        confirm "$question" no || { rc=$?; echo 'Nothing changed.'; [ "$rc" -ne 130 ] || return 130; continue; }
        run_operation stop Stop "${specs[$choice]}" "${models[$choice]}" "$REPO_DIR/scripts/down.sh" "${specs[$choice]}"
        return 1 ;;
      inventory)
        "$REPO_DIR/pulsar" inventory menu || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
    esac
  done
}

# plan_and_confirm ACTION LABEL SPEC OPERATION_ARGS...
# Runs the operation's own --plan preview and shows its summary, then asks a
# question that names the model, placement and consequence. Show full preview
# opens the whole rendered plan. Returns 0 only after an explicit yes; No is
# selected first, and Esc also changes nothing.
plan_and_confirm() {
  local action="$1" label="$2" spec="$3" plan question rc
  shift 3
  local -a render_args=(--operation "$1") plan_args=()
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
      report_outcome "$action" "$label" "$RECIPE_MODEL" "$rc" "" --preview
    fi
    HOLD_SCREEN=1
    return 1
  fi
  plan_args=(--spec-id "$spec" --action "$action" --plan-file "$plan")
  echo
  set +e
  printf '%s' "$ROW_JSON" | catalog_menu preview "${plan_args[@]}"
  rc=$?
  # A plan without a summary is shown whole.
  [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || python3 -m model_library.render "${render_args[@]}" <"$plan"
  question=$(printf '%s' "$ROW_JSON" | catalog_menu confirm "${plan_args[@]}" \
    ${MENU_SNAPSHOT:+--snapshot "$MENU_SNAPSHOT"} ${MENU_NODE:+--node "$MENU_NODE"})
  rc=$?
  set -e
  if [ "$rc" -eq 3 ]; then
    printf '✗ The plan is blocked; nothing changed.\n'
    rc=0
    blocked_plan "$plan" "${render_args[@]}" || rc=$?
    rm -f "$plan"
    [ "$rc" -ne 130 ] || return 130
    return 1
  fi
  [ "$rc" -eq 0 ] || { rm -f "$plan"; HOLD_SCREEN=1; return 1; }
  # Gum Ctrl-C (130) at the question or in the pager leaves the menu, as at
  # any other prompt.
  rc=0
  confirm_or_review "$question" "$label" "${FULL_PREVIEW[@]}" "$plan" "${render_args[@]}" || rc=$?
  rm -f "$plan"
  [ "$rc" -ne 0 ] || return 0
  echo 'Nothing changed.'
  [ "$rc" -ne 130 ] || return 130
  return 1
}

# perform ACTION LABEL SPEC — runs one menu action, then holds what it printed
# on screen until Continue. Ctrl-C at the hold returns 130.
perform() {
  local rc=0
  HOLD_SCREEN=0
  perform_action "$@" || rc=$?
  if [ "$rc" -eq 130 ]; then HOLD_SCREEN=0; return 130; fi
  hold_screen || return $?
  return "$rc"
}

# perform_action ACTION LABEL SPEC — gathers snapshot and node, previews, confirms, runs.
perform_action() {
  local action="$1" label="$2" spec="$3" rc question choice
  local -a args=() snapshots=() choices=()
  MENU_SNAPSHOT="" MENU_NODE=""
  # Each snapshot line names the snapshot the operation takes, then its choice label.
  mapfile -t snapshots < <(printf '%s' "$VIEW" | awk -F'\t' -v a="$action" '$1 == "snapshot" && $2 == a {print $3}')
  mapfile -t choices < <(printf '%s' "$VIEW" | awk -F'\t' -v a="$action" '$1 == "snapshot" && $2 == a {print $4}')
  if [ "${#snapshots[@]}" -eq 1 ]; then
    MENU_SNAPSHOT="${snapshots[0]}"
  elif [ "${#snapshots[@]}" -gt 1 ]; then
    choice=$(choose_index "Select a required snapshot" "${choices[@]}") || { rc=$?; return "$rc"; }
    MENU_SNAPSHOT="${snapshots[$choice]}"
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
    readiness) check_launch_prerequisites "$label" "$spec" "${args[@]}" ;;
    image-check) run_operation image-check "$label" "$spec" "$RECIPE_MODEL" "$REPO_DIR/pulsar" image check "$spec" "${args[@]}" ;;
    image-stage) stage_catalog_image "$label" "$spec" "${args[@]}" ;;
    # Read-only, so nothing to confirm; a failure is held on screen to read.
    logs)
      page_output "Reading service logs…" "$REPO_DIR/pulsar" logs "$spec" \
        || { rc=$?; [ "$rc" -ne 130 ] || return 130; HOLD_SCREEN=1; } ;;
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
  page_output "" bash -c 'printf "Selected spec: %s\nCompared with: %s\n\n" "$1" "$2"; shift 2; exec "$@"' _ \
    "$spec" "${ids[$index]}" "$REPO_DIR/pulsar" spec compare --before "$REPO_DIR/releases/$spec.json" \
    --after "$REPO_DIR/releases/${ids[$index]}.json" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
}

# recipe_menu SPEC [READ_ONLY] — loops over one selected spec until Back.
# operation_label CHOICE — the operation's name in a recipe choice, without
# the effect after " — " or the "(suggested)" mark.
operation_label() {
  local label="${1%% — *}"
  printf '%s\n' "${label% (suggested)}"
}

recipe_menu() {
  local spec="$1" read_only="${2:-0}" kind a b c index rc default group title="Choose one operation"
  local -a view_args=()
  if [ "$read_only" = 1 ]; then view_args+=(--read-only); title="Catalog spec (read-only)"; fi
  local -a header=() next=() frame=() main=() main_labels=() launch=() launch_labels=() storage=() storage_labels=()
  local -a labels=()
  local rows
  while true; do
    ROW_JSON=$(catalog show "$spec" --json) || return 0
    live_args
    VIEW=$(printf '%s' "$ROW_JSON" | catalog_menu view --spec-id "$spec" \
      --repo-root "$REPO_DIR" --archive-location "$(archive_location)" "${view_args[@]}" "${LIVE_ARGS[@]}" \
      ${LAST_RESULT[$spec]:+--after "${LAST_RESULT[$spec]}"} ${LAST_AT[$spec]:+--after-at "${LAST_AT[$spec]}"}) || return 0
    header=(); next=(); main=(); main_labels=(); launch=(); launch_labels=(); storage=(); storage_labels=(); default=""
    while IFS=$'\t' read -r kind a b c; do
      case "$kind" in
        recipe) RECIPE_NODES="$a"; RECIPE_MODEL="$b" ;;
        header) header+=("$a") ;;
        next) next+=("$a") ;;
        option)
          if [ "$a" = main ]; then main+=("$b"); main_labels+=("$c")
          elif [ "$a" = launch ]; then launch+=("$b"); launch_labels+=("$c")
          else storage+=("$b"); storage_labels+=("$c"); fi ;;
        suggest)
          for index in "${!main[@]}"; do [ "${main[$index]}" != "$a" ] || default="$index"; done ;;
      esac
    done <<<"$VIEW"
    labels=("${main_labels[@]}")
    [ "${#launch[@]}" -eq 0 ] || labels+=("Launch options…")
    [ "${#storage[@]}" -eq 0 ] || labels+=("Storage and archive…")
    labels+=("Show details" "Published results" "Compare catalog specs" "Back")
    # Next is the frame's last line. When the frame (with its border and the
    # blank line above) and every choice (with Gum's 5 rows) don't fit the
    # terminal, the frame keeps only its first line and Next.
    frame=("${header[@]}" "${next[@]}")
    if rows=$(_ui_terminal_rows) \
        && [ $(( ${#frame[@]} + 3 + ${#labels[@]} + 5 )) -gt "$rows" ]; then
      frame=("${header[0]}" "${next[@]}")
    fi
    echo
    printf '%s\n' "${frame[@]}" | emit_frame
    index=$(PULSAR_CHOOSE_DEFAULT="$default" choose_index "$title" "${labels[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    if [ "$index" -lt "${#main[@]}" ]; then
      perform "${main[$index]}" "$(operation_label "${main_labels[$index]}")" "$spec" \
        || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
      continue
    fi
    group="${labels[$index]}"
    case "$group" in
      "Launch options…")
        while true; do
          index=$(choose_index "Launch options for the selected catalog spec" "${launch_labels[@]}" "Back") \
            || { rc=$?; [ "$rc" -ne 130 ] || return 130; break; }
          [ "$index" -lt "${#launch[@]}" ] || break
          if perform "${launch[$index]}" "$(operation_label "${launch_labels[$index]}")" "$spec"; then break
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
          if perform "${storage[$index]}" "$(operation_label "${storage_labels[$index]}")" "$spec"; then break
          else
            rc=$?; [ "$rc" -ne 130 ] || return 130
            [ "$rc" -eq 1 ] || break
          fi
        done ;;
      "Show details")
        page_output "" python3 -m model_library.catalog show "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      "Published results")
        page_output "" python3 -m model_library.catalog results "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      "Compare catalog specs") compare_catalog_spec "$spec" || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      *) return 0 ;;
    esac
  done
}

browse() {
  # shellcheck source=ui.sh
  . "$REPO_DIR/scripts/ui.sh"
  require_gum "the catalog menu" "pulsar models list | show SPEC | check SPEC"
  ui_keep_styles
  local read_only="${1:-0}" result index rc entry observed title="Select a catalog spec"
  [ "$read_only" = 0 ] || title+=" (read-only)"
  local -a entries=() ids=() labels=()
  while true; do
    result=$(catalog list --json) || return $?
    # Each entry is "SPEC_ID<tab>label"; labels show the same saved state as
    # models list, plus services this session observed. An "observed" entry
    # dates that observation.
    live_args
    mapfile -t entries < <(printf '%s' "$result" | catalog_menu labels "${LIVE_ARGS[@]}")
    ids=(); labels=(); observed=""
    for entry in "${entries[@]}"; do
      if [ "${entry%%$'\t'*}" = observed ]; then observed="${entry#*$'\t'}"; continue; fi
      ids+=("${entry%%$'\t'*}"); labels+=("${entry#*$'\t'}")
    done
    if [ "${#ids[@]}" -eq 0 ]; then
      catalog list
      [ "$read_only" = 0 ] || return 0
    fi
    # Before setup the catalog stays saved records only: nothing probes nodes.
    [ "$read_only" = 1 ] || labels+=("Check running services (service inventory)" "Storage budget (all nodes)")
    [ -z "$observed" ] || printf '\n%s\n' "$observed" | python3 -m scripts.terminal_format
    index=$(choose_index "$title" "${labels[@]}" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    if [ "$index" -lt "${#ids[@]}" ]; then
      recipe_menu "${ids[$index]}" "$read_only" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
      continue
    fi
    case "${labels[$index]:-Back}" in
      "Check running services (service inventory)")
        check_running_services "$result" || return $?
        hold_screen || return $? ;;
      "Storage budget (all nodes)")
        run_operation budget 'Inspect storage budget' '' 'all confirmed nodes' "$REPO_DIR/pulsar" model budget
        hold_screen || return $? ;;
      *) return 0 ;;
    esac
  done
}

# The inventory menu's "Open in catalog" opens one recipe with the service
# inventory it captured; Back returns to the inventory menu.
open_recipe() {
  local spec="$PULSAR_CATALOG_OPEN_SPEC" inventory="${PULSAR_CATALOG_INVENTORY:-}"
  unset PULSAR_CATALOG_OPEN_SPEC PULSAR_CATALOG_INVENTORY
  # shellcheck source=ui.sh
  . "$REPO_DIR/scripts/ui.sh"
  require_gum "the catalog menu" "pulsar models show SPEC"
  ui_keep_styles
  if [ -n "$inventory" ] && [ -s "$inventory" ] && catalog_work; then
    cp -- "$inventory" "$CATALOG_WORK/inventory.json" && LIVE_FILE="$CATALOG_WORK/inventory.json"
  fi
  recipe_menu "$spec"
}

command="${1:-}"
[ $# -eq 0 ] || shift
case "$command" in
  "")
    if [ -t 0 ]; then
      # shellcheck source=ui.sh
      . "$REPO_DIR/scripts/ui.sh"
      if gum_available; then browse; else
        # Asked for no menu by name: show the same saved state as a list.
        printf 'warning: the catalog menu needs Gum on this terminal; showing pulsar models list\n' >&2
        catalog list
      fi
    else catalog list; fi ;;
  menu)
    for arg in "$@"; do
      [ "$arg" != --json ] || { echo "error: the catalog menu is interactive; use ./pulsar models list --json" >&2; exit 2; }
    done
    read_only=0
    if [ "${1:-}" = --read-only ]; then read_only=1; shift; fi
    [ $# -eq 0 ] || { echo "error: use ./pulsar models menu [--read-only], or ./pulsar models list --json" >&2; exit 2; }
    if [ -n "${PULSAR_CATALOG_OPEN_SPEC:-}" ] && [ "$read_only" = 0 ]; then open_recipe; else browse "$read_only"; fi ;;
  list|show|results) catalog "$command" "$@" ;;
  check) exec "$REPO_DIR/scripts/model-library.sh" check "$@" ;;
  --json) catalog list --json "$@" ;;
  --help|-h|help) catalog --help ;;
  *) echo 'usage: pulsar models list|show SPEC|results SPEC|check SPEC|menu [--read-only]' >&2; exit 2 ;;
esac
