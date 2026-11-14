#!/usr/bin/env bash
# Memory preflight for conf (+ optional max-model-len note).
#   scripts/check-memory.sh SPEC_ID [--node NODE_ID] [--cold-start] [--max-model-len N] [--json]
# Exit: 0 pass · 1 fail · 2 warn (tight) · 3 the check could not run.
#
# Cold start: require MemAvailable >= footprint + launch spike, residual buffer.
# Already serving this conf: only enforce hard floor + residual buffer (weights/KV
# are already resident — free RAM is OS headroom, not cold capacity).
set -euo pipefail
# shellcheck disable=SC2034  # read by lib.sh log/warn/die
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  echo "usage: scripts/check-memory.sh SPEC [--spec-file FILE] [--memory-estimate-file FILE] [--memory-estimate-id ID] [--node NODE] [--cold-start] [--json]"
  echo "The memory check that pulsar start runs; ./pulsar start SPEC --dry-run runs every start check."
  exit 0
fi
SCRIPT_NAME=check-memory
PLACEMENT_NODES=""
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
check_exit_convention

JSON=0
OVERRIDE_LEN=""
NODE_SELECTOR=""
FORCE_COLD_START=0
unset PULSAR_MEMORY_ESTIMATE_JSON
MEMORY_ESTIMATE_FILE="" MEMORY_ESTIMATE_FROZEN="" MEMORY_ESTIMATE_ID=""
NAME="${1:-}"
[ -n "$NAME" ] || die "the memory check needs a spec ID; ./pulsar start SPEC --dry-run runs it with every start check" 3
shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --placement-nodes) [ -n "${2:-}" ] || usage_die "--placement-nodes requires an ordered node list"; PLACEMENT_NODES="$2"; shift ;;
    --spec-file) [ $# -ge 2 ] || die "--spec-file needs a file" 3; export PULSAR_SPEC_FILE="$2"; shift ;;
    --memory-estimate-file) [ "$#" -ge 2 ] && [ -n "$2" ] || die "missing memory estimate file" 3; MEMORY_ESTIMATE_FILE="$2"; shift ;;
    --memory-estimate-id) [ "$#" -ge 2 ] && [ -n "$2" ] || die "missing memory estimate ID" 3; MEMORY_ESTIMATE_ID="$2"; shift ;;
    --memory-estimate-frozen) [ "$#" -ge 2 ] && [ -n "$2" ] || die "missing frozen memory estimate" 3; MEMORY_ESTIMATE_FROZEN="$2"; shift ;;
    --json) JSON=1 ;;
    --node)
      [ "$#" -ge 2 ] || die "--node requires a topology node id or hostname" 3
      NODE_SELECTOR="$2"
      shift
      ;;
    --cold-start) FORCE_COLD_START=1 ;;
    --max-model-len) OVERRIDE_LEN="${2:-}"; shift ;;
    *) die "unknown argument: $1" 3 ;;
  esac
  shift
done

load_conf "$NAME"
[ "$NODES" != 1 ] || [ -z "$PLACEMENT_NODES" ] || usage_die "--placement-nodes requires a multi-node spec; use --node"
if [ -n "$MEMORY_ESTIMATE_FILE$MEMORY_ESTIMATE_FROZEN$MEMORY_ESTIMATE_ID" ]; then
  select_memory_estimate "$MEMORY_ESTIMATE_FILE" "$MEMORY_ESTIMATE_FROZEN" "$MEMORY_ESTIMATE_ID" 3
fi
require_cluster_nodes "$NODES" >/dev/null || die "confirmed topology lacks required serving nodes" 3
if [ "$NODES" -eq 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" \
    || die "cannot resolve physical node placement '$NODE_SELECTOR'" 3
else
  resolve_serving_placement "$NODE_SELECTOR" "$PLACEMENT_NODES" || die "placement is not confirmed" 3
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

# An unreadable node means the check could not run, never "0 GiB available".
remote_available_gib() {
  mem_available_gib_remote "$1" || die "cannot read available memory on $1" 3
}

declare -a rank_avail=()
if [ "$NODES" -eq 1 ] && [ "$SINGLE_NODE_REMOTE" = 1 ]; then
  rank_avail[0]=$(remote_available_gib "$SINGLE_NODE_SSH_HOST")
elif [ "$NODES" -gt 1 ] && [ "${SERVING_NODE_INDEXES[0]:-0}" != 0 ]; then
  rank_avail[0]=$(remote_available_gib "${CLUSTER_NODE_SSH_HOSTS[${SERVING_NODE_INDEXES[0]}]}")
else
  rank_avail[0]=$(mem_available_gib_local)
fi
result=pass
reason=""
mode="cold-start"
topology_ready=1
if [ "$NODES" -gt 1 ]; then
  require_cluster_nodes "$NODES" >/dev/null \
    || die "confirmed topology has fewer than $NODES required nodes" 3
  for ((rank = 1; rank < NODES; rank++)); do
    index="${SERVING_NODE_INDEXES[$rank]:-$rank}"
    if [ "$index" = 0 ]; then rank_avail[$rank]=$(mem_available_gib_local); else
      rank_avail[$rank]=$(remote_available_gib "${CLUSTER_NODE_SSH_HOSTS[$index]}")
    fi
  done
fi
# The engine's startup check needs each node's total; unknown skips that check.
remote_total_gib() {
  mem_total_gib_remote "$1" || die "cannot read total memory on $1" 3
}
declare -a rank_total=()
for ((rank = 0; rank < NODES; rank++)); do
  if [ "$NODES" -eq 1 ]; then
    if [ "$SINGLE_NODE_REMOTE" = 1 ]; then rank_total[0]=$(remote_total_gib "$SINGLE_NODE_SSH_HOST")
    else rank_total[0]=$(mem_total_gib_local); fi
  else
    index="${SERVING_NODE_INDEXES[$rank]:-$rank}"
    if [ "$index" = 0 ]; then rank_total[$rank]=$(mem_total_gib_local); else
      rank_total[$rank]=$(remote_total_gib "${CLUSTER_NODE_SSH_HOSTS[$index]}")
    fi
  fi
done
# The recipe's --gpu-memory-utilization; every catalog spec states it (release_consumer.py).
utilization="${GPU_MEM_UTIL:-}"
declare -a rank_engine_need=() rank_engine_margin=() rank_engine_below_margin=()
for ((rank = 0; rank < NODES; rank++)); do
  if [ "$rank" -eq 0 ]; then rank_engine_margin[$rank]="$ENGINE_STARTUP_MARGIN_FIRST_RANK_GIB"
  else rank_engine_margin[$rank]="$ENGINE_STARTUP_MARGIN_OTHER_RANK_GIB"; fi
  rank_engine_need[$rank]=""
  rank_engine_below_margin[$rank]=0
  # vLLM v0.32.0 needs ceil(total x utilization) free at its startup, read as MemAvailable on a unified-memory GPU.
  if [ -n "$utilization" ] && [ -n "${rank_total[$rank]}" ]; then
    rank_engine_need[$rank]=$(awk -v t="${rank_total[$rank]}" -v u="$utilization" \
      'BEGIN{n=t*u; c=int(n*100); if (c < n*100) c++; printf "%.2f", c/100}')
  fi
done
head_avail="${rank_avail[0]}"
worker_avail="${rank_avail[1]:-n/a}"
# People read hostnames; a one-node spec runs on its selected placement.
memory_node_name() {
  if [ "$NODES" -eq 1 ]; then printf '%s\n' "${SINGLE_NODE_HOSTNAME:-$(human_node_name 0)}"
  else printf '%s (rank %s)\n' "$(human_node_name "${SERVING_NODE_INDEXES[$1]:-$1}")" "$1"; fi
}
availability_summary=""
for ((rank = 0; rank < NODES; rank++)); do
  availability_summary+=" $(memory_node_name "$rank")=${rank_avail[$rank]}GiB"
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

# The engine's own startup check (vLLM v0.32.0): it reads free memory after
# Start's own processes on that rank have started, and refuses below
# total x utilization. Only that certain refusal fails the check. The
# platform's margin is what those processes took in measured starts; it
# depends on the recipe, so falling inside it is information, never a warning.
engine_info=""
check_node_engine() {
  local rank="$1" label="$2" avail="$3" need="$4" margin="$5" total="$6" headroom
  [ -n "$need" ] || return 0
  if awk -v a="$avail" -v n="$need" 'BEGIN{exit !(a+0 < n)}'; then
    result=fail
    reason="${reason}${label}: available ${avail} GiB < ${need} GiB that the engine needs free at its startup (gpu-memory-utilization ${utilization} of ${total} GiB), so the engine would refuse; "
    return
  fi
  [ -n "$margin" ] || return 0
  if awk -v a="$avail" -v n="$need" -v m="$margin" 'BEGIN{exit !(a+0 < n+m)}'; then
    rank_engine_below_margin[$rank]=1
    headroom=$(awk -v a="$avail" -v n="$need" 'BEGIN{printf "%.2f", a-n}')
    engine_info="${engine_info}${label}: ${avail} GiB free is ${headroom} GiB more than the ${need} GiB the engine needs at its startup; in measured starts on this platform, Start's own processes took up to ${margin} GiB on this rank before the engine read memory, so the engine may refuse; "
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
      check_label=$(memory_node_name "$rank")
      check_node_warm "$check_label" "${rank_avail[$rank]}"
    done
  else
    for ((rank = 0; rank < NODES; rank++)); do
      check_label=$(memory_node_name "$rank")
      check_node_cold "$check_label" "${rank_avail[$rank]}" "${rank_footprints[$rank]}" "${rank_start_needs[$rank]}"
      check_node_engine "$rank" "$check_label" "${rank_avail[$rank]}" "${rank_engine_need[$rank]}" \
        "${rank_engine_margin[$rank]}" "${rank_total[$rank]}"
    done
  fi
fi

note=""
if [ "$kv_fixed" = 1 ]; then
  note="KV reserved via --kv-cache-memory-bytes (${kv} GiB/rank); lowering max-model-len does not free that reservation."
fi
if printf '%s\n' "${rank_engine_need[@]}" | grep -qx ''; then
  note="${note:+$note }A node's total memory is not known, so the engine's startup check was not judged there."
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
  ENGINE_UTILIZATION_V="$utilization" \
  ENGINE_TOTALS_V="$(IFS=,; printf '%s' "${rank_total[*]}")" \
  ENGINE_NEEDS_V="$(IFS=,; printf '%s' "${rank_engine_need[*]}")" \
  ENGINE_MARGINS_V="$(IFS=,; printf '%s' "${rank_engine_margin[*]}")" \
  ENGINE_BELOW_MARGIN_V="$(IFS=,; printf '%s' "${rank_engine_below_margin[*]}")" \
  ENGINE_INFO_V="${engine_info%; }" \
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
number=lambda v: float(v) if v else None
utilization=os.environ.get("ENGINE_UTILIZATION_V") or None
document["gpu_memory_utilization"]=float(utilization) if utilization else None
document["engine_startup_info"]=os.environ.get("ENGINE_INFO_V") or None
for row,total,need,margin,below in zip(ranks,*(os.environ.get(k,"").split(",") for k in ("ENGINE_TOTALS_V","ENGINE_NEEDS_V","ENGINE_MARGINS_V","ENGINE_BELOW_MARGIN_V"))):
 row.update(total_gib=number(total),engine_startup_need_gib=number(need),engine_startup_margin_gib=number(margin),
            engine_startup_below_margin=below=="1")
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
  engine_summary=""
  [ -z "${rank_engine_need[0]}" ] || engine_summary=" · engine needs ${rank_engine_need[0]} GiB free at its startup"
  if [ "${QUIET:-0}" = 1 ]; then
    case "$result" in
      pass)
        if [ "$already" = 1 ]; then
          print_hanging "PASS  memory    " \
            "already loaded · residual${availability_summary} · floor ${floor} GiB"
        else
          print_hanging "PASS  memory    " \
            "cold-start OK · free${availability_summary} · need ${need_start} GiB/rank${engine_summary}"
        fi
        ;;
      warn)
        if [ "$already" = 1 ]; then
          print_hanging "WARN  memory    " \
            "already loaded · residual${availability_summary} · preferred ${buffer} GiB"
        else
          print_hanging "WARN  memory    " \
            "tight · free${availability_summary} · need ${need_start} GiB/rank${engine_summary}"
        fi
        ;;
      *)
        if [ "$already" = 1 ]; then
          print_hanging "FAIL  memory    " \
            "residual${availability_summary} · hard floor ${floor} GiB"
        else
          print_hanging "FAIL  memory    " \
            "free${availability_summary} · cold-start need ${need_start} GiB/rank${engine_summary}"
        fi
        ;;
    esac
    [ -z "$engine_info" ] || print_hanging "INFO  engine    " "${engine_info%; }"
  else
    log "$NAME result=$result mode=$mode footprint=${need_footprint} GiB/rank start_need=${need_start} (w_rank=$w_rank kv=$kv oh=$overhead +spike=$spike; buffer_target=$buffer)"
    [ "$NODES" -eq 1 ] && log "placement=$(single_node_display)"
    log "available:${availability_summary}"
    [ -z "${rank_engine_need[0]}" ] || log "engine startup: needs ${rank_engine_need[0]} GiB free (gpu-memory-utilization ${utilization} of ${rank_total[0]} GiB)"
    [ -z "$engine_info" ] || log "engine startup: ${engine_info%; }"
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
          warn "memory is tight — start only if you accept risk (./pulsar start --accept-memory-warn)"
        fi
        check_result 2
        ;;
      fail)
        if [ "$already" = 1 ]; then
          warn "already-loaded geometry under hard floor — risk of OOM/earlyoom; free memory or restart with smaller geometry"
        else
          warn "free memory, stop other GPU jobs, or pick a smaller model/geometry"
        fi
        check_result 1
        ;;
    esac
  fi
fi

case "$result" in
  pass) exit 0 ;;
  warn) check_result 2 ;;
  *) check_result 1 ;;
esac
