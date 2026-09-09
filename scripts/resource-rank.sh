#!/usr/bin/env bash
# One sampler process group. Stack owns physical-node selection and SSH.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
context="${1:?context required}" rank="${2:?rank required}" interval="${3:?interval required}"
token="${4:?session token required}" relay_lock="${5:?relay lock required}"
mapfile -t fields < <(python3 - "$context" "$rank" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]));rank=value['ranks'][int(sys.argv[2])]
for field in (rank['rank_label'],rank['node_id'],value['container_name'],value['spec_id'],value['topology_id']): print(field)
PY
)
[ "${#fields[@]}" = 5 ] || die 'invalid resource context'
load_cluster_topology || die 'confirmed topology required'
[ "$CLUSTER_TOPOLOGY_ID" = "${fields[4]}" ] || die 'confirmed membership changed before resource sampling'
index=$(model_physical_rank "${fields[1]}")
args=(--rank-label "${fields[0]}" --node-id "${fields[1]}" --container-name "${fields[2]}"
      --spec-id "${fields[3]}" --topology-id "${fields[4]}" --interval "$interval" --session-token "$token")
collect() {
  if [ "$index" = 0 ]; then
    python3 -u "$REPO_DIR/scripts/resource_sample.py" "${args[@]}"
  else
    require_topology_ssh_trust >/dev/null || return 2
    ssh_node "$index" python3 -u - "${args[@]}" <"$REPO_DIR/scripts/resource_sample.py"
  fi
}
collect | python3 "$REPO_DIR/scripts/resource_relay.py" --lock "$relay_lock" --rank "${fields[0]}"
