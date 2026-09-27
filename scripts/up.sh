#!/usr/bin/env bash
# Orchestrate checks then launch serve.sh or start-cluster.sh.
#   pulsar start SPEC_ID [--spec-file FILE] [--node NODE_ID]
#                [--dry-run] [--yes] [--verbose]
set -euo pipefail
SCRIPT_NAME=up
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

up_usage() {
  cat <<'HELP' | python3 -c 'import sys; from scripts.terminal_format import TerminalWriter; w=TerminalWriter(); [w.emit(line.rstrip(),subsequent_indent="    " if line.startswith("  ") else "") for line in sys.stdin]'
usage: pulsar start SPEC_ID [options]

  --spec-file FILE       Use an explicit workbench candidate
  --override-file FILE   Explicit typed execution changes; report a modified recipe
  --memory-estimate-file FILE  Explicit estimate of resident weights for this spec
  --memory-estimate-id ID      Require the previously reviewed estimate digest
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
Explicit memory estimates are admission guidance; see docs/MEMORY_ESTIMATES.md.
HELP
}
case "${1:-}" in -h|--help) up_usage; exit 0 ;; esac

NAME="${1:-}"
unset PULSAR_OVERRIDE_FILE PULSAR_EFFECTIVE_SPEC_ID
unset PULSAR_MEMORY_ESTIMATE_JSON
[ -n "$NAME" ] || usage_die "usage: pulsar start SPEC_ID [options]; see ./pulsar start --help"
shift

SPEC_MODE=auto SKIP_PF=0 SKIP_W=0 ACCEPT_MEM=0 PULL_IMG=0 REPLACE=0
DRY=0 VERBOSE=0 NODE_SELECTOR=""
MEMORY_ESTIMATE_FILE="" MEMORY_ESTIMATE_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --override-file) [ "$#" -ge 2 ] || usage_die "--override-file requires a JSON file"; export PULSAR_OVERRIDE_FILE="$2"; shift ;;
    --spec-file) [ "$#" -ge 2 ] || usage_die "--spec-file requires a file"; export PULSAR_SPEC_FILE="$2"; shift ;;
    --memory-estimate-file) [ "$#" -ge 2 ] && [ -n "$2" ] || usage_die "--memory-estimate-file requires a file"; MEMORY_ESTIMATE_FILE="$2"; shift ;;
    --memory-estimate-id) [ "$#" -ge 2 ] && [ -n "$2" ] || usage_die "--memory-estimate-id requires a digest"; MEMORY_ESTIMATE_ID="$2"; shift ;;
    --spec-decode) set_spec_decode_mode SPEC_MODE on ;;
    --no-spec-decode) set_spec_decode_mode SPEC_MODE off ;;
    --force) refuse_removed_force_flag ;;
    --skip-preflight) SKIP_PF=1 ;;
    --skip-weights-check) usage_die "model-file verification cannot be skipped" ;;
    --accept-memory-warn) ACCEPT_MEM=1 ;;
    --pull-image) PULL_IMG=1 ;;
    --replace) REPLACE=1 ;;
    --weight-source|--weight-mode)
      refuse_removed_weight_mode_flag
      ;;
    --node)
      [ "$#" -ge 2 ] || usage_die "--node requires a topology node id or hostname"
      NODE_SELECTOR="$2"
      shift
      ;;
    --dry-run) DRY=1 ;;
    --yes|-y) : ;;  # Compatibility acknowledgement; grants no extra action.
    --verbose|-v) VERBOSE=1; export PULSAR_VERBOSE=1 ;;
    -h|--help)
      up_usage
      exit 0
      ;;
    *) usage_die "unknown argument: $1" ;;
  esac
  shift
done

acquire_model_library_lifecycle_lock shared
load_conf "$NAME"
if [ -n "$MEMORY_ESTIMATE_FILE$MEMORY_ESTIMATE_ID" ]; then
  select_memory_estimate "$MEMORY_ESTIMATE_FILE" "" "$MEMORY_ESTIMATE_ID"
fi
if [ "${CONF_SOURCE:-conf}" = spec ] && [ "$SPEC_MODE" != auto ]; then
  die "selected spec $NAME: --spec-decode/--no-spec-decode are refused (the identity is fixed)" 2
fi
require_spec_launch_admission "$NAME"
NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
acquire_model_library_hot_lock shared
PLACEMENT_ARGS=()
SERVICE_API_BASE="http://127.0.0.1:$PORT"
if [ "$NODES" -eq 1 ]; then
  resolve_single_node_placement "$NODE_SELECTOR" \
    || die "cannot resolve physical node placement '$NODE_SELECTOR'"
  PLACEMENT_SELECTOR="${SINGLE_NODE_ID:-$SINGLE_NODE_KEY}"
  PLACEMENT_ARGS=(--node "$PLACEMENT_SELECTOR")
  # Suggested commands name the node by hostname, which --node also accepts.
  # Exported so serve.sh, which records service_exists, names the node too.
  export START_BLOCKER_PLACEMENT="--node ${SINGLE_NODE_HOSTNAME:-$PLACEMENT_SELECTOR}"
  SERVICE_API_BASE=$(single_node_api_base_url "$PORT")
elif [ -n "$NODE_SELECTOR" ]; then
  usage_die "--node is only valid for one-node specs"
fi
resolve_spec_decode "$SPEC_MODE"
SPEC_REVIEW_CELL="${SPEC_REVIEW_STATUS:-not specified}"
export QUIET=1
if [ "$VERBOSE" = 1 ]; then export QUIET=0 PULSAR_VERBOSE=1; fi

echo "┌─ up  $NAME"
if [ "${CONF_SOURCE:-conf}" = spec ]; then
  echo "│  source=spec $CONF_NAME"
  echo "│  overlay=${OVERLAY_SOURCE:-?}"
fi
echo "│  nodes=$NODES  served=$SERVED_NAME  port=$PORT"
echo "│  model files=home and prepared copies"
if [ "$NODES" -eq 1 ]; then
  echo "│  placement=$(single_node_display)"
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

# Every independent check runs before start reports. Each failure is one
# recorded blocker; blockers that leave nodes unobservable end the checks.
BLOCKER_COUNT=0
blocked() {
  BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
  start_blocker "$@"
}
stop_if_blocked() {
  [ "$BLOCKER_COUNT" -eq 0 ] \
    || die "start is blocked by $BLOCKER_COUNT blocker(s) above; nothing was launched"
}
# image_ranks STATE... — "rank<TAB>hostname" for image-check ranks in STATE.
image_ranks() {
  local rank index
  while IFS=$'\t' read -r rank index; do
    printf '%s\t%s\n' "$rank" "$(human_node_name "$index")"
  done < <(printf '%s' "$img_json" | python3 -c '
import json,sys
for row in (json.load(sys.stdin).get("ranks") or []):
    if row.get("state") in sys.argv[1:]: print(row["rank"], row["topology_index"], sep="\t")
' "$@" 2>/dev/null)
}

if [ "$NODES" -gt 1 ]; then
  if ! require_profile_topology \
      "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR"; then
    echo "FAIL  topology  spec needs $NODES confirmed nodes"
    blocked topology_incomplete --detail "needs $NODES, confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
    stop_if_blocked
  fi
  echo "PASS  topology  spec needs $NODES nodes  confirmed=$CLUSTER_TOPOLOGY_COUNT  id=${CLUSTER_TOPOLOGY_ID:0:12}"
fi

# --- image ---
# With --pull-image a missing image is staged only after every other check
# passes, so a start that is blocked anyway changes nothing.
IMAGE_SYNC=""
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
      blocked topology_incomplete --detail "the image check found fewer confirmed nodes than the spec needs"
      stop_if_blocked
      ;;
    worker-unreachable|rank-unreachable|target-unreachable|worker-docker-error|rank-docker-error|head-docker-error|target-docker-error)
      # Name every affected node; later checks need every node observable.
      while IFS=$'\t' read -r rank host; do
        blocked node_unreachable --node "$host" --rank "$rank"
      done < <(image_ranks unreachable)
      while IFS=$'\t' read -r rank host; do
        blocked docker_unavailable --node "$host" --rank "$rank"
      done < <(image_ranks docker-error)
      [ "$BLOCKER_COUNT" -gt 0 ] || blocked docker_unavailable --detail "image check state $img_state"
      # Ranks already known to lack the image are reported now, not after repair.
      missing_on=$(image_ranks missing | cut -f2 | paste -sd, - | sed 's/,/, /g')
      if [ -n "$missing_on" ] && { [ "$DRY" = 1 ] || [ "$PULL_IMG" != 1 ]; }; then
        blocked image_missing --detail "$IMAGE on $missing_on"
      fi
      stop_if_blocked
      ;;
    missing-on-worker|missing-on-rank|missing-on-head|missing-on-target|missing-both|unknown|"")
      missing_on=$(image_ranks missing | cut -f2 | paste -sd, - | sed 's/,/, /g')
      if [ "$DRY" != 1 ] && [ "$PULL_IMG" = 1 ]; then
        IMAGE_SYNC="$img_state"
      else
        blocked image_missing --detail "$IMAGE${missing_on:+ on $missing_on}"
      fi
      ;;
    *)
      blocked docker_unavailable --detail "image check state $img_state"
      stop_if_blocked
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
    blocked model_files_not_ready --detail "see the weights check above"
  fi
else
  echo "SKIP  model files"
fi

# --- memory ---
MEMORY_ARGS=()
if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
  MEMORY_ARGS=(--memory-estimate-frozen "$PULSAR_MEMORY_ESTIMATE_JSON")
fi
set +e
if [ "$VERBOSE" = 1 ]; then
  "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}" "${MEMORY_ARGS[@]}"
  mem_rc=$?
else
  QUIET=1 "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}" "${MEMORY_ARGS[@]}"
  mem_rc=$?
fi
set -e
# The per-node reason (available versus needed) comes from the JSON report,
# requested only when memory blocks the start.
memory_reason() {
  QUIET=1 "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}" "${MEMORY_ARGS[@]}" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason","").strip().rstrip(";"))' 2>/dev/null || true
}
case "$mem_rc" in
  0) ;;
  1)
    blocked memory_insufficient --detail "$(memory_reason)"
    ;;
  2)
    if [ "$DRY" = 1 ]; then
      echo "      (WARN accepted for dry-run)"
    elif [ "$ACCEPT_MEM" = 1 ]; then
      echo "      (WARN accepted via --accept-memory-warn)"
    else
      blocked memory_warning --detail "$(memory_reason)"
    fi
    ;;
  *)
    blocked memory_check_failed --detail "exit $mem_rc"
    ;;
esac
stop_if_blocked

# --- deferred image staging (--pull-image) ---
case "$IMAGE_SYNC" in
  "") ;;
  missing-on-worker|missing-on-rank)
    "$REPO_DIR/scripts/sync-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --yes
    QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" \
      || die "image still missing after rank sync"
    ;;
  *)
    "$REPO_DIR/scripts/sync-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --pull --yes
    QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" \
      || die "image still missing after sync"
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
        if ! "$REPO_DIR/cluster/preflight.sh" "$NAME"; then
          blocked preflight_failed --detail "see the preflight output above"
          stop_if_blocked
        fi
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
          blocked preflight_failed --detail "log: $_pf_log"
          stop_if_blocked
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
LAUNCH_ACTION=start
[ "$REPLACE" != 1 ] || LAUNCH_ACTION=replace
write_launch_plan_file "$PLAN_FILE" "$([ "$DRY" = 1 ] && echo dry-run || echo "$LAUNCH_ACTION")"
plan_schema=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["schema_version"])' "$PLAN_FILE")
echo "PASS  plan      schema=$plan_schema ranks=$NODES; prerequisites checked separately"

if [ "$DRY" = 1 ]; then
  cat <<EOF

DRY-RUN OK
  spec:     $NAME
  served:   $SERVED_NAME
  plan:     $PLAN_FILE
  would:    $([ "$NODES" -gt 1 ] && echo "cluster/start-cluster.sh $NAME ${spec_flag[*]:-} ${launch_flags[*]:-}" || echo "serve.sh $NAME -d ${PLACEMENT_ARGS[*]:-} ${spec_flag[*]:-} ${launch_flags[*]:-}")
  live:     ./pulsar status $NAME ${PLACEMENT_ARGS[*]:-}
  note:     no containers changed
EOF
  exit 0
fi

# --- launch ---
if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
  launch_flags+=(--memory-estimate-frozen "$PULSAR_MEMORY_ESTIMATE_JSON")
fi
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
  spec:     $NAME
  served:   $SERVED_NAME
  url:      ${SERVICE_API_BASE}/v1
  inspect:  ./pulsar inventory
  status:   ./pulsar status $NAME ${PLACEMENT_ARGS[*]:-}
  stop:     ./pulsar stop $NAME ${PLACEMENT_ARGS[*]:-}
  security: do not expose :${PORT} without authentication
EOF
