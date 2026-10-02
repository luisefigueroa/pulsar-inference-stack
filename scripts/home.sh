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

SETUP_PY="${PULSAR_SETUP_STATUS_PY:-$REPO_DIR/scripts/setup_status.py}"
status_json=$(python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format json)
complete=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("complete") else "0")')
next_action=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_action") or "")')
# Without Gum the menu does not open; name the commands behind its choices.
case "$complete:$next_action" in
  1:*) require_gum "the Pulsar menu" "pulsar models list | inventory | doctor | configure archive-root | topology show | help" ;;
  *:enroll-ssh-trust) require_gum "the Pulsar menu" "pulsar ssh-trust enroll | models list | models show SPEC | doctor | topology show | help" ;;
  *) require_gum "the Pulsar menu" "pulsar topology setup | models list | models show SPEC | doctor | topology show | help" ;;
esac
echo
python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format text
echo

if [ "$complete" = 1 ]; then
  choice=$(choose_index "Pulsar Inference Stack" "Catalog and storage" "Live service inventory" \
    "Host diagnostics" "Archive storage configuration" "Cluster topology" "Help" "Exit") \
    || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  # Each choice runs as a child; the menu returns here until Exit, Esc or Ctrl-C.
  set +e
  case "$choice" in
    0) "$REPO_DIR/pulsar" models menu ;;
    1) "$REPO_DIR/pulsar" inventory menu ;;
    2) "$REPO_DIR/pulsar" doctor ;;
    3) "$REPO_DIR/pulsar" configure archive-root menu ;;
    4) "$REPO_DIR/scripts/topology.sh" menu ;;
    5) "$REPO_DIR/pulsar" help ;;
    *) exit 0 ;;
  esac
  rc=$?
  set -e
  [ "$rc" -ne 130 ] || exit 130
  # Re-read local setup state; a submenu may have changed it.
  exec "$REPO_DIR/scripts/home.sh"
fi

# First run: the next setup step and existing read-only inspection paths.
# Each choice returns here with the setup status read again.
next_label=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_label") or "")')
choice=$(choose_index "Pulsar Inference Stack" "$next_label" "Browse the catalog (read-only)" \
  "Host diagnostics" "Cluster topology (read-only)" "Help" "Exit") \
  || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
set +e
case "$choice:$next_action" in
  0:set-up-topology) "$REPO_DIR/pulsar" topology setup ;;
  0:enroll-ssh-trust) "$REPO_DIR/pulsar" ssh-trust enroll ;;
  # Saved records only: catalog browsing and topology show probe no node.
  1:*) "$REPO_DIR/pulsar" models menu --read-only ;;
  2:*) "$REPO_DIR/pulsar" doctor ;;
  3:*) "$REPO_DIR/pulsar" topology show ;;
  4:*) "$REPO_DIR/pulsar" help ;;
  *) exit 0 ;;
esac
rc=$?
set -e
[ "$rc" -ne 130 ] || exit 130
if [ "$choice" = 0 ] && [ "$rc" -ne 0 ]; then
  printf '✗ Setup step did not complete; details above\n'
fi
exec "$REPO_DIR/scripts/home.sh"
