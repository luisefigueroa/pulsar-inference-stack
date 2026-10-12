#!/usr/bin/env bash
# Stop containers by proven ownership and immutable ID. Always retain files.
set -euo pipefail
SCRIPT_NAME=down
down_usage() {
  python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/terminal_format.py" <<'HELP'
usage: pulsar stop SPEC_ID [--node NODE] [--spec-file FILE]
       pulsar stop --all --yes

Stop the owned service for the exact spec on every participating node.
Model files, pins, archives and evidence are always retained. NODE is a
confirmed node's hostname or node ID.

--all stops every Stack-managed service on all confirmed nodes, and --yes
approves that. Without --yes it warns and still runs; CLI contract 2 requires
--yes. Stopping one spec asks no confirmation and takes no --yes.
HELP
}
case "${1:-}" in
  -h|--help) down_usage; exit 0 ;;
  "")
    down_usage >&2
    # The last line names the mistake; --json reports it as the usage error.
    echo "error: stop requires a spec ID or --all" >&2
    exit "${PULSAR_USAGE_EXIT:-2}" ;;
esac
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
YES=0
case "$1" in --yes|-y) YES=1; shift ;; esac
[ $# -gt 0 ] || usage_die "stop requires a spec ID or --all"
TARGET="$1"; shift
NODE_SELECTOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --node) case "${2:-}" in ""|-*) usage_die "--node requires a node" ;; esac; NODE_SELECTOR="$2"; shift ;;
    --spec-file) case "${2:-}" in ""|-*) usage_die "--spec-file requires a file" ;; esac; export PULSAR_SPEC_FILE="$2"; shift ;;
    --retain-weights)
      warn "pulsar stop --retain-weights is deprecated: stop always retains model files. It is removed in CLI contract 2." ;;
    --pin-weights|--purge-hot) usage_die "stop retains files; use an explicit model pin or purge command" ;;
    --yes|-y) YES=1 ;;
    *) usage_die "unknown argument: $1" ;;
  esac
  shift
done
if [ "$TARGET" != --all ]; then
  # Stopping one spec's owned service asks nothing and keeps every file.
  [ "$YES" = 0 ] || usage_die "stop SPEC asks no confirmation and always keeps model files; remove --yes"
  [[ "$TARGET" =~ ^[0-9a-f]{64}$ ]] || usage_die "stop requires the complete 64-character spec id (./pulsar models list --json)"
fi
load_cluster_topology || die "confirmed topology is required for safe stop"
# Guarded trials, and any service recorded outside the default node order.
# Each line: plan ID, guarded run ID ('-' when none), spec ID, recorded hosts,
# and 'nondefault' or 'default' placement. Ordinary stop's default-placement
# ownership cannot address a nondefault placement; a guarded trial there is
# stopped on its recorded bindings once its controller has exited.
recorded_trials=$(python3 - "$PULSAR_MODEL_LIBRARY_DIR" "$TARGET" "${CLUSTER_NODE_IDS[@]}" <<'PY_STOP'
import sys
from model_library.state import Store
from scripts.container_runtime import validate_plan
store=Store(sys.argv[1]);target=sys.argv[2];confirmed=sys.argv[3:]
for row in store.records('services'):
    if target!='--all' and row['selected_spec_id']!=target:continue
    plan=validate_plan(store.get('service-plans',row['plan_id']))
    nondefault=len(plan['ranks'])>1 and [rank['node_id'] for rank in plan['ranks']]!=confirmed[:len(plan['ranks'])]
    if nondefault or plan.get('guard_run_id'):
        hosts=', '.join(rank.get('hostname') or rank['node_id'] for rank in plan['ranks'])
        # '-' marks a plan without a guarded run; tab-separated read would drop an empty field.
        print('\t'.join([plan['plan_id'], plan.get('guard_run_id') or '-', plan['selected_spec_id'], hosts,
                         'nondefault' if nondefault else 'default']))
PY_STOP
) || die "the saved service records cannot be read; nothing was stopped"
guarded_trials=""
if [ -n "$recorded_trials" ]; then
  while IFS=$'\t' read -r trial_plan trial_run trial_spec trial_hosts trial_placement; do
    trial_ref=$(spec_reference "$trial_spec")
    if [ "$trial_run" = - ]; then
      # Only guarded runs place ranks off the default order; without a guarded run
      # ID there are no labels that prove which containers belong to it.
      error_line "spec ${trial_spec:0:12} has a service recorded on $trial_hosts outside the default node order, but not by a guarded run, so stop cannot prove which containers are its own. Nothing was stopped. Next: ./pulsar inventory"
      exit 1
    fi
    # A running guarded controller holds the lifecycle lock for its whole life, on
    # any placement; refuse now rather than wait for the lock. Any other holder is
    # waited for by the ordinary lock below.
    lock_path="${PULSAR_MODEL_LIBRARY_LOCK_FILE:-$PULSAR_MODEL_LIBRARY_DIR/lifecycle.lock}"
    if [ -e "$lock_path" ] && guarded_controller_holds_lock "$lock_path"; then
      error_line "spec ${trial_spec:0:12} has guarded trial ${trial_run:0:12} on $trial_hosts, and a guarded run's controller is running, so the trial may still be running. Ordinary stop does not stop a running guarded trial. Next: ./pulsar guarded stop --output-dir <its output directory> --run-id $trial_run; once it has exited, ./pulsar stop $trial_ref stops what remains"
      exit 1
    fi
    # On the default placement ordinary stop already proves ownership by its labels.
    [ "$trial_placement" = nondefault ] || continue
    if [ "$TARGET" = --all ]; then
      error_line "stop --all does not stop guarded trial ${trial_run:0:12} (spec ${trial_spec:0:12}) on $trial_hosts; nothing was stopped. Next: ./pulsar stop $trial_ref after its controller exits, then rerun stop --all"
      exit 1
    fi
    if [ -n "$NODE_SELECTOR" ]; then
      usage_die "spec ${trial_spec:0:12} runs as guarded trial ${trial_run:0:12} on $trial_hosts; a trial stops on all its nodes together, so --node cannot select one. Nothing was stopped. Next: ./pulsar stop $trial_ref"
    fi
    guarded_trials+="$trial_plan"$'\n'
  done <<<"$recorded_trials"
fi
if [ "$TARGET" = --all ]; then
  [ -z "$NODE_SELECTOR" ] || usage_die "--node cannot be used with --all"
  [ "$YES" = 1 ] || warn "pulsar stop --all without --yes is deprecated: it stops every Stack-managed service on all $CLUSTER_TOPOLOGY_COUNT confirmed nodes. CLI contract 2 requires --yes."
  # The delegated command acquires the lock once for all confirmed ranks.
  exec "$REPO_DIR/cluster/stop-cluster.sh" --all
fi
[[ "$TARGET" =~ ^[0-9a-f]{64}$ ]] || usage_die "stop requires the complete 64-character spec id (./pulsar models list --json)"
acquire_model_library_lifecycle_lock exclusive
load_cluster_topology || die "confirmed topology is required for safe stop"
[ "$CLUSTER_TOPOLOGY_COUNT" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || die "confirmed topology is required for safe stop"
# The exclusive lock proves no guarded controller is running; stop each such
# trial on its recorded bindings before the default-placement stop. Probe every
# confirmed node first, as that stop does, so no failure follows a removal.
if [ -n "$guarded_trials" ]; then
  for ((index = 0; index < CLUSTER_TOPOLOGY_COUNT; index++)); do
    list_managed_container_ids_on_node "$index" >/dev/null || { stop_unobservable_error "$index" "$TARGET"; exit 1; }
  done
fi
while IFS= read -r trial_plan; do
  [ -n "$trial_plan" ] || continue
  stop_recorded_guarded_service "$TARGET" "$trial_plan"
done <<<"$guarded_trials"
stop_named_service_by_labels "$TARGET" "$NODE_SELECTOR"
retire_stopped_service_indexes "$TARGET" "$NODE_SELECTOR"
if [ -n "${PULSAR_STOP_RESULT_FILE:-}" ]; then
  stopped=true; [ "${STOP_NAMED_NOTHING_FOUND:-0}" != 1 ] || [ "${GUARDED_TRIAL_STOPPED:-0}" = 1 ] || stopped=false
  printf '{"spec_id": "%s", "stopped": %s}\n' "$TARGET" "$stopped" >"$PULSAR_STOP_RESULT_FILE"
fi
if [ "${STOP_NAMED_NOTHING_FOUND:-0}" = 1 ] && [ "${GUARDED_TRIAL_STOPPED:-0}" != 1 ]; then
  log "Nothing was stopped. Model files, pins, archives and experiment evidence are unchanged."
else
  log "Stopped. Model files, pins, archives and experiment evidence are retained."
fi
