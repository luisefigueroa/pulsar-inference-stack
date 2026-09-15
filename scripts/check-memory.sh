#!/usr/bin/env bash
# Memory preflight for conf (+ optional max-model-len note).
#   scripts/check-memory.sh <model-name> [--node NODE_ID] [--cold-start] [--max-model-len N] [--json]
# exit 0=pass 1=fail 2=warn (tight)
#
# Cold start: require MemAvailable >= footprint + launch spike, residual buffer.
# Already serving this conf: only enforce hard floor + residual buffer (weights/KV
# are already resident — free RAM is OS headroom, not cold capacity).
set -euo pipefail
# shellcheck disable=SC2034  # read by lib.sh log/warn/die
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then echo "Usage: check-memory.sh SPEC [--spec-file FILE] [--memory-estimate-file FILE] [--memory-estimate-id ID] [--node NODE] [--cold-start] [--json]"; exit 0; fi
SCRIPT_NAME=check-memory
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

JSON=0
OVERRIDE_LEN=""
NODE_SELECTOR=""
FORCE_COLD_START=0
unset PULSAR_MEMORY_ESTIMATE_JSON
MEMORY_ESTIMATE_FILE="" MEMORY_ESTIMATE_FROZEN="" MEMORY_ESTIMATE_ID=""
NAME="${1:-}"
[ -n "$NAME" ] || die "usage: $0 <model-name> [--node NODE_ID] [--cold-start] [--max-model-len N] [--json]"
shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --spec-file) [ $# -ge 2 ] || die "--spec-file needs a file" 2; export PULSAR_SPEC_FILE="$2"; shift ;;
    --memory-estimate-file) [ "$#" -ge 2 ] && [ -n "$2" ] || die "missing memory estimate file" 3; MEMORY_ESTIMATE_FILE="$2"; shift ;;
    --memory-estimate-id) [ "$#" -ge 2 ] && [ -n "$2" ] || die "missing memory estimate ID" 3; MEMORY_ESTIMATE_ID="$2"; shift ;;
    --memory-estimate-frozen) [ "$#" -ge 2 ] && [ -n "$2" ] || die "missing frozen memory estimate" 3; MEMORY_ESTIMATE_FROZEN="$2"; shift ;;
    --json) JSON=1 ;;
    --node)
      [ "$#" -ge 2 ] || die "--node requires a topology node id or hostname" 2
      NODE_SELECTOR="$2"
      shift
      ;;
    --cold-start) FORCE_COLD_START=1 ;;
    --max-model-len) OVERRIDE_LEN="${2:-}"; shift ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

load_conf "$NAME"
if [ -n "$MEMORY_ESTIMATE_FILE$MEMORY_ESTIMATE_FROZEN$MEMORY_ESTIMATE_ID" ]; then
  select_memory_estimate "$MEMORY_ESTIMATE_FILE" "$MEMORY_ESTIMATE_FROZEN" "$MEMORY_ESTIMATE_ID" 3
fi
require_cluster_nodes "$NODES" >/dev/null || die "confirmed topology lacks required serving nodes"
if [ "$NODES" -eq 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" \
    || die "cannot resolve physical node placement '$NODE_SELECTOR'"
elif [ -n "$NODE_SELECTOR" ]; then
  die "--node is only valid for one-node profiles" 2
fi
weights=$(estimate_weights_ram_gib)
kv=$(estimate_kv_gib)
overhead="${OVERHEAD_GIB:-$OVERHEAD_GIB_DEFAULT}"
buffer="${MEM_MIN_FREE_GIB:-$MIN_OS_BUFFER_GIB}"
spike="${LAUNCH_SPIKE_GIB}"
floor="${HARD_FLOOR_AVAILABLE_GIB}"

if [ "$NODES" -gt 1 ]; then
  w_rank=$(awk -v w="$weights" -v n="$NODES" \
    'BEGIN{printf "%.2f", w/n}')
else
  w_rank="$weights"
fi

need_footprint=$(awk -v w="$w_rank" -v k="$kv" -v o="$overhead" \
  'BEGIN{printf "%.2f", w+k+o}')
need_start=$(awk -v f="$need_footprint" -v s="$spike" 'BEGIN{printf "%.2f", f+s}')

declare -a rank_weights=() rank_footprints=() rank_start_needs=()
if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
  estimate_rows=$(python3 - <<'PY'
import json,os
from release_spec.memory_estimate import weights_gib
v=json.loads(os.environ['PULSAR_MEMORY_ESTIMATE_JSON'])
w=weights_gib(v)
print(f'{sum(w):.2f}')
for value in w: print(f'{value:.2f}')
PY
  ) || die "cannot read frozen memory estimate" 3
  mapfile -t estimate_values <<<"$estimate_rows"
  weights="${estimate_values[0]}"
  rank_weights=("${estimate_values[@]:1}")
  [ "${#rank_weights[@]}" -eq "$NODES" ] || die "memory estimate rank coverage differs" 3
else
  for ((rank = 0; rank < NODES; rank++)); do rank_weights[$rank]="$w_rank"; done
fi
for ((rank = 0; rank < NODES; rank++)); do
  rank_footprints[$rank]=$(awk -v w="${rank_weights[$rank]}" -v k="$kv" -v o="$overhead" 'BEGIN{printf "%.2f", w+k+o}')
  rank_start_needs[$rank]=$(awk -v f="${rank_footprints[$rank]}" -v s="$spike" 'BEGIN{printf "%.2f", f+s}')
done
if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
  # Preserve scalar summary fields as maxima; decisions use each rank's value.
  w_rank=$(printf '%s\n' "${rank_weights[@]}" | sort -n | tail -n 1)
  need_footprint=$(printf '%s\n' "${rank_footprints[@]}" | sort -n | tail -n 1)
  need_start=$(printf '%s\n' "${rank_start_needs[@]}" | sort -n | tail -n 1)
fi

mml=$(parse_max_model_len)
[ -n "$OVERRIDE_LEN" ] && mml="$OVERRIDE_LEN"
kv_bytes=$(parse_kv_cache_bytes)
kv_fixed=0
[ -n "$kv_bytes" ] && kv_fixed=1

# --- already serving this model? ---
cname=$(container_name_for "$NAME" "$NODES")
already=0
already_how=""
if [ "$FORCE_COLD_START" != 1 ] \
    && profile_service_is_proven_running "$NAME" "$NODE_SELECTOR"; then
  already=1
  already_how="proven stack-managed container $cname running with complete rank ownership"
fi

declare -a rank_avail=()
if [ "$NODES" -eq 1 ] && [ "$SINGLE_NODE_REMOTE" = 1 ]; then
  rank_avail[0]=$(mem_available_gib_remote "$SINGLE_NODE_SSH_HOST")
else
  rank_avail[0]=$(mem_available_gib_local)
fi
result=pass
reason=""
mode="cold-start"
topology_ready=1
if [ "$NODES" -gt 1 ]; then
  if ! require_cluster_nodes "$NODES"; then
    topology_ready=0
    result=fail
    reason="confirmed topology has fewer than $NODES required ranks; "
    for ((rank = 1; rank < NODES; rank++)); do
      rank_avail[$rank]=0
    done
  else
    for ((rank = 1; rank < NODES; rank++)); do
      rank_avail[$rank]=$(mem_available_gib_remote \
        "${CLUSTER_NODE_SSH_HOSTS[$rank]}")
    done
  fi
fi
head_avail="${rank_avail[0]}"
worker_avail="${rank_avail[1]:-n/a}"
availability_summary=""
for ((rank = 0; rank < NODES; rank++)); do
  availability_summary+=" r${rank}=${rank_avail[$rank]}GiB"
done

check_node_cold() {
  local label="$1" avail="$2" need_footprint="$3" need_start="$4"
  if awk -v a="$avail" -v f="$floor" 'BEGIN{exit !(a+0 < f)}'; then
    result=fail
    reason="${reason}${label}: available ${avail} GiB < hard floor ${floor} GiB; "
    return
  fi
  # Apply the selected platform policy slack to the estimated footprint.
  # This estimate is operational guidance, not physical qualification.
  if awk -v a="$avail" -v f="$need_footprint" \
      -v s="${PULSAR_COLD_START_FOOTPRINT_SLACK}" \
      'BEGIN{exit !(a+0 < f*s)}'; then
    result=fail
    reason="${reason}${label}: available ${avail} GiB << footprint ${need_footprint} GiB (cannot fit); "
    return
  fi
  if awk -v a="$avail" -v n="$need_start" 'BEGIN{exit !(a+0 < n)}'; then
    if [ "$result" = pass ]; then result=warn; fi
    reason="${reason}${label}: available ${avail} GiB < ideal start ${need_start} GiB (footprint+spike); tight but may run; "
  fi
  residual=$(awk -v a="$avail" -v f="$need_footprint" 'BEGIN{printf "%.2f", a-f}')
  if awk -v r="$residual" -v b="$buffer" 'BEGIN{exit !(r+0 < b)}'; then
    if [ "$result" = pass ]; then result=warn; fi
    reason="${reason}${label}: projected residual ${residual} GiB < buffer ${buffer} GiB; "
  fi
}

# Warm: model already resident — only residual OS headroom matters.
check_node_warm() {
  local label="$1" avail="$2"
  if awk -v a="$avail" -v f="$floor" 'BEGIN{exit !(a+0 < f)}'; then
    result=fail
    reason="${reason}${label}: residual ${avail} GiB < hard floor ${floor} GiB (already-loaded geometry under pressure); "
    return
  fi
  # Warn below the preferred operating buffer while retaining the hard floor.
  if awk -v a="$avail" -v b="$buffer" 'BEGIN{exit !(a+0 < b)}'; then
    if [ "$result" = pass ]; then result=warn; fi
    reason="${reason}${label}: residual ${avail} GiB < preferred buffer ${buffer} GiB (model already loaded); "
  fi
}

if [ "$topology_ready" = 1 ]; then
  if [ "$already" = 1 ]; then
    mode="already-loaded"
    for ((rank = 0; rank < NODES; rank++)); do
      check_label="rank $rank"
      [ "$NODES" -eq 1 ] && check_label="$SINGLE_NODE_HOSTNAME"
      check_node_warm "$check_label" "${rank_avail[$rank]}"
    done
  else
    for ((rank = 0; rank < NODES; rank++)); do
      check_label="rank $rank"
      [ "$NODES" -eq 1 ] && check_label="$SINGLE_NODE_HOSTNAME"
      check_node_cold "$check_label" "${rank_avail[$rank]}" "${rank_footprints[$rank]}" "${rank_start_needs[$rank]}"
    done
  fi
fi

note=""
if [ "$kv_fixed" = 1 ]; then
  note="KV reserved via --kv-cache-memory-bytes (${kv} GiB/rank); lowering max-model-len does not free that reservation."
fi
if [ "$already" = 1 ]; then
  note="${note:+$note }Mode=already-loaded (${already_how}): free RAM is residual OS headroom, not cold-start capacity."
fi

if [ "$JSON" = 1 ]; then
  PLACEMENT_INDEX_V="${SINGLE_NODE_INDEX:-}" \
  PLACEMENT_KEY_V="${SINGLE_NODE_KEY:-}" \
  PLACEMENT_ID_V="${SINGLE_NODE_ID:-}" \
  PLACEMENT_HOSTNAME_V="${SINGLE_NODE_HOSTNAME:-}" \
  PLACEMENT_SSH_V="${SINGLE_NODE_SSH_HOST:-}" \
  PLACEMENT_REMOTE_V="${SINGLE_NODE_REMOTE:-0}" \
  python3 - "$NODES" "${rank_avail[@]}" "$NAME" "$result" "$mode" "$already" \
    "$already_how" "$need_footprint" "$need_start" "$weights" "$w_rank" "$kv" \
    "$overhead" "$buffer" "$spike" "$floor" "$mml" "$kv_fixed" "$note" "$reason" <<'PY'
import json,os,sys
n=int(sys.argv[1]);available=[float(v) for v in sys.argv[2:n+2]]
(name,result,mode,already,already_how,footprint,need,weights,w_rank,kv,overhead,buffer,spike,floor,mml,kv_fixed,note,reason)=sys.argv[n+2:]
ranks=[dict(rank=i,available_gib=value) for i,value in enumerate(available)]
placement=None
if n==1:
 placement=dict(topology_index=int(os.environ.get("PLACEMENT_INDEX_V") or 0),
  node_key=os.environ.get("PLACEMENT_KEY_V") or "head",node_id=os.environ.get("PLACEMENT_ID_V") or None,
  hostname=os.environ.get("PLACEMENT_HOSTNAME_V") or None,ssh_host=os.environ.get("PLACEMENT_SSH_V") or None,
  remote=os.environ.get("PLACEMENT_REMOTE_V")=="1")
 ranks[0].update(placement)
document=dict(schema_version=1,kind="pulsar-memory-check",model=name,result=result,mode=mode,
 already_loaded=already=="1",already_how=already_how,footprint_gib=float(footprint),need_start_gib=float(need),
 weights_gib_total=float(weights),weights_gib_per_rank=float(w_rank),kv_gib=float(kv),overhead_gib=float(overhead),
 buffer_gib=float(buffer),spike_gib=float(spike),hard_floor_gib=float(floor),head_available_gib=available[0],
 worker_available_gib=available[1] if n>1 else None,placement=placement,rank_available_gib=ranks,
 max_model_len=mml or None,kv_fixed=kv_fixed=="1",note=note,reason=reason.strip())
if os.environ.get('PULSAR_MEMORY_ESTIMATE_JSON'):
 from release_spec.memory_estimate import weights_gib
 estimate=json.loads(os.environ['PULSAR_MEMORY_ESTIMATE_JSON'])
 document.update(memory_estimate_id=estimate['estimate_id'],memory_estimate_basis=estimate['estimate']['basis'])
 for row,w in zip(ranks,weights_gib(estimate)):
  f=round(round(w,2)+float(kv)+float(overhead),2)
  row.update(weights_gib=round(w,2),footprint_gib=f,need_start_gib=round(f+float(spike),2),
             projected_residual_gib=round(row['available_gib']-f,2))
print(json.dumps(document,indent=2))
PY
else
  if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
    estimate_summary=$(python3 -c 'import json,os; d=json.loads(os.environ["PULSAR_MEMORY_ESTIMATE_JSON"]); print(d["estimate_id"][:12]+" · "+d["estimate"]["basis"])')
    print_hanging "INFO  memory estimate  " "$estimate_summary"
  fi
  if [ "${QUIET:-0}" = 1 ]; then
    case "$result" in
      pass)
        if [ "$already" = 1 ]; then
          print_hanging "PASS  memory    " \
            "already loaded · residual${availability_summary} · floor ${floor} GiB"
        else
          print_hanging "PASS  memory    " \
            "cold-start OK · free${availability_summary} · need ${need_start} GiB/rank"
        fi
        ;;
      warn)
        if [ "$already" = 1 ]; then
          print_hanging "WARN  memory    " \
            "already loaded · residual${availability_summary} · preferred ${buffer} GiB"
        else
          print_hanging "WARN  memory    " \
            "tight · free${availability_summary} · need ${need_start} GiB/rank"
        fi
        ;;
      *)
        if [ "$already" = 1 ]; then
          print_hanging "FAIL  memory    " \
            "residual${availability_summary} · hard floor ${floor} GiB"
        else
          print_hanging "FAIL  memory    " \
            "free${availability_summary} · cold-start need ${need_start} GiB/rank"
        fi
        ;;
    esac
  else
    log "$NAME result=$result mode=$mode footprint=${need_footprint} GiB/rank start_need=${need_start} (w_rank=$w_rank kv=$kv oh=$overhead +spike=$spike; buffer_target=$buffer)"
    [ "$NODES" -eq 1 ] && log "placement=$(single_node_display) · node-id=${SINGLE_NODE_ID:-standalone}"
    log "available:${availability_summary}"
    [ "$already" = 1 ] && log "already loaded: $already_how"
    [ -n "$mml" ] && log "max-model-len=${mml}${OVERRIDE_LEN:+ (override)}"
    [ -n "$note" ] && log "$note"
    [ -n "$reason" ] && warn "$reason"
    case "$result" in
      pass) ;;
      warn)
        if [ "$already" = 1 ]; then
          warn "residual headroom tight but model already serving — OK for dry-run/status; cold relaunch needs free memory first"
        else
          warn "memory is tight — start only if you accept risk (up.sh --accept-memory-warn)"
        fi
        exit 2
        ;;
      fail)
        if [ "$already" = 1 ]; then
          warn "already-loaded geometry under hard floor — risk of OOM/earlyoom; free memory or restart with smaller geometry"
        else
          warn "free memory, stop other GPU jobs, or pick a smaller model/geometry"
        fi
        exit 1
        ;;
    esac
  fi
fi

case "$result" in
  pass) exit 0 ;;
  warn) exit 2 ;;
  *) exit 1 ;;
esac
