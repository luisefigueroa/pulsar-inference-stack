#!/usr/bin/env bash
# Refuse storage mutation while any container references an affected path.
set -euo pipefail
SCRIPT_NAME=storage-guard
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
node="" local_only=0 expected_topology="" spec_file="" expected_rank="" expected_home=""
paths=()
while [ $# -gt 0 ]; do
  case "$1" in
    --node|--node-id) [ $# -ge 2 ] || die "$1 needs a node"; node="$2"; shift ;;
    --path) [ $# -ge 2 ] || die "--path needs a path"; paths+=("$2"); shift ;;
    --local-only) local_only=1 ;;
    --expected-topology-id) [ $# -ge 2 ] || die "topology id required"; expected_topology="$2"; shift ;;
    --spec-file) [ $# -ge 2 ] || die "spec file required"; spec_file="$2"; shift ;;
    --expected-home-node) [ $# -ge 2 ] || die "home node required"; expected_home="$2"; shift ;;
    --expected-rank) [ $# -ge 2 ] || die "job rank required"; expected_rank="$2"; shift ;;
    --json) ;;
    -h|--help) echo 'usage: guard-storage.sh --node NODE_ID --path PATH [--path PATH] [--local-only] --json'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$node" ] && [ "${#paths[@]}" -gt 0 ] || die "node and affected paths are required"
require_cluster_nodes 1 >/dev/null || die "confirmed topology required"
if [ -n "$expected_topology" ]; then
  [ "$CLUSTER_TOPOLOGY_ID" = "$expected_topology" ] || die "confirmed topology differs from migration plan"
fi
rank=$(model_physical_rank "$node")
if [ -n "$spec_file" ] || [ -n "$expected_rank" ]; then
  [ -n "$spec_file" ] && [[ "$expected_rank" =~ ^[0-9]+$ ]] || die "spec file and expected job rank are required together"
  spec_id=$(python3 - "$REPO_DIR" "$spec_file" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from release_spec import load_spec
print(load_spec(sys.argv[2])["spec_id"])
PYCODE
  ) || die "invalid migration spec"
  export PULSAR_SPEC_FILE="$spec_file"
  load_conf "$spec_id"
  if [ "$NODES" = 1 ]; then
    [ "$expected_rank" = 0 ] || die "single-node spec has only job rank 0"
    selector=$(spec_overlay_node_selector "$node")
    resolve_single_node_placement "$selector" || die "spec placement is unconfirmed"
    [ "$SINGLE_NODE_INDEX" = "$rank" ] || die "physical node differs from spec placement"
  else
    require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" || die "spec topology is incomplete"
    [ "$expected_rank" -lt "$NODES" ] && [ "$expected_rank" = "$rank" ] || die "physical node differs from spec job rank"
  fi
fi
if [ -n "$expected_home" ]; then
  [ -n "$spec_file" ] || die "expected home node requires the selected spec"
  home_rank=$(model_physical_rank "$expected_home")
  if [ "$NODES" = 1 ]; then
    [ "$home_rank" = "$rank" ] || die "one-node home must be on the selected serving node"
  else
    [ "$home_rank" -lt "$NODES" ] || die "model home is outside the selected serving ranks"
  fi
fi
[ "$local_only" -eq 0 ] || [ "$rank" -eq 0 ] || die "local filesystem migration requires the confirmed local node"
observation=$(model_container_observation "$rank") || die "required node or containers are unobservable"
printf '%s' "$observation" | python3 -c '
import json,os,sys
from pathlib import PurePosixPath
paths=sys.argv[1:]
for path in paths:
 if not path.startswith("/") or ".." in PurePosixPath(path).parts: raise SystemExit("unsafe affected path")
obs=json.load(sys.stdin)
blockers=[]
for c in obs["containers"]:
 for mount in c["mounts"]:
  for path in paths:
   if path==mount or path.startswith(mount.rstrip("/")+"/") or mount.startswith(path.rstrip("/")+"/"):
    blockers.append({"container":c["id"],"reason":"container references affected storage"})
print(json.dumps({"kind":"pulsar-storage-guard","schema_version":1,"safe":not blockers,"blockers":blockers}))
raise SystemExit(1 if blockers else 0)
' "${paths[@]}"
