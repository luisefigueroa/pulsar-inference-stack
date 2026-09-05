#!/usr/bin/env bash
# Neutral workflow menu. Entering the menu runs no live probes or model actions.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ ! -t 0 ]; then exec "$REPO_DIR/pulsar" help; fi
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
choice=$(choose_index "Pulsar Inference Stack" "Catalog and storage" "Live service inventory" \
  "Host diagnostics" "Archive storage configuration" "Cluster topology" "Help" "Exit") || exit 0
case "$choice" in
  0) exec "$REPO_DIR/scripts/model-storage.sh" menu ;;
  1) exec "$REPO_DIR/scripts/inventory.sh" ;;
  2) exec "$REPO_DIR/scripts/doctor.sh" ;;
  3) exec "$REPO_DIR/pulsar" configure archive-root ;;
  4) exec "$REPO_DIR/scripts/topology.sh" menu ;;
  5) exec "$REPO_DIR/pulsar" help ;;
  6) exit 0 ;;
esac
