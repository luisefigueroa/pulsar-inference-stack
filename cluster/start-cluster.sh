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
  echo 'usage: start-cluster.sh SPEC_ID [--spec-file FILE] [--override-file FILE] [--memory-estimate-file FILE] [--memory-estimate-id ID] [--replace] [--dry-run] [--skip-preflight] [--skip-warmup]'
  echo 'The spec fixes geometry and recipe; every selected node must be verified.'
  exit 0 ;;
esac
MODEL_NAME="${1:?usage: cluster/start-cluster.sh SPEC_ID [options]}"
shift
unset PULSAR_MEMORY_ESTIMATE_JSON
MEMORY_ESTIMATE_FILE="" MEMORY_ESTIMATE_FROZEN="" MEMORY_ESTIMATE_ID=""
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
require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" \
  || exit 1

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
  cluster/preflight.sh "$MODEL_NAME" || {
    error_line "preflight failed — not starting (--skip-preflight overrides at your own risk)"
    exit 1
  }
fi

CONTAINER="$(container_name_for "$MODEL_NAME" "$NODES")"
MASTER_ADDR="${CLUSTER_NODE_CONTROL_IPS[0]}"
PLAN_FILE=$(mktemp "${TMPDIR:-/tmp}/pulsar-launch-plan.XXXXXX")
# shellcheck disable=SC2064
trap 'rm -f "${PLAN_FILE:-}"' EXIT
LAUNCH_ACTION=start
[ "$REPLACE" != 1 ] || LAUNCH_ACTION=replace
write_launch_plan_file "$PLAN_FILE" "$([ "$DRY_RUN" = 1 ] && echo dry-run || echo "$LAUNCH_ACTION")"

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
    echo "  rank $rank · ssh ${CLUSTER_NODE_SSH_HOSTS[$rank]} · ${CLUSTER_NODE_HOSTNAMES[$rank]}"
    echo "    ${REMOTE_REDACTED[$rank]}"
  done
  echo "  rank 0 · local · ${CLUSTER_NODE_HOSTNAMES[0]}"
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
  error_line "$why — removing launch-tracked IDs only"
  if [ -n "${TRACKED_CIDS[0]:-}" ]; then
    log "abort: remove rank 0 id=${TRACKED_CIDS[0]:0:12}" >&2
    remove_container_id_local "${TRACKED_CIDS[0]}"
  fi
  for ((rank = NODES - 1; rank >= 1; rank--)); do
    [ -n "${TRACKED_CIDS[$rank]:-}" ] || continue
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    log "abort: remove rank $rank id=${TRACKED_CIDS[$rank]:0:12} on $host" >&2
    remove_container_id_remote "$host" "${TRACKED_CIDS[$rank]}"
  done
}

existing=0
existing_nodes=()
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  probe_rc=0
  container_ownership_inspect_remote "$host" "$CONTAINER" >/dev/null || probe_rc=$?
  case "$probe_rc" in 0) existing=1; existing_nodes+=("$(human_node_name "$rank")") ;; 3) ;; *) die "cannot inspect existing service on $(human_node_name "$rank") (rank $rank); refusing launch" ;; esac
done
probe_rc=0
container_ownership_inspect_local "$CONTAINER" >/dev/null || probe_rc=$?
case "$probe_rc" in 0) existing=1; existing_nodes=("$(human_node_name 0)" "${existing_nodes[@]}") ;; 3) ;; *) die "cannot inspect existing service on $(human_node_name 0) (rank 0); refusing launch" ;; esac
if [ "$existing" = 1 ] && [ "$REPLACE" != 1 ]; then
  where=$(printf '%s, ' "${existing_nodes[@]}"); where="${where%, }"
  START_BLOCKER_SPEC="$MODEL_NAME" start_blocker service_exists --detail "container $CONTAINER on $where"
  die "service $CONTAINER already exists on $where; inspect every rank, then pass --replace only with explicit replacement approval"
fi
if [ "$REPLACE" = 1 ]; then
  log "removing existing stack-managed ranks (ownership required)"
  stale_rc=0
  remove_stack_owned_cluster "$MODEL_NAME" "$CONTAINER" "$NODES" || stale_rc=$?
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
for ((rank = 1; rank < NODES; rank++)); do
  build_docker_cmd "$rank"
  REMOTE_COMMANDS["$rank"]="$(shell_join_q "${_DOCKER_CMD[@]}")"
done
build_docker_cmd 0
HEAD_CMD=("${_DOCKER_CMD[@]}")
for cluster_port in "$PORT" "${MASTER_PORT:-29500}"; do
  if ! port_free "$cluster_port"; then
    START_BLOCKER_SPEC="$MODEL_NAME" start_blocker port_in_use --node "$(human_node_name 0)" \
      --node-id "${CLUSTER_NODE_IDS[0]:-}" --detail "port $cluster_port"
    refuse_launch "port $cluster_port is unavailable on $(human_node_name 0) (rank 0); refusing launch"
  fi
done

STARTUP_STARTED_NS=$(date +%s%N)
persist_launch_plan_file "$PLAN_FILE"
STARTUP_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  log "starting rank $rank on $host"
  raw_id=""
  if ! raw_id=$("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
      "${REMOTE_COMMANDS[$rank]}"); then
    cluster_abort "rank $rank docker run failed"
    exit 1
  fi
  if ! TRACKED_CIDS["$rank"]=$(parse_docker_run_container_id "$raw_id"); then
    unset "TRACKED_CIDS[$rank]"
    error_line "rank $rank docker run returned an invalid ID"
    report_untracked_launch_container remote "$MODEL_NAME" "$rank" "$CONTAINER" "$host"
    cluster_abort "rank $rank docker run ID invalid"
    exit 1
  fi
  log "rank $rank id=${TRACKED_CIDS[$rank]:0:12}"
done

log "starting rank 0 locally"
HEAD_RUN=("${HEAD_CMD[@]}")
HEAD_RUN[0]="$PULSAR_DOCKER"
head_raw=""
if ! head_raw=$("${HEAD_RUN[@]}"); then
  cluster_abort "rank 0 docker run failed"
  exit 1
fi
if ! TRACKED_CIDS[0]=$(parse_docker_run_container_id "$head_raw"); then
  unset 'TRACKED_CIDS[0]'
  error_line "rank 0 docker run returned an invalid ID"
  report_untracked_launch_container head "$MODEL_NAME" 0 "$CONTAINER"
  cluster_abort "rank 0 docker run ID invalid"
  exit 1
fi
log "rank 0 id=${TRACKED_CIDS[0]:0:12}"
# Every rank's container exists; its references protect its files from here.
release_model_library_locks

log "waiting for http://127.0.0.1:${PORT}/health (cold load can take ~10 min)"
API_AUTH_ARGS=()
api_auth_curl_args API_AUTH_ARGS
for _attempt in $(seq 1 "${WAIT_ATTEMPTS:-120}"); do
  if curl -fsS --max-time 3 "${API_AUTH_ARGS[@]}" \
      "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    STARTUP_HEALTHY_NS=$(date +%s%N)
    STARTUP_HEALTHY_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    STARTUP_ELAPSED=$(
      python3 -c \
        'import sys; print(f"{(int(sys.argv[2])-int(sys.argv[1]))/1e9:.3f}")' \
        "$STARTUP_STARTED_NS" "$STARTUP_HEALTHY_NS"
    )
    log "healthy · first-health=${STARTUP_ELAPSED}s."
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
        exit 1
        ;;
    esac
    rm -f "$verify_err"
    exit 0
  fi

  for ((rank = 0; rank < NODES; rank++)); do
    rank_host=""
    [ "$rank" = 0 ] || rank_host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    case "$(container_state_exact "$CONTAINER" "$rank_host")" in
      exited)
        error_line "the rank $rank container on $(human_node_name "$rank") exited before the service became healthy; last logs:"
        if [ "$rank" = 0 ]; then
          "$PULSAR_DOCKER" logs --tail 80 "$CONTAINER" >&2 || true
        else
          "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$rank_host" \
            "docker logs --tail 80 $(printf '%q' "$CONTAINER")" >&2 || true
        fi
        cluster_abort "rank $rank exited during health wait"
        START_BLOCKER_SPEC="$MODEL_NAME" start_blocker container_exited --node "$(human_node_name "$rank")" \
          --node-id "${CLUSTER_NODE_IDS[$rank]:-}" --rank "$rank" --no-command \
          --note "The cluster's containers were removed; the logs are shown above."
        exit 1
        ;;
      absent)
        error_line "the rank $rank container on $(human_node_name "$rank") was removed before the service became healthy"
        cluster_abort "rank $rank was removed during health wait"
        START_BLOCKER_SPEC="$MODEL_NAME" start_blocker service_stopped --node "$(human_node_name "$rank")" \
          --node-id "${CLUSTER_NODE_IDS[$rank]:-}" --rank "$rank"
        exit 1
        ;;
    esac
  done
  sleep "${WAIT_SECONDS:-10}"
done

error_line "timed out waiting for health. Rank 0 logs:"
"$PULSAR_DOCKER" logs --tail 120 "$CONTAINER" >&2 || true
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  log "rank $rank logs ($host):" >&2
  "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
    "docker logs --tail 120 $(printf '%q' "$CONTAINER")" >&2 || true
done
cluster_abort "health wait timed out"
START_BLOCKER_SPEC="$MODEL_NAME" start_blocker health_timeout --node "$(human_node_name 0)" \
  --node-id "${CLUSTER_NODE_IDS[0]:-}" --no-command \
  --note "The cluster's containers were removed; the logs are shown above."
exit 1
