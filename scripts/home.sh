#!/usr/bin/env bash
# Neutral workflow menu. Local files decide whether cluster bind is still open.
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
echo
python3 "$SETUP_PY" --repo-root "$REPO_DIR" --format text
echo
complete=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("complete") else "0")')

if [ "$complete" = 1 ]; then
  choice=$(choose_index "Pulsar Inference Stack" "Catalog and storage" "Live service inventory" \
    "Host diagnostics" "Archive storage configuration" "Cluster topology" "Help" "Exit") || exit 0
  case "$choice" in
    0) exec "$REPO_DIR/scripts/model-storage.sh" menu ;;
    1) exec "$REPO_DIR/scripts/inventory.sh" ;;
    2) exec "$REPO_DIR/scripts/doctor.sh" ;;
    3) exec "$REPO_DIR/pulsar" configure archive-root menu ;;
    4) exec "$REPO_DIR/scripts/topology.sh" menu ;;
    5) exec "$REPO_DIR/pulsar" help ;;
    6) exit 0 ;;
  esac
fi

next_action=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_action") or "")')
next_label=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_label") or "")')
choice=$(choose_index "Pulsar Inference Stack" "$next_label" "Exit") || exit 0
[ "$choice" = 0 ] || exit 0
case "$next_action" in
  confirm-membership) "$REPO_DIR/scripts/detect-fabric.sh" --write-topology || true ;;
  enroll-ssh-trust) "$REPO_DIR/scripts/topology-ssh-trust.sh" enroll || true ;;
  select-archive) "$REPO_DIR/pulsar" configure archive-root menu || true ;;
  *) exit 0 ;;
esac
exec "$REPO_DIR/scripts/home.sh"
