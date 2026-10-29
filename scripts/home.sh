#!/usr/bin/env bash
# Neutral workflow menu. Local files decide whether cluster setup is complete.
# Entering the menu runs no SSH, Docker, GPU or Hugging Face probes.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ ! -t 0 ] && [ "${PULSAR_FORCE_MENU:-0}" != 1 ]; then exec "$REPO_DIR/pulsar" help; fi
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"

review_catalog_error() {
  printf '\n✗ The catalog could not be displayed; review the error above.\n  exit %s\n' "$1"
  wait_continue "Catalog error"
}

# Help: the words, the serving steps and the command reference. Each page opens
# in the pager and returns here; Back or Esc returns to the home menu.
help_menu() {
  local choice rc
  while true; do
    choice=$(choose_index "Help" "What the words mean" "How serving works" "Command reference" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "$choice" in
      0) page_output "" "$REPO_DIR/pulsar" help terms || rc=$? ;;
      1) page_output "" "$REPO_DIR/pulsar" help workflow || rc=$? ;;
      2) page_output "" "$REPO_DIR/pulsar" help || rc=$? ;;
      *) return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
  done
}

SETUP_PY="${PULSAR_SETUP_STATUS_PY:-$REPO_DIR/scripts/setup_status.py}"
status_json=$(python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format json)
complete=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("complete") else "0")')
next_action=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_action") or "")')
# Without Gum the menu does not open; name the commands behind its choices.
case "$complete:$next_action" in
  1:*) require_gum "the Pulsar menu" "./pulsar models list" "browse the catalog" \
    "./pulsar inventory" "see what runs on each node" "./pulsar topology check" "check saved nodes and fabric" \
    "./pulsar model budget" "disk headroom on every node" "./pulsar doctor" "host diagnostics" \
    "./pulsar configure archive-root" "archive storage setting" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help workflow)" ;;
  *:enroll-ssh-trust) require_gum "the Pulsar menu" "./pulsar ssh-trust enroll" "enroll SSH trust (next setup step)" \
    "./pulsar models list" "browse the catalog" "./pulsar models show SPEC" "one spec's saved details" \
    "./pulsar doctor" "host diagnostics" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help workflow)" ;;
  *) require_gum "the Pulsar menu" "./pulsar topology setup" "set up cluster membership (next setup step)" \
    "./pulsar models list" "browse the catalog" "./pulsar models show SPEC" "one spec's saved details" \
    "./pulsar doctor" "host diagnostics" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help workflow)" ;;
esac
ui_keep_styles
# The setup status prints on entry, then again only when a choice changed it;
# the previous run of this menu passes what it printed.
status_text=$(python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format text)
# After setup, a Next line from saved catalog records; a catalog that cannot
# be read leaves it out, and Catalog and storage reports the error. So does an
# unreadable archive setting, since suggestions depend on it; the status
# above already says how to fix it.
if [ "$complete" = 1 ]; then
  archives=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["archives"]["status"])')
  if [ "$archives" != unreadable ]; then
    next_line=$(python3 -m model_library.catalog list --json 2>/dev/null \
      | python3 -m model_library.catalog_menu next --archive-location "$archives" 2>/dev/null \
      | python3 -m scripts.terminal_format --text) || next_line=""
    [ -z "$next_line" ] || status_text+=$'\n\n'"$next_line"
  fi
fi
shown=${PULSAR_HOME_STATUS_SHOWN-}
unset PULSAR_HOME_STATUS_SHOWN
[ "$status_text" = "$shown" ] || printf '\n%s\n\n' "$status_text"

if [ "$complete" = 1 ]; then
  choice=$(PULSAR_CHOOSE_ESC=exit choose_index "Pulsar Inference Stack" "Catalog and storage" "Live service inventory" \
    "Cluster overview (checks nodes now)" "Host diagnostics" "Archive storage configuration" \
    "Cluster topology" "Help" "Exit") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  # Each choice runs as a child; the menu returns here until Exit, Esc or Ctrl-C.
  set +e
  case "$choice" in
    0) "$REPO_DIR/pulsar" models menu ;;
    1) "$REPO_DIR/pulsar" inventory menu ;;
    # Probes the nodes only now: topology check, inventory and model budget.
    2) "$REPO_DIR/scripts/cluster-overview.sh" ;;
    # Long output opens in the pager, so this menu cannot bury it.
    3) page_output "Running host diagnostics…" "$REPO_DIR/pulsar" doctor ;;
    4) "$REPO_DIR/pulsar" configure archive-root menu ;;
    5) "$REPO_DIR/scripts/topology.sh" menu ;;
    6) help_menu ;;
    *) exit 0 ;;
  esac
  rc=$?
  set -e
  [ "$rc" -ne 130 ] || exit 130
  if [ "$choice" = 0 ] && [ "$rc" -ne 0 ]; then
    review_catalog_error "$rc" || exit $?
  fi
  # Re-read local setup state; a submenu may have changed it.
  PULSAR_HOME_STATUS_SHOWN="$status_text" exec "$REPO_DIR/scripts/home.sh"
fi

# First run: the next setup step and existing read-only inspection paths.
# Each choice returns here with the setup status read again.
next_label=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_label") or "")')
choice=$(PULSAR_CHOOSE_ESC=exit choose_index "Pulsar Inference Stack" "$next_label" "Browse the catalog (read-only)" \
  "Host diagnostics" "Cluster topology (read-only)" "Help" "Exit") \
  || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
set +e
case "$choice:$next_action" in
  0:set-up-topology) "$REPO_DIR/pulsar" topology setup ;;
  0:enroll-ssh-trust) "$REPO_DIR/pulsar" ssh-trust enroll ;;
  # Saved records only: catalog browsing and topology show probe no node.
  1:*) "$REPO_DIR/pulsar" models menu --read-only ;;
  2:*) page_output "Running host diagnostics…" "$REPO_DIR/pulsar" doctor ;;
  3:*) page_output "" "$REPO_DIR/pulsar" topology show ;;
  4:*) help_menu ;;
  *) exit 0 ;;
esac
rc=$?
set -e
[ "$rc" -ne 130 ] || exit 130
if [ "$choice" = 1 ] && [ "$rc" -ne 0 ]; then
  review_catalog_error "$rc" || exit $?
fi
if [ "$choice" = 0 ] && [ "$rc" -ne 0 ]; then
  printf '✗ Setup step did not complete; details above\n'
fi
PULSAR_HOME_STATUS_SHOWN="$status_text" exec "$REPO_DIR/scripts/home.sh"
