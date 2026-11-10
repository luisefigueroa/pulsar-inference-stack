#!/usr/bin/env bash
# The Pulsar menu: what the operator came to do, not a list of tools.
# Local files decide whether cluster setup is complete. Entering the menu runs
# no SSH, Docker, GPU or Hugging Face probes: the status block reads only
# saved records, each with its age (scripts/cluster_status.py home).
#
# After setup: Serve a model…, Running services…, Models and disk space…,
# Cluster…, Help and troubleshooting…, Exit. Serve a model… answers with a
# plan (scripts/serve-menu.sh). Until later phases replace them, Running
# services… and Models and disk space… open today's inventory menu and Model
# files page unchanged. Before setup: Set up this cluster… (or Start over: forget
# this cluster… when saved membership is invalid), read-only catalog
# browsing, Help and troubleshooting… and Exit. Help and troubleshooting…
# holds Advanced: single operations…, today's menus unchanged.
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

# catalog_menu ARGS...: the catalog menu, held on screen when it fails.
catalog_menu() {
  local rc=0
  "$REPO_DIR/pulsar" models menu "$@" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  [ "$rc" -eq 0 ] || review_catalog_error "$rc" || return $?
}

# Advanced: single operations…: today's menus, unchanged, each the same
# command as before. Before setup, only what reads saved records.
advanced_menu() {
  local choice rc
  while true; do
    rc=0
    if [ "$complete" = 1 ]; then
      choice=$(choose_index "Advanced: single operations" "Catalog and storage" \
        "Model files and disk space (checks nodes now)" "Live service inventory" "Cluster topology" \
        "Archive storage configuration" "Back") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
      case "$choice" in
        0) catalog_menu || rc=$? ;;
        1) "$REPO_DIR/pulsar" models files || rc=$? ;;
        2) "$REPO_DIR/pulsar" inventory menu || rc=$? ;;
        3) "$REPO_DIR/scripts/topology.sh" menu || rc=$? ;;
        4) "$REPO_DIR/pulsar" configure archive-root menu || rc=$? ;;
        *) return 0 ;;
      esac
    else
      choice=$(choose_index "Advanced: single operations" "Cluster topology (read-only)" "Back") \
        || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
      case "$choice" in
        0) page_output "" "$REPO_DIR/pulsar" topology show || rc=$? ;;
        *) return 0 ;;
      esac
    fi
    [ "$rc" -ne 130 ] || return 130
  done
}

# Help and troubleshooting…: diagnostics, the guides and the command
# reference, each opening in the pager and returning here, and Advanced.
# Back or Esc returns home.
help_menu() {
  local choice rc
  while true; do
    choice=$(choose_index "Help and troubleshooting" "Run diagnostics (checks nodes now)" "What the words mean" \
      "What the labels mean" "How serving works" "Command reference" "Advanced: single operations…" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "$choice" in
      # Long output opens in the pager, so this menu cannot bury it.
      0) page_output "Running diagnostics…" "$REPO_DIR/pulsar" doctor || rc=$? ;;
      1) page_output "" "$REPO_DIR/pulsar" help terms || rc=$? ;;
      2) page_output "" "$REPO_DIR/pulsar" help labels || rc=$? ;;
      3) page_output "" "$REPO_DIR/pulsar" help workflow || rc=$? ;;
      4) page_output "" "$REPO_DIR/pulsar" help || rc=$? ;;
      5) advanced_menu || rc=$? ;;
      *) return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
  done
}

SETUP_PY="${PULSAR_SETUP_STATUS_PY:-$REPO_DIR/scripts/setup_status.py}"
status_json=$(python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format json)
read -r complete topology_state < <(printf '%s' "$status_json" | python3 -c 'import json,sys
d=json.load(sys.stdin); print("1" if d.get("complete") else "0", d["topology"]["status"])')
# Without Gum the menu does not open; name the commands behind its choices.
if [ "$complete" = 1 ]; then
  require_gum "the Pulsar menu" "./pulsar models list" "serve a model: each recipe and its saved state" \
    "./pulsar start SPEC --dry-run" "what Start would check; then ./pulsar start SPEC" \
    "./pulsar models menu" "single operations: the catalog and each recipe's operations" \
    "./pulsar inventory menu" "running services: status and stop" \
    "./pulsar models files" "models and disk space on every node" \
    "./pulsar topology check" "check the cluster: saved nodes and fabric" \
    "./pulsar topology detect" "look for changes to the cluster; saves nothing" \
    "./pulsar doctor" "diagnostics with every node's prerequisites" \
    "./pulsar configure archive-root" "archive location setting" \
    "./pulsar help" "commands and guides (help terms, help labels, help workflow)"
elif [ "$topology_state" = invalid ]; then
  require_gum "the Pulsar menu" "./pulsar topology forget" "start over: forget this cluster (next step)" \
    "./pulsar models list" "browse the catalog" "./pulsar doctor" "diagnostics" \
    "./pulsar help" "commands and guides (help terms, help labels, help workflow)"
else
  require_gum "the Pulsar menu" "./pulsar topology setup" "set up this cluster (next step)" \
    "./pulsar models list" "browse the catalog" "./pulsar models show SPEC" "one spec's saved details" \
    "./pulsar doctor" "diagnostics" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help labels, help workflow)"
fi
ui_keep_styles
# The status is captured before it prints, so Python can't see the terminal;
# it gets the width ui.sh reads from it, as the catalog menu does.
columns=$(_ui_terminal_columns) || columns=""
with_width() {
  if [ -n "$columns" ]; then COLUMNS=$columns "$@"; else "$@"; fi
}
# The status prints on entry, then again only when a saved record changed;
# the previous run of this menu passes the key it printed.
if [ "$complete" = 1 ]; then
  # The catalog names the services' models; a catalog that cannot be read
  # leaves them by their served names, and Serve a model… reports the error.
  catalog_json=$(python3 -m model_library.catalog list --json 2>/dev/null) || catalog_json='{"entries": []}'
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  output=$(printf '%s' "$catalog_json" | with_width python3 -m scripts.cluster_status home \
    --now "$now" --repo-root "$REPO_DIR" 2>/dev/null) || output=""
  status_key="" status_text=""
  case "$output" in
    key$'\t'*$'\n'?*)
      status_key=${output%%$'\n'*}
      status_key=${status_key#key$'\t'}
      status_text=${output#*$'\n'}
      ;;
  esac
else
  status_text=$(with_width python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format text)
  status_key="$status_text"
fi
shown=${PULSAR_HOME_STATUS_SHOWN-}
unset PULSAR_HOME_STATUS_SHOWN
[ -z "$status_text" ] || [ "$status_key" = "$shown" ] || printf '\n%s\n' "$status_text"

if [ "$complete" = 1 ]; then
  choice=$(PULSAR_CHOOSE_ESC=exit choose_index "Pulsar Inference Stack" "Serve a model…" "Running services…" \
    "Models and disk space…" "Cluster…" "Help and troubleshooting…" "Exit") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  # Each choice runs as a child; the menu returns here until Exit, Esc or Ctrl-C.
  set +e
  case "$choice" in
    0) "$REPO_DIR/scripts/serve-menu.sh" ;;
    1) "$REPO_DIR/pulsar" inventory menu ;;
    2) "$REPO_DIR/pulsar" models files ;;
    3) "$REPO_DIR/scripts/cluster-menu.sh" ;;
    4) help_menu ;;
    *) exit 0 ;;
  esac
  rc=$?
  set -e
  [ "$rc" -ne 130 ] || exit 130
  # Re-read local setup state; a submenu may have changed it.
  PULSAR_HOME_STATUS_SHOWN="$status_key" exec "$REPO_DIR/scripts/home.sh"
fi

# Before setup: the next setup step and read-only inspection. Each choice
# returns here with the setup status read again. Invalid saved membership
# cannot be set up again; forget is the way out.
if [ "$topology_state" = invalid ]; then
  first="Start over: forget this cluster…"
else
  first="Set up this cluster…"
fi
choice=$(PULSAR_CHOOSE_ESC=exit choose_index "Pulsar Inference Stack" "$first" "Browse the catalog (read-only)" \
  "Help and troubleshooting…" "Exit") \
  || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
set +e
case "$choice:$topology_state" in
  0:invalid) "$REPO_DIR/pulsar" topology forget ;;
  0:*) "$REPO_DIR/pulsar" topology setup ;;
  # Saved records only: catalog browsing probes no node.
  1:*) catalog_menu --read-only ;;
  2:*) help_menu ;;
  *) exit 0 ;;
esac
rc=$?
set -e
[ "$rc" -ne 130 ] || exit 130
if [ "$choice" = 0 ] && [ "$rc" -ne 0 ]; then
  printf '✗ Setup step did not complete; details above\n'
fi
PULSAR_HOME_STATUS_SHOWN="$status_key" exec "$REPO_DIR/scripts/home.sh"
