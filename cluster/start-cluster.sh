#!/usr/bin/env bash
# Start an exact N-node vLLM spec: remote headless ranks first,
# then local rank 0 with the API. Every active rank is one GB10.
#
#   cluster/start-cluster.sh <model-name> [--spec-decode|--no-spec-decode]
#                            [--skip-preflight] [--skip-warmup] [--dry-run]
#
# Backend: vLLM native --nnodes/--node-rank with the mp executor over RoCE.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"

case "${1:-}" in -h|--help)
  echo 'usage: start-cluster.sh SPEC_ID [--spec-file FILE] [--override-file FILE] [--memory-estimate-file FILE] [--memory-estimate-id ID] [--replace] [--dry-run] [--skip-preflight] [--skip-warmup]'
  echo 'The spec fixes geometry and recipe; every selected node must be verified.'
  exit 0 ;;
esac
MODEL_NAME="${1:?usage: cluster/start-cluster.sh <model-name> [options]}"
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
require_spec_platform_admission "$MODEL_NAME"
acquire_model_library_hot_lock shared
[ "$(model_source_kind)" = hf ] \
  || die "non-HF model profiles are not servable (ADR 0006)"
resolve_spec_decode "$SPEC_MODE"
SPEC_DECODE_STATE=$([ "$SPEC_DECODE_ENABLED" = 1 ] && printf on || printf off)
if [ "$NODES" -le 1 ]; then
  echo "$MODEL_NAME is a single-node profile; use ./serve.sh" >&2
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
echo "[cluster] exact profile: $MODEL_NAME · $NODES ranks · topology ${CLUSTER_TOPOLOGY_ID:0:12}"
echo "[cluster] weights: model library · local hot staging · home=${WEIGHT_OWNER_ID:0:12} · identity=$LIBRARY_VIEW_IDENTITY_STATUS · revision=${LIBRARY_VIEW_REVISION:0:12}"
echo "[cluster] recipe is fixed by the selected spec"
if [ "$SKIP_PREFLIGHT" = 0 ]; then
  cluster/preflight.sh "$MODEL_NAME" || {
    echo "[cluster] preflight FAILED — not starting. (--skip-preflight to override at your own risk)" >&2
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
    echo "[cluster] API key auth enabled on rank 0 (secret redacted)"
  else
    echo "[cluster] API open on rank 0 (no VLLM_API_KEY) — trusted lab network only"
  fi
  exit 0
fi

require_launch_operational_checks
# Rebuild from immediately rechecked files before any replacement.
write_launch_plan_file "$PLAN_FILE" "$LAUNCH_ACTION"
for ((rank = 1; rank < NODES; rank++)); do
  build_docker_cmd "$rank"
  REMOTE_COMMANDS["$rank"]="$(shell_join_q "${_DOCKER_CMD[@]}")"
done
build_docker_cmd 0
HEAD_CMD=("${_DOCKER_CMD[@]}")

declare -A TRACKED_CIDS=()

# Best-effort teardown by immutable IDs created by this invocation only.
cluster_abort() {
  local why="${1:-cluster start failed}" rank host
  echo "[cluster] ABORT: $why — removing launch-tracked IDs only" >&2
  if [ -n "${TRACKED_CIDS[0]:-}" ]; then
    echo "[cluster] abort: remove rank 0 id=${TRACKED_CIDS[0]:0:12}" >&2
    remove_container_id_local "${TRACKED_CIDS[0]}"
  fi
  for ((rank = NODES - 1; rank >= 1; rank--)); do
    [ -n "${TRACKED_CIDS[$rank]:-}" ] || continue
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    echo "[cluster] abort: remove rank $rank id=${TRACKED_CIDS[$rank]:0:12} on $host" >&2
    remove_container_id_remote "$host" "${TRACKED_CIDS[$rank]}"
  done
}

existing=0
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  probe_rc=0
  container_ownership_inspect_remote "$host" "$CONTAINER" >/dev/null || probe_rc=$?
  case "$probe_rc" in 0) existing=1 ;; 3) ;; *) die "cannot inspect existing service on rank $rank; refusing launch" ;; esac
done
probe_rc=0
container_ownership_inspect_local "$CONTAINER" >/dev/null || probe_rc=$?
case "$probe_rc" in 0) existing=1 ;; 3) ;; *) die "cannot inspect existing service on rank 0; refusing launch" ;; esac
if [ "$existing" = 1 ] && [ "$REPLACE" != 1 ]; then
  die "service $CONTAINER already exists; inspect every rank, then pass --replace only with explicit replacement approval"
fi
if [ "$REPLACE" = 1 ]; then
  echo "[cluster] removing existing stack-managed ranks (ownership required)"
  stale_rc=0
  remove_stack_owned_cluster "$MODEL_NAME" "$CONTAINER" "$NODES" || stale_rc=$?
  if [ "$stale_rc" -eq 2 ]; then
    echo "[cluster] ERROR: ownership not proven on every existing rank of $CONTAINER" >&2
    echo "[cluster] No ambiguous rank was removed. Inspect labels or stop manually." >&2
    exit 1
  fi
  if [ "$stale_rc" -ne 0 ]; then
    echo "[cluster] ERROR: failed while removing existing cluster ranks (rc=$stale_rc)" >&2
    exit 1
  fi
fi

STARTUP_STARTED_NS=$(date +%s%N)
persist_launch_plan_file "$PLAN_FILE"
STARTUP_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  echo "[cluster] starting rank $rank on $host"
  raw_id=""
  if ! raw_id=$("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
      "${REMOTE_COMMANDS[$rank]}"); then
    cluster_abort "rank $rank docker run failed"
    exit 1
  fi
  if ! TRACKED_CIDS["$rank"]=$(parse_docker_run_container_id "$raw_id"); then
    unset "TRACKED_CIDS[$rank]"
    echo "[cluster] ERROR: rank $rank docker run returned an invalid ID" >&2
    report_untracked_launch_container remote "$MODEL_NAME" "$rank" "$CONTAINER" "$host"
    cluster_abort "rank $rank docker run ID invalid"
    exit 1
  fi
  echo "[cluster] rank $rank id=${TRACKED_CIDS[$rank]:0:12}"
done

echo "[cluster] starting rank 0 locally"
HEAD_RUN=("${HEAD_CMD[@]}")
HEAD_RUN[0]="$PULSAR_DOCKER"
head_raw=""
if ! head_raw=$("${HEAD_RUN[@]}"); then
  cluster_abort "rank 0 docker run failed"
  exit 1
fi
if ! TRACKED_CIDS[0]=$(parse_docker_run_container_id "$head_raw"); then
  unset 'TRACKED_CIDS[0]'
  echo "[cluster] ERROR: rank 0 docker run returned an invalid ID" >&2
  report_untracked_launch_container head "$MODEL_NAME" 0 "$CONTAINER"
  cluster_abort "rank 0 docker run ID invalid"
  exit 1
fi
echo "[cluster] rank 0 id=${TRACKED_CIDS[0]:0:12}"

echo "[cluster] waiting for http://127.0.0.1:${PORT}/health (cold load can take ~10 min)"
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
    echo "[cluster] healthy · first-health=${STARTUP_ELAPSED}s."
    # Qualification/warmup belongs to the workbench; serving checks health only.
    "$REPO_DIR/scripts/observe-serving.sh" "$MODEL_NAME" --json >/dev/null || {
      cluster_abort "all-rank verification failed after health"
      exit 1
    }
    exit 0
  fi

  if ! container_running_exact "$CONTAINER"; then
    echo "[cluster] rank 0 container died; last logs:" >&2
    "$PULSAR_DOCKER" logs --tail 80 "$CONTAINER" >&2 || true
    cluster_abort "rank 0 exited during health wait"
    exit 1
  fi
  for ((rank = 1; rank < NODES; rank++)); do
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    if ! container_running_exact_remote "$host" "$CONTAINER"; then
      echo "[cluster] rank $rank container died on $host; last logs:" >&2
      "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
        "docker logs --tail 80 $(printf '%q' "$CONTAINER")" >&2 || true
      cluster_abort "rank $rank exited during health wait"
      exit 1
    fi
  done
  sleep "${WAIT_SECONDS:-10}"
done

echo "[cluster] timed out. Rank 0 logs:" >&2
"$PULSAR_DOCKER" logs --tail 120 "$CONTAINER" >&2 || true
for ((rank = 1; rank < NODES; rank++)); do
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  echo "[cluster] rank $rank logs ($host):" >&2
  "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
    "docker logs --tail 120 $(printf '%q' "$CONTAINER")" >&2 || true
done
cluster_abort "health wait timed out"
exit 1
