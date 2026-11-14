#!/usr/bin/env bash
# Running services… (home menu): what runs, how to reach it, and what to do.
#
# Entering takes one service inventory (./pulsar inventory --json). The last
# one saved on disk is not reused: a Stop or a Start run elsewhere, from
# another menu or the command line, does not change it. The page opens
# on the service when there is one, on a list when there are several, and
# says so when there is none; a node that did not answer makes what runs
# there unknown, not absent. Each fact on a service's card says where it came
# from and how old it is (scripts/services_page.py). Its choices:
#   Check it now      ./pulsar status SPEC: its API, key and health
#   How to connect    the Connect card from that check (a first request)
#   Logs              ./pulsar logs SPEC, in the pager
#   Restart…          Stop then Start of the same recipe on the same nodes,
#                     as a plan that starts on Cancel (serve-menu.sh --restart)
#   Switch to another model…
#                     the Serve list; its plan stops this service directly
#                     before Start (serve-menu.sh --switch-from)
#   Stop…             ./pulsar stop SPEC, after today's question
#   Stop every service…
#                     ./pulsar stop --all --yes, after today's question
# GPU work and containers Pulsar did not start are named read-only and
# offered nothing. A service outside the catalog can be stopped and its logs
# read; it gets no Check it now, Restart or Switch. The capture is reused
# while it is under a minute old, so no choice probes twice in a row; a Stop
# or a Start forgets it, and Check it now's result with it. Nothing here
# passes --replace, --pull-image or --accept-memory-warn.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "Running services…" \
  "./pulsar inventory" "what runs on each node, with its age" \
  "./pulsar status SPEC" "check it now: its API, key and health" \
  "./pulsar logs SPEC" "read its containers' output" \
  "./pulsar stop SPEC" "stop it; model files are kept" \
  "./pulsar start SPEC" "then start it again: Restart" \
  "./pulsar start OTHER --dry-run" "switch: its checks; then stop it and start OTHER" \
  "./pulsar stop --all --yes" "stop every Pulsar service; model files are kept"
ui_keep_styles

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-services.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
# The width is read once, before any capture: a captured stdout hides the terminal.
COLUMNS_SEEN=$(_ui_terminal_columns) || COLUMNS_SEEN=""
page() { python3 -m scripts.services_page "$@" --repo-root "$REPO_DIR" ${COLUMNS_SEEN:+--width "$COLUMNS_SEEN"}; }
text() { python3 -m scripts.terminal_format --text; }
CHANGED=4

COMMAND=()
resolve() {
  COMMAND=("$@")
  [ "${COMMAND[0]:-}" != ./pulsar ] || COMMAND[0]="$REPO_DIR/pulsar"
}

if ! CATALOG=$("$REPO_DIR/pulsar" models list --json); then
  printf '\n✗ The catalog could not be read, so services cannot be named; the reason is above.\n'
  wait_continue "Catalog error" || exit $?
  exit 0
fi
view() { page "$@" --inventory "$INVENTORY" <<<"$CATALOG"; }

# ---------------------------------------------------------------------------
# One service inventory
# ---------------------------------------------------------------------------

INVENTORY="$WORK/inventory.json" INVENTORY_AT=0 INVENTORY_ERROR=""
# Check it now's last result, for one service.
STATUS_CONF="" STATUS_RC="" STATUS_AT="" STATUS_FILE="$WORK/status.json"
capture_inventory() {
  local now rc=0
  now=$(date +%s)
  if [ -s "$INVENTORY" ] && [ "$INVENTORY_AT" -gt 0 ] && [ $((now - INVENTORY_AT)) -lt 60 ]; then return 0; fi
  spin "Checking running services on every node…" bash -c '"$@" 2>"$0"' "$WORK/inventory.err" \
    "$REPO_DIR/pulsar" inventory --json >"$INVENTORY.next" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  # A node that did not answer still gives a document; the page names that node.
  if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d,dict) and "services" in d' \
      "$INVENTORY.next" 2>/dev/null; then
    mv -f -- "$INVENTORY.next" "$INVENTORY"
    INVENTORY_AT=$now INVENTORY_ERROR=""
  else
    rm -f -- "$INVENTORY.next" "$INVENTORY"
    INVENTORY_AT=0 INVENTORY_ERROR="the service inventory exited $rc without a readable result"
  fi
}
# A Stop or a Start makes the capture and the last check stale.
forget() { INVENTORY_AT=0 STATUS_CONF=""; }
status_args() {
  [ "$STATUS_CONF" = "$1" ] || return 0
  printf '%s\n' --status "$STATUS_FILE" --status-rc "$STATUS_RC" --status-at "$STATUS_AT"
}

# ---------------------------------------------------------------------------
# The choices
# ---------------------------------------------------------------------------

# check_now CONF: ./pulsar status, its output shown, its result kept for the card.
check_now() {
  local conf="$1" rc=0
  mapfile -t COMMAND < <(view command --conf "$conf" --action check)
  resolve "${COMMAND[@]}"
  rm -f -- "$STATUS_FILE"
  page_output "Checking the service now…" env PULSAR_SERVICE_RESULT_FILE="$STATUS_FILE" "${COMMAND[@]}" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  STATUS_CONF=$conf STATUS_RC=$rc STATUS_AT=$(date +%s)
  printf 'Same as: %s\n' "$(view command --conf "$conf" --action check --shown)" | text
}

# connect CONF: the Connect card from this page's last check of it, checking first when there is none.
connect() {
  local conf="$1" card=""
  if [ "$STATUS_CONF" != "$conf" ] || [ "$STATUS_RC" != 0 ]; then check_now "$conf" || return $?; fi
  [ "$STATUS_RC" != 0 ] || card=$(python3 "$REPO_DIR/scripts/connect_card.py" --result "$STATUS_FILE" 2>/dev/null) || card=""
  if [ -n "$card" ]; then
    printf '%s\n' "$card"
    printf '\nFrom Check it now, %s.\n' "$(python3 -c 'import sys; from model_library.catalog import age_text; print(age_text(int(sys.argv[1])))' \
      $(( $(date +%s) - STATUS_AT )))" | text
  else
    printf '\n✗ Check it now did not find its API, so there is no first request to show; the reason is above.\n' | text
  fi
  wait_continue "How to connect" || return $?
}

logs() {
  local conf="$1" rc=0
  mapfile -t COMMAND < <(view command --conf "$conf" --action logs)
  resolve "${COMMAND[@]}"
  page_output "Reading its logs…" "${COMMAND[@]}" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  printf 'Same as: %s\n' "$(view command --conf "$conf" --action logs --shown)" | text
  # A failed read is held on screen until Continue.
  [ "$rc" -eq 0 ] || wait_continue "Logs" || return $?
}

# plan_from_here ACTION CONF: Restart… or Switch to another model…, through
# Serve a model…'s plan and run, with this page's capture; with no ACTION,
# Serve a model… itself.
plan_from_here() {
  local action="$1" conf="$2" rc=0 at
  local -a arguments=()
  [ -z "$action" ] || mapfile -t arguments < <(view command --conf "$conf" --action "$action")
  rm -f -- "$INVENTORY.at"
  "$REPO_DIR/scripts/serve-menu.sh" "${arguments[@]}" --inventory "$INVENTORY" --inventory-at "$INVENTORY_AT" \
    || rc=$?
  [ "$rc" -ne 130 ] || return 130
  # 0 when a Stop or a Start ran there: what runs, and the last check, are stale.
  at=$(cat -- "$INVENTORY.at" 2>/dev/null) || at=0
  [[ "$at" =~ ^[0-9]+$ ]] || at=0
  if [ "$at" -eq 0 ]; then forget; else INVENTORY_AT=$at; fi
  return "$CHANGED"
}

# stop_one CONF: today's question, then ./pulsar stop. Returns CHANGED when it ran.
stop_one() {
  local conf="$1" question rc=0 name
  capture_inventory || return $?
  if ! question=$(view question --conf "$conf" 2>/dev/null); then
    printf '\nStop is not offered for it now; the card says why.\n' | text
    return 0
  fi
  confirm "$question" no || { rc=$?; echo 'Nothing was stopped.'; [ "$rc" -ne 130 ] || return 130; return 0; }
  mapfile -t COMMAND < <(view command --conf "$conf" --action stop)
  local shown
  name=$(view command --conf "$conf" --action model) || name="the service"
  shown=$(view command --conf "$conf" --action stop --shown)
  resolve "${COMMAND[@]}"
  "${COMMAND[@]}" || rc=$?
  forget
  [ "$rc" -ne 130 ] || { page outcome --action stop --rc 130 --name "$name" --shown "$shown"; return 130; }
  echo
  page outcome --action stop --rc "$rc" --name "$name" --shown "$shown"
  wait_continue "Stop" || return $?
  return "$CHANGED"
}

# stop_every: stop --all's question, naming each service it removes, then the command.
stop_every() {
  local question rc=0
  capture_inventory || return $?
  if ! question=$(view question --all 2>/dev/null); then
    printf '\nNo Pulsar service was seen just now; nothing was stopped.\n' | text
    return 0
  fi
  confirm "$question" no || { rc=$?; echo 'Nothing was stopped.'; [ "$rc" -ne 130 ] || return 130; return 0; }
  "$REPO_DIR/pulsar" stop --all --yes || rc=$?
  forget
  [ "$rc" -ne 130 ] || return 130
  echo
  page outcome --action stop-all --rc "$rc" --shown "./pulsar stop --all --yes"
  wait_continue "Stop every service" || return $?
  return "$CHANGED"
}

# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------

# service_page CONF: one service's card and choices until Back (0), a change
# that makes the page look again (CHANGED), or Ctrl-C (130).
service_page() {
  local conf="$1" choice rc action label
  local -a actions=() labels=() status=()
  while true; do
    capture_inventory || return $?
    [ -z "$INVENTORY_ERROR" ] || return "$CHANGED"
    if ! view services | cut -f1 | grep -x -- "$conf" >/dev/null; then
      printf '\nThat service was not seen just now.\n' | text
      return "$CHANGED"
    fi
    mapfile -t status < <(status_args "$conf")
    echo
    view screen --conf "$conf" "${status[@]}"
    actions=() labels=()
    while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done \
      < <(view choices --conf "$conf" "${status[@]}")
    choice=$(choose_index "Running services" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "${actions[$choice]}" in
      check) check_now "$conf" || rc=$? ;;
      connect) connect "$conf" || rc=$? ;;
      logs) logs "$conf" || rc=$? ;;
      restart) plan_from_here restart "$conf" || rc=$? ;;
      switch) plan_from_here switch "$conf" || rc=$? ;;
      stop) stop_one "$conf" || rc=$? ;;
      stop-all) stop_every || rc=$? ;;
      *) return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
    [ "$rc" -ne "$CHANGED" ] || return "$CHANGED"
  done
}

# list_page: one choice per service by its name, then Stop every service… and Back.
list_page() {
  local choice rc action label conf body
  local -a confs=() options=() actions=()
  while true; do
    capture_inventory || return $?
    [ -z "$INVENTORY_ERROR" ] || return "$CHANGED"
    confs=() options=() actions=()
    while IFS=$'\t' read -r conf body; do
      confs+=("$conf"); options+=("${body//$'\x1f'/$'\n'}")
    done < <(view names)
    [ "${#confs[@]}" -gt 1 ] || return "$CHANGED"
    while IFS=$'\t' read -r action label; do actions+=("$action"); options+=("$label"); done < <(view choices)
    echo
    view screen
    choice=$(choose_index "Running services" "${options[@]}") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    if [ "$choice" -lt "${#confs[@]}" ]; then
      service_page "${confs[$choice]}" || rc=$?
    else
      case "${actions[$((choice - ${#confs[@]}))]}" in
        stop-all) stop_every || rc=$? ;;
        *) return 0 ;;
      esac
    fi
    [ "$rc" -ne 130 ] || return 130
    [ "$rc" -ne "$CHANGED" ] || return "$CHANGED"
  done
}

# none_page: nothing seen running, said only as far as every node answered.
none_page() {
  local choice rc action label
  local -a actions=() labels=()
  echo
  view screen
  while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done < <(view choices)
  choice=$(choose_index "Running services" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
  case "${actions[$choice]}" in
    serve) plan_from_here "" "" ;;
    again) forget; return "$CHANGED" ;;
    *) return 0 ;;
  esac
}

while true; do
  rc=0
  capture_inventory || rc=$?
  [ "$rc" -ne 130 ] || exit 130
  if [ -n "$INVENTORY_ERROR" ]; then
    printf '\n✗ Running services could not be checked: %s. What runs is unknown, not absent.\n' \
      "$INVENTORY_ERROR" | text
    choice=$(choose_index "Running services" "Look again" "Back") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
    [ "$choice" = 0 ] || exit 0
    forget
    continue
  fi
  mapfile -t found < <(view services | cut -f1)
  case "${#found[@]}" in
    0) none_page || rc=$? ;;
    1) service_page "${found[0]}" || rc=$? ;;
    *) list_page || rc=$? ;;
  esac
  [ "$rc" -ne 130 ] || exit 130
  [ "$rc" -eq "$CHANGED" ] || exit 0
done
