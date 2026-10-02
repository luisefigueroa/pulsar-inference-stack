#!/usr/bin/env bash
# Reconcile one completed invocation; Docker and SSH operations are read-only.
set -euo pipefail
SCRIPT_NAME=guarded-reconcile
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
output="" run_id=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir|--run-id)
      case "${2:-}" in
        ""|-*) echo "error: $1 requires a value" >&2; exit "${PULSAR_USAGE_EXIT:-2}" ;;
      esac
      if [ "$1" = --output-dir ]; then output="$2"; else run_id="$2"; fi
      shift ;;
    --help|-h)
      cat <<'HELP'
usage: pulsar guarded reconcile --output-dir DIR --run-id SHA256 [--json]

Verify completed cleanup and current rank absence, then retire only the
matching active service locator. Containers and historical records are retained.
HELP
      exit 0 ;;
    *) echo "error: unsupported reconciliation argument: $1" >&2; exit "${PULSAR_USAGE_EXIT:-2}" ;;
  esac
  shift
done
[ -n "$output" ] && [ -n "$run_id" ] || { echo 'error: --output-dir and --run-id required' >&2; exit "${PULSAR_USAGE_EXIT:-2}"; }
observed=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-guard-reconcile.XXXXXX")
trap 'rm -rf "$observed"' EXIT
python3 -m serving_guard.reconciliation validate --output "$output" --run-id "$run_id" >"$observed/plan.json"
. "$repo/scripts/lib.sh"
acquire_model_library_lifecycle_lock exclusive
topology_args=()
topology() {
  local index
  reload_cluster_topology || die 'confirmed topology required for guarded reconciliation'
  [ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || die 'confirmed topology required for guarded reconciliation'
  topology_args=(--topology-id "$CLUSTER_TOPOLOGY_ID")
  for ((index=0;index<CLUSTER_TOPOLOGY_COUNT;index++)); do
    topology_args+=(--member "${CLUSTER_NODE_IDS[$index]}" --member "${CLUSTER_NODE_HOSTNAMES[$index]}"
      --member "${CLUSTER_NODE_SSH_HOSTS[$index]}" --member "${CLUSTER_NODE_CONTROL_IPS[$index]}"
      --member "${CLUSTER_NODE_CONTROL_IFS[$index]}")
  done
}
topology
python3 -m serving_guard.reconciliation placement --output "$output" --run-id "$run_id" "${topology_args[@]}" >"$observed/indexes"
mapfile -t indexes <"$observed/indexes"
mapfile -t binding < <(python3 - "$observed/plan.json" <<'PY'
import json,sys
plan=json.load(open(sys.argv[1]))
for key in ('container_name','guard_run_id','plan_id','selected_spec_id'):print(plan[key])
PY
)
filters=("name=^/${binding[0]}$" "label=io.pulsar.gb10.guard-run=${binding[1]}"
  "label=io.pulsar.gb10.launch-plan=${binding[2]}" "label=io.pulsar.gb10.selected-spec-id=${binding[3]}")
selectors=(name run plan spec)
for ((rank=0;rank<${#indexes[@]};rank++)); do
  index="${indexes[$rank]}"
  for ((selector=0;selector<${#filters[@]};selector++)); do
    if [ "$index" = 0 ]; then
      "$PULSAR_DOCKER" container ls -aq --no-trunc --filter "${filters[$selector]}" >"$observed/$rank-${selectors[$selector]}.out" \
        || die 'Docker rank absence is unknown; service locator preserved'
    else
      require_topology_ssh_trust >/dev/null || die 'confirmed SSH trust required; service locator preserved'
      command=$(shell_join_q docker container ls -aq --no-trunc --filter "${filters[$selector]}")
      ssh_node "$index" "$command" </dev/null >"$observed/$rank-${selectors[$selector]}.out" \
        || die 'remote Docker rank absence is unknown; service locator preserved'
    fi
  done
done
before=("${topology_args[@]}")
topology
[ "${#topology_args[@]}" = "${#before[@]}" ] || die 'confirmed topology changed during reconciliation; service locator preserved'
for ((index=0;index<${#before[@]};index++)); do
  [ "${topology_args[$index]}" = "${before[$index]}" ] || die 'confirmed topology changed during reconciliation; service locator preserved'
done
python3 -m serving_guard.reconciliation retire --output "$output" --run-id "$run_id" \
  --state-root "$PULSAR_MODEL_LIBRARY_DIR" --observed "$observed" "${topology_args[@]}"
