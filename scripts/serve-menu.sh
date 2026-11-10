#!/usr/bin/env bash
# Serve a model… (home menu): pick a model, see the plan, approve it once.
#
# 1. The cluster change check: one discovery (./pulsar topology detect --json,
#    which saves the changes noticed) unless the saved record is for the saved
#    membership and under ten minutes old. No change, or only nodes the
#    operator kept as only down: straight to the list. A change: the Review
#    screen (scripts/cluster-review.sh --serve) with Serve on the cluster as
#    saved and, while a service runs, Stop it, save the change, then continue.
# 2. The list: each recipe with one readiness phrase (scripts/serve_plan.py
#    list), from saved records and one service inventory, reused while it is
#    under a minute old.
# 3. A one-node recipe asks which machine serves it, starting where its files
#    are; a multi-node recipe serves on the saved node order, as Start does.
# 4. The plan: the previews that can run now, composed into ordered steps
#    (scripts/serve_plan.py compose). Cancel first, then Go ahead, Get it
#    ready but do not start, Change… and Details.
# 5. The run: each step runs its own preview first and stops the run when it
#    is blocked or would do what the plan did not say. A step that fails or is
#    interrupted ends the run; the outcome names the commands that ran.
# Stopping another service and pulling an image are listed steps, approved
# with the plan (D1). Nothing here passes --replace or --pull-image. Only the
# outcome of a Start refused for nothing but its memory warning offers Start
# with --accept-memory-warn, as a choice of its own.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "Serve a model…" \
  "./pulsar topology detect" "look for changes to the cluster first" \
  "./pulsar models list" "each recipe's saved state" \
  "./pulsar model acquire SPEC --node NODE --plan" "download preview; then --yes" \
  "./pulsar model restore SPEC --node NODE --plan" "restore preview; then --yes" \
  "./pulsar model prepare SPEC --plan" "copy preview; then --yes" \
  "./pulsar image stage SPEC --plan" "serving image preview; then --yes" \
  "./pulsar stop SPEC" "stop the service in the way" \
  "./pulsar start SPEC --dry-run" "Start's checks; then start it"
ui_keep_styles

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-serve.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
# The width is read once, before any capture: a captured stdout hides the terminal.
COLUMNS_SEEN=$(_ui_terminal_columns) || COLUMNS_SEEN=""
serve_plan() { python3 -m scripts.serve_plan "$@" --repo-root "$REPO_DIR" ${COLUMNS_SEEN:+--width "$COLUMNS_SEEN"}; }
text() { python3 -m scripts.terminal_format --text; }

# resolve COMMAND...: prints the composed command one argument per line, with
# ./pulsar as this checkout's.
COMMAND=()
resolve() {
  COMMAND=("$@")
  [ "${COMMAND[0]:-}" != ./pulsar ] || COMMAND[0]="$REPO_DIR/pulsar"
}

# One service inventory, reused while it is under a minute old and no Stop or
# Start ran since. A failed capture leaves INVENTORY_ERROR, and the plan says
# what runs is unknown.
INVENTORY="$WORK/inventory.json" INVENTORY_AT=0 INVENTORY_ERROR=""
capture_inventory() {
  local now rc=0
  now=$(date +%s)
  if [ -s "$INVENTORY" ] && [ "$INVENTORY_AT" -gt 0 ] && [ $((now - INVENTORY_AT)) -lt 60 ]; then return 0; fi
  spin "Checking running services on every node…" bash -c '"$@" 2>"$0"' "$WORK/inventory.err" \
    "$REPO_DIR/pulsar" inventory --json >"$INVENTORY.next" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  # A node that did not answer still gives a document; the plan names that node.
  if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d,dict) and "services" in d' \
      "$INVENTORY.next" 2>/dev/null; then
    mv -f -- "$INVENTORY.next" "$INVENTORY"
    INVENTORY_AT=$now INVENTORY_ERROR=""
  else
    rm -f -- "$INVENTORY.next" "$INVENTORY"
    INVENTORY_AT=0 INVENTORY_ERROR="the service inventory exited $rc without a readable result"
  fi
}
forget_inventory() { INVENTORY_AT=0; }
inventory_args() {
  if [ -n "$INVENTORY_ERROR" ]; then printf '%s\n' --inventory-error "$INVENTORY_ERROR"
  else printf '%s\n' --inventory "$INVENTORY"; fi
}

# ---------------------------------------------------------------------------
# 1. The cluster change check
# ---------------------------------------------------------------------------

# entry_check: 0 to go on to the list, 1 to return home, 130 on Ctrl-C.
entry_check() {
  local state rc=0 choice
  state=$(serve_plan entry) || state=not-checked
  if [ "$state" = discover ]; then
    spin "Checking the cluster for changes…" bash -c '"$@" >/dev/null 2>"$0"' "$WORK/detect.err" \
      "$REPO_DIR/pulsar" topology detect --json || rc=$?
    [ "$rc" -ne 130 ] || return 130
    state=$(serve_plan entry --discovered) || state=not-checked
  fi
  capture_inventory || return $?
  while true; do
    case "$state" in
      unchanged|kept) return 0 ;;
      not-checked)
        echo
        serve_plan entry --explain
        choice=$(choose_index "Serve a model" "Serve on the cluster as saved" "Back") \
          || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 1; }
        [ "$choice" = 0 ] || return 1
        return 0 ;;
      *)
        rc=0
        "$REPO_DIR/scripts/cluster-review.sh" --serve || rc=$?
        case "$rc" in
          130) return 130 ;;
          20) return 0 ;;
          21) stop_save_continue; return $? ;;
          0)
            # A save or a kept answer leaves nothing to review; Back leaves the change.
            state=$(serve_plan entry --discovered) || state=not-checked
            case "$state" in unchanged|kept) forget_inventory; capture_inventory || return $?; return 0 ;; esac
            return 1 ;;
          *) return 1 ;;
        esac ;;
    esac
  done
}

# stop_save_continue: Stop every running Pulsar service, save the change and
# record the nodes' SSH identities, each with its own question where it has
# one, then go on to the list, whose plan copies again where needed and starts.
stop_save_continue() {
  local conf name hosts choice rc=0 nodes
  local -a confs=() names=() refused=() flags=() lines=()
  while IFS=$'\t' read -r conf name hosts; do
    [ -n "$conf" ] || continue
    if [ "$conf" = refused ]; then refused+=("$name"); continue; fi
    confs+=("$conf"); names+=("$name"); lines+=("Stop $name on $hosts. Its model files are kept.")
  done < <(python3 -m model_library.catalog list --json 2>/dev/null | serve_plan running --inventory "$INVENTORY")
  if [ "${#refused[@]}" -gt 0 ] || [ "${#confs[@]}" -eq 0 ]; then
    { echo; printf '✗ Pulsar cannot stop what runs from here: %s\n' "${refused[*]:-no running service was identified}"; } | text
    wait_continue "Not saved" || return $?
    return 1
  fi
  mapfile -t flags < <(serve_plan entry --save-flags)
  {
    echo
    echo "Stop, save the change, then continue"
    echo
    echo "Pulsar will:"
    local index=1 line
    for line in "${lines[@]}"; do printf '  %s. %s\n' "$index" "$line"; index=$((index + 1)); done
    printf '  %s. Save the cluster as it is now; it asks once more before it saves.\n' "$index"
    printf '  %s. Record the nodes'"'"' SSH identities; it shows the fingerprints and asks first.\n' $((index + 1))
    echo "Then choose the model to serve. Its plan copies the files again where needed, taking over copies that"
    echo "still verify, and starts it. Nothing serves from step 1 until that Start."
  } | text
  choice=$(choose_index "Stop, save, then continue" "Cancel" "Go ahead") \
    || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 1; }
  [ "$choice" = 1 ] || return 1
  for index in "${!confs[@]}"; do
    rc=0
    "$REPO_DIR/pulsar" stop "${confs[$index]}" || rc=$?
    forget_inventory
    [ "$rc" -ne 130 ] || return 130
    if [ "$rc" -ne 0 ]; then
      printf '\n✗ %s was not stopped; the reason is above. The cluster was not saved.\n' "${names[$index]}" | text
      wait_continue "Not saved" || return $?
      return 1
    fi
  done
  rc=0
  "$REPO_DIR/pulsar" topology configure "${flags[@]}" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  if [ "$rc" -ne 0 ] || [ "$(serve_plan entry --discovered)" = changed ]; then
    printf '\n✗ The cluster was not saved; the reason is above. Nothing is serving now: %s stopped.\n' \
      "${names[*]}" | text
    wait_continue "Not saved" || return $?
    return 1
  fi
  nodes=$(python3 -m scripts.cluster_status node-count --repo-root "$REPO_DIR" 2>/dev/null || echo 0)
  if [ "$nodes" -gt 1 ]; then
    rc=0
    "$REPO_DIR/pulsar" ssh-trust enroll || rc=$?
    [ "$rc" -ne 130 ] || return 130
    [ "$rc" -eq 0 ] || printf '\n✗ The SSH identities were not recorded; the reason is above.\n' | text
  fi
  printf '\nThe cluster is saved, and nothing is serving. Choose the model to serve.\n' | text
  capture_inventory || return $?
  return 0
}

# ---------------------------------------------------------------------------
# 2. and 3. The list and the machine question
# ---------------------------------------------------------------------------

# read_cards — the list's KIND<TAB>TEXT lines on stdin into CARD_IDS and
# CARD_OPTIONS, one menu option per recipe; a model's name leads its first
# recipe's option and a gap ends its last one (as the catalog list's cards).
CARD_IDS=() CARD_OPTIONS=()
read_cards() {
  local line kind body heading=""
  CARD_IDS=() CARD_OPTIONS=()
  while IFS= read -r line; do
    kind=${line%%$'\t'*} body=${line#*$'\t'}
    case "$kind" in
      heading) heading=$body ;;
      spec)
        CARD_IDS+=("${body%%$'\t'*}")
        CARD_OPTIONS+=("${heading:+$heading$'\n'}${body#*$'\t'}")
        heading="" ;;
      line) [ "${#CARD_OPTIONS[@]}" -eq 0 ] || CARD_OPTIONS[-1]+=$'\n'"$body" ;;
      gap) [ "${#CARD_OPTIONS[@]}" -eq 0 ] || CARD_OPTIONS[-1]+=$'\n' ;;
    esac
  done
}

# choose_node SPEC: a one-node recipe's machine, as its node_id; nothing for a
# multi-node recipe. Returns 1 on Back, 130 on Ctrl-C.
choose_node() {
  local spec="$1" kind first second title="" default="" start="" index
  local -a ids=() labels=()
  while IFS=$'\t' read -r kind first second; do
    case "$kind" in
      title) title=$first ;;
      default) default=$first ;;
      node) ids+=("$first"); labels+=("$second") ;;
    esac
  done < <(printf '%s' "$CATALOG" | serve_plan nodes --spec-id "$spec")
  [ "${#ids[@]}" -gt 0 ] || return 0
  for index in "${!ids[@]}"; do [ "${ids[$index]}" != "$default" ] || start=$index; done
  index=$(PULSAR_CHOOSE_DEFAULT="$start" choose_index "$title" "${labels[@]}" "Back") || return $?
  [ "$index" -lt "${#ids[@]}" ] || return 1
  printf '%s\n' "${ids[$index]}"
}

# ---------------------------------------------------------------------------
# 4. The plan
# ---------------------------------------------------------------------------

# What the operator chose for this recipe: its machine, where missing files
# come from, where they go, and found files Download's plan did not take.
NODE="" SOURCE="" FILES_NODE=""
NOT_FOUND=()
PLAN="$WORK/plan.json"

preview_title() {
  case "$1" in
    files) echo "Looking for model files on every node…" ;;
    restore:*) echo "Planning the restore…" ;;
    hf) echo "Checking hf where the files would be downloaded…" ;;
    prepare) echo "Planning the copies…" ;;
    image) echo "Checking the serving image on each node…" ;;
    start) echo "Running Start's checks without starting…" ;;
    *) echo "Checking…" ;;
  esac
}

# build_plan SPEC: runs the previews that can run now and composes the plan.
build_plan() {
  local spec="$1" line name rest rc again=1 file
  local -a args=(--spec-id "$spec") previews=() hf=() command=() inventory=()
  [ -z "$NODE" ] || args+=(--node "$NODE")
  [ -z "$SOURCE" ] || args+=(--source "$SOURCE")
  [ -z "$FILES_NODE" ] || args+=(--files-node "$FILES_NODE")
  for name in "${NOT_FOUND[@]}"; do args+=(--not-found "$name"); done
  [ ! -e "$WORK/files.json" ] || args+=(--files "$WORK/files.json")
  capture_inventory || return $?
  rm -f -- "$WORK"/preview-*.json "$WORK/hf.json"
  while [ "$again" = 1 ]; do
    again=0
    while IFS= read -r line; do
      name=${line%%$'\t'*} rest=${line#*$'\t'}
      IFS=$'\t' read -r -a command <<<"$rest"
      rc=0
      case "$name" in
        files)
          # The sources depend on it: run it, then ask again which previews to run.
          resolve "${command[@]}"
          spin "$(preview_title files)" bash -c '"$@" 2>"$0"' "$WORK/files.err" "${COMMAND[@]}" \
            >"$WORK/files.json" || rc=$?
          [ "$rc" -ne 130 ] || return 130
          args+=(--files "$WORK/files.json")
          again=1
          break ;;
        hf)
          spin "$(preview_title hf)" bash -c '"$@" 2>"$0"' "$WORK/hf.err" \
            "$REPO_DIR/scripts/node-prerequisites.sh" "${command[0]}" >"$WORK/hf.json" || rc=$?
          [ "$rc" -ne 130 ] || return 130
          hf=(--hf "$WORK/hf.json") ;;
        *)
          file="$WORK/preview-${name//[^A-Za-z0-9_-]/_}.json"
          resolve "${command[@]}"
          spin "$(preview_title "$name")" bash -c '"$@" 2>"$0"' "$WORK/preview.err" "${COMMAND[@]}" \
            >"$file" || rc=$?
          [ "$rc" -ne 130 ] || return 130
          previews+=(--preview "$name=$file") ;;
      esac
    done < <(printf '%s' "$CATALOG" | serve_plan previews "${args[@]}")
  done
  mapfile -t inventory < <(inventory_args)
  COMPOSE_ARGS=("${args[@]}" "${previews[@]}" "${hf[@]}" "${inventory[@]}")
  printf '%s' "$CATALOG" | serve_plan compose "${COMPOSE_ARGS[@]}" --out "$PLAN"
}
COMPOSE_ARGS=()

# show_plan CHOICES: the plan screen above its CHOICES when both fit the
# terminal's rows (a blank line, the screen, then Gum's choices and its five
# rows), else in the pager, as Review does. The pager runs an executable, not
# a shell function, and renders at its own width.
show_plan() {
  local count="$1" screen lines rows rc=0
  screen=$(serve_plan render --plan "$PLAN") || return 1
  lines=$(printf '%s\n' "$screen" | wc -l)
  if rows=$(_ui_terminal_rows) && [ $((lines + 1 + count + 5)) -gt "$rows" ]; then
    page_output "" python3 -m scripts.serve_plan render --plan "$PLAN" --repo-root "$REPO_DIR" || rc=$?
    return "$rc"
  fi
  printf '\n%s\n' "$screen"
}

change_menu() {
  local key a b choice rc
  local -a labels=() actions=() destinations=() names=()
  local nodes=0 files=0 archive=0 source=-
  while IFS=$'\t' read -r key a b; do
    case "$key" in
      nodes) nodes=$a ;;
      files) files=$a ;;
      archive) archive=$a ;;
      source) source=$a ;;
      destination) destinations+=("$a"); names+=("$b") ;;
    esac
  done < <(serve_plan facts --plan "$PLAN")
  if [ -n "$NODE" ]; then labels+=("Serve it on another machine…"); actions+=(node); fi
  if [ "$files" = 1 ] && [ "$source" = restore ]; then
    labels+=("Download the files instead of restoring them"); actions+=(download)
  elif [ "$files" = 1 ] && [ "$archive" = 1 ]; then
    labels+=("Restore the files from the archive instead"); actions+=(archive)
  fi
  if [ "$files" = 1 ] && [ "${#destinations[@]}" -gt 1 ]; then
    labels+=("Keep the files on another node…"); actions+=(destination)
  fi
  if [ "${#labels[@]}" -eq 0 ]; then
    printf '\nNothing in this plan can be changed: it serves on the saved node order.\n' | text
    return 0
  fi
  choice=$(choose_index "Change the plan" "${labels[@]}" "Back") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  [ "$choice" -lt "${#actions[@]}" ] || return 0
  case "${actions[$choice]}" in
    node)
      local picked
      picked=$(choose_node "$SPEC") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
      [ -z "$picked" ] || NODE=$picked ;;
    download) SOURCE=download ;;
    archive) SOURCE=archive ;;
    destination)
      choice=$(choose_index "Which node should keep the files?" "${names[@]}" "Back") \
        || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
      [ "$choice" -lt "${#destinations[@]}" ] || return 0
      FILES_NODE=${destinations[$choice]} ;;
  esac
}

details_menu() {
  local choice rc
  while true; do
    choice=$(choose_index "Details" "Recipe details" "Published results" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "$choice" in
      0) page_output "" python3 -m model_library.catalog show "$SPEC" || rc=$? ;;
      1) page_output "" python3 -m model_library.catalog results "$SPEC" || rc=$? ;;
      *) return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
  done
}

# serve_recipe SPEC: the machine question, then the plan until Cancel or a
# run. Returns 0, or 4 after a run (home redraws), or 130.
RAN=4
serve_recipe() {
  SPEC="$1" NODE="" SOURCE="" FILES_NODE="" NOT_FOUND=()
  local rc=0 action label choice
  local -a actions=() labels=()
  NODE=$(choose_node "$SPEC") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  while true; do
    build_plan "$SPEC" || { rc=$?; [ "$rc" -ne 130 ] || return 130; wait_continue "Plan not made" || return $?; return 0; }
    actions=() labels=()
    while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done \
      < <(serve_plan choices --plan "$PLAN")
    show_plan "${#labels[@]}" || { rc=$?; [ "$rc" -ne 130 ] || return 130; }
    # The cursor starts on Cancel (D1): approving a plan takes a deliberate move.
    choice=$(PULSAR_CHOOSE_DEFAULT=0 choose_index "Serve a model" "${labels[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "${actions[$choice]}" in
      go) run_plan || rc=$?; [ "$rc" -ne 5 ] || continue; return "$rc" ;;
      ready)
        # The same previews without the Stop and the Start: its steps were shown above.
        printf '%s' "$CATALOG" | serve_plan compose "${COMPOSE_ARGS[@]}" --ready-only --out "$PLAN" || return 0
        run_plan || rc=$?; [ "$rc" -ne 5 ] || continue; return "$rc" ;;
      change) change_menu || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      details) details_menu || { rc=$?; [ "$rc" -ne 130 ] || return 130; } ;;
      *) return 0 ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# 5. The run
# ---------------------------------------------------------------------------

# run_plan: each step's own preview, then its command, in order. Returns RAN
# when it ran (the outcome was shown), 5 to plan again, 130 on Ctrl-C.
run_plan() {
  local index kind preview snapshot sentence verdict reason rc interrupted done=0 total
  local failed="" halted="" choice action label
  local -a indexes=() kinds=() previews=() snapshots=() sentences=() ran=() actions=() labels=()
  while IFS=$'\t' read -r index kind _ preview snapshot sentence; do
    indexes+=("$index"); kinds+=("$kind"); previews+=("$preview"); snapshots+=("$snapshot")
    sentences+=("$sentence")
  done < <(serve_plan steps --plan "$PLAN")
  total=${#indexes[@]}
  for index in "${indexes[@]}"; do
    kind=${kinds[$index]} rc=0 verdict=same reason=""
    # Its own preview, now that its turn has come.
    if [ "$kind" = stop ]; then
      capture_inventory || return $?
      if [ -n "$INVENTORY_ERROR" ]; then
        verdict=blocked reason="running services could not be checked"
      else
        IFS=$'\t' read -r verdict reason < <(serve_plan check-step --plan "$PLAN" --step "$index" \
          --preview-file "$INVENTORY")
      fi
    elif [ "${previews[$index]}" = 1 ]; then
      mapfile -t COMMAND < <(serve_plan command --plan "$PLAN" --step "$index" --preview)
      resolve "${COMMAND[@]}"
      spin "Checking step $((index + 1)) of $total before it runs…" bash -c '"$@" 2>"$0"' "$WORK/step.err" \
        "${COMMAND[@]}" >"$WORK/step.json" || rc=$?
      [ "$rc" -ne 130 ] || { failed=$index; rc=130; break; }
      IFS=$'\t' read -r verdict reason < <(serve_plan check-step --plan "$PLAN" --step "$index" \
        --preview-file "$WORK/step.json" --preview-rc "$rc")
    fi
    case "$verdict" in
      same) ;;
      skip)
        printf '\n• Step %s of %s needs nothing: %s.\n' $((index + 1)) "$total" "$reason" | text
        done=$((done + 1))
        continue ;;
      *)
        # Found files Download's plan did not take: the next plan skips them.
        [ "$kind" != register ] || NOT_FOUND+=("${snapshots[$index]}")
        failed=$index halted=${reason:-its own check did not confirm the plan}
        break ;;
    esac
    mapfile -t COMMAND < <(serve_plan command --plan "$PLAN" --step "$index")
    resolve "${COMMAND[@]}"
    if [ "$kind" = start ]; then
      # Start records why it refused; only a refusal for nothing but its
      # memory warning offers starting anyway, on the outcome screen.
      : >"$WORK/blockers.jsonl"
      COMMAND=(env PULSAR_START_BLOCKERS_FILE="$WORK/blockers.jsonl" "$REPO_DIR/scripts/with-connect-card.sh"
        "${COMMAND[@]}")
    fi
    printf '\n→ Step %s of %s: %s Ctrl-C requests interruption; this menu returns when the command exits.\n' \
      $((index + 1)) "$total" "${sentences[$index]}" | text
    interrupted=0
    trap 'interrupted=1' INT
    set +e
    "${COMMAND[@]}"
    rc=$?
    set -e
    trap - INT
    case "$kind" in stop|start) forget_inventory ;; esac
    [ "$interrupted" = 0 ] || [ "$rc" -gt 128 ] || rc=130
    # A failed command ran too: the outcome names it with the others.
    if [ "$rc" -ne 0 ]; then failed=$index ran+=(--ran-step "$index"); break; fi
    done=$((done + 1)) ran+=(--ran-step "$index")
  done
  local -a result=(--plan "$PLAN" --done "$done" "${ran[@]}")

  if [ -n "$failed" ]; then
    result+=(--failed "$failed" --rc "$rc")
    [ -z "$halted" ] || result+=(--halted "$halted")
    [ "${kinds[$failed]}" != start ] || [ -n "$halted" ] || result+=(--blockers "$WORK/blockers.jsonl")
  fi
  echo
  serve_plan outcome "${result[@]}"
  actions=() labels=()
  while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done \
    < <(serve_plan outcome "${result[@]}" --choices)
  choice=$(choose_index "Serve a model" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return "$RAN"; }
  rc=0
  case "${actions[$choice]}" in
    again) return 5 ;;
    restart:*)
      mapfile -t COMMAND < <(serve_plan command --plan "$PLAN" --restart "${actions[$choice]#restart:}")
      resolve "${COMMAND[@]}"
      printf '\n→ Starting %s again.\n' "${labels[$choice]#Start }" | text
      "$REPO_DIR/scripts/with-connect-card.sh" "${COMMAND[@]}" || rc=$?
      forget_inventory
      [ "$rc" -ne 130 ] || return 130
      printf '\nSame as: %s\n' "$(serve_plan command --plan "$PLAN" --restart "${actions[$choice]#restart:}" --shown)" | text
      wait_continue "Start again" || return $? ;;
    memory)
      # Start's own acknowledgement of its memory warning, chosen here by name;
      # it is never part of a plan approved beforehand.
      mapfile -t COMMAND < <(serve_plan command --plan "$PLAN" --accept-memory-warning)
      resolve "${COMMAND[@]}"
      printf '\n→ %s.\n' "${labels[$choice]}" | text
      "$REPO_DIR/scripts/with-connect-card.sh" "${COMMAND[@]}" || rc=$?
      forget_inventory
      [ "$rc" -ne 130 ] || return 130
      printf '\nSame as: %s\n' "$(serve_plan command --plan "$PLAN" --accept-memory-warning --shown)" | text
      wait_continue "Start" || return $? ;;
    inspect)
      "$REPO_DIR/pulsar" models check "${SPEC:0:12}" || rc=$?
      [ "$rc" -ne 130 ] || return 130
      printf '\nSame as: ./pulsar models check %s\n' "${SPEC:0:12}" | text
      wait_continue "Files checked" || return $? ;;
    status)
      page_output "Checking the service…" "$REPO_DIR/pulsar" status "${SPEC:0:12}" || rc=$?
      [ "$rc" -ne 130 ] || return 130 ;;
  esac
  return "$RAN"
}

# ---------------------------------------------------------------------------

SPEC=""
rc=0
entry_check || rc=$?
[ "$rc" -ne 130 ] || exit 130
[ "$rc" -eq 0 ] || exit 0
while true; do
  if ! CATALOG=$(python3 -m model_library.catalog list --json); then
    printf '\n✗ The catalog could not be read; the reason is above.\n'
    wait_continue "Catalog error" || exit $?
    exit 0
  fi
  capture_inventory || exit $?
  inventory=()
  [ -n "$INVENTORY_ERROR" ] || inventory=(--inventory "$INVENTORY")
  read_cards < <(printf '%s' "$CATALOG" | serve_plan list "${inventory[@]}")
  if [ "${#CARD_IDS[@]}" -eq 0 ]; then
    printf '\nThe catalog has no recipe this menu can serve.\n'
    wait_continue "Serve a model" || exit $?
    exit 0
  fi
  index=$(choose_index "Serve a model" "${CARD_OPTIONS[@]}" "Back") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  [ "$index" -lt "${#CARD_IDS[@]}" ] || exit 0
  rc=0
  serve_recipe "${CARD_IDS[$index]}" || rc=$?
  [ "$rc" -ne 130 ] || exit 130
  # After a run, home redraws with what changed.
  [ "$rc" -ne "$RAN" ] || exit 0
done
