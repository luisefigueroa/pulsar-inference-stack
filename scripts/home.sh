#!/usr/bin/env bash
# Neutral workflow menu. Local files decide whether cluster setup is complete.
# Entering the menu runs no SSH, Docker, GPU or Hugging Face probes. The
# Running block reads the last saved service inventory and storage budget.
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

# Help: the words, the catalog's labels, the serving steps and the command reference. Each page opens
# in the pager and returns here; Back or Esc returns to the home menu.
help_menu() {
  local choice rc
  while true; do
    choice=$(choose_index "Help" "What the words mean" "What the labels mean" "How serving works" "Command reference" "Back") \
      || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "$choice" in
      0) page_output "" "$REPO_DIR/pulsar" help terms || rc=$? ;;
      1) page_output "" "$REPO_DIR/pulsar" help labels || rc=$? ;;
      2) page_output "" "$REPO_DIR/pulsar" help workflow || rc=$? ;;
      3) page_output "" "$REPO_DIR/pulsar" help || rc=$? ;;
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
  1:*) require_gum "the Pulsar menu" "./pulsar topology check" "check saved nodes and fabric" \
    "./pulsar model budget" "disk headroom on every node" "./pulsar models list" "browse the catalog" \
    "./pulsar model files" "model files and placement on every node" \
    "./pulsar inventory" "see what runs on each node" "./pulsar doctor" "host diagnostics" \
    "./pulsar configure archive-root" "archive storage setting" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help labels, help workflow)" ;;
  *:enroll-ssh-trust) require_gum "the Pulsar menu" "./pulsar ssh-trust enroll" "enroll SSH trust (next setup step)" \
    "./pulsar models list" "browse the catalog" "./pulsar models show SPEC" "one spec's saved details" \
    "./pulsar doctor" "host diagnostics" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help labels, help workflow)" ;;
  *) require_gum "the Pulsar menu" "./pulsar topology setup" "set up cluster membership (next setup step)" \
    "./pulsar models list" "browse the catalog" "./pulsar models show SPEC" "one spec's saved details" \
    "./pulsar doctor" "host diagnostics" "./pulsar topology show" "saved cluster membership" \
    "./pulsar help" "commands and guides (help terms, help labels, help workflow)" ;;
esac
ui_keep_styles
# The status is captured before it prints, so Python can't see the terminal;
# it gets the width ui.sh reads from it, as the catalog menu does.
columns=$(_ui_terminal_columns) || columns=""
with_width() {
  if [ -n "$columns" ]; then COLUMNS=$columns "$@"; else "$@"; fi
}
# The setup status prints on entry, then again only when a choice changed it;
# the previous run of this menu passes what it compared.
status_text=$(with_width python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format text)
status_key="$status_text"
if [ "$complete" = 1 ]; then
  # The catalog is listed once for the Next line and the Running block; a
  # catalog that cannot be read leaves both out, and Catalog and storage
  # reports the error.
  catalog_json=$(python3 -m model_library.catalog list --json 2>/dev/null) || catalog_json=""
  # The Next line comes from saved catalog records. An unreadable archive
  # setting leaves it out, since suggestions depend on it; the status above
  # already says how to fix it.
  archives=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["archives"]["status"])')
  if [ -n "$catalog_json" ] && [ "$archives" != unreadable ]; then
    next_line=$(printf '%s' "$catalog_json" \
      | python3 -m model_library.catalog_menu next --archive-location "$archives" 2>/dev/null \
      | with_width python3 -m scripts.terminal_format --text) || next_line=""
    [ -z "$next_line" ] || status_text+=$'\n\n'"$next_line"
  fi
  # The Running block comes from the last saved service inventory and storage
  # budget, with their ages; nothing here probes a node. It prints again only
  # when a new observation was saved or the saved inventory became "last seen",
  # so its ages do not tick while the menu is open. One call reads the records
  # once and prints the block's change key on its first line, so the key
  # remembered is always the one for the block printed.
  running="" running_key=""
  if [ -n "$catalog_json" ]; then
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    output=$(printf '%s' "$catalog_json" | with_width python3 -m scripts.saved_observations home \
      --now "$now" --repo-root "$REPO_DIR" 2>/dev/null) || output=""
    # Only a key line followed by a block is used; anything else leaves both out.
    case "$output" in
      key$'\t'*$'\n'?*)
        running_key=${output%%$'\n'*}
        running_key=${running_key#key$'\t'}
        running=${output#*$'\n'}
        ;;
    esac
  fi
  status_key="$status_text"$'\n'"$running_key"
  [ -z "$running" ] || status_text+=$'\n\n'"$running"
fi
shown=${PULSAR_HOME_STATUS_SHOWN-}
unset PULSAR_HOME_STATUS_SHOWN
[ "$status_key" = "$shown" ] || printf '\n%s\n\n' "$status_text"

if [ "$complete" = 1 ]; then
  # The overview comes first: it answers what runs where before a catalog
  # choice. Cluster topology stays at 6 and Exit at 8.
  choice=$(PULSAR_CHOOSE_ESC=exit choose_index "Pulsar Inference Stack" "Cluster overview (checks nodes now)" \
    "Catalog and storage" "Model files and disk space (checks nodes now)" "Live service inventory" \
    "Host diagnostics" "Archive storage configuration" "Cluster topology" "Help" "Exit") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  # Each choice runs as a child; the menu returns here until Exit, Esc or Ctrl-C.
  set +e
  case "$choice" in
    # Probes the nodes only now: topology check, inventory and model budget.
    0) "$REPO_DIR/scripts/cluster-overview.sh" ;;
    1) "$REPO_DIR/pulsar" models menu ;;
    2) "$REPO_DIR/pulsar" models files ;;
    3) "$REPO_DIR/pulsar" inventory menu ;;
    # Long output opens in the pager, so this menu cannot bury it.
    4) page_output "Running host diagnostics…" "$REPO_DIR/pulsar" doctor ;;
    5) "$REPO_DIR/pulsar" configure archive-root menu ;;
    6) "$REPO_DIR/scripts/topology.sh" menu ;;
    7) help_menu ;;
    *) exit 0 ;;
  esac
  rc=$?
  set -e
  [ "$rc" -ne 130 ] || exit 130
  if [ "$choice" = 1 ] && [ "$rc" -ne 0 ]; then
    review_catalog_error "$rc" || exit $?
  fi
  # Re-read local setup state; a submenu may have changed it.
  PULSAR_HOME_STATUS_SHOWN="$status_key" exec "$REPO_DIR/scripts/home.sh"
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
PULSAR_HOME_STATUS_SHOWN="$status_key" exec "$REPO_DIR/scripts/home.sh"
