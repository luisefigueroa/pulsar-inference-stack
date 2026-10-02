#!/usr/bin/env bash
# Navigate a captured inventory; existing public commands own every operation.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
[ $# -eq 0 ] || { echo 'error: the inventory menu is interactive; use ./pulsar inventory --json' >&2; exit 2; }
. "$REPO_DIR/scripts/ui.sh"
require_gum "the inventory menu" "pulsar inventory | status SPEC | stop SPEC"
work=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-inventory-menu.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

refresh_inventory() {
  local rc
  if spin "Checking service inventory…" "$REPO_DIR/pulsar" inventory --json >"$work/inventory.json"; then
    return 0
  else
    rc=$?
  fi
  [ "$rc" -ne 130 ] || exit 130
  printf 'Inventory could not be refreshed; no service actions are available.\n' >&2
  return 1
}

inventory_view() {
  python3 "$REPO_DIR/scripts/inventory_menu.py" --repo-root "$REPO_DIR" --inventory "$work/inventory.json" "$@"
}

load_service() {
  local spec="$1" result kind first second
  result=$(inventory_view --spec-id "$spec") || return 1
  STATUS_ARGS=() STOP_ARGS=() STOP_QUESTION=""
  while IFS=$'\t' read -r kind first second; do
    case "$kind" in
      header) printf '%s\n' "$first" ;;
      status) STATUS_ARGS=("$first"); [ "$second" = - ] || STATUS_ARGS+=(--node "$second") ;;
      stop) STOP_ARGS=("$first"); [ "$second" = - ] || STOP_ARGS+=(--node "$second") ;;
      confirm) STOP_QUESTION="$first" ;;
    esac
  done <<<"$result"
}

service_actions() {
  local spec="$1" index choice rc
  local -a options=() STATUS_ARGS=() STOP_ARGS=()
  local STOP_QUESTION=""
  while load_service "$spec"; do
    options=("Detailed status")
    [ "${#STOP_ARGS[@]}" -eq 0 ] || options+=("Stop service")
    options+=("Refresh inventory" "Back")
    index=$(choose_index "Selected catalog service" "${options[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    choice="${options[$index]}"
    case "$choice" in
      "Detailed status")
        rc=0
        "$REPO_DIR/pulsar" status "${STATUS_ARGS[@]}" || rc=$?
        [ "$rc" -ne 130 ] || return 130
        ;;
      "Stop service")
        # Show fresh scope and ownership before asking for stop approval.
        refresh_inventory || return 0
        load_service "$spec" || return 0
        [ "${#STOP_ARGS[@]}" -gt 0 ] || continue
        confirm "$STOP_QUESTION" no \
          || { rc=$?; echo 'Nothing was stopped.'; [ "$rc" -ne 130 ] || return 130; continue; }
        rc=0
        "$REPO_DIR/pulsar" stop "${STOP_ARGS[@]}" || rc=$?
        [ "$rc" -ne 130 ] || return 130
        ;;
      "Refresh inventory") ;;
      *) return 0 ;;
    esac
    refresh_inventory || return 0
  done
  return 0
}

while true; do
  ids=() labels=()
  if refresh_inventory && result=$(inventory_view); then
    while IFS=$'\t' read -r kind first second; do
      case "$kind" in
        header) printf '%s\n' "$first" ;;
        service) ids+=("$first"); labels+=("$second") ;;
      esac
    done <<<"$result"
  fi
  labels+=("Refresh inventory" "Show full inventory" "Back")
  index=$(choose_index "Select a catalog service" "${labels[@]}") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  if [ "$index" -lt "${#ids[@]}" ]; then
    service_actions "${ids[$index]}" || { rc=$?; [ "$rc" -ne 130 ] || exit 130; }
    continue
  fi
  case "${labels[$index]}" in
    "Refresh inventory") ;;
    "Show full inventory")
      rc=0
      "$REPO_DIR/pulsar" inventory || rc=$?
      [ "$rc" -ne 130 ] || exit 130
      ;;
    *) exit 0 ;;
  esac
done
