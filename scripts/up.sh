#!/usr/bin/env bash
# Orchestrate checks then launch serve.sh or start-cluster.sh.
#   scripts/up.sh SPEC_ID [--spec-file FILE] [--node NODE_ID]
#                 [--dry-run] [--yes] [--verbose]
set -euo pipefail
SCRIPT_NAME=up
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

up_usage() {
  cat <<'HELP' | python3 -c 'import sys; from scripts.terminal_format import TerminalWriter; w=TerminalWriter(); [w.emit(line.rstrip(),subsequent_indent="    " if line.startswith("  ") else "") for line in sys.stdin]'
usage: scripts/up.sh SPEC_ID [options]

  --spec-file FILE       Use an explicit workbench candidate
  --override-file FILE   Explicit typed execution changes; report a modified recipe
  --dry-run             Check prerequisites without launching
  --verbose             Show full diagnostic output
  --node NODE_ID        Select a confirmed node for a one-node spec
  --accept-memory-warn  Explicitly accept a memory warning
  --pull-image          Permit staging the pinned image when missing
  --replace             Permit stopping an existing exact-name service
  --yes                 Confirm the requested start only; never implies the above
  --skip-preflight      Use when the cluster preflight was run separately

Model-file verification cannot be skipped. Overrides create a distinct effective
spec without changing the selected catalog entry or inheriting its measurements.
HELP
}
case "${1:-}" in -h|--help) up_usage; exit 0 ;; esac

NAME="${1:-}"
unset PULSAR_OVERRIDE_FILE PULSAR_EFFECTIVE_SPEC_ID
[ -n "$NAME" ] || die "usage: $0 <model-name> [options]"
shift

SPEC_MODE=auto SKIP_PF=0 SKIP_W=0 ACCEPT_MEM=0 PULL_IMG=0 REPLACE=0
DRY=0 VERBOSE=0 NODE_SELECTOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --override-file) [ "$#" -ge 2 ] || die "--override-file requires a JSON file" 2; export PULSAR_OVERRIDE_FILE="$2"; shift ;;
    --spec-file) [ "$#" -ge 2 ] || die "--spec-file requires a file" 2; export PULSAR_SPEC_FILE="$2"; shift ;;
    --spec-decode) set_spec_decode_mode SPEC_MODE on ;;
    --no-spec-decode) set_spec_decode_mode SPEC_MODE off ;;
    --force) refuse_removed_force_flag ;;
    --skip-preflight) SKIP_PF=1 ;;
    --skip-weights-check) die "model-file verification cannot be skipped" 2 ;;
    --accept-memory-warn) ACCEPT_MEM=1 ;;
    --pull-image) PULL_IMG=1 ;;
    --replace) REPLACE=1 ;;
    --weight-source|--weight-mode)
      refuse_removed_weight_mode_flag
      ;;
    --node)
      [ "$#" -ge 2 ] || die "--node requires a topology node id or hostname" 2
      NODE_SELECTOR="$2"
      shift
      ;;
    --dry-run) DRY=1 ;;
    --yes|-y) : ;;  # Compatibility acknowledgement; grants no extra action.
    --verbose|-v) VERBOSE=1 ;;
    -h|--help)
      up_usage
      exit 0
      ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

acquire_model_library_lifecycle_lock shared
load_conf "$NAME"
if [ "${CONF_SOURCE:-conf}" = spec ] && [ "$SPEC_MODE" != auto ]; then
  die "selected spec $NAME: --spec-decode/--no-spec-decode are refused (the identity is fixed)" 2
fi
require_spec_platform_admission "$NAME"
NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
acquire_model_library_hot_lock shared
PLACEMENT_ARGS=()
SERVICE_API_BASE="http://127.0.0.1:$PORT"
if [ "$NODES" -eq 1 ]; then
  resolve_single_node_placement "$NODE_SELECTOR" \
    || die "cannot resolve physical node placement '$NODE_SELECTOR'"
  PLACEMENT_SELECTOR="${SINGLE_NODE_ID:-$SINGLE_NODE_KEY}"
  PLACEMENT_ARGS=(--node "$PLACEMENT_SELECTOR")
  SERVICE_API_BASE=$(single_node_api_base_url "$PORT")
elif [ -n "$NODE_SELECTOR" ]; then
  die "--node is only valid for one-node profiles" 2
fi
resolve_spec_decode "$SPEC_MODE"
SPEC_REVIEW_CELL="${SPEC_REVIEW_STATUS:-not specified}"
export QUIET=1
[ "$VERBOSE" = 1 ] && export QUIET=0

echo "┌─ up  $NAME"
if [ "${CONF_SOURCE:-conf}" = spec ]; then
  echo "│  source=spec $CONF_NAME"
  echo "│  overlay=${OVERLAY_SOURCE:-?}"
fi
echo "│  nodes=$NODES  served=$SERVED_NAME  port=$PORT"
echo "│  weights=model library (hot staging)"
if [ "$NODES" -eq 1 ]; then
  echo "│  placement=$(single_node_display)  node-id=${SINGLE_NODE_ID:-standalone}"
fi
if [ "${PULSAR_EFFECTIVE_SPEC_ID:-$NAME}" != "$NAME" ]; then
  echo "│  Modified recipe: ${PULSAR_EFFECTIVE_SPEC_ID}"
  echo "│  Selected spec: $NAME; its measurements are reference only"
else
  echo "│  recipe=exact selected spec"
fi
echo "│  spec-review=$SPEC_REVIEW_CELL (display-only)"
[ "$DRY" = 1 ] && echo "│  mode=DRY-RUN (checks only)"
echo "├─ checks"

echo "INFO  spec-review $SPEC_REVIEW_CELL (display-only)"
echo "PASS  recipe    exact spec contract parsed"

if [ "$NODES" -gt 1 ]; then
  if ! require_profile_topology \
      "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR"; then
    echo "FAIL  topology  profile needs $NODES confirmed ranks"
    die "run scripts/detect-fabric.sh --write-topology, then retry"
  fi
  echo "PASS  topology  profile=$NODES ranks  available=$CLUSTER_TOPOLOGY_COUNT  id=${CLUSTER_TOPOLOGY_ID:0:12}"
fi

# --- image ---
set +e
if [ "$VERBOSE" = 1 ]; then
  "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}"
  img_rc=$?
else
  img_line=$(QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" 2>&1)
  img_rc=$?
  echo "$img_line"
fi
img_json=$(QUIET=0 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --json 2>/dev/null || true)
set -e
if [ "$img_rc" != 0 ]; then
  img_state=$(printf '%s' "$img_json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || echo unknown)
  case "$img_state" in
    need-topology)
      die "confirmed topology has fewer ranks than this profile requires"
      ;;
    missing-on-worker|missing-on-rank)
      if [ "$DRY" != 1 ] && [ "$PULL_IMG" = 1 ]; then
        "$REPO_DIR/scripts/sync-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --yes
        QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" \
          || die "image still missing after rank sync"
      else
        die "image missing on remote rank(s) — run: scripts/sync-image.sh $NAME --yes"
      fi
      ;;
    worker-unreachable|rank-unreachable|target-unreachable)
      die "one or more required physical nodes are unreachable over BatchMode SSH"
      ;;
    worker-docker-error|rank-docker-error|head-docker-error|target-docker-error)
      die "Docker is unavailable on one or more required physical nodes"
      ;;
    missing-on-head|missing-on-target|missing-both|unknown|"")
      if [ "$DRY" != 1 ] && [ "$PULL_IMG" = 1 ]; then
        "$REPO_DIR/scripts/sync-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --pull --yes
        QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" \
          || die "image still missing after sync"
      else
        die "image missing ($img_state): $IMAGE"
      fi
      ;;
    *)
      die "image check failed (state=$img_state)"
      ;;
  esac
fi

# --- weights ---
if [ "$SKIP_W" != 1 ]; then
  set +e
  if [ "$VERBOSE" = 1 ]; then
    "$REPO_DIR/scripts/check-weights.sh" "$NAME" "${PLACEMENT_ARGS[@]}"
    w_rc=$?
  else
    QUIET=1 "$REPO_DIR/scripts/check-weights.sh" "$NAME" "${PLACEMENT_ARGS[@]}"
    w_rc=$?
  fi
  set -e
  if [ "$w_rc" != 0 ]; then
    die "model files are not ready — see the weights check above"
  fi
else
  echo "SKIP  weights"
fi

# --- memory ---
set +e
if [ "$VERBOSE" = 1 ]; then
  "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}"
  mem_rc=$?
else
  QUIET=1 "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}"
  mem_rc=$?
fi
set -e
case "$mem_rc" in
  0) ;;
  1)
    die "memory preflight FAILED"
    ;;
  2)
    if [ "$DRY" = 1 ]; then
      echo "      (WARN accepted for dry-run)"
    elif [ "$ACCEPT_MEM" = 1 ]; then
      echo "      (WARN accepted via --accept-memory-warn)"
    else
      die "memory WARN — re-run with --accept-memory-warn to launch"
    fi
    ;;
  *)
    die "memory preflight failed internally (exit=$mem_rc) — refusing launch"
    ;;
esac

# --- multi-node preflight ---
if [ "$NODES" -gt 1 ]; then
  if [ "$SKIP_PF" != 1 ]; then
    if [ "$DRY" = 1 ]; then
      echo "PASS  preflight would-run cluster/preflight.sh $NAME"
    else
      echo "│  running cluster preflight…"
      if [ "$VERBOSE" = 1 ]; then
        "$REPO_DIR/cluster/preflight.sh" "$NAME" \
          || die "cluster preflight failed"
      else
        _pf_log=$(mktemp "${TMPDIR:-/tmp}/pulsar-preflight.XXXXXX")
        # shellcheck disable=SC2064
        trap 'rm -f "${_pf_log:-}"' RETURN
        if "$REPO_DIR/cluster/preflight.sh" "$NAME" \
            >"$_pf_log" 2>&1; then
          echo "PASS  preflight cluster OK"
          rm -f "$_pf_log"
        else
          echo "FAIL  preflight — see $_pf_log"
          tail -20 "$_pf_log" >&2 || true
          die "cluster preflight failed"
        fi
      fi
    fi
  else
    echo "SKIP  preflight"
  fi
fi

spec_flag=()
case "$SPEC_MODE" in
  on) spec_flag=(--spec-decode) ;;
  off) spec_flag=(--no-spec-decode) ;;
  auto) ;;
esac

launch_flags=()
if [ "$REPLACE" = 1 ]; then
  launch_flags+=(--replace)
fi
if [ "$NODES" -gt 1 ]; then
  # up.sh already ran (or explicitly skipped) this preflight. Always suppress
  # start-cluster.sh's duplicate run while preserving the caller's decision.
  launch_flags+=(--skip-preflight)
fi

echo "└─"

resolve_library_hot_for_profile "$NAME"
PLAN_FILE="${PULSAR_LAUNCH_PLAN_OUT:-$(mktemp "${TMPDIR:-/tmp}/pulsar-launch-plan.XXXXXX")}"
if [ -z "${PULSAR_LAUNCH_PLAN_OUT:-}" ]; then
  # shellcheck disable=SC2064
  trap 'rm -f "${PLAN_FILE:-}"' EXIT
fi
write_launch_plan_file "$PLAN_FILE" "$([ "$DRY" = 1 ] && echo dry-run || echo start)"
echo "PASS  plan      schema=3 ranks=$NODES; prerequisites checked separately"

if [ "$DRY" = 1 ]; then
  cat <<EOF

DRY-RUN OK
  conf:     $NAME
  served:   $SERVED_NAME
  plan:     $PLAN_FILE
  would:    $([ "$NODES" -gt 1 ] && echo "cluster/start-cluster.sh $NAME ${spec_flag[*]:-} ${launch_flags[*]:-}" || echo "serve.sh $NAME -d ${PLACEMENT_ARGS[*]:-} ${spec_flag[*]:-} ${launch_flags[*]:-}")
  live:     scripts/status.sh $NAME ${PLACEMENT_ARGS[*]:-}
  note:     no containers changed
EOF
  exit 0
fi

# --- launch ---
export PULSAR_ACCEPT_MEMORY_WARN="$ACCEPT_MEM"
if [ "$NODES" -gt 1 ]; then
  log "starting exact $NODES-node cluster…"
  "$REPO_DIR/cluster/start-cluster.sh" "$NAME" \
    ${spec_flag[@]+"${spec_flag[@]}"} "${launch_flags[@]}"
else
  log "starting single-node…"
  "$REPO_DIR/serve.sh" "$NAME" -d \
    "${PLACEMENT_ARGS[@]}" \
    ${spec_flag[@]+"${spec_flag[@]}"} "${launch_flags[@]}"
  api_auth_args=()
  api_auth_curl_args api_auth_args
  cname=$(container_name_for "$NAME" 1)
  log "waiting for ${SERVICE_API_BASE}/health on $(single_node_display) (cold load can take minutes)"
  ok=0
  for i in $(seq 1 "${WAIT_ATTEMPTS:-90}"); do
    if curl -fsS --max-time 3 "${api_auth_args[@]}" "${SERVICE_API_BASE}/health" >/dev/null 2>&1; then
      ok=1
      break
    fi
    container_rc=0
    if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
      container_running_exact_remote "$SINGLE_NODE_SSH_HOST" "$cname" || container_rc=$?
    else
      container_running_exact "$cname" || container_rc=$?
    fi
    if [ "$container_rc" -ne 0 ]; then
      warn "container died; last logs:"
      if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
        remote_logs=$(shell_join_q docker logs --tail 80 "$cname")
        "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$SINGLE_NODE_SSH_HOST" "$remote_logs" >&2 || true
      else
        "$PULSAR_DOCKER" logs --tail 80 "$cname" >&2 || true
      fi
      exit 1
    fi
    sleep "${WAIT_SECONDS:-5}"
  done
  if [ "$ok" != 1 ]; then
    warn "timed out waiting for health; logs:"
    if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
      remote_logs=$(shell_join_q docker logs --tail 100 "$cname")
      "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$SINGLE_NODE_SSH_HOST" "$remote_logs" >&2 || true
    else
      "$PULSAR_DOCKER" logs --tail 100 "$cname" >&2 || true
    fi
    exit 1
  fi
  log "healthy — smoke completion"
  curl -fsS --max-time 120 "${SERVICE_API_BASE}/v1/completions" \
    "${api_auth_args[@]}" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${SERVED_NAME}\",\"prompt\":\"2+2=\",\"max_tokens\":4,\"temperature\":0}" \
    && echo
fi

cat <<EOF

READY
  conf:     $NAME
  served:   $SERVED_NAME
  url:      ${SERVICE_API_BASE}/v1
  inspect:  scripts/quick-status.sh
  status:   scripts/status.sh $NAME ${PLACEMENT_ARGS[*]:-}
  stop:     scripts/down.sh $NAME ${PLACEMENT_ARGS[*]:-}
  security: do not expose :${PORT} without auth (SECURITY.md)
EOF
