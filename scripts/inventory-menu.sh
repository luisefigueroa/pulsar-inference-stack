#!/usr/bin/env bash
# Navigate a captured inventory; existing public commands own every operation.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
[ $# -eq 0 ] || { echo 'error: the inventory menu is interactive; use ./pulsar inventory --json' >&2; exit 2; }
. "$REPO_DIR/scripts/ui.sh"
require_gum "the inventory menu" "./pulsar inventory" "see what runs on each node" \
  "./pulsar status SPEC" "check one service now" "./pulsar logs SPEC" "read its containers' output" \
  "./pulsar stop SPEC" "stop it; files are kept" "./pulsar stop --all --yes" "stop every Pulsar service; files are kept"
ui_keep_styles
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
  STATUS_ARGS=() LOGS_ARGS=() STOP_ARGS=() STOP_QUESTION="" SERVICE_MODEL=""
  while IFS=$'\t' read -r kind first second; do
    case "$kind" in
      header) printf '%s\n' "$first" ;;
      status) STATUS_ARGS=("$first"); [ "$second" = - ] || STATUS_ARGS+=(--node "$second") ;;
      logs) LOGS_ARGS=("$first") ;;
      model) SERVICE_MODEL="$first" ;;
      stop) STOP_ARGS=("$first"); [ "$second" = - ] || STOP_ARGS+=(--node "$second") ;;
      confirm) STOP_QUESTION="$first" ;;
    esac
  done <<<"$result"
}

service_actions() {
  local spec="$1" index choice rc
  local -a options=() STATUS_ARGS=() LOGS_ARGS=() STOP_ARGS=()
  local STOP_QUESTION="" SERVICE_MODEL="" started subject
  while load_service "$spec"; do
    subject="${SERVICE_MODEL:+$SERVICE_MODEL }[${spec:0:12}]"
    options=("Detailed status")
    [ "${#LOGS_ARGS[@]}" -eq 0 ] || options+=("Service logs")
    [ "${#STOP_ARGS[@]}" -eq 0 ] || options+=("Stop service")
    options+=("Open in catalog" "Refresh inventory" "Back")
    index=$(choose_index "Selected catalog service" "${options[@]}") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    choice="${options[$index]}"
    case "$choice" in
      "Detailed status")
        rc=0 started=$SECONDS
        page_output "Checking service status…" "$REPO_DIR/scripts/with-connect-card.sh" "$REPO_DIR/pulsar" status "${STATUS_ARGS[@]}" || rc=$?
        [ "$rc" -ne 130 ] || return 130
        # Not running (1) and not observable (3) need different next steps.
        [ "$rc" -eq 0 ] || report_outcome status "Detailed status" "$subject" "$rc" \
          "$(elapsed_text $((SECONDS - started)))" --command "$REPO_DIR/pulsar" status "${STATUS_ARGS[@]}"
        ;;
      "Service logs")
        # Read-only; a failed read is held on screen until Continue.
        rc=0
        page_output "Reading service logs…" "$REPO_DIR/pulsar" logs "${LOGS_ARGS[@]}" || rc=$?
        [ "$rc" -ne 130 ] || return 130
        [ "$rc" -eq 0 ] || wait_continue || return $?
        ;;
      "Stop service")
        # Show fresh scope and ownership before asking for stop approval.
        refresh_inventory || return 0
        load_service "$spec" || return 0
        [ "${#STOP_ARGS[@]}" -gt 0 ] || continue
        confirm "$STOP_QUESTION" no \
          || { rc=$?; echo 'Nothing was stopped.'; [ "$rc" -ne 130 ] || return 130; continue; }
        rc=0 started=$SECONDS
        "$REPO_DIR/pulsar" stop "${STOP_ARGS[@]}" || rc=$?
        [ "$rc" -ne 130 ] || return 130
        # The same outcome line as the catalog menu's operations, held on
        # screen until Continue.
        report_outcome stop Stop "$subject" "$rc" "$(elapsed_text $((SECONDS - started)))" \
          --command "$REPO_DIR/pulsar" stop "${STOP_ARGS[@]}"
        wait_continue || return $?
        ;;
      "Open in catalog")
        # The recipe opens with this inventory, so its labels and suggestion
        # know the service; Back returns here with a fresh inventory.
        rc=0
        PULSAR_CATALOG_OPEN_SPEC="$spec" PULSAR_CATALOG_INVENTORY="$work/inventory.json" \
          "$REPO_DIR/pulsar" models menu || rc=$?
        [ "$rc" -ne 130 ] || return 130
        ;;
      "Refresh inventory") ;;
      *) return 0 ;;
    esac
    refresh_inventory || return 0
  done
  return 0
}

# stop --all with --yes, after a fresh inventory names what it would remove.
stop_every_service() {
  local result kind first second question="" rc started
  refresh_inventory || return 0
  result=$(inventory_view) || return 0
  while IFS=$'\t' read -r kind first second; do
    [ "$kind" != stopall ] || question="$first"
  done <<<"$result"
  if [ -z "$question" ]; then
    printf 'No Pulsar service was observed just now; nothing was stopped.\n'
    return 0
  fi
  confirm "$question" no || { rc=$?; echo 'Nothing was stopped.'; [ "$rc" -ne 130 ] || return 130; return 0; }
  rc=0 started=$SECONDS
  "$REPO_DIR/pulsar" stop --all --yes || rc=$?
  [ "$rc" -ne 130 ] || return 130
  report_outcome stop Stop "every Pulsar service" "$rc" \
    "$(elapsed_text $((SECONDS - started)))" --command "$REPO_DIR/pulsar" stop --all --yes
  wait_continue
}

while true; do
  ids=() labels=() stop_all=0
  if refresh_inventory && result=$(inventory_view); then
    while IFS=$'\t' read -r kind first second; do
      case "$kind" in
        header) printf '%s\n' "$first" ;;
        service) ids+=("$first"); labels+=("$second") ;;
        stopall) stop_all=1 ;;
      esac
    done <<<"$result"
  fi
  labels+=("Refresh inventory" "Show full inventory")
  [ "$stop_all" -eq 0 ] || labels+=("Stop every Pulsar service…")
  labels+=("Back")
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
      page_output "Checking service inventory…" "$REPO_DIR/pulsar" inventory || rc=$?
      [ "$rc" -ne 130 ] || exit 130
      ;;
    "Stop every Pulsar service…")
      rc=0
      stop_every_service || rc=$?
      [ "$rc" -ne 130 ] || exit 130
      ;;
    *) exit 0 ;;
  esac
done
