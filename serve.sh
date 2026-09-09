#!/usr/bin/env bash
# Launch one exact catalog or candidate spec on a selected confirmed node.
# Usage: serve.sh SPEC_ID [-d] [--spec-file FILE] [--node NODE_ID] [--dry-run]
# Multi-node specs use cluster/start-cluster.sh. Recipe variants belong in
# frozen candidate specs; deployment-only settings belong in the overlay.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# shellcheck disable=SC1091
. "$REPO_DIR/scripts/lib.sh"

if [ "${1:-}" = "--list" ]; then
  for f in "${PULSAR_RELEASES_ROOT:-$REPO_DIR/releases}"/*.json; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .json)
    [[ "$name" =~ ^[0-9a-f]{64}$ ]] || continue
    (
      load_conf "$name"
      printf "%s nodes=%s %s\n" "$name" "$NODES" "$MODEL"
    )
  done
  exit 0
fi

case "${1:-}" in -h|--help)
  echo 'usage: serve.sh SPEC_ID [-d] [--spec-file FILE] [--node NODE_ID] [--port N] [--replace] [--dry-run]'
  echo 'Use --list to inspect catalog specs. Recipe changes require a new candidate.'
  exit 0 ;;
esac
MODEL_NAME="${1:?usage: serve.sh SPEC_ID [-d] [--spec-file FILE] [--node NODE_ID] [--dry-run]}"
shift

DETACH="" SPEC_MODE=auto DRY_RUN=0 PORT_OVERRIDE="" NODE_SELECTOR="" REPLACE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --accept-memory-warn) export PULSAR_ACCEPT_MEMORY_WARN=1 ;;
    --spec-file) [ "$#" -ge 2 ] || die "--spec-file requires a file" 2; export PULSAR_SPEC_FILE="$2"; shift ;;
    -d) DETACH="-d" ;;
    --spec-decode) set_spec_decode_mode SPEC_MODE on ;;
    --no-spec-decode) set_spec_decode_mode SPEC_MODE off ;;
    --dry-run) DRY_RUN=1 ;;
    --replace) REPLACE=1 ;;
    --force) refuse_removed_force_flag ;;
    --weight-source|--weight-mode)
      refuse_removed_weight_mode_flag
      ;;
    --node)
      [ "$#" -ge 2 ] || die "--node requires a topology node id or hostname" 2
      NODE_SELECTOR="$2"
      shift
      ;;
    --port)
      [ "$#" -ge 2 ] || die "--port requires a value (1-65535)" 2
      PORT_OVERRIDE="$2"
      shift
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

acquire_model_library_lifecycle_lock shared
load_conf "$MODEL_NAME"
require_spec_platform_admission "$MODEL_NAME"
acquire_model_library_hot_lock shared
if [ -n "$PORT_OVERRIDE" ]; then
  case "$PORT_OVERRIDE" in
    *[!0-9]*|"") die "invalid --port '$PORT_OVERRIDE' (expected 1-65535)" 2 ;;
  esac
  [ "$PORT_OVERRIDE" -ge 1 ] && [ "$PORT_OVERRIDE" -le 65535 ] \
    || die "invalid --port '$PORT_OVERRIDE' (expected 1-65535)" 2
  PORT="$PORT_OVERRIDE"
fi
resolve_spec_decode "$SPEC_MODE"
LAUNCH_CONTRACT_ID=$(loaded_launch_contract_id)
SPEC_DECODE_STATE=$([ "$SPEC_DECODE_ENABLED" = 1 ] && printf on || printf off)


if [ "$NODES" != "1" ]; then
  echo "$MODEL_NAME is a ${NODES}-node config. Use: cluster/start-cluster.sh $MODEL_NAME" >&2
  exit 1
fi
resolve_single_node_placement "$NODE_SELECTOR" \
  || die "cannot resolve physical node placement '$NODE_SELECTOR'"

[ "$(model_source_kind)" = hf ] \
  || die "non-HF model profiles are not servable (ADR 0006)"
LIBRARY_VIEW_HOME_NODE_ID=""
LIBRARY_VIEW_CONTENT_ID=""
# Prefer topology from placement when available
if [ -z "${CLUSTER_TOPOLOGY_ID:-}" ] && [ -n "${SINGLE_NODE_TOPOLOGY_ID:-}" ]; then
  CLUSTER_TOPOLOGY_ID="$SINGLE_NODE_TOPOLOGY_ID"
fi
load_cluster_topology >/dev/null 2>&1 && [ -n "${CLUSTER_TOPOLOGY_ID:-}" ] \
  || die "serving requires a confirmed topology manifest (one machine is fine): run scripts/detect-fabric.sh --write-topology"
resolve_library_hot_for_profile "$MODEL_NAME"
runtime_model="$LIBRARY_VIEW_CONTAINER_MODEL_PATH"
echo "library identity=$LIBRARY_VIEW_IDENTITY_STATUS revision=${LIBRARY_VIEW_REVISION:0:12} model_path=$runtime_model"

CONTAINER=$(container_name_for "$MODEL_NAME" 1)
PLAN_FILE=$(mktemp "${TMPDIR:-/tmp}/pulsar-launch-plan.XXXXXX")
# shellcheck disable=SC2064
trap 'rm -f "${PLAN_FILE:-}"' EXIT
write_launch_plan_file "$PLAN_FILE" "$([ "$DRY_RUN" = 1 ] && echo dry-run || echo start)"
CMD=()
load_docker_argv_from_plan "$PLAN_FILE" 0 CMD "$([ -n "$DETACH" ] && echo 1 || echo 0)"
_api_key="${VLLM_API_KEY:-${API_KEY:-}}"

if [ "$DRY_RUN" = "1" ]; then
  if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
    remote_redacted=$(shell_join_q_redacted "${CMD[@]}")
    print_shell_command_redacted "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- \
      "$SINGLE_NODE_SSH_HOST" "$remote_redacted"
  else
    CMD[0]="$PULSAR_DOCKER"
    print_shell_command_redacted "${CMD[@]}"
  fi
  exit 0
fi

require_launch_operational_checks
# Rebuild from immediately rechecked files before any replacement.
write_launch_plan_file "$PLAN_FILE" start
load_docker_argv_from_plan "$PLAN_FILE" 0 CMD "$([ -n "$DETACH" ] && echo 1 || echo 0)"

# Starting is non-replacing by default. Inspect the exact target first; only an
# explicit --replace reaches the ownership-proven removal transaction.
existing_rc=0
if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
  container_ownership_inspect_remote "$SINGLE_NODE_SSH_HOST" "$CONTAINER" >/dev/null \
    || existing_rc=$?
else
  container_ownership_inspect_local "$CONTAINER" >/dev/null || existing_rc=$?
fi
case "$existing_rc" in
  0)
    [ "$REPLACE" = 1 ] \
      || die "service $CONTAINER already exists on $(single_node_display); inspect it, then pass --replace only with explicit replacement approval"
    ;;
  3) ;;
  *) die "cannot determine whether $CONTAINER already exists on $(single_node_display); refusing launch" ;;
esac
if [ "$REPLACE" = 1 ]; then
  stale_rc=0
  remove_stack_owned_single_at_resolved_node "$MODEL_NAME" || stale_rc=$?
  if [ "$stale_rc" -eq 2 ]; then
    echo "[serve] ERROR: refusing to replace $CONTAINER on $(single_node_display) — ownership or physical node identity is not proven" >&2
    echo "[serve] Inspect labels (${PULSAR_MANAGED_LABEL}/${PULSAR_CONF_LABEL}/${PULSAR_RANK_LABEL}/${PULSAR_NODE_ID_LABEL}) or remove manually if you intend to clobber it." >&2
    exit 1
  fi
  if [ "$stale_rc" -ne 0 ]; then
    echo "[serve] ERROR: failed while removing prior container $CONTAINER on $(single_node_display) (rc=$stale_rc)" >&2
    exit 1
  fi
fi

port_probe='import socket,sys; s=socket.socket(); s.bind(("0.0.0.0",int(sys.argv[1]))); s.close()'
if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
  port_cmd="python3 -c $(printf '%q' "$port_probe") $(printf '%q' "$PORT")"
  if ! "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$SINGLE_NODE_SSH_HOST" \
      "$port_cmd" >/dev/null 2>&1; then
    die "port $PORT is unavailable on $(single_node_display); refusing launch"
  fi
elif ! python3 -c "$port_probe" "$PORT" >/dev/null 2>&1; then
  die "port $PORT is unavailable on $(single_node_display); refusing launch"
fi

echo "[serve] $MODEL_NAME ($MODEL) on $(single_node_display), port $PORT, image $IMAGE container=$CONTAINER"
echo "[serve] recipe is fixed by the selected spec"
[ -n "${NOTES:-}" ] && echo "[serve] notes: $NOTES"
if [ -n "$_api_key" ]; then
  echo "[serve] API key auth enabled (VLLM_API_KEY/API_KEY)"
else
  echo "[serve] API open (no VLLM_API_KEY) — lab network only"
fi
if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
  remote_cmd=$(shell_join_q "${CMD[@]}")
  exec "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- \
    "$SINGLE_NODE_SSH_HOST" "$remote_cmd"
fi
CMD[0]="$PULSAR_DOCKER"
exec "${CMD[@]}"
