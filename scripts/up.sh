#!/usr/bin/env bash
# Orchestrate checks then launch serve.sh or start-cluster.sh.
#   pulsar start SPEC_ID [--spec-file FILE] [--node NODE]
#                [--dry-run] [--yes] [--verbose]
set -euo pipefail
SCRIPT_NAME=up
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

up_usage() {
  python3 "$REPO_DIR/scripts/terminal_format.py" <<'HELP'
usage: pulsar start SPEC_ID [options]

  --spec-file FILE       Use an explicit workbench candidate
  --override-file FILE   Explicit typed execution changes; report a modified recipe
  --memory-estimate-file FILE  Explicit estimate of resident weights for this spec
  --memory-estimate-id ID      Require the previously reviewed estimate digest
  --dry-run             Check prerequisites without launching
  --verbose             Show full diagnostic output
  --node NODE           Select a confirmed node, by hostname or node ID, for a one-node spec
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
# The launchers receive only the frozen estimate; suggestions they record keep
# the operator's own estimate arguments.
export START_BLOCKER_MEMORY_ESTIMATE_FILE="$MEMORY_ESTIMATE_FILE" START_BLOCKER_MEMORY_ESTIMATE_ID="$MEMORY_ESTIMATE_ID"

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
  if ! resolve_single_node_placement "$NODE_SELECTOR"; then
    # Without a confirmed topology no node can be selected: report the topology.
    if ! require_cluster_nodes 1 >/dev/null 2>&1; then
      START_BLOCKER_PLACEMENT="" start_blocker topology_incomplete --detail "needs 1, confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
      die "start is blocked by 1 blocker(s) above; nothing was launched"
    fi
    die "--node '$NODE_SELECTOR' does not select exactly one confirmed node; use a hostname or node ID from ./pulsar topology show"
  fi
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

# Suggested start commands repeat the operator's own flags, so following one
# keeps a dry run dry and keeps permissions already granted.
start_flags=()
[ "$DRY" != 1 ] || start_flags+=(--dry-run)
[ "$PULL_IMG" != 1 ] || start_flags+=(--pull-image)
[ "$ACCEPT_MEM" != 1 ] || start_flags+=(--accept-memory-warn)
[ "$REPLACE" != 1 ] || start_flags+=(--replace)
[ "$VERBOSE" != 1 ] || start_flags+=(--verbose)
[ "$SKIP_PF" != 1 ] || start_flags+=(--skip-preflight)
export START_BLOCKER_START_FLAGS="${start_flags[*]:-}"

# check_error FILE — the last "error:" line a check script printed, for a detail.
check_error() {
  sed -nE 's/^(\[[^]]+\] )?error: //p' "$1" 2>/dev/null | tail -n1
}

# image_ranks STATE... — "rank<TAB>hostname<TAB>node_id" for image-check ranks in STATE.
image_ranks() {
  local rank index
  while IFS=$'\t' read -r rank index; do
    printf '%s\t%s\t%s\n' "$rank" "$(human_node_name "$index")" "${CLUSTER_NODE_IDS[$index]:-}"
  done < <(printf '%s' "$img_json" | python3 -c '
import json,sys
for row in (json.load(sys.stdin).get("ranks") or []):
    if row.get("state") in sys.argv[1:]: print(row["rank"], row["topology_index"], sep="\t")
' "$@" 2>/dev/null)
}

# --- topology: every spec; the later checks need the confirmed nodes ---
if ! require_cluster_nodes "$NODES" >/dev/null 2>&1; then
  echo "FAIL  topology  spec needs $NODES confirmed node(s); confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
  blocked topology_incomplete --detail "needs $NODES, confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
  stop_if_blocked
fi
if [ "$NODES" -gt 1 ]; then
  fabric_err=$(mktemp "${TMPDIR:-/tmp}/pulsar-fabric.XXXXXX")
  if ! require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" 2>"$fabric_err"; then
    fabric_reason=$(sed -n 's/^topology: //p' "$fabric_err" | tail -n1)
    rm -f "$fabric_err"
    echo "FAIL  fabric    ${fabric_reason:-the confirmed fabric does not meet this spec}"
    blocked fabric_incomplete --detail "${fabric_reason:-run ./pulsar topology check for details}"
    stop_if_blocked
  fi
  rm -f "$fabric_err"
fi
echo "PASS  topology  spec needs $NODES node(s)  confirmed=$CLUSTER_TOPOLOGY_COUNT  id=${CLUSTER_TOPOLOGY_ID:0:12}"
# A new home goes on the start placement (one node) or on rank 0.
if [ "$NODES" -eq 1 ]; then
  START_BLOCKER_HOME_NODE="${SINGLE_NODE_HOSTNAME:-}"
else
  START_BLOCKER_HOME_NODE="$(human_node_name 0)"
fi
export START_BLOCKER_HOME_NODE

# --- image ---
# With --pull-image a missing image is staged only after every other check
# passes, so a start that is blocked anyway changes nothing.
IMAGE_SYNC=""
# Each check's stderr is kept in a file for the blocker detail and shown after
# the check ends.
img_err=$(mktemp "${TMPDIR:-/tmp}/pulsar-image.XXXXXX")
set +e
if [ "$VERBOSE" = 1 ]; then
  "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" 2>"$img_err"
  img_rc=$?
else
  img_line=$(QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" 2>"$img_err")
  img_rc=$?
  [ -z "$img_line" ] || echo "$img_line"
fi
cat "$img_err" >&2
img_json=$(QUIET=0 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --json 2>/dev/null)
set -e
img_state=$(printf '%s' "$img_json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)
if [ "$img_rc" != 0 ]; then
  if [ -z "$img_state" ]; then
    # No report: the check itself failed, which says nothing about the image.
    img_detail=$(check_error "$img_err")
    blocked image_check_failed --detail "${img_detail:-exit $img_rc}"
  else
    missing_on=$(image_ranks missing | cut -f2 | paste -sd, - | sed 's/,/, /g')
    case "$img_state" in
      need-topology)
        blocked topology_incomplete --detail "the image check found fewer confirmed nodes than the spec needs"
        stop_if_blocked
        ;;
      worker-unreachable|rank-unreachable|target-unreachable|worker-docker-error|rank-docker-error|head-docker-error|target-docker-error)
        # Name every affected node; later checks need every node observable.
        while IFS=$'\t' read -r rank host node_id; do
          blocked node_unreachable --node "$host" --node-id "$node_id" --rank "$rank"
        done < <(image_ranks unreachable)
        while IFS=$'\t' read -r rank host node_id; do
          blocked docker_unavailable --node "$host" --node-id "$node_id" --rank "$rank"
        done < <(image_ranks docker-error)
        [ "$BLOCKER_COUNT" -gt 0 ] || blocked docker_unavailable --detail "image check state $img_state"
        # Ranks already known to lack the image are reported now, not after repair.
        if [ -n "$missing_on" ] && [ "$PULL_IMG" != 1 ]; then
          blocked image_missing --detail "$IMAGE on $missing_on"
        elif [ -n "$missing_on" ]; then
          echo "INFO  image     missing on $missing_on; --pull-image stages it once the other blockers are resolved"
        fi
        stop_if_blocked
        ;;
      missing-on-worker|missing-on-rank|missing-on-head|missing-on-target|missing-both)
        if [ "$PULL_IMG" != 1 ]; then
          blocked image_missing --detail "$IMAGE${missing_on:+ on $missing_on}"
        elif [ "$DRY" = 1 ]; then
          echo "INFO  image     missing${missing_on:+ on $missing_on}; --pull-image would stage it"
        else
          IMAGE_SYNC="$img_state"
        fi
        ;;
      *)
        blocked image_check_failed --detail "image check state $img_state"
        ;;
    esac
  fi
fi
rm -f "$img_err"

# --- existing service: before memory and port, which a running service distorts ---
CONTAINER=$(container_name_for "$NAME" "$NODES")
REPLACING=0
existing_hosts=() existing_ids=() uninspectable=()
for ((rank = 0; rank < NODES; rank++)); do
  probe_rc=0
  if [ "$NODES" -eq 1 ]; then
    index="${SINGLE_NODE_INDEX:-0}"
    if [ "${SINGLE_NODE_REMOTE:-0}" = 1 ]; then
      container_ownership_inspect_remote "$SINGLE_NODE_SSH_HOST" "$CONTAINER" >/dev/null || probe_rc=$?
    else
      container_ownership_inspect_local "$CONTAINER" >/dev/null || probe_rc=$?
    fi
  else
    index="$rank"
    if [ "$rank" -gt 0 ]; then
      container_ownership_inspect_remote "${CLUSTER_NODE_SSH_HOSTS[$rank]}" "$CONTAINER" >/dev/null || probe_rc=$?
    else
      container_ownership_inspect_local "$CONTAINER" >/dev/null || probe_rc=$?
    fi
  fi
  case "$probe_rc" in
    0) existing_hosts+=("$(human_node_name "$index")"); existing_ids+=("${CLUSTER_NODE_IDS[$index]:-}") ;;
    3) ;;
    *) uninspectable+=("$index") ;;
  esac
done
# A node whose containers cannot be inspected ends the checks; the later ones
# would misread a service that may be running there.
if [ "${#uninspectable[@]}" -gt 0 ]; then
  for index in "${uninspectable[@]}"; do
    blocked docker_unavailable --node "$(human_node_name "$index")" --node-id "${CLUSTER_NODE_IDS[$index]:-}" \
      --detail "could not inspect existing containers"
  done
  stop_if_blocked
fi
if [ "${#existing_hosts[@]}" -gt 0 ]; then
  where=$(printf '%s, ' "${existing_hosts[@]}"); where="${where%, }"
  if [ "$REPLACE" = 1 ]; then
    REPLACING=1
    echo "INFO  service   $CONTAINER exists on $where; --replace removes it after the image recheck"
  elif [ "$NODES" -eq 1 ]; then
    blocked service_exists --node "${existing_hosts[0]}" --node-id "${existing_ids[0]}" --detail "container $CONTAINER"
    stop_if_blocked
  else
    blocked service_exists --detail "container $CONTAINER on $where"
    stop_if_blocked
  fi
fi

# --- model files ---
if [ "$SKIP_W" != 1 ]; then
  w_err=$(mktemp "${TMPDIR:-/tmp}/pulsar-files.XXXXXX")
  set +e
  if [ "$VERBOSE" = 1 ]; then
    "$REPO_DIR/scripts/check-weights.sh" "$NAME" "${PLACEMENT_ARGS[@]}" 2>"$w_err"
    w_rc=$?
  else
    QUIET=1 "$REPO_DIR/scripts/check-weights.sh" "$NAME" "${PLACEMENT_ARGS[@]}" 2>"$w_err"
    w_rc=$?
  fi
  set -e
  cat "$w_err" >&2
  case "$w_rc" in
    0) ;;
    1) blocked model_files_not_ready --detail "see the model-files check above" ;;
    *) w_detail=$(check_error "$w_err"); blocked model_files_check_failed --detail "${w_detail:-exit $w_rc}" ;;
  esac
  rm -f "$w_err"
else
  echo "SKIP  model files"
fi

# --- memory ---
MEMORY_ARGS=()
if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
  MEMORY_ARGS=(--memory-estimate-frozen "$PULSAR_MEMORY_ESTIMATE_JSON")
fi
# The per-node reason (available versus needed) comes from the JSON report,
# requested only when memory blocks the start.
memory_reason() {
  QUIET=1 "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}" "${MEMORY_ARGS[@]}" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason","").strip().rstrip(";"))' 2>/dev/null || true
}
if [ "$REPLACING" = 1 ]; then
  echo "SKIP  memory    rechecked after the previous service is removed"
else
  m_err=$(mktemp "${TMPDIR:-/tmp}/pulsar-memory.XXXXXX")
  set +e
  if [ "$VERBOSE" = 1 ]; then
    "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}" "${MEMORY_ARGS[@]}" 2>"$m_err"
    mem_rc=$?
  else
    QUIET=1 "$REPO_DIR/scripts/check-memory.sh" "$NAME" "${PLACEMENT_ARGS[@]}" "${MEMORY_ARGS[@]}" 2>"$m_err"
    mem_rc=$?
  fi
  set -e
  cat "$m_err" >&2
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
      m_detail=$(check_error "$m_err")
      blocked memory_check_failed --detail "${m_detail:-exit $mem_rc}"
      ;;
  esac
  rm -f "$m_err"
fi

# --- port: after the existence check; a service being replaced frees its port ---
if [ "$REPLACING" = 1 ]; then
  echo "SKIP  port      rechecked after the previous service is removed"
else
  port_rows=()
  if [ "$NODES" -eq 1 ]; then
    port_rows=("${SINGLE_NODE_INDEX:-0}:$PORT")
  else
    port_rows=("0:$PORT" "0:${MASTER_PORT:-29500}")
  fi
  for row in "${port_rows[@]}"; do
    index="${row%%:*}" checked_port="${row#*:}" port_host=""
    if [ "$NODES" -eq 1 ] && [ "${SINGLE_NODE_REMOTE:-0}" = 1 ]; then port_host="$SINGLE_NODE_SSH_HOST"; fi
    if port_free "$checked_port" ${port_host:+"$port_host"}; then
      continue
    fi
    echo "FAIL  port      $checked_port is in use on $(human_node_name "$index")"
    blocked port_in_use --node "$(human_node_name "$index")" --node-id "${CLUSTER_NODE_IDS[$index]:-}" \
      --detail "port $checked_port"
  done
fi
stop_if_blocked

# --- deferred image staging (--pull-image) ---
case "$IMAGE_SYNC" in
  "") ;;
  missing-on-worker|missing-on-rank)
    # Streaming from this node can omit the digest reference; --pull-image
    # also permits pulling the exact digest on a node where that happens.
    "$REPO_DIR/scripts/sync-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --yes --pull-if-stream-incomplete
    QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" \
      || die "the pinned image is still missing after staging; see the staging errors above"
    ;;
  *)
    "$REPO_DIR/scripts/sync-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" --pull --yes
    QUIET=1 "$REPO_DIR/scripts/check-image.sh" "$NAME" "${PLACEMENT_ARGS[@]}" \
      || die "the pinned image is still missing after pulling it; see the pull errors above"
    ;;
esac

# --- multi-node preflight ---
if [ "$NODES" -gt 1 ]; then
  if [ "$SKIP_PF" != 1 ]; then
    if [ "$DRY" = 1 ]; then
      echo "PASS  preflight would run before launch"
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
SERVICE_ID=$(launch_plan_service_id "$PLAN_FILE")
# The launch scripts take their own locks and recheck image, model files and
# memory under them before creating containers, then release them. Start's own
# locks are released here, so stop, status and model-file work are not blocked
# while a service loads; the containers' references protect their files.
release_model_library_locks
api_auth_args=()
api_auth_curl_args api_auth_args
if [ "$NODES" -gt 1 ]; then
  log "starting exact $NODES-node cluster…"
  "$REPO_DIR/cluster/start-cluster.sh" "$NAME" \
    ${spec_flag[@]+"${spec_flag[@]}"} "${launch_flags[@]}"
  SERVICE_NODE="$(human_node_name 0)" SERVICE_NODE_ID="${CLUSTER_NODE_IDS[0]:-}"
else
  log "starting single-node…"
  "$REPO_DIR/serve.sh" "$NAME" -d \
    "${PLACEMENT_ARGS[@]}" \
    ${spec_flag[@]+"${spec_flag[@]}"} "${launch_flags[@]}"
  SERVICE_NODE="${SINGLE_NODE_HOSTNAME:-}" SERVICE_NODE_ID="${SINGLE_NODE_ID:-}"
  state_host=""
  [ "$SINGLE_NODE_REMOTE" != 1 ] || state_host="$SINGLE_NODE_SSH_HOST"
  service_logs() {
    if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
      "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$SINGLE_NODE_SSH_HOST" "$(shell_join_q docker logs --tail "$1" "$CONTAINER")" >&2 || true
    else
      "$PULSAR_DOCKER" logs --tail "$1" "$CONTAINER" >&2 || true
    fi
  }
  log "waiting for ${SERVICE_API_BASE}/health on $(single_node_display) (cold load can take minutes)"
  ok=0 container_state=""
  for i in $(seq 1 "${WAIT_ATTEMPTS:-90}"); do
    if curl -fsS --max-time 3 "${api_auth_args[@]}" "${SERVICE_API_BASE}/health" >/dev/null 2>&1; then
      ok=1
      break
    fi
    container_state=$(container_state_exact "$CONTAINER" "$state_host")
    case "$container_state" in
      exited)
        error_line "the container $CONTAINER exited before it became healthy; last logs:"
        service_logs 80
        blocked container_exited --node "$SERVICE_NODE" --node-id "$SERVICE_NODE_ID" --service-id "$SERVICE_ID"
        exit 1
        ;;
      absent)
        error_line "the container $CONTAINER was removed before it became healthy"
        blocked service_stopped --node "$SERVICE_NODE" --node-id "$SERVICE_NODE_ID" --service-id "$SERVICE_ID"
        exit 1
        ;;
    esac
    sleep "${WAIT_SECONDS:-5}"
  done
  if [ "$ok" != 1 ]; then
    if [ "$container_state" = unknown ]; then
      # Docker or SSH stopped answering: the service is not known to run.
      error_line "timed out waiting for health; the container on $(single_node_display) could not be observed"
      blocked health_timeout --node "$SERVICE_NODE" --node-id "$SERVICE_NODE_ID" --service-id "$SERVICE_ID" \
        --note "Its state could not be observed when the wait ended, so it may still be running."
    else
      error_line "timed out waiting for health; last logs:"
      service_logs 100
      blocked health_timeout --node "$SERVICE_NODE" --node-id "$SERVICE_NODE_ID" --service-id "$SERVICE_ID"
    fi
    exit 1
  fi
fi

# --- smoke test: READY only after the model answers a request ---
log "healthy — sending a test completion"
smoke_body=$(mktemp "${TMPDIR:-/tmp}/pulsar-smoke.XXXXXX")
smoke_status=0
smoke_http=$(curl -sS --max-time 120 -o "$smoke_body" -w '%{http_code}' "${api_auth_args[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${SERVED_NAME}\",\"prompt\":\"2+2=\",\"max_tokens\":4,\"temperature\":0}" \
  "${SERVICE_API_BASE}/v1/completions" 2>"$smoke_body.err") || smoke_status=$?
if [ "$smoke_status" != 0 ] || [ "${smoke_http:-000}" != 200 ]; then
  if [ "$smoke_status" != 0 ]; then
    smoke_detail="curl: $(tr '\n' ' ' <"$smoke_body.err" | sed 's/^curl: ([0-9]*) //; s/ *$//')"
  else
    smoke_detail="HTTP $smoke_http: $(head -c 200 "$smoke_body" | tr '\n' ' ')"
  fi
  error_line "the test completion failed ($smoke_detail); the service is still running; last logs:"
  if [ "$NODES" -gt 1 ]; then
    "$PULSAR_DOCKER" logs --tail 80 "$CONTAINER" >&2 || true
  else
    service_logs 80
  fi
  rm -f "$smoke_body" "$smoke_body.err"
  blocked smoke_test_failed --node "$SERVICE_NODE" --node-id "$SERVICE_NODE_ID" --service-id "$SERVICE_ID" \
    --detail "$smoke_detail"
  exit 1
fi
head -c 400 "$smoke_body"; echo
rm -f "$smoke_body" "$smoke_body.err"

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
