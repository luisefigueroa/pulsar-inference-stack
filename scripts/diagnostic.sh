#!/usr/bin/env bash
# Development/unaccepted diagnostic lifecycle. Bash is the sole process owner.
set -Eeuo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OP=${1:-help}
[ $# -eq 0 ] || shift
PY=(python3 -I -B "$REPO_DIR/scripts/diagnostic_cli.py")
case "$OP" in
  plan|show) exec "${PY[@]}" "$OP" "$@" ;;
  help|--help|-h)
    echo 'Development/unaccepted: diagnostic plan|run|show|cleanup. No execution release.'
    exit 0 ;;
  _journal-query)
    # Preserve source stderr independently of the owning client's teardown.
    journal_error=$1; shift
    exec journalctl "$@" 2>"$journal_error" ;;
  _admission)
    ATTEMPT=$1
    if [ -n "${PULSAR_DIAGNOSTIC_STACK_EXECUTABLE:-}" ]; then
      # A development checkout can use the confirmed installation's public
      # admission boundary without copying its topology or SSH trust state.
      [[ "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" = /* ]] && [ -x "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" ]
      "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" topology show --json >"$ATTEMPT/topology-saved.json"
      "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" topology check --json >"$ATTEMPT/topology.json"
      "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" ssh-trust check --json >"$ATTEMPT/trust.json"
      "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" inventory --json >"$ATTEMPT/inventory.json"
      "$PULSAR_DIAGNOSTIC_STACK_EXECUTABLE" doctor --json >"$ATTEMPT/doctor.json"
      python3 -I -B - "$ATTEMPT" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
load=lambda name: json.loads((p/name).read_text())
saved=load('topology-saved.json'); checked=load('topology.json')
trust=load('trust.json'); inventory=load('inventory.json'); doctor=load('doctor.json')
nodes=saved['topology']['nodes']; expected={n['node_id'] for n in nodes}
assert nodes and len(expected)==len(nodes)
assert checked['status']=='ready' and {n['node_id'] for n in checked['nodes']}==expected
assert all(n['status']=='ready' for n in checked['nodes'])
assert trust['ok'] is True and {n['node_id'] for n in trust['nodes']}==expected
assert all(n['ok'] is True for n in trust['nodes'])
assert len({saved['topology']['topology_id'],checked['topology']['topology_id'],trust['topology_id'],inventory['topology_id']})==1
inventory_nodes=list(inventory['nodes'].values())
assert {n['node_id'] for n in inventory_nodes}==expected
assert all(n['confirmed'] is True and n['probe_status']=='ok' for n in inventory_nodes)
assert not inventory['services'] and not inventory['unmanaged_gpu_processes']
assert doctor['kind']=='pulsar-doctor' and type(doctor['fail']) is int and doctor['fail']==0
assert doctor['result'] in ('pass','pass_with_warnings')
local=[n for n in nodes if n['ssh_host']=='local']
assert len(local)==1 and local[0]['rank']==0
print(local[0]['node_id']); print('local')
PY
      exit 0
    fi
    . "$REPO_DIR/scripts/lib.sh"
    load_cluster_topology
    [ "${CLUSTER_TOPOLOGY_COUNT:-0}" -gt 0 ]
    "$REPO_DIR/scripts/topology.sh" check --json >"$ATTEMPT/topology.json"
    "$REPO_DIR/scripts/topology-ssh-trust.sh" check >"$ATTEMPT/trust.txt"
    require_topology_rewrite_idle "$CLUSTER_TOPOLOGY_FILE" >"$ATTEMPT/idle.txt"
    "$REPO_DIR/scripts/doctor.sh" --json >"$ATTEMPT/doctor.json"
    python3 -I -B - "$ATTEMPT/doctor.json" <<'PY'
import json,sys
with open(sys.argv[1]) as f:
    d=json.load(f)
assert d.get('kind') == 'pulsar-doctor' and type(d.get('fail')) is int and d['fail'] == 0
assert d.get('result') in ('pass','pass_with_warnings') and isinstance(d.get('checks'),list) and d['checks']
PY
    printf '%s\n%s\n' "${CLUSTER_NODE_IDS[0]}" "${CLUSTER_NODE_SSH_HOSTS[0]}"
    exit 0 ;;
esac
PLAN= ATTEMPT= YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --plan) PLAN=${2:?}; shift ;;
    --attempt-dir) ATTEMPT=${2:?}; shift ;;
    --yes) YES=1 ;;
    --json) ;;
    *) echo 'invalid diagnostic argument' >&2; exit 2 ;;
  esac
  shift
done
[ "$YES" = 1 ] && [ -n "$ATTEMPT" ] || exit 2
ATTEMPT=$(realpath -m -- "$ATTEMPT")
export PULSAR_DIAGNOSTIC_OWNER_PID=$$
CLEANUP_ONLY=false
if [ "$OP" = run ]; then
  [ -n "$PLAN" ] || exit 2
  if ! mkdir -m 700 -- "$ATTEMPT"; then
    echo '{"schema_version":1,"ok":false,"error":{"code":"already_claimed"}}'
    exit 2
  fi
  exec 9>"$ATTEMPT/attempt.lock"
elif [ "$OP" = cleanup ]; then
  CLEANUP_ONLY=true
  exec 9<"$ATTEMPT/attempt.lock"
else
  exit 2
fi
if ! flock -n 9; then
  echo '{"schema_version":1,"ok":false,"error":{"code":"already_claimed"}}'
  exit 2
fi
CAPTURE=$ATTEMPT
helper() { timeout --signal=TERM --kill-after=0.1s 0.5s "${PY[@]}" "$1" --attempt-dir "$ATTEMPT" --cleanup-only "$CLEANUP_ONLY" --capture-dir "$CAPTURE" "${@:2}"; }
field() { helper field --file "$1" --key "$2" --raw; }
CANCEL=0 CLEANING=0 OWNED=0 OBSERVER_PID= CLIENT_PID= CID= NAME= FAILURE= GUARD=0
STOP_RC=null RM_RC=null QUERY_RC=null LOGS_RC=null
CLIENT_RC=0 OBSERVER_WAITED=false OBSERVER_RC=null OBSERVER_FORCED=false
CLIENTS_CLOSED=true
JOURNAL_PID= JOURNAL_START= JOURNAL_CURSOR= JOURNAL_BACKEND=
JOURNAL_WAITED=false JOURNAL_RC=null JOURNAL_FORCED=false JOURNAL_VERIFIED=false
JOURNAL_QUERY_RC=null
JOURNAL_ARGS=()
trap 'CANCEL=1; FAILURE=${FAILURE:-cancelled by SIGTERM}' TERM
trap 'CANCEL=1; FAILURE=${FAILURE:-cancelled by SIGINT}' INT

# A bounded command completion and its process-group closure are separate facts.
# Keep the session leader alive until closure so the group cannot be reused.
CLIENT_START= CLIENT_KEY= CLIENT_WAIT=null
client_identity() {
  local line
  local -a fields
  IFS= read -r line <"/proc/$CLIENT_PID/stat" 2>/dev/null || return 1
  read -r -a fields <<<"${line##*) }"
  [[ "${fields[2]:-}" = "$CLIENT_PID" && "${fields[3]:-}" = "$CLIENT_PID" && "${fields[19]:-}" =~ ^[0-9]+$ ]] || return 1
  if [ -n "$CLIENT_START" ]; then [ "${fields[19]}" = "$CLIENT_START" ]; else CLIENT_START=${fields[19]}; fi
}
group_gone() {
  local diagnostic
  if diagnostic=$(LC_ALL=C kill -0 -- "-$CLIENT_PID" 2>&1); then return 1; fi
  [[ "$diagnostic" == *'No such process'* ]]
}
close_client() {
  [ -n "$CLIENT_PID" ] || return 0
  local verified=false group_closed=false sent_kill=false i
  if client_identity; then
    verified=true
    # These are only the owned external-client session, never container PIDs.
    kill -TERM -- "-$CLIENT_PID" 2>/dev/null || :
    sleep 0.025
    if client_identity; then
      kill -KILL -- "-$CLIENT_PID" 2>/dev/null && sent_kill=true
    fi
  fi
  for ((i=0; i<30; i++)); do
    if group_gone; then group_closed=true; break; fi
    sleep 0.01
  done
  if ! kill -0 "$CLIENT_PID" 2>/dev/null; then
    if wait "$CLIENT_PID"; then CLIENT_WAIT=0; else CLIENT_WAIT=$?; fi
  else
    CLIENT_WAIT=null
  fi
  if [ "$verified" != true ] || [ "$group_closed" != true ] || [ "$CLIENT_WAIT" = null ]; then
    CLIENTS_CLOSED=false
    FAILURE=${FAILURE:-external client session closure is unknown}
  fi
  printf '{"pid":%s,"starttime":"%s","identity_verified":%s,"kill_sent":%s,"group_absent":%s,"wait_exit_code":%s}\n' \
    "$CLIENT_PID" "$CLIENT_START" "$verified" "$sent_kill" "$group_closed" "$CLIENT_WAIT" >"$CAPTURE/$CLIENT_KEY.group.json"
  CLIENT_PID= CLIENT_START=
}
run_client() {
  local key=$1 seconds=$2 blocks=$3
  shift 3
  if [ "$CLEANING" = 0 ] && [ "$CANCEL" = 1 ]; then CLIENT_RC=125; return 0; fi
  if [ "$CLEANING" = 0 ] && [ "$GUARD" = 1 ] && ! helper guard >"$CAPTURE/guard.json"; then
    FAILURE=${FAILURE:-observer safety or coverage guard failed before dispatch}
    CLIENT_RC=125
    return 0
  fi
  if [ -f "$CAPTURE/$key.rc" ]; then rm -- "$CAPTURE/$key.rc"; fi
  local owner_stat owner_start
  local -a owner_fields
  IFS= read -r owner_stat <"/proc/$$/stat"
  read -r -a owner_fields <<<"${owner_stat##*) }"
  owner_start=${owner_fields[19]}
  setsid bash -c '
    ulimit -Sf "$1"
    receipt=$2; seconds=$3; owner=$4; owner_start=$5; shift 5
    # A caught signal keeps the group identity pinned; children receive their
    # ordinary signal dispositions. The owner closes the entire client group.
    trap : INT TERM
    if timeout --foreground --signal=TERM --kill-after=0.2s "${seconds}s" "$@" 9>&-; then rc=0; else rc=$?; fi
    printf "%s\n" "$rc" >"$receipt"
    for ((n=0; n<240; n++)); do
      if ! IFS= read -r stat_line <"/proc/$owner/stat" 2>/dev/null; then break; fi
      read -r -a fields <<<"${stat_line##*) }"
      [ "${fields[19]:-}" = "$owner_start" ] || break
      sleep 0.025
    done
    # Owner loss cannot leave this owned session running without a deadline.
    kill -KILL -- "-$$"
  ' _ "$blocks" "$CAPTURE/$key.rc" "$seconds" "$$" "$owner_start" "$@" \
    >"$CAPTURE/$key.stdout" 2>"$CAPTURE/$key.stderr" &
  CLIENT_PID=$! CLIENT_START= CLIENT_KEY=$key CLIENT_WAIT=null
  local i
  for ((i=0; i<10; i++)); do client_identity && break; sleep 0.005; done
  while kill -0 "$CLIENT_PID" 2>/dev/null; do
    [ ! -f "$CAPTURE/$key.rc" ] || break
    if [ "$CLEANING" = 0 ]; then
      if [ "$CANCEL" = 1 ]; then close_client; CLIENT_RC=125; return 0; fi
      if [ "$GUARD" = 1 ] && ! helper guard >"$CAPTURE/guard.json"; then
        FAILURE=${FAILURE:-observer safety or coverage guard failed}
        close_client; CLIENT_RC=125; return 0
      fi
    fi
    sleep 0.025
  done
  local command_rc=
  if [ -f "$CAPTURE/$key.rc" ]; then read -r command_rc <"$CAPTURE/$key.rc"; fi
  close_client
  if [[ "$command_rc" =~ ^[0-9]{1,3}$ ]] && [ "$command_rc" -le 255 ]; then
    CLIENT_RC=$command_rc
  else
    CLIENT_RC=125
  fi
  if [ "$CLIENTS_CLOSED" != true ]; then
    record_failure 'external client session closure is unknown'
    if [ "$CLEANING" = 0 ]; then CLIENT_RC=125; fi
  fi
}

docker_client() { local key=$1 seconds=$2 blocks=$3; shift 3; run_client "$key" "$seconds" "$blocks" "${PULSAR_DOCKER:-docker}" "$@"; }
ack() {
  local i
  for ((i=0; i<15; i++)); do
    if helper ack >"$CAPTURE/ack.json"; then return 0; fi
    [ -n "$OBSERVER_PID" ] && kill -0 "$OBSERVER_PID" 2>/dev/null || return 1
    sleep 0.02
  done
  return 1
}
record_failure() { helper failure --reason "$1" >"$CAPTURE/failure.json" || :; }

journal_identity() {
  local line
  local -a fields
  IFS= read -r line <"/proc/$JOURNAL_PID/stat" 2>/dev/null || return 1
  read -r -a fields <<<"${line##*) }"
  [[ "${fields[19]:-}" =~ ^[0-9]+$ ]] || return 1
  if [ -n "$JOURNAL_START" ]; then [ "${fields[19]}" = "$JOURNAL_START" ]; else JOURNAL_START=${fields[19]}; fi
}
close_journal() {
  [ -n "$JOURNAL_PID" ] || return 0
  local i
  helper journal-closing >"$CAPTURE/journal-closing-result.json" || record_failure 'journal close handshake failed'
  if journal_identity; then
    JOURNAL_VERIFIED=true
    kill -TERM "$JOURNAL_PID" 2>/dev/null || :
    for ((i=0; i<20; i++)); do
      kill -0 "$JOURNAL_PID" 2>/dev/null || break
      sleep 0.025
    done
    if journal_identity; then
      JOURNAL_FORCED=true
      # Only this exact owned journal client; never a container process.
      kill -KILL "$JOURNAL_PID" 2>/dev/null || :
      for ((i=0; i<10; i++)); do
        kill -0 "$JOURNAL_PID" 2>/dev/null || break
        sleep 0.025
      done
    fi
  fi
  if ! kill -0 "$JOURNAL_PID" 2>/dev/null; then
    if wait "$JOURNAL_PID"; then JOURNAL_RC=0; else JOURNAL_RC=$?; fi
    JOURNAL_WAITED=true
  fi
  if [ "$JOURNAL_VERIFIED" != true ] || [ "$JOURNAL_WAITED" != true ] || [ "$JOURNAL_FORCED" != false ]; then
    CLIENTS_CLOSED=false
    record_failure 'owned journal client closure is unconfirmed or forced'
  fi
  helper journal-closed --facts "{\"query_rc\":$JOURNAL_QUERY_RC,\"waited\":$JOURNAL_WAITED,\"exit_code\":$JOURNAL_RC,\"forced\":$JOURNAL_FORCED,\"identity_verified\":$JOURNAL_VERIFIED}" >"$CAPTURE/journal-closure.json" || record_failure 'journal final query receipt failed'
  JOURNAL_PID=
}

reconcile() {
  local original_rc=$?
  [ "$CLEANING" = 0 ] || return 0
  CLEANING=1
  # Cancellation is already latched. Repeated signals must not interrupt Bash
  # wait builtins or replace actual child exit statuses during reconciliation.
  trap '' INT TERM
  trap - ERR
  set +e
  close_client
  if [ "$OWNED" != 1 ]; then return 0; fi
  [ -z "$FAILURE" ] || record_failure "$FAILURE"
  if [ "$original_rc" -ne 0 ]; then record_failure 'unexpected owner/helper exit'; fi
  # Reconcile ALL create replies, including non-timeout errors and owner signals.
  # Lookup by intended name establishes no mutation authority by itself.
  if [ -f "$ATTEMPT/container-create.json" ]; then
    if [ -z "$CID" ]; then
      docker_client reconcile 2 128 inspect "$NAME"
      if [ "$CLIENT_RC" = 0 ] && helper created --file reconcile.stdout >"$CAPTURE/reconciled.json"; then
        CID=$(field lifecycle.json container_id)
        if [ -n "$OBSERVER_PID" ]; then ack || record_failure 'observer did not acknowledge created identity'; fi
      fi
    fi
    if [[ "$CID" =~ ^[0-9a-f]{64}$ ]]; then
      docker_client before-stop 2 128 inspect "$CID"
      if [ "$CLIENT_RC" = 0 ] && helper identity --file before-stop.stdout >"$CAPTURE/identity.json"; then
        # A never-started created object has a separate valid stopped disposition.
        if ! helper check-stopped --file before-stop.stdout >"$CAPTURE/stopped-check.json"; then
          docker_client stop 3 128 stop --timeout -1 "$CID"
          STOP_RC=$CLIENT_RC
        fi
        docker_client stopped 2 128 inspect "$CID"
        if [ "$CLIENT_RC" = 0 ] && helper stopped --file stopped.stdout >"$CAPTURE/stopped.json"; then
          if [ -n "$OBSERVER_PID" ]; then ack || record_failure 'observer stopped-state acknowledgement missing'; fi
          docker_client logs 2 12288 logs "$CID"
          LOGS_RC=$CLIENT_RC
          [ "$LOGS_RC" = 0 ] || record_failure 'workload output capture failed'
          # Revalidate actual identity and stopped state immediately before rm.
          docker_client before-remove 2 128 inspect "$CID"
          if [ "$CLIENT_RC" = 0 ] && helper check-stopped --file before-remove.stdout >"$CAPTURE/remove-authority.json"; then
            docker_client remove 3 128 rm "$CID"
            RM_RC=$CLIENT_RC
          fi
        fi
      fi
      docker_client absence 2 128 container ls --all --no-trunc --filter "id=$CID" --format '{{json .ID}}'
    else
      docker_client absence 2 128 container ls --all --no-trunc --filter "name=^/$NAME$" --format '{{json .ID}}'
    fi
    QUERY_RC=$CLIENT_RC
  fi
  helper cleanup-facts --facts "{\"stop_rc\":$STOP_RC,\"rm_rc\":$RM_RC,\"query_rc\":$QUERY_RC,\"logs_rc\":$LOGS_RC}" >"$CAPTURE/cleanup-facts.json"
  if [ -n "$JOURNAL_PID" ]; then
    helper journal-begin --name final >"$CAPTURE/journal-final-begin-result.json"
    # Inclusive seek must replay the retained anchor. A successful query alone
    # does not establish cursor retention. Python checks the actual first row.
    run_client journal-final 1 8192 "$REPO_DIR/scripts/diagnostic.sh" _journal-query "$ATTEMPT/journal-final.source-stderr" "${JOURNAL_ARGS[@]}" --no-tail "--cursor=$JOURNAL_CURSOR"
    JOURNAL_QUERY_RC=$CLIENT_RC
    close_journal
  fi
  if [ -n "$OBSERVER_PID" ]; then
    local observer_wait_ticks=120
    [ "$JOURNAL_BACKEND" != journal ] || observer_wait_ticks=240
    for ((i=0; i<observer_wait_ticks; i++)); do
      kill -0 "$OBSERVER_PID" 2>/dev/null || break
      sleep 0.025
    done
    if kill -0 "$OBSERVER_PID" 2>/dev/null; then
      OBSERVER_FORCED=true
      kill -TERM "$OBSERVER_PID" 2>/dev/null
      for ((i=0; i<10; i++)); do
        kill -0 "$OBSERVER_PID" 2>/dev/null || break
        sleep 0.025
      done
      kill -KILL "$OBSERVER_PID" 2>/dev/null || :
      for ((i=0; i<10; i++)); do
        kill -0 "$OBSERVER_PID" 2>/dev/null || break
        sleep 0.025
      done
    fi
    if ! kill -0 "$OBSERVER_PID" 2>/dev/null; then
      if wait "$OBSERVER_PID"; then OBSERVER_RC=0; else OBSERVER_RC=$?; fi
      OBSERVER_WAITED=true
    fi
    OBSERVER_PID=
  fi
  helper closed --facts "{\"waited\":$OBSERVER_WAITED,\"exit_code\":$OBSERVER_RC,\"forced\":$OBSERVER_FORCED,\"clients_closed\":$CLIENTS_CLOSED}" >"$CAPTURE/closure.json"
  if ! helper finalize --json; then
    # No clean inference when publication itself fails. Preserve original files.
    printf '%s\n' '{"schema_version":1,"ok":false,"result":{"outcome":"cleanup_unconfirmed","reason":"terminal publication failed; inspect retained attempt evidence"}}' | tee "$CAPTURE/emergency-result.json"
  fi
  trap - EXIT
}
trap reconcile EXIT
trap 'FAILURE=${FAILURE:-unexpected owner/helper failure}' ERR

if [ "$CLEANUP_ONLY" = true ]; then
  PULSAR_DIAGNOSTIC_CLEANUP_NONCE=$(helper cleanup-owner --raw)
  export PULSAR_DIAGNOSTIC_CLEANUP_NONCE
  CAPTURE="$ATTEMPT/cleanup-$PULSAR_DIAGNOSTIC_CLEANUP_NONCE"
  OWNED=1
  NAME=$(field claim.json intended_name)
  CID=$(field lifecycle.json container_id)
  [ "$CID" != null ] || CID=
  reconcile; exit 0
fi
if ! helper prepare --plan "$PLAN" >"$ATTEMPT/prepared.json"; then
  echo '{"schema_version":1,"ok":false,"result":{"outcome":"preflight_failed","reason":"attempt admission/sealing failed"}}'
  exit 2
fi
OWNED=1
export PULSAR_DIAGNOSTIC_NONCE
PULSAR_DIAGNOSTIC_NONCE=$(field claim.json attempt_nonce)
NAME=$(field claim.json intended_name)
# Admission uses the existing operation budget, preserving the full workload
# allowance and cleanup reserve instead of imposing a separate short timeout.
admission_seconds=$(( $(field plan.json definition.observer.operation_seconds) - $(field plan.json definition.observer.workload_deadline_seconds) - $(field plan.json definition.observer.cleanup_reserve_seconds) - SECONDS ))
[ "$admission_seconds" -gt 0 ] || { FAILURE='operation budget exhausted before admission'; reconcile; exit 0; }
run_client admission "$admission_seconds" 128 "$REPO_DIR/scripts/diagnostic.sh" _admission "$ATTEMPT"
[ "$CLIENT_RC" = 0 ] || { FAILURE='fresh all-rank readiness/trust/doctor/idle failed'; reconcile; exit 0; }
mapfile -t local_identity <"$ATTEMPT/admission.stdout"
image_ref=$(field plan.json definition.image_reference)
docker_client image 2 128 image inspect "$image_ref"
[ "$CLIENT_RC" = 0 ] || { FAILURE='loaded immutable image is unavailable'; reconcile; exit 0; }
helper admit --local-node "${local_identity[0]:-}" --local-ssh "${local_identity[1]:-}" >"$ATTEMPT/admitted.json"
JOURNAL_BACKEND=$(field plan.json definition.observer.backend)
if [ "$JOURNAL_BACKEND" = journal ]; then
  journal_boot=$(field claim.json boot_id)
  JOURNAL_ARGS=(--system --dmesg "--boot=$journal_boot" --no-pager --all --output=json
    --output-fields=_BOOT_ID,_TRANSPORT,__CURSOR,__MONOTONIC_TIMESTAMP,__REALTIME_TIMESTAMP,MESSAGE)
  helper journal-begin --name anchor >"$ATTEMPT/journal-anchor-begin-result.json"
  run_client journal-anchor 1 128 "$REPO_DIR/scripts/diagnostic.sh" _journal-query "$ATTEMPT/journal-anchor.source-stderr" "${JOURNAL_ARGS[@]}" --lines=1
  helper journal-anchor --rc "$CLIENT_RC" >"$ATTEMPT/journal-anchor-result.json"
  JOURNAL_CURSOR=$(field journal-anchor.json record.cursor)
  (ulimit -Sf 8192; exec journalctl "${JOURNAL_ARGS[@]}" --follow --no-tail "--cursor=$JOURNAL_CURSOR") \
    9>&- >"$ATTEMPT/journal-follow.stdout" 2>"$ATTEMPT/journal-follow.stderr" &
  JOURNAL_PID=$!
  journal_identity
  helper journal-started --pid "$JOURNAL_PID" >"$ATTEMPT/journal-started.json"
  [ "$JOURNAL_START" = "$(field journal-client.json starttime)" ]
fi
python3 -I -B "$REPO_DIR/scripts/diagnostic_kmsg.py" --attempt-dir "$ATTEMPT" \
  9>&- >"$ATTEMPT/observer.stdout" 2>"$ATTEMPT/observer.stderr" &
OBSERVER_PID=$!
helper observer-started --pid "$OBSERVER_PID" >"$ATTEMPT/observer-started.json"
ready=0
for ((i=0; i<40; i++)); do
  [ "$CANCEL" = 0 ] || break
  if helper ready >"$ATTEMPT/ready.json"; then ready=1; break; fi
  kill -0 "$OBSERVER_PID" 2>/dev/null || break
  sleep 0.025
done
[ "$ready" = 1 ] || { FAILURE='fresh safe observer readiness not established'; reconcile; exit 0; }
GUARD=1
helper create-claim --raw >"$ATTEMPT/create.argv"
mapfile -d '' -t create_argv <"$ATTEMPT/create.argv"
docker_client create 3 128 "${create_argv[@]}"
[ "$CLIENT_RC" = 0 ] || { FAILURE='create reply failed or uncertain'; reconcile; exit 0; }
# Never trust the returned name/CID alone: inspect the unique intended binding.
docker_client created 2 128 inspect "$NAME"
[ "$CLIENT_RC" = 0 ] || { FAILURE='created identity query failed'; reconcile; exit 0; }
helper created --file created.stdout >"$ATTEMPT/created.json"
CID=$(field lifecycle.json container_id)
ack
helper controls --file created.stdout >"$ATTEMPT/controls.json"
helper consume-start >"$ATTEMPT/start-consumed.json"
ack
docker_client start 3 128 start "$CID"
[ "$CLIENT_RC" = 0 ] || { FAILURE='start reply failed or uncertain'; reconcile; exit 0; }
while [ "$CANCEL" = 0 ]; do
  helper guard >"$CAPTURE/guard.json"
  docker_client live 1 128 inspect "$CID"
  [ "$CLIENT_RC" = 0 ] || { FAILURE='live inspect failed'; break; }
  helper identity --file live.stdout >"$CAPTURE/identity.json"
  if helper check-stopped --file live.stdout >"$CAPTURE/stopped-check.json"; then break; fi
  helper running --file live.stdout >"$ATTEMPT/running.json"
  ack
  sleep 0.025
done
reconcile; exit 0
