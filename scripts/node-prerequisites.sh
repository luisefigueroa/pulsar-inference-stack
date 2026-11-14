#!/usr/bin/env bash
# node-prerequisites.sh NODE — one confirmed node's prerequisites as
# `probe-node.py --prerequisites-only` JSON on stdout, for Serve a model…: a
# plan that downloads to NODE checks `hf` there first (D14). NODE is a node_id
# or hostname of saved membership. The probe asks the Hub once from that node,
# with its own login, and returns state words only, never a token or account.
# It is the same probe doctor and guided setup run (probe_node_json_for_rank);
# discovery, topology check and Start never ask for it.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=lib.sh
. "$REPO_DIR/scripts/lib.sh"
node="${1:?usage: node-prerequisites.sh NODE}"
require_cluster_nodes 1 >/dev/null || die "no saved cluster membership"
for index in "${!CLUSTER_NODE_IDS[@]}"; do
  if [ "${CLUSTER_NODE_IDS[$index]}" = "$node" ] || [ "$(human_node_name "$index")" = "$node" ]; then
    probe_node_json_for_rank "$index" --prerequisites-only
    exit
  fi
done
die "$node is not a node of saved membership"
