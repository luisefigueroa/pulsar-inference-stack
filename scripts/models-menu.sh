#!/usr/bin/env bash
# Models and disk space… (home menu): room on each node, each model on the
# cluster with its size, where it is, and whether it is ready, kept and
# archived; then what to do about them, each answered with a plan.
#
# Check and manage shares the overview's file scan and storage budget. Look
# again and the return from a plan request a new observation. Direct entry
# without the overview reads both commands here. A node that did not answer makes its files and room
# unknown, not absent, and nothing destructive is offered while it does not
# answer (scripts/models_page.py). The choices follow a model's life on the
# cluster, in four groups. Each is here because the page's own words send the
# operator to it. Two operations are reached another way too, each for another
# purpose: Get a model with a recipe ready to serve… is Serve a model… without
# its Start, and a home also moves from Free up space…, where the page offers
# the move that makes room on a node past its reserve:
#   Download a model that has no recipe yet…
#                       model acquire --model-id ORG/NAME --model-commit COMMIT:
#                       it asks the model, the commit and the node, shows the
#                       command's own preview, then runs it with the exact
#                       commit and saves the manifest (scripts/download_page.py).
#                       The command refuses a second copy before it downloads.
#   Get a model with a recipe ready to serve…
#                       Serve a model…'s plan without its Start
#                       (scripts/serve-menu.sh --ready)
#   Move a model to another node…
#                       model move to the node the operator names, at any time
#                       (Free up space… offers a move only off a node past its
#                       reserve): each home nothing depends on, or why it stays
#
#   Keep a model on the cluster…
#                       model pin, or model unpin to stop keeping it
#   Back up a model to the archive…
#                       model archive create, one step per required snapshot
#   Check the archive copies…
#                       model archive verify, one step per archived snapshot
#
#   Free up space…      what can go, largest gain first, each with what it
#                       frees and what it costs to get back, then what cannot
#                       go and why: model purge, model remove, model move
#
#   Record model files found on the nodes…
#                       one plan from the same scan: model acquire (Download's
#                       own reuse; it never downloads) for each home folder
#                       found, then model prepare --adopt-unrecorded for each
#                       recipe's copies no record names (the only choice that
#                       passes that flag), and what neither can record, with why
#   Look again (checks nodes now)
# A choice whose label cannot say what it does has a second line under it
# (scripts/models_page.py HINTS), and a blank line ends each group; on a
# terminal too short for the page and those lines, the labels stand alone.
# A home that no recipe in the catalog uses, as Download a model that has no
# recipe yet… leaves it, is reached by Free up space…, Back up… and Check… with
# the manifest file saved under .pulsar/manifests (--manifest FILE in place of
# a spec); without that file the page says so, names the choice that saves
# it, and offers nothing for it.
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
# The locations its Python helpers read, as ./pulsar commands read them (.env included).
# shellcheck source=menu-settings.sh
. "$REPO_DIR/scripts/menu-settings.sh"
require_gum "Models and disk space…" \
  "./pulsar model files" "each model on every node: its size and where it is" \
  "./pulsar model budget" "room on each node" \
  "./pulsar model purge SPEC --plan" "free up space: remove a model from its serving nodes; then --yes" \
  "./pulsar model remove SPEC --plan" "after purge: remove it from the cluster; then --yes" \
  "./pulsar model move SPEC --node NODE --plan" "move a model's files to a node with room; then --yes" \
  "./pulsar model pin SPEC --plan" "keep a model ready (model unpin stops keeping it); then --yes" \
  "./pulsar model archive create SPEC --plan" "back up a model; then --yes" \
  "./pulsar model archive verify SPEC" "check a model's archives" \
  "./pulsar model prepare SPEC --plan" "get a model with a recipe ready to serve; then --yes" \
  "./pulsar model acquire SPEC --node NODE --plan" "record model files found on the nodes: a home with no record; then --yes" \
  "./pulsar model acquire --model-id ORG/NAME --model-commit COMMIT --node NODE --plan" "download a model that has no recipe yet; then --manifest-out FILE --yes" \
  "./pulsar model remove --manifest FILE --plan" "remove files no recipe uses (also: model move, model archive create); then --yes" \
  "./pulsar model prepare SPEC --adopt-unrecorded --plan" "record model files found on the nodes: take over a copy no record names; then --yes"
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
REFRESH=()
READ=(--files "$FILES" --budget "$BUDGET" --catalog "$CATALOG")
capture() {
  local rc=0
  [ "$FILES_AT" -eq 0 ] || return 0
  if [[ -n ${PULSAR_MENU_SESSION:-} ]]; then
    python3 -u "$REPO_DIR/scripts/storage_overview.py" manage "$WORK" "${REFRESH[@]}" 2>"$WORK/files.err" || rc=$?
    case "$rc" in
      0) FILES_AT=$(date +%s) FILES_ERROR=""; return 0 ;;
      10) rc=0 ;;  # The overview could not use its cache. Check the nodes directly.
      130) return 130 ;;
      *) FILES_ERROR=$(<"$WORK/files.err"); FILES_ERROR=${FILES_ERROR:-the storage check failed}; return 0 ;;
    esac
  fi
  if ! "$REPO_DIR/pulsar" models list --json >"$CATALOG"; then
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
forget() { FILES_AT=0; REFRESH=(--again); }
view() { models "$@" "${READ[@]}"; }

# show CHOICE_ROWS ARGS...: the screen scripts/models_page.py ARGS prints, above
# its choices (CHOICE_ROWS lines) when both fit the terminal's rows (a blank
# line, the screen, Gum's choices and its five rows), else in the pager. The
# pager runs an executable and renders at its own width.
show() {
  local count="$1" screen
  shift
  screen=$(models "$@") || return 1
  show_screen "$count" "$screen" "$@"
}
# show_screen CHOICE_ROWS SCREEN ARGS...: SCREEN, which ARGS printed, shown as show does.
show_screen() {
  local count="$1" screen="$2" lines rows rc=0
  shift 2
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
  # The list arrives on descriptor 3 and each preview reads /dev/null: a
  # preview that reads its standard input (ssh to a remote node does) must
  # not take the previews still to run. Gum's spinner keeps the terminal.
  while IFS= read -r -u 3 line; do
    index=${line%%$'\t'*} rest=${line#*$'\t'}
    IFS=$'\t' read -r -a command <<<"$rest"
    resolve "${command[@]}"
    rc=0
    spin "Checking what step $((index + 1)) would do…" bash -c '"$@" 2>"$0" </dev/null' "$WORK/preview.err" \
      "${COMMAND[@]}" >"$WORK/preview-$index.json" || rc=$?
    [ "$rc" -ne 130 ] || return 130
    previews+=(--preview "$index=$WORK/preview-$index.json" --preview-rc "$index=$rc")
  done 3< <(view previews --target "$target" "${extra[@]}")
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
    target=${targets[$choice]}
    if [[ "$target" == pick:* ]]; then
      # Move a model to another node…: the operator names the node; no plan chooses it.
      local -a places=() hosts=()
      while IFS=$'\t' read -r target label; do places+=("$target"); hosts+=("${label//$'\x1f'/$'\n'}"); done \
        < <(view destinations --manifest "${targets[$choice]#pick:}")
      [ "${#places[@]}" -gt 0 ] || continue
      choice=$(choose_index "Which node should keep the files?" "${hosts[@]}" "Back") \
        || { rc=$?; [ "$rc" -ne 130 ] || return 130; continue; }
      [ "$choice" -lt "${#places[@]}" ] || continue
      target=${places[$choice]}
    fi
    rc=0
    plan_page "$title" "$target" || rc=$?
    [ "$rc" -ne 130 ] || return 130
    [ "$rc" -ne "$CHANGED" ] || return "$CHANGED"
  done
}

# ---------------------------------------------------------------------------
# Download a model that has no recipe yet
# ---------------------------------------------------------------------------

# The page's own options go before the arguments: outcome takes the command
# that ran after "--", and nothing after that is an option.
download() {
  local command="$1"
  shift
  python3 -m scripts.download_page "$command" --repo-root "$REPO_DIR" ${COLUMNS_SEEN:+--width "$COLUMNS_SEEN"} "$@"
}

# download_page: the model, the commit and the node, then the command's own
# preview with the cursor on Cancel, then the command. Returns CHANGED after a
# run, 130 on Ctrl-C, else 0.
download_page() {
  local title="Download a model" model ref host choice rc=0 interrupted
  local action commit manifest save label now_action now_commit rest
  local -a hosts=() selector=() ran=() saved=()
  model=$(prompt_input "Which model on Hugging Face?" "ORG/NAME") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  if ! [[ "$model" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    printf '\n✗ That is not a model name. Write it as on Hugging Face: the organization, a slash, the model.\n' | text
    wait_continue "$title" || return $?
    return 0
  fi
  ref=$(prompt_input "Which commit, branch or tag of $model?" "main") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  if ! [[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    printf '\n✗ That is not a commit, a branch or a tag.\n' | text
    wait_continue "$title" || return $?
    return 0
  fi
  mapfile -t hosts < <(download nodes --files "$FILES")
  if [ "${#hosts[@]}" -eq 0 ]; then
    printf '\n✗ No node answered the check, so there is nowhere to download to. Look again first.\n' | text
    wait_continue "$title" || return $?
    return 0
  fi
  choice=$(choose_index "Which node should keep the files?" "${hosts[@]}" "Back") \
    || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  [ "$choice" -lt "${#hosts[@]}" ] || return 0
  host=${hosts[$choice]}
  selector=(model acquire --model-id "$model" --model-commit "$ref" --node "$host")
  rc=0
  spin "Checking what the download would do…" bash -c '"$@" 2>"$0" </dev/null' "$WORK/download.err" \
    "$REPO_DIR/pulsar" "${selector[@]}" --plan --json >"$WORK/download-preview.json" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  echo
  download plan --model "$model" --ref "$ref" --host "$host" --preview "$WORK/download-preview.json" \
    --preview-rc "$rc" --out "$WORK/download.facts" || { wait_continue "$title" || return $?; return 0; }
  IFS=$'\x1f' read -r action commit manifest save label <"$WORK/download.facts"
  if [ "$action" = blocked ]; then
    wait_continue "$title" || return $?
    return 0
  fi
  # The cursor starts on Cancel: a download takes a deliberate move.
  choice=$(PULSAR_CHOOSE_DEFAULT=0 choose_index "$title" "Cancel" "$label") \
    || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  [ "$choice" = 1 ] || return 0
  # The command's own preview again, now with the exact commit: the run goes
  # ahead only when it still does what was approved. Files that were to be
  # kept and are gone since would otherwise become a download nobody approved.
  rc=0
  spin "Checking that the plan still holds…" bash -c '"$@" 2>"$0" </dev/null' "$WORK/download.err" \
    "$REPO_DIR/pulsar" model acquire --model-id "$model" --model-commit "$commit" --node "$host" --plan --json \
    >"$WORK/download-check.json" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  download plan --model "$model" --ref "$commit" --host "$host" --preview "$WORK/download-check.json" \
    --preview-rc "$rc" --out "$WORK/download-check.facts" >/dev/null || true
  now_action="" now_commit=""
  [ ! -s "$WORK/download-check.facts" ] \
    || IFS=$'\x1f' read -r now_action now_commit rest <"$WORK/download-check.facts"
  if [ "$now_action" != "$action" ] || [ "$now_commit" != "$commit" ]; then
    printf '\n✗ Nothing ran: what the command would do changed after the plan was shown. Choose this again to see the plan as it is now.\n' | text
    # Something changed on the nodes, so the page looks again.
    forget
    wait_continue "$title" || return $?
    return "$CHANGED"
  fi
  # The run names the exact commit the preview printed, never the branch or tag.
  ran=(./pulsar model acquire --model-id "$model" --model-commit "$commit" --node "$host")
  saved=(--manifest "$manifest")
  if [ "$save" = 1 ]; then
    mkdir -p -- "$(dirname "$manifest")"
    ran+=(--manifest-out "$manifest")
  else
    saved+=(--kept)
  fi
  ran+=(--yes)
  resolve "${ran[@]}"
  printf '\n→ %s Ctrl-C requests interruption; this menu returns when the command exits.\n' \
    "Putting $model on $host." | text
  interrupted=0 rc=0
  trap 'interrupted=1' INT
  set +e
  "${COMMAND[@]}"
  rc=$?
  set -e
  trap - INT
  [ "$interrupted" = 0 ] || [ "$rc" -gt 128 ] || rc=130
  forget
  echo
  download outcome --model "$model" --commit "$commit" --host "$host" --action "$action" --rc "$rc" \
    "${saved[@]}" -- "${ran[@]}"
  wait_continue "$title" || return $?
  return "$CHANGED"
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
  # Each choice is a card: its label, a second line where the label cannot say what it does, and
  # a blank line after the last choice of a group. When the page and the cards do not fit the
  # terminal's rows together but the page and the labels alone do, the labels alone are shown,
  # so the page stays on screen above its choices.
  actions=() labels=() cards=()
  while IFS=$'\t' read -r action label card; do
    actions+=("$action"); labels+=("$label"); cards+=("${card//$'\x1f'/$'\n'}")
  done < <(view choices)
  screen=$(view page) || screen=""
  lines=$(printf '%s\n' "$screen" | wc -l)
  card_rows=$(choice_rows "${cards[@]}")
  if ! rows=$(_ui_terminal_rows) || [ $((lines + 1 + card_rows + 5)) -le "$rows" ] \
      || [ $((lines + 1 + ${#labels[@]} + 5)) -gt "$rows" ]; then
    labels=("${cards[@]}")
  fi
  [ -z "$screen" ] || PAGE_OUTPUT_HINT='↑/↓ scroll · q storage actions' \
    show_screen "$(choice_rows "${labels[@]}")" "$screen" page "${READ[@]}" \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; }
  choice=$(choose_index "Models and disk space" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  rc=0
  case "${actions[$choice]}" in
    free) intent_page free "Free up space" || rc=$? ;;
    keep) intent_page keep "Keep a model on the cluster" || rc=$? ;;
    move) intent_page move "Move a model to another node" || rc=$? ;;
    backup) intent_page backup "Back up a model to the archive" || rc=$? ;;
    check) intent_page check "Check the archive copies" || rc=$? ;;
    # One plan from the page's scan: what it can record, then what it cannot.
    find) plan_page "Record model files" find || rc=$? ;;
    download)
      download_page || rc=$?
      [ "$rc" -eq 130 ] || rc=0 ;;
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
