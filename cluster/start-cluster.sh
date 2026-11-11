#!/usr/bin/env bash
# Start an exact N-node vLLM spec: remote headless ranks first,
# then local rank 0 with the API. Every active rank is one GB10.
#
#   cluster/start-cluster.sh SPEC_ID [--spec-decode|--no-spec-decode]
#                            [--skip-preflight] [--skip-warmup] [--dry-run]
#
# Backend: vLLM native --nnodes/--node-rank with the mp executor over RoCE.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
SCRIPT_NAME=cluster
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"

case "${1:-}" in -h|--help)
  echo 'usage: start-cluster.sh SPEC_ID [--spec-file FILE] [--override-file FILE] [--memory-estimate-file FILE] [--memory-estimate-id ID] [--placement-nodes LIST] [--launch-plan-id ID] [--replace] [--dry-run] [--skip-preflight] [--skip-warmup]'
  echo 'The spec fixes geometry and recipe; every selected node must be verified.'
  echo 'LIST names the nodes in role order, as scripts/up.sh resolved them; ID is the'
  echo 'launch plan up.sh checked: a launch whose own plan differs starts nothing.'
  exit 0 ;;
esac
MODEL_NAME="${1:?usage: cluster/start-cluster.sh SPEC_ID [options]}"
shift
unset PULSAR_MEMORY_ESTIMATE_JSON
MEMORY_ESTIMATE_FILE="" MEMORY_ESTIMATE_FROZEN="" MEMORY_ESTIMATE_ID=""
PLACEMENT_NODES="" EXPECTED_PLAN_ID=""
SPEC_MODE=auto
SKIP_PREFLIGHT=0
SKIP_WARMUP=0
DRY_RUN=0
REPLACE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --memory-estimate-file) [ "$#" -ge 2 ] && [ -n "$2" ] || die "--memory-estimate-file requires a file" 2; MEMORY_ESTIMATE_FILE="$2"; shift ;;
    --memory-estimate-id) [ "$#" -ge 2 ] && [ -n "$2" ] || die "--memory-estimate-id requires a digest" 2; MEMORY_ESTIMATE_ID="$2"; shift ;;
    --memory-estimate-frozen) [ "$#" -ge 2 ] && [ -n "$2" ] || die "internal memory estimate is missing" 2; MEMORY_ESTIMATE_FROZEN="$2"; shift ;;
    --accept-memory-warn) export PULSAR_ACCEPT_MEMORY_WARN=1 ;;
    --placement-nodes) [ "$#" -ge 2 ] && [ -n "$2" ] || die "--placement-nodes requires an ordered node list" 2; PLACEMENT_NODES="$2"; shift ;;
    --launch-plan-id) [ "$#" -ge 2 ] && [[ "$2" =~ ^[0-9a-f]{64}$ ]] || die "--launch-plan-id requires a launch plan ID" 2; EXPECTED_PLAN_ID="$2"; shift ;;
    --override-file) [ "$#" -ge 2 ] || die "--override-file requires a JSON file" 2; export PULSAR_OVERRIDE_FILE="$2"; shift ;;
    --spec-file) [ "$#" -ge 2 ] || die "--spec-file requires a file" 2; export PULSAR_SPEC_FILE="$2"; shift ;;
    --spec-decode) set_spec_decode_mode SPEC_MODE on ;;
    --no-spec-decode) set_spec_decode_mode SPEC_MODE off ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --skip-warmup) SKIP_WARMUP=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --replace) REPLACE=1 ;;
    --force) refuse_removed_force_flag ;;
    --weight-source|--weight-mode)
      refuse_removed_weight_mode_flag
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# Ctrl-C or a stop signal says what this launch left and removes nothing;
# stop removes a service after probing every rank. scripts/up.sh leaves the
# report to this script while it runs.
CLUSTER_PHASE=preparing LAUNCHING_RANK=0 CLUSTER_INTERRUPTED=0
cluster_interrupted() {
  local detail=""
  local -a where=() noted=()
  # Read by the EXIT trap: an interruption reports and removes nothing.
  CLUSTER_INTERRUPTED=1
  trap - INT TERM HUP
  load_progress_end
  case "$CLUSTER_PHASE" in
    preparing)
      if [ "${LAUNCH_AFTER_REPLACE:-0}" = 1 ]; then
        printf 'Interrupted before launch; nothing was started. %s\n' "$AFTER_REPLACE_NOTE" >&2 || true
      else
        printf 'Interrupted before launch; nothing was started.\n' >&2 || true
      fi ;;
    launching)
      where=(--node "$(role_name "$LAUNCHING_RANK")" --node-id "$(role_id "$LAUNCHING_RANK")"
             --rank "$LAUNCHING_RANK")
      detail="while this rank was starting; the cluster is incomplete and cannot serve" ;;
    loading)
      where=(--node "$(role_name 0)" --node-id "$(role_id 0)")
      detail="while the service was loading; it keeps loading on all $NODES ranks" ;;
    verifying)
      where=(--node "$(role_name 0)" --node-id "$(role_id 0)")
      detail="after the service became healthy, while every rank was being verified; it is running on all $NODES ranks" ;;
    replacing)
      detail="while --replace was removing the previous service; some of its containers may remain"
      noted=(--note "Nothing was started. To remove what remains of the previous service: {stop}") ;;
    removing)
      detail="while the failed launch's containers were being removed; some may remain"
      noted=(--note "Some may already be removed. To remove the rest: {stop}") ;;
  esac
  if [ -n "$detail" ]; then
    START_BLOCKER_SPEC="$MODEL_NAME" start_blocker start_interrupted ${where[@]+"${where[@]}"} \
      --service-id "${SERVICE_ID:-}" --detail "$detail" ${noted[@]+"${noted[@]}"} || true
  fi
  exit $((128 + $1))
}
trap 'cluster_interrupted 2' INT
trap 'cluster_interrupted 15' TERM
trap 'cluster_interrupted 1' HUP

acquire_model_library_lifecycle_lock shared
load_conf "$MODEL_NAME"
if [ -n "$MEMORY_ESTIMATE_FILE$MEMORY_ESTIMATE_FROZEN$MEMORY_ESTIMATE_ID" ]; then
  select_memory_estimate "$MEMORY_ESTIMATE_FILE" "$MEMORY_ESTIMATE_FROZEN" "$MEMORY_ESTIMATE_ID"
fi
require_spec_launch_admission "$MODEL_NAME"
acquire_model_library_hot_lock shared
[ "$(model_source_kind)" = hf ] \
  || die "non-HF model specs are not servable (ADR 0006)"
resolve_spec_decode "$SPEC_MODE"
SPEC_DECODE_STATE=$([ "$SPEC_DECODE_ENABLED" = 1 ] && printf on || printf off)
if [ "$NODES" -le 1 ]; then
  echo "spec ${MODEL_NAME:0:12} is a single-node spec; use ./pulsar start" >&2
  exit 1
fi
# One role-to-node map for the whole launch: role rank r runs on the node at
# saved position SERVING_NODE_INDEXES[r], the same resolution scripts/up.sh
# made from the same --placement-nodes (the default order without it).
resolve_serving_placement "" "$PLACEMENT_NODES" || die "confirmed serving placement is unavailable"
# Suggested start commands in records repeat the placement (start_blocker).
PLACEMENT_ARGS=()
[ -z "$PLACEMENT_NODES" ] || PLACEMENT_ARGS=(--placement-nodes "$PLACEMENT_NODES")
# role_index R, role_host R, role_name R, role_id R — the saved position of
# role rank R's node, its SSH host (empty when it is this machine, saved
# position 0, which local Docker serves), its hostname and its node ID.
role_index() { printf '%s' "${SERVING_NODE_INDEXES[$1]}"; }
role_host() { [ "${SERVING_NODE_INDEXES[$1]}" = 0 ] || printf '%s' "${CLUSTER_NODE_SSH_HOSTS[${SERVING_NODE_INDEXES[$1]}]}"; }
role_name() { human_node_name "${SERVING_NODE_INDEXES[$1]}"; }
role_id() { printf '%s' "${CLUSTER_NODE_IDS[${SERVING_NODE_INDEXES[$1]}]:-}"; }
# --replace removes the existing service by saved position, so it acts only on
# the default order; elsewhere the service is stopped first, then started.
for ((rank = 0; rank < NODES; rank++)); do
  [ "$REPLACE" != 1 ] || [ "${SERVING_NODE_INDEXES[$rank]}" = "$rank" ] \
    || die "--replace replaces a service on the default node order only; stop this spec's service first, then start it" 2
done
# docker_for_role R ARGS... — one docker command on role rank R's node.
docker_for_role() {
  local role="$1" host
  shift
  host=$(role_host "$role")
  if [ -z "$host" ]; then
    "$PULSAR_DOCKER" "$@"
  else
    "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$(shell_join_q docker "$@")"
  fi
}
require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" \
  || exit 1

# require_expected_plan — the plan this launch built must be the one start
# checked; otherwise nothing is started.
require_expected_plan() {
  local actual
  [ -n "$EXPECTED_PLAN_ID" ] || return 0
  actual=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$PLAN_FILE") \
    || die "the launch plan cannot be read; nothing was started"
  [ "$actual" = "$EXPECTED_PLAN_ID" ] \
    || die "the launch plan differs from the one start checked (${actual:0:12}, not ${EXPECTED_PLAN_ID:0:12}): its nodes or files changed since. Nothing was started. Next: rerun ./pulsar start"
}

LIBRARY_VIEW_HUB_PATH=""
LIBRARY_VIEW_CONTENT_DIGEST=""
LIBRARY_VIEW_TRANSPORT=""
LIBRARY_VIEW_INTEGRITY_SCHEME=""
resolve_library_hot_for_profile "$MODEL_NAME"
WEIGHT_OWNER_ID="${LIBRARY_VIEW_HOME_NODE_ID}"
WEIGHT_CONFIG_ID="${LIBRARY_VIEW_CONTENT_ID}"
runtime_model="$LIBRARY_VIEW_CONTAINER_MODEL_PATH"
log "exact spec: ${MODEL_NAME:0:12} · $NODES nodes · topology ${CLUSTER_TOPOLOGY_ID:0:12}"
log "model files: prepared copies · home=${WEIGHT_OWNER_ID:0:12} · identity=$LIBRARY_VIEW_IDENTITY_STATUS · revision=${LIBRARY_VIEW_REVISION:0:12}"
log "recipe is fixed by the selected spec"
if [ "$SKIP_PREFLIGHT" = 0 ]; then
  cluster/preflight.sh "$MODEL_NAME" ${PLACEMENT_NODES:+--placement-nodes "$PLACEMENT_NODES"} || {
    error_line "preflight failed — not starting (--skip-preflight overrides at your own risk)"
    exit 1
  }
fi

CONTAINER="$(container_name_for "$MODEL_NAME" "$NODES")"
MASTER_ADDR="${CLUSTER_NODE_CONTROL_IPS[$(role_index 0)]}"
PLAN_FILE=$(mktemp "${TMPDIR:-/tmp}/pulsar-launch-plan.XXXXXX")
# From the first docker run to the end of the health wait, any exit that is
# not an interruption (cluster_interrupted ran: it reports and removes
# nothing), whatever its status, and that did not
# already remove what this launch started removes it now: no rank is left
# running with nothing said.
CLUSTER_ABORTED=0
# shellcheck disable=SC2317 # run by the EXIT trap
launch_exit() {
  local rc=$?
  rm -f "${PLAN_FILE:-}"
  if [ "$rc" -ne 0 ] && [ "$CLUSTER_INTERRUPTED" = 0 ] && [ "$CLUSTER_ABORTED" = 0 ]; then
    case "$CLUSTER_PHASE" in
      launching|loading)
        cluster_abort "the launch stopped unexpectedly (exit $rc)"
        failed_launch_record "$(launch_containers_left)" ;;
    esac
  fi
  return "$rc"
}
trap launch_exit EXIT
LAUNCH_ACTION=start
[ "$REPLACE" != 1 ] || LAUNCH_ACTION=replace
write_launch_plan_file "$PLAN_FILE" "$([ "$DRY_RUN" = 1 ] && echo dry-run || echo "$LAUNCH_ACTION")"
[ "$DRY_RUN" = 1 ] || require_expected_plan

# Load docker argv for one rank from the shared launch plan. _DOCKER_CMD is
# the output array. Bare docker is serialized to remote ranks; local rank 0
# replaces argv[0] with PULSAR_DOCKER immediately before execution.
build_docker_cmd() {
  local role_rank="${1:?rank required}"
  shift
  load_docker_argv_from_plan "$PLAN_FILE" "$role_rank" _DOCKER_CMD 1
}

shell_join_q() {
  local output="" value
  for value in "$@"; do
    output+="$(printf '%q' "$value") "
  done
  printf '%s' "${output% }"
}

declare -A REMOTE_COMMANDS=()
declare -A REMOTE_REDACTED=()
for ((rank = 1; rank < NODES; rank++)); do
  build_docker_cmd "$rank" --node-rank "$rank" --headless
  REMOTE_COMMANDS["$rank"]="$(shell_join_q "${_DOCKER_CMD[@]}")"
  REMOTE_REDACTED["$rank"]="$(shell_join_q_redacted "${_DOCKER_CMD[@]}")"
done

build_docker_cmd 0 --node-rank 0
HEAD_CMD=("${_DOCKER_CMD[@]}")
_api_key="${VLLM_API_KEY:-${API_KEY:-}}"

if [ "$DRY_RUN" = 1 ]; then
  echo "RANKS"
  for ((rank = 1; rank < NODES; rank++)); do
    if [ -n "$(role_host "$rank")" ]; then
      echo "  rank $rank · ssh $(role_host "$rank") · ${CLUSTER_NODE_HOSTNAMES[$(role_index "$rank")]}"
    else
      echo "  rank $rank · local · ${CLUSTER_NODE_HOSTNAMES[$(role_index "$rank")]}"
    fi
    echo "    ${REMOTE_REDACTED[$rank]}"
  done
  if [ -n "$(role_host 0)" ]; then
    echo "  rank 0 · ssh $(role_host 0) · ${CLUSTER_NODE_HOSTNAMES[$(role_index 0)]}"
  else
    echo "  rank 0 · local · ${CLUSTER_NODE_HOSTNAMES[$(role_index 0)]}"
  fi
  echo "    $(shell_join_q_redacted "${HEAD_CMD[@]}")"
  if [ -n "$_api_key" ]; then
    log "API key auth enabled on rank 0 (secret redacted)"
  else
    log "API open on rank 0 (no VLLM_API_KEY) — trusted lab network only"
  fi
  exit 0
fi

require_launch_image_check

declare -A TRACKED_CIDS=()

# Best-effort teardown by immutable IDs created by this invocation only.
cluster_abort() {
  local why="${1:-cluster start failed}" rank host
  CLUSTER_PHASE=removing CLUSTER_ABORTED=1
  error_line "$why — removing launch-tracked IDs only"
  # Rank 0 first, then the others; each on its own node, by the ID it returned.
  for rank in 0 $(seq $((NODES - 1)) -1 1); do
    [ -n "${TRACKED_CIDS[$rank]:-}" ] || continue
    host=$(role_host "$rank")
    if [ -z "$host" ]; then
      log "abort: remove rank $rank id=${TRACKED_CIDS[$rank]:0:12}" >&2
      remove_container_id_local "${TRACKED_CIDS[$rank]}"
    else
      log "abort: remove rank $rank id=${TRACKED_CIDS[$rank]:0:12} on $host" >&2
      remove_container_id_remote "$host" "${TRACKED_CIDS[$rank]}"
    fi
  done
}

# cluster_failure CODE RANK [start_blocker options...]
# Records a launch failure after cluster_abort. Removal failures are not
# reported by cluster_abort, so each rank's container is looked up by name: the
# record keeps its "removed" note only when every one is confirmed gone, and
# otherwise names the nodes left and suggests stop. An unobservable node is left.
cluster_failure() {
  local code="$1" rank="$2" left
  local -a cleanup=(--removed)
  shift 2
  left=$(launch_containers_left)
  [ -z "$left" ] || cleanup=(--unconfirmed "$left")
  START_BLOCKER_SPEC="$MODEL_NAME" start_blocker "$code" --node "$(role_name "$rank")" \
    --node-id "$(role_id "$rank")" --rank "$rank" --service-id "${SERVICE_ID:-}" "$@" "${cleanup[@]}"
  failed_launch_record "$left"
}

# launch_containers_left — the serving nodes, as "NAME (rank R)" joined by
# ", ", where this launch's container is not confirmed absent: still there,
# or a node that could not be observed. Empty when every one is gone.
launch_containers_left() {
  local other host left=""
  for ((other = 0; other < NODES; other++)); do
    host=$(role_host "$other")
    [ "$(container_state_exact "$CONTAINER" "$host")" = absent ] \
      || left+="${left:+, }$(role_name "$other") (rank $other)"
  done
  printf '%s' "$left"
}

# failed_launch_record LEFT — what a failed launch does with the service record
# it saved before its first docker run (SAVED_RECORD: "created" or "existed",
# SAVED_PLAN_ID). Every container of this start confirmed gone on every node
# the record names (LEFT empty): a record this start created is retired, and
# one line says so. A node not confirmed (LEFT names it) or a record that was
# there before this start keeps the record, and one line says why and names
# the stop that clears it. Nothing is said before the record was saved.
failed_launch_record() {
  local left="$1" outcome stop
  [ -n "${SAVED_RECORD:-}" ] && [ -n "${SAVED_PLAN_ID:-}" ] || return 0
  stop="./pulsar stop $(spec_reference "$MODEL_NAME")"
  if [ -n "$left" ]; then
    log "The service record this start saved is kept, because removing its containers could not be confirmed on $left. Next: $stop, which removes what remains and retires the record."
  elif [ "$SAVED_RECORD" != created ]; then
    log "The service record is kept, because it was saved before this start. Next: $stop, which retires a record whose containers are gone."
  elif outcome=$(retire_recorded_plan "$SAVED_PLAN_ID"); then
    log "No container this start launched is left on its nodes; $(retired_words "$outcome" "")."
  else
    log "The service record this start saved could not be retired. Next: $stop"
  fi
}

existing=0
existing_nodes=()
# The serving nodes only: the other ranks first, then rank 0's node.
for rank in $(seq 1 $((NODES - 1))) 0; do
  host=$(role_host "$rank")
  probe_rc=0
  if [ -n "$host" ]; then
    container_ownership_inspect_remote "$host" "$CONTAINER" >/dev/null || probe_rc=$?
  else
    container_ownership_inspect_local "$CONTAINER" >/dev/null || probe_rc=$?
  fi
  case "$probe_rc" in
    0) existing=1
       if [ "$rank" = 0 ]; then existing_nodes=("$(role_name 0)" ${existing_nodes[@]+"${existing_nodes[@]}"})
       else existing_nodes+=("$(role_name "$rank")"); fi ;;
    3) ;;
    *) die "cannot inspect existing service on $(role_name "$rank") (rank $rank); refusing launch" ;;
  esac
done
if [ "$existing" = 1 ] && [ "$REPLACE" != 1 ]; then
  where=$(printf '%s, ' "${existing_nodes[@]}"); where="${where%, }"
  START_BLOCKER_SPEC="$MODEL_NAME" start_blocker service_exists --detail "container $CONTAINER on $where"
  die "service $CONTAINER already exists on $where; inspect every rank, then pass --replace only with explicit replacement approval"
fi
# refuse_recorded_elsewhere HOSTS — one service_recorded_elsewhere blocker per
# line of HOSTS (another service of this spec), then the launch is refused.
refuse_recorded_elsewhere() {
  local hosts
  while IFS= read -r hosts; do
    [ -z "$hosts" ] || START_BLOCKER_SPEC="$MODEL_NAME" start_blocker service_recorded_elsewhere --detail "on $hosts"
  done <<<"$1"
  refuse_launch "a service of spec ${MODEL_NAME:0:12} is recorded on other nodes; a spec runs as one service, so nothing was started"
}
if [ "$REPLACE" = 1 ]; then
  # Saving this launch's record would refuse it if another service of the spec
  # is recorded: ask the records first, read-only, so nothing is removed for
  # a launch that cannot start. The save asks again.
  check_rc=0
  elsewhere=$(python3 "$REPO_DIR/scripts/service_state.py" check-save --state-root "$PULSAR_MODEL_LIBRARY_DIR" \
    --plan "$PLAN_FILE") || check_rc=$?
  if [ "$check_rc" = 3 ]; then
    refuse_recorded_elsewhere "$elsewhere"
  elif [ "$check_rc" != 0 ]; then
    die "the saved service records cannot be read; nothing was removed or started"
  fi
  log "removing existing stack-managed ranks (ownership required)"
  stale_rc=0
  # The service ID is the same for the service being replaced.
  SERVICE_ID=$(launch_plan_service_id "$PLAN_FILE")
  CLUSTER_PHASE=replacing
  remove_stack_owned_cluster "$MODEL_NAME" "$CONTAINER" "$NODES" || stale_rc=$?
  CLUSTER_PHASE=preparing
  if [ "$stale_rc" -eq 2 ]; then
    error_line "ownership not proven on every existing rank of $CONTAINER"
    log "No ambiguous rank was removed. Inspect labels or stop manually." >&2
    exit 1
  fi
  if [ "$stale_rc" -ne 0 ]; then
    error_line "failed while removing existing cluster ranks (rc=$stale_rc)"
    exit 1
  fi
  # shellcheck disable=SC2034 # read by start_blocker and refuse_launch in lib.sh
  [ "$existing" != 1 ] || LAUNCH_AFTER_REPLACE=1
fi

require_launch_memory_check
# Rebuild from immediately rechecked files.
write_launch_plan_file "$PLAN_FILE" "$LAUNCH_ACTION"
require_expected_plan
SERVICE_ID=$(launch_plan_service_id "$PLAN_FILE")
for ((rank = 1; rank < NODES; rank++)); do
  build_docker_cmd "$rank"
  REMOTE_COMMANDS["$rank"]="$(shell_join_q "${_DOCKER_CMD[@]}")"
done
build_docker_cmd 0
HEAD_CMD=("${_DOCKER_CMD[@]}")
first_host=$(role_host 0)
for cluster_port in "$PORT" "${MASTER_PORT:-29500}"; do
  if ! port_free "$cluster_port" ${first_host:+"$first_host"}; then
    START_BLOCKER_SPEC="$MODEL_NAME" start_blocker port_in_use --node "$(role_name 0)" \
      --node-id "$(role_id 0)" --detail "port $cluster_port"
    refuse_launch "port $cluster_port is unavailable on $(role_name 0) (rank 0); refusing launch"
  fi
done

STARTUP_STARTED_NS=$(date +%s%N)
# Saving the record decides one service per spec under the record's own lock:
# another service of this spec recorded meanwhile refuses the launch here,
# before any docker run.
save_rc=0
saved_file=$(mktemp "${TMPDIR:-/tmp}/pulsar-record-saved.XXXXXX")
elsewhere=$(persist_launch_plan_file "$PLAN_FILE" "$saved_file") || save_rc=$?
if [ "$save_rc" = 3 ]; then
  rm -f "$saved_file"
  refuse_recorded_elsewhere "$elsewhere"
elif [ "$save_rc" != 0 ]; then
  rm -f "$saved_file"
  die "the launch record could not be saved; nothing was started"
fi
# Whether this save created the service record, which a failed launch then
# retires once its containers are confirmed gone (failed_launch_record).
SAVED_RECORD=$(cat "$saved_file" 2>/dev/null || true)
rm -f "$saved_file"
SAVED_PLAN_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$PLAN_FILE" 2>/dev/null) \
  || SAVED_PLAN_ID=""
STARTUP_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
CLUSTER_PHASE=launching
# launch_role R — docker run of role rank R on its node: local Docker when the
# node is this machine, the node's SSH otherwise. Rank 0's command carries the
# API key when the service requires one, as a one-node start on a remote node
# does; no command is printed. A failure removes what this launch started.
launch_role() {
  local role="$1" host raw_id=""
  local -a run=()
  LAUNCHING_RANK=$role
  host=$(role_host "$role")
  # Every rank's command was built before the first docker run; a rank's
  # shell-quoted command reads back as its argv.
  if [ "$role" = 0 ]; then run=("${HEAD_CMD[@]}"); else eval "run=(${REMOTE_COMMANDS[$role]})"; fi
  if [ -z "$host" ]; then
    log "starting rank $role locally"
    run[0]="$PULSAR_DOCKER"
    raw_id=$("${run[@]}") || raw_id="//failed"
  else
    log "starting rank $role on $host"
    raw_id=$("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$(shell_join_q "${run[@]}")") || raw_id="//failed"
  fi
  if [ "$raw_id" = //failed ]; then
    cluster_abort "rank $role docker run failed"
    cluster_failure container_start_failed "$role" \
      --note "Docker's error is shown above. The cluster's containers were removed."
    exit 1
  fi
  if ! TRACKED_CIDS["$role"]=$(parse_docker_run_container_id "$raw_id"); then
    unset "TRACKED_CIDS[$role]"
    error_line "rank $role docker run returned an invalid ID"
    if [ -n "$host" ]; then
      report_untracked_launch_container remote "$MODEL_NAME" "$role" "$CONTAINER" "$host"
    else
      report_untracked_launch_container local "$MODEL_NAME" "$role" "$CONTAINER"
    fi
    cluster_abort "rank $role docker run ID invalid"
    cluster_failure container_start_failed "$role" --detail "docker run returned an invalid container ID" \
      --note "The cluster's containers were removed."
    exit 1
  fi
  log "rank $role id=${TRACKED_CIDS[$role]:0:12}"
}
# The other ranks first; rank 0, which serves the API, last.
for ((rank = 1; rank < NODES; rank++)); do
  launch_role "$rank"
done
launch_role 0
CLUSTER_PHASE=loading
# Every rank's container exists; its references protect its files from here.
release_model_library_locks

WAIT_LIMIT="${WAIT_ATTEMPTS:-120}" WAIT_STARTED=$SECONDS
# Health comes from the first node's own address: the loopback address only
# when that node is this machine, so another service listening here on the
# same port can never answer for it.
if [ -n "$(role_host 0)" ]; then
  HEALTH_BASE="http://$(url_host "${CLUSTER_NODE_CONTROL_IPS[$(role_index 0)]}"):${PORT}"
else
  HEALTH_BASE="http://127.0.0.1:${PORT}"
fi
log "waiting for ${HEALTH_BASE}/health (cold load can take ~10 min;" \
  "up to $WAIT_LIMIT health checks, ${WAIT_SECONDS:-10} s apart)"
API_AUTH_ARGS=()
api_auth_curl_args API_AUTH_ARGS
for _attempt in $(seq 1 "$WAIT_LIMIT"); do
  if curl -fsS --max-time 3 "${API_AUTH_ARGS[@]}" \
      "${HEALTH_BASE}/health" >/dev/null 2>&1; then
    load_progress_end
    STARTUP_HEALTHY_NS=$(date +%s%N)
    STARTUP_HEALTHY_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    STARTUP_ELAPSED=$(
      python3 -c \
        'import sys; print(f"{(int(sys.argv[2])-int(sys.argv[1]))/1e9:.3f}")' \
        "$STARTUP_STARTED_NS" "$STARTUP_HEALTHY_NS"
    )
    log "healthy · first-health=${STARTUP_ELAPSED}s."
    CLUSTER_PHASE=verifying
    # Qualification/warmup belongs to the workbench; serving checks health only.
    # A busy model library defers this verification; it never aborts a healthy
    # cluster. PULSAR_LOCK_BUSY_EXIT marks that case apart from a mismatch.
    verify_rc=0
    verify_err=$(mktemp "${TMPDIR:-/tmp}/pulsar-verify.XXXXXX")
    PULSAR_LOCK_BUSY_EXIT=75 PULSAR_MODEL_LIBRARY_LOCK_TIMEOUT_SECONDS="${PULSAR_POST_START_LOCK_WAIT_SECONDS:-120}" \
      "$REPO_DIR/scripts/observe-serving.sh" "$MODEL_NAME" --json >/dev/null 2>"$verify_err" || verify_rc=$?
    case "$verify_rc" in
      0) ;;
      75)
        warn "all-rank verification was deferred because the model library is busy; run ./pulsar status ${MODEL_NAME:0:12} once it is free"
        sed -nE 's/^(\[[^]]+\] )?error: /  /p' "$verify_err" >&2
        ;;
      *)
        cat "$verify_err" >&2
        rm -f "$verify_err"
        cluster_abort "all-rank verification failed after health"
        failed_launch_record "$(launch_containers_left)"
        exit 1
        ;;
    esac
    rm -f "$verify_err"
    exit 0
  fi

  RANK_STATES=()
  for ((rank = 0; rank < NODES; rank++)); do
    rank_host=$(role_host "$rank")
    RANK_STATES[rank]=$(container_state_exact "$CONTAINER" "$rank_host")
    case "${RANK_STATES[rank]}" in exited|absent) load_progress_end ;; esac
    case "${RANK_STATES[rank]}" in
      exited)
        error_line "the rank $rank container on $(role_name "$rank") exited before the service became healthy; last logs:"
        # Read once, before the clean-up removes the container, and shown as before.
        rank_log=$(mktemp "${TMPDIR:-/tmp}/pulsar-rank-log.XXXXXX")
        docker_for_role "$rank" logs --tail 80 "$CONTAINER" >"$rank_log" 2>&1 || true
        cat "$rank_log" >&2
        cluster_abort "rank $rank exited during health wait"
        cluster_failure container_exited "$rank" --no-command --note "The cluster's containers were removed; the logs are shown above."
        engine_memory_refusal "$(role_name "$rank") (rank $rank)" "$rank_log" || true
        rm -f "$rank_log"
        exit 1
        ;;
      absent)
        error_line "the rank $rank container on $(role_name "$rank") was removed before the service became healthy"
        cluster_abort "rank $rank was removed during health wait"
        cluster_failure service_stopped "$rank"
        exit 1
        ;;
    esac
  done
  load_progress "$WAIT_STARTED" "$_attempt" "$WAIT_LIMIT" "$(rank_states_text "${RANK_STATES[@]}")"
  sleep "${WAIT_SECONDS:-10}"
done

load_progress_end
error_line "timed out waiting for health. Rank 0 logs:"
# Each rank's log is read once, before the clean-up removes its container.
declare -a rank_logs=()
for ((rank = 0; rank < NODES; rank++)); do
  if [ "$rank" -gt 0 ]; then
    host=$(role_host "$rank")
    log "rank $rank logs (${host:-$(role_name "$rank")}):" >&2
  fi
  rank_logs[rank]=$(mktemp "${TMPDIR:-/tmp}/pulsar-rank-log.XXXXXX")
  docker_for_role "$rank" logs --tail 120 "$CONTAINER" >"${rank_logs[rank]}" 2>&1 || true
  cat "${rank_logs[rank]}" >&2
done
cluster_abort "health wait timed out"
cluster_failure health_timeout 0 --no-command --note "The cluster's containers were removed; the logs are shown above."
for ((rank = 0; rank < NODES; rank++)); do
  engine_memory_refusal "$(role_name "$rank") (rank $rank)" "${rank_logs[rank]}" && break
done
rm -f "${rank_logs[@]}"
exit 1
