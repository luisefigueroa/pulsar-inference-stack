#!/usr/bin/env bash
# Orchestrate checks then launch serve.sh or start-cluster.sh.
#   pulsar start SPEC_ID [--spec-file FILE] [--node NODE]
#                [--dry-run] [--yes] [--verbose]
set -euo pipefail
SCRIPT_NAME=up
PLACEMENT_NODES=""
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
  --placement-nodes LIST  Ordered confirmed nodes for guarded dry-run planning
  --accept-memory-warn  Explicitly accept a memory warning
  --pull-image          Permit staging the pinned image when missing
  --replace             Permit stopping an existing exact-name service
  --yes                 Deprecated; start asks no confirmation and --yes never implies the above
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
    --placement-nodes) [ -n "${2:-}" ] || usage_die "--placement-nodes requires an ordered node list"; PLACEMENT_NODES="$2"; shift ;;
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
    --yes|-y)
      # Accepted until CLI contract 2; --replace, --pull-image and --accept-memory-warn grant start's permissions.
      warn "pulsar start --yes is deprecated: start asks no confirmation; --replace, --pull-image and --accept-memory-warn grant its permissions. It is removed in CLI contract 2." ;;
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

# Ctrl-C or a stop signal says what this start left and removes nothing:
# removing a service is stop's job, which takes the lock, probes every rank and
# confirms absence. START_PHASE says how far start got, from the first lock
# wait on; a multi-node launch reports its own phases from cluster/start-cluster.sh.
START_PHASE=checks
start_interrupted() {
  local detail=""
  trap - INT TERM HUP
  load_progress_end
  case "$START_PHASE" in
    checks) printf 'Interrupted before launch; nothing was started.\n' >&2 || true ;;
    creating) detail="while its container was being created; the container may exist" ;;
    loading) detail="while the service was loading; it keeps loading" ;;
    testing) detail="during the test completion; the service is running and passed its health check" ;;
  esac
  if [ -n "$detail" ]; then
    # With --replace, the launcher removed the existing service before launching.
    local -a noted=()
    if [ "$REPLACE" = 1 ] && [ "${#existing_hosts[@]}" -gt 0 ]; then
      if [ "$START_PHASE" = creating ]; then
        noted=(--note "Start was replacing the previous service, which may already be removed. To remove what remains: {stop}")
      else
        LAUNCH_AFTER_REPLACE=1
      fi
    fi
    blocked start_interrupted --node "$SERVICE_NODE" --node-id "$SERVICE_NODE_ID" --service-id "${SERVICE_ID:-}" \
      --detail "$detail" ${noted[@]+"${noted[@]}"} || true
  fi
  exit $((128 + $1))
}
trap 'start_interrupted 2' INT
trap 'start_interrupted 15' TERM
trap 'start_interrupted 1' HUP

acquire_model_library_lifecycle_lock shared
load_conf "$NAME"
[ "$NODES" != 1 ] || [ -z "$PLACEMENT_NODES" ] || usage_die "--placement-nodes requires a multi-node spec; use --node"
if [ -n "$MEMORY_ESTIMATE_FILE$MEMORY_ESTIMATE_ID" ]; then
  select_memory_estimate "$MEMORY_ESTIMATE_FILE" "" "$MEMORY_ESTIMATE_ID"
fi
if [ "${CONF_SOURCE:-conf}" = spec ] && [ "$SPEC_MODE" != auto ]; then
  usage_die "selected spec $NAME: --spec-decode/--no-spec-decode are refused (the identity is fixed)"
fi
if [ "$DRY" = 1 ]; then
  require_spec_launch_admission "$NAME" dry-run
else
  require_spec_launch_admission "$NAME"
fi
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
else
  resolve_serving_placement "$NODE_SELECTOR" "$PLACEMENT_NODES" || die "confirmed serving placement is unavailable"
  [ -z "$PLACEMENT_NODES" ] || PLACEMENT_ARGS=(--placement-nodes "$PLACEMENT_NODES")
  if [ "$DRY" != 1 ] && [ -n "$PLACEMENT_NODES" ]; then
    for ((slot=0;slot<NODES;slot++)); do
      [ "${SERVING_NODE_INDEXES[$slot]}" = "$slot" ] || usage_die "nondefault multi-node placement requires guarded run; ordinary start cannot launch a remote head"
    done
  fi
fi
resolve_spec_decode "$SPEC_MODE"
SPEC_REVIEW_CELL="${SPEC_REVIEW_STATUS:-not specified}"
export QUIET=1
if [ "$VERBOSE" = 1 ]; then export QUIET=0 PULSAR_VERBOSE=1; fi
# The check scripts leave the next step to start's own blocker lines.
export PULSAR_RUN_BY_START=1

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

# start_check OUTCOME ID MESSAGE [--node NODE] — records one check for start
# --json and the menu's checklist (scripts/start_checks.py), beside the human
# line printed as before. Recording never changes what start does.
start_check() {
  [ -n "${PULSAR_START_CHECKS_FILE:-}" ] || return 0
  python3 "$REPO_DIR/scripts/start_checks.py" record "$@" || warn "the $2 check could not be recorded for start --json"
}
# The checks this start expects; any a blocker keeps it from running are not_run.
if [ -n "${PULSAR_START_CHECKS_FILE:-}" ]; then
  expected_checks=(spec-review recipe topology)
  [ "$NODES" -le 1 ] || expected_checks+=(fabric)
  expected_checks+=(image service model-files memory port)
  [ "$NODES" -le 1 ] || expected_checks+=(preflight)
  python3 "$REPO_DIR/scripts/start_checks.py" expect "${expected_checks[@]}" plan \
    || warn "the start checks could not be recorded for start --json"
fi

echo "INFO  spec-review $SPEC_REVIEW_CELL (display-only)"
start_check info spec-review "$SPEC_REVIEW_CELL (display-only)"
echo "PASS  recipe    exact spec contract parsed"
start_check pass recipe "exact spec contract parsed"

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
  status_line "FAIL  topology  spec needs $NODES confirmed node(s); confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
  start_check fail topology "spec needs $NODES confirmed node(s); confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
  blocked topology_incomplete --detail "needs $NODES, confirmed ${CLUSTER_TOPOLOGY_COUNT:-0}"
  stop_if_blocked
fi
if [ "$NODES" -gt 1 ]; then
  fabric_err=$(mktemp "${TMPDIR:-/tmp}/pulsar-fabric.XXXXXX")
  if ! require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" 2>"$fabric_err"; then
    fabric_reason=$(sed -n 's/^topology: //p' "$fabric_err" | tail -n1)
    rm -f "$fabric_err"
    status_line "FAIL  fabric    ${fabric_reason:-the confirmed fabric does not meet this spec}"
    start_check fail fabric "${fabric_reason:-the confirmed fabric does not meet this spec}"
    blocked fabric_incomplete --detail "${fabric_reason:-run ./pulsar topology check for details}"
    stop_if_blocked
  fi
  rm -f "$fabric_err"
  start_check pass fabric "the confirmed fabric meets this spec"
fi
echo "PASS  topology  spec needs $NODES node(s)  confirmed=$CLUSTER_TOPOLOGY_COUNT  id=${CLUSTER_TOPOLOGY_ID:0:12}"
start_check pass topology "spec needs $NODES node(s); confirmed $CLUSTER_TOPOLOGY_COUNT"
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
# The check's own line, without its PASS/FAIL tag, is the recorded message.
img_message=$(printf '%s\n' "${img_line:-}" | sed -E '1s/^(PASS|FAIL|INFO|WARN) +image +//' | tr -s ' \n' ' ')
img_message="${img_message% }"
if [ "$img_rc" = 0 ]; then
  start_check pass image "${img_message:-pinned image verified on every serving rank}"
else
  if [ -z "$img_state" ]; then
    # No report: the check itself failed, which says nothing about the image.
    img_detail=$(check_error "$img_err")
    start_check fail image "the image check could not complete: ${img_detail:-exit $img_rc}"
    blocked image_check_failed --detail "${img_detail:-exit $img_rc}"
  else
    missing_on=$(image_ranks missing | cut -f2 | paste -sd, - | sed 's/,/, /g')
    case "$img_state" in
      need-topology)
        start_check fail image "the image check found fewer confirmed nodes than the spec needs"
        blocked topology_incomplete --detail "the image check found fewer confirmed nodes than the spec needs"
        stop_if_blocked
        ;;
      worker-unreachable|rank-unreachable|target-unreachable|worker-docker-error|rank-docker-error|head-docker-error|target-docker-error)
        # Name every affected node; later checks need every node observable.
        start_check fail image "${img_message:-image check state $img_state}"
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
          start_check fail image "${img_message:-the pinned image is missing${missing_on:+ on $missing_on}}"
          blocked image_missing --detail "$IMAGE${missing_on:+ on $missing_on}"
        elif [ "$DRY" = 1 ]; then
          echo "INFO  image     missing${missing_on:+ on $missing_on}; --pull-image would stage it"
          start_check info image "missing${missing_on:+ on $missing_on}; --pull-image would stage it"
        else
          start_check info image "missing${missing_on:+ on $missing_on}; --pull-image stages it after the other checks"
          IMAGE_SYNC="$img_state"
        fi
        ;;
      *)
        start_check fail image "image check state $img_state"
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
    index="${SERVING_NODE_INDEXES[$rank]:-$rank}"
    if [ "$index" -gt 0 ]; then
      container_ownership_inspect_remote "${CLUSTER_NODE_SSH_HOSTS[$index]}" "$CONTAINER" >/dev/null || probe_rc=$?
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
    start_check fail service "could not inspect existing containers" --node "$(human_node_name "$index")"
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
    start_check info service "$CONTAINER exists on $where; --replace removes it after the image recheck"
  elif [ "$NODES" -eq 1 ]; then
    start_check fail service "container $CONTAINER exists on $where"
    blocked service_exists --node "${existing_hosts[0]}" --node-id "${existing_ids[0]}" --detail "container $CONTAINER"
    stop_if_blocked
  else
    start_check fail service "container $CONTAINER exists on $where"
    blocked service_exists --detail "container $CONTAINER on $where"
    stop_if_blocked
  fi
else
  start_check pass service "no container of this spec exists on its serving nodes"
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
    0) start_check pass model-files "prepared files verified on every serving rank" ;;
    1)
      start_check fail model-files "prepared files are not ready on every serving rank"
      blocked model_files_not_ready --detail "see the model-files check above" ;;
    *)
      w_detail=$(check_error "$w_err")
      start_check fail model-files "the model-files check could not complete: ${w_detail:-exit $w_rc}"
      blocked model_files_check_failed --detail "${w_detail:-exit $w_rc}" ;;
  esac
  rm -f "$w_err"
else
  echo "SKIP  model files"
  start_check info model-files "skipped"
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
  start_check info memory "rechecked after the previous service is removed"
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
    0) start_check pass memory "enough available memory on every serving node" ;;
    1)
      mem_reason=$(memory_reason)
      start_check fail memory "${mem_reason:-not enough available memory}"
      blocked memory_insufficient --detail "$mem_reason"
      ;;
    2)
      if [ "$DRY" = 1 ]; then
        echo "      (WARN accepted for dry-run)"
        start_check info memory "below the recommended headroom; accepted for the dry run"
      elif [ "$ACCEPT_MEM" = 1 ]; then
        echo "      (WARN accepted via --accept-memory-warn)"
        start_check info memory "below the recommended headroom; accepted via --accept-memory-warn"
      else
        mem_reason=$(memory_reason)
        start_check fail memory "${mem_reason:-below the recommended headroom}"
        blocked memory_warning --detail "$mem_reason"
      fi
      ;;
    *)
      m_detail=$(check_error "$m_err")
      start_check fail memory "the memory check could not complete: ${m_detail:-exit $mem_rc}"
      blocked memory_check_failed --detail "${m_detail:-exit $mem_rc}"
      ;;
  esac
  rm -f "$m_err"
fi

# --- port: after the existence check; a service being replaced frees its port ---
if [ "$REPLACING" = 1 ]; then
  echo "SKIP  port      rechecked after the previous service is removed"
  start_check info port "rechecked after the previous service is removed"
else
  port_rows=() ports_busy=0
  if [ "$NODES" -eq 1 ]; then
    port_rows=("${SINGLE_NODE_INDEX:-0}:$PORT")
  else
    head_index="${SERVING_NODE_INDEXES[0]:-0}"
    port_rows=("$head_index:$PORT" "$head_index:${MASTER_PORT:-29500}")
  fi
  for row in "${port_rows[@]}"; do
    index="${row%%:*}" checked_port="${row#*:}" port_host=""
    if [ "$index" -gt 0 ]; then port_host="${CLUSTER_NODE_SSH_HOSTS[$index]}"; fi
    if port_free "$checked_port" ${port_host:+"$port_host"}; then
      continue
    fi
    status_line "FAIL  port      $checked_port is in use on $(human_node_name "$index")"
    start_check fail port "$checked_port is in use" --node "$(human_node_name "$index")"
    ports_busy=1
    blocked port_in_use --node "$(human_node_name "$index")" --node-id "${CLUSTER_NODE_IDS[$index]:-}" \
      --detail "port $checked_port"
  done
  if [ "$ports_busy" = 0 ]; then
    ports=$(printf '%s, ' "${port_rows[@]##*:}")
    start_check pass port "${ports%, } free on $(human_node_name "${port_rows[0]%%:*}")"
  fi
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
      start_check info preflight "would run before launch"
    else
      echo "│  running cluster preflight…"
      if [ "$VERBOSE" = 1 ]; then
        if ! "$REPO_DIR/cluster/preflight.sh" "$NAME"; then
          start_check fail preflight "see the preflight output above"
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
          start_check pass preflight "cluster OK"
          rm -f "$_pf_log"
        else
          status_line "FAIL  preflight — see $_pf_log"
          start_check fail preflight "see $_pf_log"
          tail -20 "$_pf_log" >&2 || true
          blocked preflight_failed --detail "log: $_pf_log"
          stop_if_blocked
        fi
      fi
    fi
  else
    echo "SKIP  preflight"
    start_check info preflight "skipped with --skip-preflight"
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
start_check pass plan "schema $plan_schema, $NODES rank(s)"

# Suggested follow-up commands name the node by hostname and a catalog spec by
# its 12-character prefix when that prefix is unique; a candidate keeps its full ID.
SUGGEST_SPEC="$NAME"
if [ -z "${PULSAR_SPEC_FILE:-}" ] && [ -f "$REPO_DIR/releases/$NAME.json" ]; then
  shopt -s nullglob; prefix_matches=("$REPO_DIR/releases/${NAME:0:12}"*.json); shopt -u nullglob
  [ "${#prefix_matches[@]}" -ne 1 ] || SUGGEST_SPEC="${NAME:0:12}"
fi
SUGGEST_ARGS=("${PLACEMENT_ARGS[@]}")
if [ "${PLACEMENT_ARGS[0]:-}" = --node ] && [ -n "${SINGLE_NODE_HOSTNAME:-}" ]; then
  SUGGEST_ARGS=(--node "$SINGLE_NODE_HOSTNAME")
fi

if [ "$DRY" = 1 ]; then
  # What start would do, in words: the nodes it would serve on and what it replaces.
  if [ "$NODES" -gt 1 ]; then
    would_where=""
    for ((slot = 0; slot < NODES; slot++)); do
      would_where+="${would_where:+, }$(human_node_name "${SERVING_NODE_INDEXES[$slot]:-$slot}") (rank $slot)"
    done
    would="start a $NODES-node service on $would_where"
  else
    would="start the service on ${SINGLE_NODE_HOSTNAME:-$(single_node_display)}"
  fi
  [ "$REPLACE" != 1 ] || would="replace the existing service, then $would"
  case "$SPEC_MODE" in on) would+=" with speculative decoding" ;; off) would+=" without speculative decoding" ;; esac
  cat <<EOF

DRY-RUN OK
  spec:     $NAME
  served:   $SERVED_NAME
  plan:     $PLAN_FILE
  would:    $would
  live:     ./pulsar status $SUGGEST_SPEC ${SUGGEST_ARGS[*]:-}
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
  SERVICE_NODE="$(human_node_name 0)" SERVICE_NODE_ID="${CLUSTER_NODE_IDS[0]:-}"
  START_PHASE=cluster
  "$REPO_DIR/cluster/start-cluster.sh" "$NAME" \
    ${spec_flag[@]+"${spec_flag[@]}"} "${launch_flags[@]}"
else
  log "starting single-node…"
  SERVICE_NODE="${SINGLE_NODE_HOSTNAME:-}" SERVICE_NODE_ID="${SINGLE_NODE_ID:-}"
  START_PHASE=creating
  "$REPO_DIR/serve.sh" "$NAME" -d \
    "${PLACEMENT_ARGS[@]}" \
    ${spec_flag[@]+"${spec_flag[@]}"} "${launch_flags[@]}"
  START_PHASE=loading
  state_host=""
  [ "$SINGLE_NODE_REMOTE" != 1 ] || state_host="$SINGLE_NODE_SSH_HOST"
  service_logs() {
    if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
      "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$SINGLE_NODE_SSH_HOST" "$(shell_join_q docker logs --tail "$1" "$CONTAINER")" >&2 || true
    else
      "$PULSAR_DOCKER" logs --tail "$1" "$CONTAINER" >&2 || true
    fi
  }
  wait_attempts="${WAIT_ATTEMPTS:-90}" wait_started=$SECONDS
  log "waiting for ${SERVICE_API_BASE}/health on $(single_node_display) (cold load can take minutes;" \
    "up to $wait_attempts health checks, ${WAIT_SECONDS:-5} s apart)"
  ok=0 container_state=""
  for i in $(seq 1 "$wait_attempts"); do
    if curl -fsS --max-time 3 "${api_auth_args[@]}" "${SERVICE_API_BASE}/health" >/dev/null 2>&1; then
      ok=1
      break
    fi
    container_state=$(container_state_exact "$CONTAINER" "$state_host")
    case "$container_state" in exited|absent) load_progress_end ;; esac
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
    load_progress "$wait_started" "$i" "$wait_attempts" "container $(container_state_word "$container_state")"
    sleep "${WAIT_SECONDS:-5}"
  done
  load_progress_end
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
START_PHASE=testing
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
START_PHASE=ready
head -c 400 "$smoke_body"; echo
rm -f "$smoke_body" "$smoke_body.err"
# The menu's Connect card reads this (scripts/connect_card.py).
if [ -n "${PULSAR_SERVICE_RESULT_FILE:-}" ]; then
  python3 -c 'import json,sys; plan=json.load(open(sys.argv[1]))
open(sys.argv[2],"w").write(json.dumps(dict(state="running",service_id=plan["service_id"],api_url=sys.argv[3],
    served_name=plan.get("served_name"),api_auth=plan.get("api_auth"))))' \
    "$PLAN_FILE" "$PULSAR_SERVICE_RESULT_FILE" "$SERVICE_API_BASE" || true
fi

echo
status_line "READY"
cat <<EOF
  spec:     $NAME
  served:   $SERVED_NAME
  url:      ${SERVICE_API_BASE}/v1
  inspect:  ./pulsar inventory
  status:   ./pulsar status $SUGGEST_SPEC ${SUGGEST_ARGS[*]:-}
  stop:     ./pulsar stop $SUGGEST_SPEC ${SUGGEST_ARGS[*]:-}
  security: do not expose :${PORT} without authentication
EOF
