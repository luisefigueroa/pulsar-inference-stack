#!/usr/bin/env bash
# Models and disk space… (home menu): room on each node, each model on the
# cluster with its size, where it is, and whether it is ready, kept and
# archived; then what to do about them, each answered with a plan.
#
# Entering reads the model files (./pulsar model files --json) and the storage
# budget (./pulsar model budget --json) once, as the Model files page does, and
# reuses them until a plan runs or Look again is chosen, so no screen probes
# twice in a row. A node that did not answer makes its files and room
# unknown, not absent, and nothing destructive is offered while it does not
# answer (scripts/models_page.py). The choices:
#   Free up space…      what can go, largest gain first, each with what it
#                       frees and what it costs to get back, then what cannot
#                       go and why: model purge, model remove, model move
#   Get a model ready ahead of time…
#                       Serve a model…'s plan without its Start
#                       (scripts/serve-menu.sh --ready)
#   Keep a model ready… model pin, or model unpin to stop keeping it
#   Back up a model…    model archive create, one step per required snapshot
#   Check the archives… model archive verify, one step per archived snapshot
# Back up and Check need the archive location; without one the page says where
# it is set instead of offering them. Each plan lists its steps and what each
# changes, starts on Cancel, runs each step's own preview when its turn comes
# and stops when it is blocked or would do what the plan did not say. The
# outcome names the commands that ran. Nothing here unpins on its own, deletes
# an archive, deletes files no record names, or moves a home the operator did
# not choose to move. The Model files page stays under Help and
# troubleshooting… → Advanced: single operations….
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "Models and disk space…" \
  "./pulsar model files" "each model on every node: its size and where it is" \
  "./pulsar model budget" "room on each node" \
  "./pulsar model purge SPEC --plan" "free up space: remove a model from its serving nodes; then --yes" \
  "./pulsar model remove SPEC --plan" "after purge: remove it from the cluster; then --yes" \
  "./pulsar model move SPEC --node NODE --plan" "move a model's files to a node with room; then --yes" \
  "./pulsar model pin SPEC --plan" "keep a model ready (model unpin stops keeping it); then --yes" \
  "./pulsar model archive create SPEC --plan" "back up a model; then --yes" \
  "./pulsar model archive verify SPEC" "check a model's archives" \
  "./pulsar model prepare SPEC --plan" "get a model ready ahead of time; then --yes"
ui_keep_styles

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-models.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
# The width is read once, before any capture: a captured stdout hides the terminal.
COLUMNS_SEEN=$(_ui_terminal_columns) || COLUMNS_SEEN=""
models() { python3 -m scripts.models_page "$@" --repo-root "$REPO_DIR" ${COLUMNS_SEEN:+--width "$COLUMNS_SEEN"}; }
text() { python3 -m scripts.terminal_format --text; }
CHANGED=4

COMMAND=()
resolve() {
  COMMAND=("$@")
  [ "${COMMAND[0]:-}" != ./pulsar ] || COMMAND[0]="$REPO_DIR/pulsar"
}

# ---------------------------------------------------------------------------
# One files scan and one storage budget
# ---------------------------------------------------------------------------

FILES="$WORK/files.json" BUDGET="$WORK/budget.json" CATALOG="$WORK/catalog.json" FILES_AT=0 FILES_ERROR=""
READ=(--files "$FILES" --budget "$BUDGET" --catalog "$CATALOG")
capture() {
  local rc=0
  [ "$FILES_AT" -eq 0 ] || return 0
  if ! python3 -m model_library.catalog list --json >"$CATALOG"; then
    FILES_ERROR="the catalog could not be read; the reason is above"
    return 0
  fi
  spin "Checking model files on every node…" bash -c '"$@" 2>"$0"' "$WORK/files.err" \
    "$REPO_DIR/pulsar" model files --json >"$FILES" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  # A node that did not answer still gives a document; the page names that node.
  if ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d=d.get("result",d); assert d["kind"]=="pulsar-model-file-inventory"' \
      "$FILES" 2>/dev/null; then
    FILES_ERROR="the model files scan exited $rc without a readable result"
    return 0
  fi
  # Read-only, but it takes the shared storage lock, so it runs after the scan,
  # as the Model files page does. A failure leaves the room for copies unknown.
  rc=0
  spin "Reading storage budgets…" bash -c '"$@" 2>"$0"' "$WORK/budget.err" \
    "$REPO_DIR/pulsar" model budget --json >"$BUDGET" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  FILES_AT=$(date +%s) FILES_ERROR=""
}
# A plan that ran, or Look again, makes the scan stale.
forget() { FILES_AT=0; }
view() { models "$@" "${READ[@]}"; }

# show CHOICE_ROWS ARGS...: the screen scripts/models_page.py ARGS prints, above
# its choices (CHOICE_ROWS lines) when both fit the terminal's rows (a blank
# line, the screen, Gum's choices and its five rows), else in the pager. The
# pager runs an executable and renders at its own width.
show() {
  local count="$1" screen lines rows rc=0
  shift
  screen=$(models "$@") || return 1
  lines=$(printf '%s\n' "$screen" | wc -l)
  if rows=$(_ui_terminal_rows) && [ $((lines + 1 + count + 5)) -gt "$rows" ]; then
    page_output "" python3 -m scripts.models_page "$@" --repo-root "$REPO_DIR" || rc=$?
    return "$rc"
  fi
  printf '\n%s\n' "$screen"
}
# choice_rows LABEL...: how many rows the choices take, a card's lines counted.
choice_rows() { printf '%s\n' "$@" | wc -l; }

# ---------------------------------------------------------------------------
# A plan and its run
# ---------------------------------------------------------------------------

PLAN="$WORK/plan.json"

# build_plan TARGET [--archive-first]: the previews that can run now, then the plan.
build_plan() {
  local target="$1" line index rest rc
  shift
  local -a extra=("$@") previews=() command=()
  rm -f -- "$WORK"/preview-*.json
  while IFS= read -r line; do
    index=${line%%$'\t'*} rest=${line#*$'\t'}
    IFS=$'\t' read -r -a command <<<"$rest"
    resolve "${command[@]}"
    rc=0
    spin "Checking what step $((index + 1)) would do…" bash -c '"$@" 2>"$0"' "$WORK/preview.err" \
      "${COMMAND[@]}" >"$WORK/preview-$index.json" || rc=$?
    [ "$rc" -ne 130 ] || return 130
    previews+=(--preview "$index=$WORK/preview-$index.json" --preview-rc "$index=$rc")
  done < <(view previews --target "$target" "${extra[@]}")
  view compose --target "$target" "${extra[@]}" "${previews[@]}" --out "$PLAN"
}

# run_plan TITLE: each step's own preview, then its command, in order; the
# outcome names the commands that ran. Returns CHANGED, or 130.
run_plan() {
  local title="$1" index kind preview sentence verdict reason rc=0 interrupted done=0 total failed="" halted=""
  local -a indexes=() kinds=() previews=() sentences=() ran=()
  while IFS=$'\t' read -r index kind preview sentence; do
    indexes+=("$index"); kinds+=("$kind"); previews+=("$preview"); sentences+=("$sentence")
  done < <(models steps --plan "$PLAN")
  total=${#indexes[@]}
  for index in "${indexes[@]}"; do
    rc=0 verdict=same reason=""
    if [ "${previews[$index]}" = 1 ]; then
      mapfile -t COMMAND < <(models command --plan "$PLAN" --step "$index" --preview)
      resolve "${COMMAND[@]}"
      spin "Checking step $((index + 1)) of $total before it runs…" bash -c '"$@" 2>"$0"' "$WORK/step.err" \
        "${COMMAND[@]}" >"$WORK/step.json" || rc=$?
      [ "$rc" -ne 130 ] || { failed=$index; break; }
      IFS=$'\t' read -r verdict reason < <(models check-step --plan "$PLAN" --step "$index" \
        --preview-file "$WORK/step.json" --preview-rc "$rc")
      rc=0
    fi
    case "$verdict" in
      same) ;;
      skip)
        printf '\n• Step %s of %s needs nothing: %s.\n' $((index + 1)) "$total" "$reason" | text
        done=$((done + 1))
        continue ;;
      *) failed=$index halted=${reason:-its own check did not confirm the plan}; break ;;
    esac
    mapfile -t COMMAND < <(models command --plan "$PLAN" --step "$index")
    resolve "${COMMAND[@]}"
    printf '\n→ Step %s of %s: %s Ctrl-C requests interruption; this menu returns when the command exits.\n' \
      $((index + 1)) "$total" "${sentences[$index]}" | text
    interrupted=0
    trap 'interrupted=1' INT
    set +e
    "${COMMAND[@]}"
    rc=$?
    set -e
    trap - INT
    [ "$interrupted" = 0 ] || [ "$rc" -gt 128 ] || rc=130
    # A failed command ran too: the outcome names it with the others.
    if [ "$rc" -ne 0 ]; then failed=$index ran+=(--ran-step "$index"); break; fi
    done=$((done + 1)) ran+=(--ran-step "$index")
  done
  local -a result=(--plan "$PLAN" --done "$done" "${ran[@]}")
  if [ -n "$failed" ]; then
    result+=(--failed "$failed" --rc "$rc")
    [ -z "$halted" ] || result+=(--halted "$halted")
  fi
  # Whatever ran, or whatever a step's own check saw, the page looks again.
  forget
  echo
  models outcome "${result[@]}"
  wait_continue "$title" || return $?
  return "$CHANGED"
}

# plan_page TITLE TARGET: the plan, Cancel first, until Cancel or a run.
plan_page() {
  local title="$1" target="$2" choice action label rc
  local -a extra=() actions=() labels=()
  while true; do
    build_plan "$target" "${extra[@]}" \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; wait_continue "Plan not made" || return $?; return 0; }
    actions=() labels=()
    while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done \
      < <(models choices-of --plan "$PLAN")
    show "${#labels[@]}" render --plan "$PLAN" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
    # The cursor starts on Cancel (D1): approving a plan takes a deliberate move.
    choice=$(PULSAR_CHOOSE_DEFAULT=0 choose_index "$title" "${labels[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    case "${actions[$choice]}" in
      go) run_plan "$title"; return $? ;;
      archive-first) extra=(--archive-first) ;;
      *) return 0 ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# The intents
# ---------------------------------------------------------------------------

# intent_page INTENT TITLE: its screen and choices until Back; a plan that ran
# returns CHANGED, so the page looks again.
intent_page() {
  local intent="$1" title="$2" choice rc target label
  local -a targets=() labels=()
  while true; do
    capture || return $?
    [ -z "$FILES_ERROR" ] || return "$CHANGED"
    targets=() labels=()
    # A label too long for the row is a card of lines at their screen columns.
    while IFS=$'\t' read -r target label; do targets+=("$target"); labels+=("${label//$'\x1f'/$'\n'}"); done \
      < <(view options "$intent")
    show "$(choice_rows "${labels[@]}")" screen "$intent" "${READ[@]}" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
    choice=$(choose_index "$title" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    [ "${targets[$choice]}" != back ] || return 0
    rc=0
    plan_page "$title" "${targets[$choice]}" || rc=$?
    [ "$rc" -ne 130 ] || return 130
    [ "$rc" -ne "$CHANGED" ] || return "$CHANGED"
  done
}

# ---------------------------------------------------------------------------

while true; do
  rc=0
  capture || rc=$?
  [ "$rc" -ne 130 ] || exit 130
  if [ -n "$FILES_ERROR" ]; then
    printf '\n✗ Models and disk space could not be checked: %s. What is on the nodes is unknown, not absent.\n' \
      "$FILES_ERROR" | text
    choice=$(choose_index "Models and disk space" "Look again" "Back") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
    [ "$choice" = 0 ] || exit 0
    forget
    continue
  fi
  actions=() labels=()
  while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done < <(view choices)
  show "${#labels[@]}" page "${READ[@]}" || { rc=$?; [ "$rc" -ne 130 ] || exit 130; }
  choice=$(choose_index "Models and disk space" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  rc=0
  case "${actions[$choice]}" in
    free) intent_page free "Free up space" || rc=$? ;;
    keep) intent_page keep "Keep a model ready" || rc=$? ;;
    backup) intent_page backup "Back up a model" || rc=$? ;;
    check) intent_page check "Check the archives" || rc=$? ;;
    ready)
      # Serve a model…'s list and plan, without Start. It exits 4 when its plan
      # ran; then the page looks again, and otherwise reuses its scan.
      "$REPO_DIR/scripts/serve-menu.sh" --ready || rc=$?
      [ "$rc" -ne 4 ] || forget
      [ "$rc" -eq 130 ] || rc=0 ;;
    again) forget ;;
    *) exit 0 ;;
  esac
  [ "$rc" -ne 130 ] || exit 130
done
