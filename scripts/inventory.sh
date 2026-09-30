#!/usr/bin/env bash
# Read-only managed-service + memory inventory for confirmed DGX Spark ranks.
#   scripts/inventory.sh [--json] [--verbose] [--from-fixture path]
#
# Live mode probes Docker/SSH/nvidia-smi (never mutates). Fixture mode is pure
# classification for deterministic selftests. safe_to_stop is true ONLY for
# containers with io.pulsar.gb10.managed=true and conf/rank labels that map
# consistently to a repository profile.
#
# Human output (default) is a stacked, width-aware operator view of active and
# actionable services, unmanaged GPU PIDs, and summarized inactive diagnostics.
# --verbose prints every service plus diagnostic metadata. --json always retains
# the full inventory (schema_version=1).
set -euo pipefail
SCRIPT_NAME=inventory
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
# lib.sh sets REPO_DIR in the shell but does not export it; Python helpers need it.
export REPO_DIR

JSON=0
VERBOSE=0
FROM_FIXTURE=""

usage() {
  python3 "$REPO_DIR/scripts/terminal_format.py" <<'EOF'
usage: pulsar inventory [--json] [--verbose] [--from-fixture path]

  Read-only inventory of vLLM-related containers on this node and every other
  confirmed cluster node. Reports ownership, safe_to_stop, MemAvailable, and
  best-effort GPU/unified memory via nvidia-smi + docker top (never docker stats).

  --json            machine-readable output (schema_version=1; full inventory)
  --verbose         human mode: show every inactive unknown/legacy container
                    plus IDs, sources, paths, and other diagnostic metadata
  --from-fixture    classify a pre-collected raw snapshot (no Docker/SSH/GPU)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --spec-file) [ $# -ge 2 ] || die "spec file required"; export PULSAR_SPEC_FILE="$2"; shift ;;
    --verbose) VERBOSE=1 ;;
    --from-fixture)
      FROM_FIXTURE="${2:-}"
      [ -n "$FROM_FIXTURE" ] || die "--from-fixture requires a path"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage_die "unknown argument: $1" ;;
  esac
  shift
done

# Injectable command names for tests/shims (never mutate; only query).
INVENTORY_DOCKER="${INVENTORY_DOCKER:-$PULSAR_DOCKER}"
INVENTORY_SSH="${INVENTORY_SSH:-$PULSAR_SSH}"
INVENTORY_NVIDIA_SMI="${INVENTORY_NVIDIA_SMI:-nvidia-smi}"

# ---------------------------------------------------------------------------
# Profile catalog (schema-valid specs under releases/)
# ---------------------------------------------------------------------------
build_profile_catalog_json() {
  # Pure bash catalog so we do not depend on Python seeing REPO_DIR, then
  # emit JSON via a single Python encode step.
  local spec name tmp line releases_root="${PULSAR_RELEASES_ROOT:-$REPO_DIR/releases}"
  tmp=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-profiles.XXXXXX")
  : >"$tmp"
  # Verify the catalog as a whole instead of silently omitting invalid entries.
  "$REPO_DIR/scripts/release.sh" list --json >/dev/null || { rm -f "$tmp"; return 1; }
  local -a spec_files=()
  shopt -s nullglob
  spec_files=("$releases_root"/*.json)
  [ -z "${PULSAR_SPEC_FILE:-}" ] || spec_files+=("$PULSAR_SPEC_FILE")
  for spec in "${spec_files[@]}"; do
    name="${spec##*/}"
    name="${name%.json}"
    if [ "$spec" = "${PULSAR_SPEC_FILE:-}" ]; then
      name=$(python3 -c 'from release_spec import load_spec; import sys; print(load_spec(sys.argv[1])["spec_id"])' "$spec") || return 1
    fi
    [[ "$name" =~ ^[0-9a-f]{64}$ ]] || continue
    # Subshell isolates load_conf state between profiles.
    line=$(
      set -euo pipefail
      # shellcheck disable=SC1091
      . "$REPO_DIR/scripts/lib.sh"
      export PULSAR_SPEC_FILE="$spec"
      load_conf "$name"
      w=$(estimate_weights_ram_gib)
      k=$(estimate_kv_gib)
      o="${OVERHEAD_GIB}"
      nodes="${NODES}"
      if [ "$nodes" -gt 1 ]; then
        wr=$(awk -v w="$w" -v n="$nodes" 'BEGIN{printf "%.2f", w/n}')
      else
        wr="$w"
      fi
      fp=$(awk -v w="$wr" -v k="$k" -v o="$o" 'BEGIN{printf "%.2f", w+k+o}')
      python3 - "$CONF_NAME" "$SERVED_NAME" "$NODES" "$PORT" "$fp" "$(container_name_for "$CONF_NAME" "$NODES")" <<'PYPROFILE'
import json,sys
name,served,nodes,port,footprint,container=sys.argv[1:];count=int(nodes)
print(json.dumps({name:{"served_name":served,"nodes":count,"port":int(port),
 "container_name":container,"expected_ranks":[str(i) for i in range(count)] if count>1 else ["single"],
 "estimated_footprint_gib_per_rank":float(footprint),"catalog_entry":True}}))
PYPROFILE
    ) || { rm -f "$tmp"; return 1; }
    printf '%s\n' "$line" >>"$tmp"
  done
  python3 - "$tmp" <<'PY'
import json,sys
out={}
for line in open(sys.argv[1],encoding="utf-8"):
 if line.strip():out.update(json.loads(line))
print(json.dumps(out))
PY
  rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# Remote/local probes (read-only)
# ---------------------------------------------------------------------------
_ssh_worker_raw() {
  local host="$1"
  shift
  "$INVENTORY_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$@"
}

mem_available_gib_cmd() {
  # stdout: number or empty on failure
  if [ -r /proc/meminfo ]; then
    awk '/MemAvailable:/ {printf "%.2f", $2/1048576}' /proc/meminfo 2>/dev/null || true
    return
  fi
  echo ""
}

mem_total_gib_cmd() {
  # stdout: MemTotal GiB or empty on failure (additive inventory field)
  if [ -r /proc/meminfo ]; then
    awk '/MemTotal:/ {printf "%.2f", $2/1048576}' /proc/meminfo 2>/dev/null || true
    return
  fi
  echo ""
}

collect_gpu_processes_local() {
  # CSV lines: pid,process_name,used_memory_mib  (used_memory may be empty/N/A)
  if ! command -v "$INVENTORY_NVIDIA_SMI" >/dev/null 2>&1; then
    return 0
  fi
  "$INVENTORY_NVIDIA_SMI" \
    --query-compute-apps=pid,process_name,used_gpu_memory \
    --format=csv,noheader,nounits 2>/dev/null || true
}

collect_gpu_processes_remote() {
  local host="$1"
  _ssh_worker_raw "$host" \
    "command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader,nounits 2>/dev/null || true" \
    2>/dev/null || true
}

# Inspect one node: print NDJSON container records (filtered fields only).
collect_containers_node() {
  local node="$1"  # head|worker
  local mode="$2"  # local|remote
  local host="${3:-}"

  local ids
  if [ "$mode" = local ]; then
    if ! ids=$("$INVENTORY_DOCKER" ps -aq 2>/dev/null); then
      warn "$node Docker container enumeration failed"
      return 1
    fi
  else
    if ! ids=$(_ssh_worker_raw "$host" "docker ps -aq" 2>/dev/null); then
      warn "$node Docker container enumeration failed"
      return 1
    fi
  fi
  [ -n "$ids" ] || return 0

  local id
  for id in $ids; do
    local raw pids
    if [ "$mode" = local ]; then
      raw=$("$INVENTORY_DOCKER" inspect "$id" 2>/dev/null) || return 1
      [ -n "$raw" ] || return 1
      # host PIDs via docker top (first column after header)
      pids=$("$INVENTORY_DOCKER" top "$id" -eo pid 2>/dev/null \
        | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print $1}' || true)
    else
      raw=$(_ssh_worker_raw "$host" "docker inspect $(printf '%q' "$id")" 2>/dev/null) || return 1
      [ -n "$raw" ] || return 1
      pids=$(_ssh_worker_raw "$host" \
        "docker top $(printf '%q' "$id") -eo pid 2>/dev/null | awk 'NR>1 && \$1 ~ /^[0-9]+\$/ {print \$1}' || true" \
        2>/dev/null || true)
    fi

    # Write inspect JSON to a temp file (avoid ARG_MAX; heredoc owns stdin).
    local insp
    insp=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-insp.XXXXXX")
    printf '%s' "$raw" >"$insp"
    NODE="$node" PIDS="$pids" INSP_PATH="$insp" python3 - <<'PY'
import json, os, sys

try:
    with open(os.environ["INSP_PATH"], encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
if isinstance(data, list):
    if not data:
        sys.exit(0)
    data = data[0]

labels_all = (data.get("Config") or {}).get("Labels") or {}
owned_keys = (
    "io.pulsar.gb10.managed",
    "io.pulsar.gb10.conf",
    "io.pulsar.gb10.rank",
    "io.pulsar.gb10.world-size",
    "io.pulsar.gb10.topology",
    "io.pulsar.gb10.node-id",
    "io.pulsar.gb10.weight-source",
    "io.pulsar.gb10.weight-owner",
    "io.pulsar.gb10.weight-config",
    "io.pulsar.gb10.model-revision",
    "io.pulsar.gb10.model-identity-status",
    "io.pulsar.gb10.launch-contract",
    "io.pulsar.gb10.spec-decode",
    "io.pulsar.gb10.spec-id",
    "io.pulsar.gb10.selected-spec-id",
    "io.pulsar.gb10.launch-plan",
)
labels = {k: labels_all[k] for k in owned_keys if k in labels_all and labels_all[k] is not None}

name = (data.get("Name") or "").lstrip("/")
image = (data.get("Config") or {}).get("Image") or ""
cmd = (data.get("Config") or {}).get("Cmd") or []
state = data.get("State") or {}
running = bool(state.get("Running"))
status = state.get("Status") or ("running" if running else "exited")
cid = data.get("Id") or ""

# Relevance filter: vLLM name/image/cmd or stack ownership labels.
managed = str(labels.get("io.pulsar.gb10.managed", "")).lower() == "true"
name_l = name.lower()
image_l = image.lower()
cmd_s = " ".join(str(x) for x in cmd).lower()
relevant = (
    managed
    or "vllm" in name_l
    or "vllm" in image_l
    or "vllm" in cmd_s
    or "--served-model-name" in cmd_s
    or any(str(x) == "--model" for x in cmd)
)
if not relevant:
    sys.exit(0)

pids = []
for p in os.environ.get("PIDS", "").split():
    try:
        pids.append(int(p))
    except ValueError:
        pass

rec = {
    "node": os.environ["NODE"],
    "id": cid,
    "name": name,
    "running": running,
    "status": status,
    "image": image,
    "cmd": cmd if isinstance(cmd, list) else [],
    "labels": labels,
    "host_pids": pids,
}
print(json.dumps(rec, separators=(",", ":")))
PY
    rm -f "$insp"
  done
}

parse_gpu_csv_to_json_lines() {
  local node="$1"
  # stdin: nvidia-smi csv. Must not feed stdin to `python3 <<EOF` (heredoc
  # steals stdin); buffer the pipe first.
  local csv
  csv=$(cat || true)
  NODE="$node" CSV_DATA="$csv" python3 - <<'PY'
import json, os
node = os.environ["NODE"]
for line in os.environ.get("CSV_DATA", "").splitlines():
    line = line.strip()
    if not line:
        continue
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        continue
    try:
        pid = int(parts[0])
    except ValueError:
        continue
    name = parts[1] if len(parts) > 1 else ""
    mem = None
    if len(parts) > 2:
        m = parts[2].replace("MiB", "").strip()
        if m and m.upper() != "N/A" and m != "[N/A]":
            try:
                mem = int(float(m))
            except ValueError:
                mem = None
    print(json.dumps({
        "node": node,
        "pid": pid,
        "process_name": name,
        "used_memory_mib": mem,
        "status": "ok",
    }, separators=(",", ":")))
PY
}

collect_live_snapshot() {
  local profiles_json worker_ip worker_status worker_reason topology_count
  local local_hostname
  profiles_json=$(build_profile_catalog_json) || return 1

  if ! "$INVENTORY_DOCKER" info >/dev/null 2>&1; then
    warn "Docker is unavailable on this node — inventory is incomplete; no lifecycle action is safe"
    return 1
  fi

  load_cluster_topology || { warn "confirmed topology is invalid; inventory cannot claim complete ownership"; return 1; }
  topology_count="$CLUSTER_TOPOLOGY_COUNT"
  [ "$topology_count" -gt 0 ] && [ -n "$CLUSTER_TOPOLOGY_ID" ] || { warn "confirmed topology is required for inventory"; return 1; }
  local_hostname="${CLUSTER_NODE_HOSTNAMES[0]:-}"
  if [ -z "$local_hostname" ]; then
    local_hostname=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo localhost)
  fi
  worker_ip=""
  worker_status="unset"
  worker_reason="no other nodes confirmed"
  if [ "$topology_count" -gt 1 ]; then
    worker_ip="${CLUSTER_NODE_SSH_HOSTS[1]}"
    worker_status="ok"
    worker_reason=""
  fi

  local tmp_c tmp_g tmp_n profiles_file
  tmp_c=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-c.XXXXXX")
  tmp_g=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-g.XXXXXX")
  tmp_n=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-n.XXXXXX")
  profiles_file=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-prof.XXXXXX")
  : >"$tmp_c"
  : >"$tmp_g"
  : >"$tmp_n"

  local head_mem head_total head_node_id head_ssh_host head_control_ip
  head_mem=$(mem_available_gib_cmd)
  head_total=$(mem_total_gib_cmd)
  [ -n "$head_mem" ] || head_mem=null
  [ -n "$head_total" ] || head_total=null
  head_node_id="${CLUSTER_NODE_IDS[0]:-}"
  head_ssh_host="${CLUSTER_NODE_SSH_HOSTS[0]:-local}"
  head_control_ip="${CLUSTER_NODE_CONTROL_IPS[0]:-127.0.0.1}"
  printf 'head\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t0\n' \
    "$local_hostname" "$head_mem" "$head_total" \
    "$([ "$head_mem" = null ] && echo unavailable || echo ok)" \
    "$([ "$head_mem" = null ] && echo unavailable || echo proc_meminfo)" \
    ok "" "$head_node_id" "$head_ssh_host" "$head_control_ip" >>"$tmp_n"

  if ! collect_containers_node head local >>"$tmp_c"; then
    warn "container inventory failed on this node"
    return 1
  fi
  collect_gpu_processes_local \
    | parse_gpu_csv_to_json_lines head >>"$tmp_g" || true

  if [ "$topology_count" -le 1 ]; then
    printf 'worker\t\tnull\tnull\tunset\tunset\tunset\tno other nodes confirmed\t\t\t\t\n' \
      >>"$tmp_n"
  fi

  local rank host node_name node_hostname node_label rank_mem rank_total rank_mem_status
  local rank_mem_source rank_status rank_reason node_id control_ip
  for ((rank = 1; rank < topology_count; rank++)); do
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    if [ "$rank" = 1 ]; then
      node_name=worker
    else
      node_name="rank-$rank"
    fi
    node_hostname="${CLUSTER_NODE_HOSTNAMES[$rank]:-$host}"
    node_id="${CLUSTER_NODE_IDS[$rank]:-}"
    control_ip="${CLUSTER_NODE_CONTROL_IPS[$rank]:-}"
    rank_mem=null
    rank_total=null
    rank_mem_status=unreachable
    rank_mem_source=unreachable
    rank_status=unreachable
    node_label="$node_hostname"
    rank_reason="$node_label · SSH unreachable ($host)"

    if _ssh_worker_raw "$host" true >/dev/null 2>&1; then
      rank_mem=$(_ssh_worker_raw "$host" \
        "awk '/MemAvailable:/ {printf \"%.2f\", \$2/1048576}' /proc/meminfo" \
        2>/dev/null || true)
      rank_total=$(_ssh_worker_raw "$host" \
        "awk '/MemTotal:/ {printf \"%.2f\", \$2/1048576}' /proc/meminfo" \
        2>/dev/null || true)
      if [ -n "$rank_mem" ]; then
        rank_mem_status=ok
        rank_mem_source=ssh_proc_meminfo
      else
        rank_mem=null
        rank_mem_status=unavailable
        rank_mem_source=unavailable
      fi
      [ -n "$rank_total" ] || rank_total=null
      collect_gpu_processes_remote "$host" \
        | parse_gpu_csv_to_json_lines "$node_name" >>"$tmp_g" || true

      if _ssh_worker_raw "$host" "docker info >/dev/null 2>&1"; then
        rank_status=ok
        rank_reason=""
        if ! collect_containers_node "$node_name" remote "$host" >>"$tmp_c"; then
          rank_status=docker-error
          rank_reason="$node_label · Docker container inventory failed ($host)"
        fi
      else
        rank_status=docker-error
        rank_reason="$node_label · reachable, but Docker is unavailable ($host)"
      fi
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$node_name" "$node_hostname" "$rank_mem" "$rank_total" \
      "$rank_mem_status" "$rank_mem_source" \
      "$rank_status" "$rank_reason" "$node_id" "$host" "$control_ip" "$rank" \
      >>"$tmp_n"
    if [ "$rank_status" != ok ]; then
      worker_status="$rank_status"
      worker_reason="${worker_reason:+$worker_reason; }$rank_reason"
    fi
  done

  printf '%s' "$profiles_json" >"$profiles_file"
  PROFILES_FILE="$profiles_file" \
  WORKER_IP_V="$worker_ip" \
  WORKER_STATUS="$worker_status" \
  WORKER_REASON="$worker_reason" \
  TOPOLOGY_ID_V="${CLUSTER_TOPOLOGY_ID:-}" \
  NODES_FILE="$tmp_n" \
  CONTAINERS_FILE="$tmp_c" \
  GPU_FILE="$tmp_g" \
  python3 - <<'PY_SNAPSHOT'
import json
import os


def load_ndjson(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def number(value):
    if value in ("", "null", None):
        return None
    try:
        return float(value)
    except ValueError:
        return None


with open(os.environ["PROFILES_FILE"], encoding="utf-8") as handle:
    profiles = json.load(handle)

nodes = {}
with open(os.environ["NODES_FILE"], encoding="utf-8") as handle:
    for line in handle:
        parts = line.rstrip("\n").split("\t", 11)
        while len(parts) < 12:
            parts.append("")
        (
            name,
            hostname,
            available,
            total,
            status,
            source,
            probe_status,
            probe_reason,
            node_id,
            ssh_host,
            control_ip,
            topology_index,
        ) = parts
        nodes[name] = {
            "hostname": hostname or None,
            "node_id": node_id or None,
            "ssh_host": ssh_host or None,
            "control_ip": control_ip or None,
            "topology_index": int(topology_index) if topology_index else None,
            "local": name == "head",
            "confirmed": bool(node_id) or name == "head",
            "mem_available_gib": number(available),
            "mem_total_gib": number(total),
            "mem_status": status,
            "mem_source": source,
            "probe_status": probe_status or "unknown",
            "probe_reason": probe_reason or None,
        }

print(json.dumps({
    "profiles": profiles,
    "topology_id": os.environ.get("TOPOLOGY_ID_V") or None,
    "worker_ip": os.environ.get("WORKER_IP_V") or None,
    "worker_status": os.environ["WORKER_STATUS"],
    "worker_reason": os.environ.get("WORKER_REASON") or None,
    "nodes": nodes,
    "containers": load_ndjson(os.environ["CONTAINERS_FILE"]),
    "gpu_processes": load_ndjson(os.environ["GPU_FILE"]),
}))
PY_SNAPSHOT
  rm -f "$tmp_c" "$tmp_g" "$tmp_n" "$profiles_file"
}

# ---------------------------------------------------------------------------
# Classifier (pure; fixture- and live-snapshot compatible)
# ---------------------------------------------------------------------------
classify_snapshot() {
  # SNAP_PATH env: raw snapshot JSON → stdout: inventory JSON (schema_version=1)
  # (Cannot use stdin: the Python body is a heredoc.)
  python3 - <<'PY'
import json
import os
import sys
import re
from collections import defaultdict
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MANAGED_KEY = "io.pulsar.gb10.managed"
CONF_KEY = "io.pulsar.gb10.conf"
RANK_KEY = "io.pulsar.gb10.rank"
TOPOLOGY_KEY = "io.pulsar.gb10.topology"
NODE_ID_KEY = "io.pulsar.gb10.node-id"
WEIGHT_SOURCE_KEY = "io.pulsar.gb10.weight-source"
WEIGHT_OWNER_KEY = "io.pulsar.gb10.weight-owner"
WEIGHT_CONFIG_KEY = "io.pulsar.gb10.weight-config"
MODEL_REVISION_KEY = "io.pulsar.gb10.model-revision"
WORLD_SIZE_KEY = "io.pulsar.gb10.world-size"
MODEL_IDENTITY_STATUS_KEY = "io.pulsar.gb10.model-identity-status"
LAUNCH_CONTRACT_KEY = "io.pulsar.gb10.launch-contract"
SPEC_DECODE_KEY = "io.pulsar.gb10.spec-decode"
SPEC_KEY = "io.pulsar.gb10.spec-id"
SELECTED_SPEC_KEY = "io.pulsar.gb10.selected-spec-id"
PLAN_KEY = "io.pulsar.gb10.launch-plan"

with open(os.environ["SNAP_PATH"], encoding="utf-8") as _sf:
    snap = json.load(_sf)
profiles = snap.get("profiles") or {}
containers = snap.get("containers") or []
gpu_procs = snap.get("gpu_processes") or []
nodes_info = snap.get("nodes") or {}
topology_id = snap.get("topology_id")
node_name_by_id = {
    str(info.get("node_id")): name
    for name, info in nodes_info.items()
    if isinstance(info, dict) and info.get("node_id")
}
worker_status = snap.get("worker_status") or "unset"
worker_reason = snap.get("worker_reason")
worker_ip = snap.get("worker_ip")

# --- helpers ---------------------------------------------------------------

def cmd_value(cmd, flag):
    if not isinstance(cmd, list):
        return None
    try:
        i = cmd.index(flag)
        return cmd[i + 1]
    except (ValueError, IndexError):
        return None


def short_id(cid):
    if not cid:
        return ""
    c = cid[7:] if cid.startswith("sha256:") else cid
    return c[:12]


def filter_labels(labels):
    labels = labels or {}
    out = {}
    for k in (
        MANAGED_KEY,
        CONF_KEY,
        RANK_KEY,
        TOPOLOGY_KEY,
        NODE_ID_KEY,
        WEIGHT_SOURCE_KEY,
        WEIGHT_OWNER_KEY,
        WEIGHT_CONFIG_KEY,
        MODEL_REVISION_KEY,
        WORLD_SIZE_KEY,
        MODEL_IDENTITY_STATUS_KEY,
        LAUNCH_CONTRACT_KEY,
        SPEC_DECODE_KEY,
        SPEC_KEY, SELECTED_SPEC_KEY, PLAN_KEY,
    ):
        if k in labels and labels[k] is not None:
            out[k] = str(labels[k])
    return out


def is_managed(labels):
    return str((labels or {}).get(MANAGED_KEY, "")).lower() == "true"


def looks_like_vllm(c):
    name = (c.get("name") or "").lower()
    image = (c.get("image") or "").lower()
    cmd = c.get("cmd") or []
    cmd_s = " ".join(str(x) for x in cmd).lower()
    return (
        "vllm" in name
        or "vllm" in image
        or "vllm" in cmd_s
        or "--served-model-name" in cmd_s
        or any(str(x) == "--model" for x in cmd)
    )


def rank_valid_for_profile(rank, profile):
    if not profile:
        return False
    expected = profile.get("expected_ranks") or []
    return rank in expected


def expected_node_for_rank(rank, profile, labels=None):
    """Resolve exact physical placement while preserving canonical multi-rank roles."""
    if not profile or rank is None or rank == "":
        return None
    nodes = int(profile.get("nodes") or 1)
    if nodes == 1:
        if rank != "single":
            return None
        node_id = str((labels or {}).get(NODE_ID_KEY) or "")
        # Compatibility: old managed one-node containers had no node-id and
        # were local-only. A remote one-node container must prove its identity.
        return node_name_by_id.get(node_id) if node_id else "head"
    try:
        rank_i = int(rank)
    except (TypeError, ValueError):
        return None
    if rank_i == 0:
        return "head"
    if rank_i == 1 and nodes > 1:
        return "worker"
    if 1 < rank_i < nodes:
        return f"rank-{rank_i}"
    return None


def remote_probe_states(profile, ranks=None):
    """Probe states for only the physical nodes required by this service."""
    states = []
    nodes = int((profile or {}).get("nodes") or 1)
    targets = []
    if nodes == 1:
        for item in ranks or []:
            node = item.get("_expected_node")
            if node and node not in targets:
                targets.append(node)
        rank_nodes = [("single", node) for node in targets]
    else:
        rank_nodes = [
            (rank, expected_node_for_rank(rank, profile))
            for rank in (profile or {}).get("expected_ranks") or []
        ]

    for rank, node in rank_nodes:
        if not node or node == "head":
            continue
        info = nodes_info.get(node) or {}
        if "probe_status" in info:
            status = info.get("probe_status") or "unknown"
            reason = info.get("probe_reason")
        else:
            # schema_version=1 fixture/consumer compatibility
            status = worker_status
            reason = worker_reason
        states.append({
            "rank": rank,
            "node": node,
            "status": status,
            "reason": reason,
        })
    return states


def classify_container(c):
    """Return ownership, safe_to_stop, conf guess, rank, reasons, profile ref."""
    labels = filter_labels(c.get("labels"))
    cmd = c.get("cmd") or []
    name = c.get("name") or ""
    node = c.get("node") or "head"
    reasons = []
    conf = None
    rank = None
    profile = None

    if is_managed(labels):
        conf = labels.get(CONF_KEY) or ""
        rank = labels.get(RANK_KEY) or ""
        profile = profiles.get(conf)
        if not conf:
            reasons.append("managed label set but conf label empty")
            return "mismatch", False, conf or None, rank or None, reasons, None
        if profile is None:
            # Candidate specs remain stoppable from exact ownership labels even
            # when their private file is not loaded into this operator checkout.
            try:
                count = int(labels.get(WORLD_SIZE_KEY) or "")
            except ValueError:
                count = 0
            if re.fullmatch(r"[0-9a-f]{64}", conf) is None or not 1 <= count <= len(node_name_by_id):
                reasons.append("managed candidate lacks usable exact spec/world-size labels")
                return "mismatch", False, conf, rank or None, reasons, None
            profile = dict(nodes=count, expected_ranks=["single"] if count==1 else [str(i) for i in range(count)],
                           served_name=cmd_value(cmd,"--served-model-name"),port=cmd_value(cmd,"--port"),
                           container_name=None,catalog_entry=False)
            profiles[conf]=profile
            reasons.append("candidate is outside the released catalog; recipe was not verified by this inventory")
        if not rank:
            reasons.append("managed conf known but rank label empty")
            return "mismatch", False, conf, None, reasons, profile
        if not rank_valid_for_profile(rank, profile):
            reasons.append(
                f"rank '{rank}' inconsistent with spec {str(conf)[:12]} "
                f"(expected {','.join(profile.get('expected_ranks') or [])})"
            )
            return "mismatch", False, conf, rank, reasons, profile
        want_node = expected_node_for_rank(rank, profile, labels)
        profile_nodes = int(profile.get("nodes") or 1)
        node_id = str(labels.get(NODE_ID_KEY) or "")
        observed_node_id = (nodes_info.get(node) or {}).get("node_id")
        if not topology_id or labels.get(TOPOLOGY_KEY) != topology_id or not node_id or node_id != observed_node_id:
            reasons.append("physical node or topology identity is missing or differs")
            return "mismatch", False, conf, rank, reasons, profile
        if profile_nodes == 1 and node_id and not want_node:
            reasons.append(
                f"single-node placement references unknown node-id '{node_id}'"
            )
            return "mismatch", False, conf, rank, reasons, profile
        if profile_nodes == 1 and node != "head" and not node_id:
            reasons.append(
                "remote single-node placement has no physical node-id label"
            )
            return "mismatch", False, conf, rank, reasons, profile
        if want_node and node != want_node:
            reasons.append(
                f"rank '{rank}' expected on {want_node}, observed on {node}"
            )
            return "mismatch", False, conf, rank, reasons, profile
        label_topology = str(labels.get(TOPOLOGY_KEY) or "")
        if (
            profile_nodes == 1
            and node_id
            and topology_id
            and label_topology
            and label_topology != topology_id
        ):
            reasons.append(
                "single-node placement topology label does not match "
                "the confirmed topology"
            )
            return "mismatch", False, conf, rank, reasons, profile
        # Labels + placement map consistently → rank may be safe_to_stop.
        expected_name = profile.get("container_name")
        if expected_name and name and name != expected_name:
            reasons.append(
                f"container name '{name}' differs from the spec's default '{expected_name}'"
            )
            # Name drift is a warning; labels remain authoritative for ownership.
        return "managed", True, conf, rank, reasons, profile

    # Unlabeled: never safe_to_stop. Recognize legacy vs unknown.
    conf_guess = None
    rank_guess = None
    # Name patterns used by this stack
    if name.startswith("vllm-cluster-"):
        conf_guess = name[len("vllm-cluster-") :]
        rank_guess = cmd_value(cmd, "--node-rank") or "0"
    elif name.startswith("vllm-"):
        conf_guess = name[len("vllm-") :]
        rank_guess = "single"

    served = cmd_value(cmd, "--served-model-name")
    model = cmd_value(cmd, "--model")
    node_rank = cmd_value(cmd, "--node-rank")

    if conf_guess and conf_guess in profiles:
        profile = profiles[conf_guess]
        conf = conf_guess
        rank = rank_guess if rank_guess is not None else (
            str(node_rank) if node_rank is not None else None
        )
        reasons.append("unlabeled; recognized via name/argv (legacy, not safe_to_stop)")
        return "legacy", False, conf, rank, reasons, profile

    # Argv served/model match a unique profile?
    matches = []
    for pid, p in profiles.items():
        if served and served == p.get("served_name"):
            matches.append(pid)
        elif model and served is None:
            # weak — skip unique match on model path alone
            pass
    if len(matches) == 1:
        conf = matches[0]
        profile = profiles[conf]
        rank = str(node_rank) if node_rank is not None else (
            "single" if profile.get("nodes") == 1 else None
        )
        reasons.append("unlabeled; argv matched spec (legacy, not safe_to_stop)")
        return "legacy", False, conf, rank, reasons, profile

    if looks_like_vllm(c) or is_managed(labels):
        reasons.append("vLLM-related container without consistent stack ownership")
        if served:
            conf = None
        return "unknown", False, conf, rank, reasons, None

    reasons.append("not classified")
    return "unknown", False, None, None, reasons, None


def node_mem(node):
    info = (nodes_info.get(node) or {})
    return info.get("mem_available_gib"), info.get("mem_status"), info.get("mem_source")


# GPU PID index: node -> {pid: proc}
gpu_by_node = defaultdict(dict)
for g in gpu_procs:
    node = g.get("node") or "head"
    try:
        pid = int(g["pid"])
    except (KeyError, TypeError, ValueError):
        continue
    gpu_by_node[node][pid] = g

claimed_gpu = set()  # (node, pid)

def measure_gpu(c):
    node = c.get("node") or "head"
    host_pids = set()
    for p in c.get("host_pids") or []:
        try:
            host_pids.add(int(p))
        except (TypeError, ValueError):
            pass
    matched = []
    total = 0
    any_mem = False
    for pid in host_pids:
        g = gpu_by_node.get(node, {}).get(pid)
        if not g:
            continue
        matched.append(pid)
        claimed_gpu.add((node, pid))
        m = g.get("used_memory_mib")
        if m is not None:
            try:
                total += int(m)
                any_mem = True
            except (TypeError, ValueError):
                pass
    if not host_pids and not gpu_by_node.get(node):
        return {
            "measured_mib": None,
            "source": "unavailable",
            "status": "unavailable",
            "pids": [],
        }
    if not matched:
        # docker top or nvidia-smi missing correlation
        if not gpu_by_node.get(node):
            return {
                "measured_mib": None,
                "source": "unavailable",
                "status": "unavailable",
                "pids": [],
            }
        return {
            "measured_mib": None,
            "source": "nvidia-smi+docker-top",
            "status": "unmatched",
            "pids": [],
        }
    return {
        "measured_mib": total if any_mem else None,
        "source": "nvidia-smi+docker-top",
        "status": "ok" if any_mem else "partial",
        "pids": sorted(matched),
    }


# --- classify each container -----------------------------------------------
classified = []
for c in containers:
    ownership, safe, conf, rank, reasons, profile = classify_container(c)
    labels = filter_labels(c.get("labels"))
    running = bool(c.get("running"))
    stale = not running
    mem_a, mem_st, mem_src = node_mem(c.get("node") or "head")
    gpu = measure_gpu(c)
    port = None
    if profile:
        port = profile.get("port")
    elif conf and conf in profiles:
        port = profiles[conf].get("port")

    expected_node = expected_node_for_rank(rank, profile, labels)
    rec = {
        "node": c.get("node") or "head",
        "_expected_node": expected_node,
        "container_name": c.get("name") or "",
        "container_id": c.get("id") or "",
        "container_id_short": short_id(c.get("id") or ""),
        "image": c.get("image") or "",
        "running": running,
        "stale": stale,
        "status": c.get("status") or ("running" if running else "exited"),
        "ownership": ownership,
        "safe_to_stop": bool(safe),
        "conf": conf,
        "rank": rank,
        "labels": labels,
        "api_port": port,
        "mem_available_gib": mem_a,
        "mem_status": mem_st,
        "mem_source": mem_src,
        "gpu_memory": gpu,
        "estimated_footprint_gib_per_rank": (
            profile.get("estimated_footprint_gib_per_rank") if profile else None
        ),
        "served_name": (profile.get("served_name") if profile else cmd_value(c.get("cmd") or [], "--served-model-name")),
        "reasons": reasons,
        "_cmd": c.get("cmd") or [],  # internal; stripped later if unused
    }
    classified.append(rec)

# --- group into services ---------------------------------------------------
# Key: managed/legacy with conf → conf; else synthetic per container
groups = defaultdict(list)
for rec in classified:
    if rec["conf"]:
        key = rec["conf"]
    else:
        key = f"unknown:{rec['node']}:{rec['container_name'] or rec['container_id_short']}"
    groups[key].append(rec)

services = []
for key, ranks_list in sorted(groups.items(), key=lambda kv: kv[0]):
    profile = profiles.get(key) if not key.startswith("unknown:") else None
    conf = key if not key.startswith("unknown:") else None
    if profile:
        expected_nodes = profile["nodes"]
        expected_ranks = list(profile["expected_ranks"])
        served = profile["served_name"]
        port = profile["port"]
        est = profile.get("estimated_footprint_gib_per_rank")
        cname = profile.get("container_name")
    else:
        # Infer from members
        expected_nodes = None
        expected_ranks = []
        served = None
        port = None
        est = None
        cname = ranks_list[0]["container_name"] if ranks_list else None
        for r in ranks_list:
            if r.get("served_name"):
                served = r["served_name"]
            if r.get("api_port"):
                port = r["api_port"]
            if r.get("estimated_footprint_gib_per_rank") is not None:
                est = r["estimated_footprint_gib_per_rank"]

    remote_states = remote_probe_states(profile, ranks_list) if profile else []
    remote_bad = [
        item for item in remote_states
        if item.get("status") not in ("ok", "unset")
    ]
    remote_unset = [
        item for item in remote_states if item.get("status") == "unset"
    ]

    observed = []
    for r in ranks_list:
        if r.get("rank") is not None and r["rank"] not in observed:
            observed.append(r["rank"])
    # stable order
    def rank_sort(x):
        if x == "single":
            return (0, 0)
        try:
            return (1, int(x))
        except (TypeError, ValueError):
            return (2, str(x))
    observed_sorted = sorted(observed, key=rank_sort)

    ownerships = {r["ownership"] for r in ranks_list}
    if ownerships == {"managed"}:
        svc_ownership = "managed"
    elif ownerships == {"legacy"}:
        svc_ownership = "legacy"
    elif ownerships == {"mismatch"}:
        svc_ownership = "mismatch"
    elif ownerships == {"unknown"}:
        svc_ownership = "unknown"
    else:
        svc_ownership = "mixed"

    # Service-level safe_to_stop: every *observed* rank is individually safe.
    # Does NOT imply a complete healthy cluster — see complete/observability.
    if not ranks_list:
        safe_to_stop = False
    elif svc_ownership not in ("managed",):
        # All-managed only; mixed/legacy/mismatch/unknown never service-safe as a unit
        # unless every member is managed+safe (mixed with one mismatch → false).
        safe_to_stop = all(
            r["ownership"] == "managed" and r["safe_to_stop"] for r in ranks_list
        )
    else:
        safe_to_stop = all(
            r["ownership"] == "managed" and r["safe_to_stop"] for r in ranks_list
        )

    reasons = []
    for r in ranks_list:
        for msg in r.get("reasons") or []:
            tagged = f"{r.get('node')}/rank={r.get('rank')}: {msg}"
            if tagged not in reasons:
                reasons.append(tagged)

    def uniform_label(label_key):
        raw = [
            str((r.get("labels") or {}).get(label_key) or "")
            for r in ranks_list
        ]
        values = {value for value in raw if value}
        missing = any(not value for value in raw)
        uniform = next(iter(values)) if len(values) == 1 and not missing else None
        return uniform, values, missing

    weight_source, weight_sources, weight_source_missing = uniform_label(
        WEIGHT_SOURCE_KEY
    )
    weight_owner, weight_owners, weight_owner_missing = uniform_label(
        WEIGHT_OWNER_KEY
    )
    weight_config, weight_configs, weight_config_missing = uniform_label(
        WEIGHT_CONFIG_KEY
    )
    launch_contract_id, launch_contracts, launch_contract_missing = uniform_label(
        LAUNCH_CONTRACT_KEY
    )
    spec_decode, spec_decode_states, spec_decode_missing = uniform_label(
        SPEC_DECODE_KEY
    )
    model_revision, model_revisions, model_revision_missing = uniform_label(
        MODEL_REVISION_KEY
    )
    model_identity_status, identity_states, identity_missing = uniform_label(
        MODEL_IDENTITY_STATUS_KEY
    )
    spec_id, spec_ids, spec_missing = uniform_label(SPEC_KEY)
    selected_id, selected_ids, selected_missing = uniform_label(SELECTED_SPEC_KEY)
    contract_fields = (
        ("launch contract", launch_contracts, launch_contract_missing, True),
        ("speculative-decode state", spec_decode_states, spec_decode_missing, True),
        ("model revision", model_revisions, model_revision_missing, False),
        ("model identity status", identity_states, identity_missing, False),
    )
    if spec_ids:
        contract_fields = (("effective spec", spec_ids, spec_missing, True),
                           ("selected spec", selected_ids, selected_missing, True),
                           *contract_fields[2:])
    if len(weight_sources) > 1:
        weight_source = "mixed"
    elif weight_source_missing and weight_sources:
        weight_source = "mixed"
    if weight_source_missing:
        reasons.append("one or more ranks lack the model-file source label")
    if len(weight_sources) > 1:
        reasons.append("ranks disagree on the model-file source label")
    if weight_source == "local-files" and (
        weight_owner_missing
        or weight_config_missing
        or len(weight_owners) != 1
        or len(weight_configs) != 1
    ):
        weight_owner = None
        weight_config = None
        reasons.append("model-file provenance labels are missing or inconsistent")
    for label, values, missing, required in contract_fields:
        if len(values) > 1:
            reasons.append(f"ranks disagree on {label}")
        elif missing and (required or values):
            reasons.append(f"one or more ranks lack {label}")

    state = "running"
    any_running = any(r["running"] for r in ranks_list)
    all_stale = all(r["stale"] for r in ranks_list)
    if all_stale:
        state = "stale"
    elif not any_running:
        state = "stopped"

    missing = []
    dup_ranks = []
    dup_nodes = []
    if profile:
        missing = [rk for rk in expected_ranks if rk not in observed_sorted]
        rank_counts = {}
        for r in ranks_list:
            rk = r.get("rank")
            if rk is None:
                continue
            rank_counts[rk] = rank_counts.get(rk, 0) + 1
        dup_ranks = sorted(
            [rk for rk, n in rank_counts.items() if n > 1],
            key=rank_sort,
        )
        node_counts = {}
        for r in ranks_list:
            n = r.get("node") or "head"
            node_counts[n] = node_counts.get(n, 0) + 1
        # Each topology role node should appear at most once for a clean cluster.
        for n, cnt in sorted(node_counts.items()):
            if cnt > 1:
                dup_nodes.append(n)

        if missing:
            state = "partial"
            reasons.append(f"missing expected rank(s): {','.join(missing)}")
        if dup_ranks:
            state = "degraded"
            reasons.append(f"duplicate rank(s): {','.join(str(x) for x in dup_ranks)}")
        if dup_nodes:
            state = "degraded"
            reasons.append(
                f"duplicate node placement(s): {','.join(dup_nodes)} "
                f"(expected one container per role node)"
            )
        if remote_bad:
            state = "degraded"
            details = "; ".join(
                f"rank {item['rank']} {item['status']}"
                + (f" ({item['reason']})" if item.get("reason") else "")
                for item in remote_bad
            )
            reasons.append(f"required remote probe failure: {details}")
        elif remote_unset:
            missing_remote = [
                item["rank"] for item in remote_unset
                if item["rank"] not in observed_sorted
            ]
            if missing_remote:
                state = "degraded"
                reasons.append(
                    "confirmed remote topology unavailable for rank(s): "
                    + ",".join(missing_remote)
                )
        if any(r["stale"] for r in ranks_list) and any(r["running"] for r in ranks_list):
            state = "degraded"
            reasons.append("mixed running/stale ranks")
        if any(r["ownership"] == "mismatch" for r in ranks_list):
            state = "degraded"
    else:
        if svc_ownership in ("legacy", "unknown", "mismatch"):
            if all_stale:
                state = "stale"
            elif any_running:
                state = "running"
        if svc_ownership == "unknown":
            reasons.append("unknown vLLM container (read-only)")

    # complete: expected topology fully observed, unique ranks, correct placement,
    # all managed — independent of safe_to_stop (stale managed can be complete).
    complete = False
    if profile and expected_ranks:
        complete = (
            not missing
            and not dup_ranks
            and not dup_nodes
            and len(ranks_list) == len(expected_ranks)
            and all(r.get("ownership") == "managed" for r in ranks_list)
            and all(r.get("safe_to_stop") for r in ranks_list)
            and all(r.get("rank") in expected_ranks for r in ranks_list)
        )
        # Exact-profile completeness depends only on its required other nodes.
        # Extra confirmed capacity may be offline without invalidating this view.
        if complete and any(item.get("status") != "ok" for item in remote_states):
            complete = False

    if not profile:
        observability = "unknown"
    elif complete and state in ("running", "stale"):
        observability = "complete"
    elif remote_unset:
        observability = "worker_unset"
    elif remote_bad:
        observability = "worker_unreachable"
    elif missing and not dup_ranks and not any(
        r["ownership"] == "mismatch" for r in ranks_list
    ):
        observability = "partial"
    else:
        observability = "degraded"

    rank_entries = []
    for r in sorted(ranks_list, key=lambda x: (x.get("node") or "", rank_sort(x.get("rank")))):
        want = r.get("_expected_node") if profile else None
        rank_entries.append({
            "rank": r.get("rank"),
            "node": r.get("node"),
            "expected_node": want,
            "container_name": r.get("container_name"),
            "container_id": r.get("container_id"),
            "container_id_short": r.get("container_id_short"),
            "image": r.get("image"),
            "running": r.get("running"),
            "stale": r.get("stale"),
            "status": r.get("status"),
            "ownership": r.get("ownership"),
            "safe_to_stop": r.get("safe_to_stop"),
            "labels": r.get("labels") or {},
            "api_port": r.get("api_port"),
            "mem_available_gib": r.get("mem_available_gib"),
            "mem_status": r.get("mem_status"),
            "mem_source": r.get("mem_source"),
            "gpu_memory": r.get("gpu_memory"),
            "estimated_footprint_gib_per_rank": r.get("estimated_footprint_gib_per_rank"),
            "reasons": r.get("reasons") or [],
        })

    # Named cleanup performs an all-confirmed-node observation barrier before
    # removing anything. Inventory must not advertise permission it lacks.
    if not topology_id or any(info.get("probe_status") != "ok" for info in nodes_info.values()
                              if info.get("confirmed") or info.get("node_id")):
        safe_to_stop = False
        reasons.append("not every confirmed node is observable for cleanup")
    services.append({
        "service_id": conf or key,
        "profile": conf if profile else None,
        "conf": conf,
        "served_name": served,
        "expected_nodes": expected_nodes,
        "expected_ranks": expected_ranks,
        "observed_ranks": observed_sorted,
        "container_name": cname,
        "state": state,
        "ownership": svc_ownership,
        # Observed ranks only; pair with complete/observability for cluster health.
        "safe_to_stop": safe_to_stop,
        "complete": complete,
        "observability": observability,
        "required_remote_probes": remote_states,
        "api_port": port,
        "weight_source": weight_source,
        "weight_owner_node_id": weight_owner,
        "weight_configuration_id": weight_config,
        "launch_contract_id": launch_contract_id,
        "selected_spec_id": selected_id,
        "spec_id": spec_id,
        "matches_selected_spec": spec_id == selected_id if spec_id and selected_id else None,
        "spec_decode": spec_decode,
        "model_revision": model_revision,
        "recipe_verified": False,
        "model_identity_status": model_identity_status,
        "estimated_footprint_gib_per_rank": est,
        "reasons": reasons,
        "ranks": rank_entries,
    })

# --- unmanaged GPU processes (read-only diagnostics) -----------------------
unmanaged = []
for node, by_pid in sorted(gpu_by_node.items()):
    for pid, g in sorted(by_pid.items()):
        if (node, pid) in claimed_gpu:
            continue
        unmanaged.append({
            "node": node,
            "pid": pid,
            "process_name": g.get("process_name") or "",
            "used_memory_mib": g.get("used_memory_mib"),
            "note": "read-only diagnostic; not stack-managed — no kill action",
        })

inv = {
    "schema_version": SCHEMA_VERSION,
    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "topology_id": topology_id,
    "worker": {
        "ip": worker_ip,
        "status": worker_status,
        "reason": worker_reason,
    },
    "nodes": nodes_info,
    "services": services,
    "unmanaged_gpu_processes": unmanaged,
}
json.dump(inv, sys.stdout, indent=2)
print()
PY
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
snap_file=""
inv_json_path=""
cleanup_snap() {
  # Must always return 0: under set -e, a failing EXIT trap makes an otherwise
  # successful main command exit nonzero (e.g. `[ -n "" ] && rm` → status 1).
  if [ -n "${snap_file:-}" ]; then
    case "$snap_file" in
      "${TMPDIR:-/tmp}"/pulsar-inv-snap.*) rm -f "$snap_file" || true ;;
    esac
  fi
  if [ -n "${inv_json_path:-}" ]; then
    rm -f "$inv_json_path" || true
  fi
  return 0
}
trap cleanup_snap EXIT

if [ -n "$FROM_FIXTURE" ]; then
  [ -f "$FROM_FIXTURE" ] || die "fixture not found: $FROM_FIXTURE"
  snap_file="$FROM_FIXTURE"
else
  snap_file=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-snap.XXXXXX")
  collect_live_snapshot >"$snap_file"
fi

export SNAP_PATH="$snap_file"
inventory=$(classify_snapshot)

if [ "$JSON" = 1 ]; then
  printf '%s\n' "$inventory"
else
  inv_json_path=$(mktemp "${TMPDIR:-/tmp}/pulsar-inv-out.XXXXXX")
  printf '%s\n' "$inventory" >"$inv_json_path"
  INV_JSON_PATH="$inv_json_path" INV_VERBOSE="$VERBOSE" python3 - <<'PY'
import json
import os
from scripts.terminal_format import TerminalWriter

with open(os.environ["INV_JSON_PATH"], encoding="utf-8") as f:
    inv = json.load(f)
verbose = os.environ.get("INV_VERBOSE", "0") == "1"
w = inv.get("worker") or {}
nodes = inv.get("nodes") or {}
term = TerminalWriter()
emit = term.emit


def field(label, value, indent=2):
    term.field(label, value, indent=indent)


def fmt_mem(n):
    if n is None:
        return "n/a"
    return f"{n:.2f} GiB"


def node_order(item):
    name = item[0]
    if name == "head":
        return (0, name)
    if name == "worker":
        return (1, name)
    if name.startswith("rank-"):
        try:
            return (int(name.split("-", 1)[1]), name)
        except ValueError:
            pass
    return (1_000_000, name)


def node_label(name):
    if name == "head":
        return "this node"
    if name == "worker":
        return "rank 1 node (hostname not recorded)"
    if name.startswith("rank-"):
        try:
            return f"rank {int(name.split('-', 1)[1])} node (hostname not recorded)"
        except ValueError:
            pass
    return name


def placement_label(node_name):
    hostname = str((nodes.get(node_name) or {}).get("hostname") or "").strip()
    if hostname:
        return f"{hostname} (this node)" if node_name == "head" else hostname
    return node_label(node_name) if node_name else "unknown node placement"


def fmt_gpu_mem(mib):
    if mib is None:
        return "n/a"
    gib = float(mib) / 1024
    if verbose:
        return f"{gib:.1f} GiB ({int(mib):,} MiB)"
    return f"{gib:.1f} GiB"


def service_active(s):
    state = s.get("state")
    if state in ("running", "partial", "degraded"):
        return True
    return any(r.get("running") for r in (s.get("ranks") or []))


def service_actionable(s):
    """Managed stale cleanup targets, label mismatches — always show in default."""
    own = s.get("ownership")
    if own == "managed" and s.get("state") in ("stale", "stopped", "partial", "degraded"):
        return True
    if own in ("mismatch", "mixed"):
        return True
    return False


def service_inactive_unknown_legacy(s):
    own = s.get("ownership")
    if own not in ("unknown", "legacy"):
        return False
    return not service_active(s)


def fmt_ranks(vals):
    if not vals:
        return "-"
    return ",".join(str(x) for x in vals)


def print_service(s):
    sid = s.get("service_id") or "?"
    state = s.get("state") or "?"
    own = s.get("ownership") or "?"
    safe = "safe_to_stop" if s.get("safe_to_stop") else "not_safe_to_stop"
    complete = "complete" if s.get("complete") else "incomplete"
    obs_y = s.get("observability") or "?"
    served = s.get("served_name") or "?"
    port = s.get("api_port")
    expected_count = len(s.get("expected_ranks") or [])
    observed_count = len(s.get("observed_ranks") or [])
    nodes_e = s.get("expected_nodes")
    fp = s.get("estimated_footprint_gib_per_rank")
    fp_s = f"{fp:.2f} GiB per node" if fp is not None else "n/a"

    emit(f"{state.upper()}  {sid}", subsequent_indent="  ")
    endpoint = f"{served} on :{port}" if port is not None else served
    field("serves", endpoint)
    field("status", f"{own} · {complete} · {safe}")
    field("nodes", f"{nodes_e} required · {observed_count}/{expected_count} observed")
    weight_source = s.get("weight_source")
    if weight_source:
        field("model files", weight_source)
    elif verbose:
        field("model files", "unlabeled legacy runtime")
    field("estimate", fp_s)
    if verbose or obs_y != "complete":
        field("observe", obs_y)

    for r in s.get("ranks") or []:
        g = r.get("gpu_memory") or {}
        gm = g.get("measured_mib")
        run = "running" if r.get("running") else ("stale" if r.get("stale") else "stopped")
        st = "safe_to_stop" if r.get("safe_to_stop") else "not_safe_to_stop"
        print()
        emit(
            placement_label(r.get("node")),
            initial_indent="  ",
            subsequent_indent="    ",
        )
        field("container", r.get("container_name") or "?", indent=4)
        if verbose:
            field("id", r.get("container_id_short") or "?", indent=4)
        field("status", f"{run} · {r.get('ownership') or '?'} · {st}", indent=4)
        field(
            "memory",
            f"GPU {fmt_gpu_mem(gm)} · host available {fmt_mem(r.get('mem_available_gib'))}",
            indent=4,
        )
        if verbose:
            source = str(g.get("source") or "?").replace("+", " + ")
            field("source", f"{g.get('status') or '?'} · {source}", indent=4)
        for reason in r.get("reasons") or []:
            field("reason", reason, indent=4)
    for reason in s.get("reasons") or []:
        field("reason", reason)


print("INVENTORY")
if verbose:
    field("Schema", f"version {inv.get('schema_version')}", indent=0)
    field("Generated", inv.get("generated_at") or "?", indent=0)

ws = w.get("status")
wr = w.get("reason") or ""
wip = w.get("ip") or ""
ordered_nodes = sorted(nodes.items(), key=node_order)
remote_nodes = [
    (name, block)
    for name, block in ordered_nodes
    if name != "head"
    and not (
        name == "worker"
        and ws == "unset"
        and block.get("mem_available_gib") is None
        and block.get("probe_status") in (None, "", "unset")
    )
]
if ws == "ok":
    noun = "node" if len(remote_nodes) == 1 else "nodes"
    remote_detail = f"{len(remote_nodes)} other cluster {noun} confirmed"
    if wip:
        remote_detail += f" · {placement_label('worker')} SSH {wip}"
    field("Nodes", f"OK · {remote_detail}", indent=0)
elif ws == "unset":
    field("Nodes", "no other nodes confirmed", indent=0)
    if wr:
        field("reason", wr)
else:
    noun = "node" if len(remote_nodes) == 1 else "nodes"
    field(
        "Nodes",
        f"{str(ws or '?').upper()} · {len(remote_nodes)} other cluster {noun} confirmed",
        indent=0,
    )
    if wr:
        field("reason", wr)

display_nodes = [
    (name, block)
    for name, block in ordered_nodes
    if name == "head" or (name, block) in remote_nodes
]
if not display_nodes:
    field("Memory", "unavailable", indent=0)
for index, (name, block) in enumerate(display_nodes):
    if name == "head":
        probe_status = block.get("mem_status")
    else:
        probe_status = (
            block.get("probe_status")
            or (ws if name == "worker" else block.get("mem_status"))
        )
    status_detail = f" · {probe_status}" if probe_status else ""
    field(
        "Memory" if index == 0 else "",
        f"{placement_label(name)} · {fmt_mem(block.get('mem_available_gib'))} "
        f"available{status_detail}",
        indent=0,
    )
if verbose:
    for index, (name, block) in enumerate(display_nodes):
        field(
            "Sources" if index == 0 else "",
            f"{placement_label(name)} · {block.get('mem_source') or '?'}",
            indent=0,
        )

services = inv.get("services") or []
shown = []
inactive_diag = []
for s in services:
    if verbose or service_active(s) or service_actionable(s):
        shown.append(s)
    elif service_inactive_unknown_legacy(s):
        inactive_diag.append(s)
    else:
        # e.g. unexpected stopped states — include when not clearly inactive diag
        shown.append(s)

print()
if not services:
    print("SERVICES  none")
else:
    print(f"SERVICES  {len(shown)} shown / {len(services)} total")
    if not shown and not inactive_diag:
        emit("none", initial_indent="  ")
    for s in shown:
        print()
        print_service(s)
    if inactive_diag and not verbose:
        print()
        print(f"OTHER CONTAINERS  {len(inactive_diag)} inactive/legacy")
        emit(
            "Hidden by default; not safe_to_stop. Use --verbose.",
            initial_indent="  ",
            subsequent_indent="  ",
        )

unmanaged = inv.get("unmanaged_gpu_processes") or []
print()
if unmanaged:
    measured = [u.get("used_memory_mib") for u in unmanaged]
    measured = [m for m in measured if isinstance(m, (int, float))]
    aggregate = f" · {int(sum(measured)):,} MiB" if measured else ""
    noun = "process" if len(unmanaged) == 1 else "processes"
    print(f"UNMANAGED GPU  {len(unmanaged)} {noun}{aggregate}")
    emit(
        "Read-only; Pulsar will not stop these processes.",
        initial_indent="  ",
        subsequent_indent="  ",
    )
    for u in unmanaged:
        mem = u.get("used_memory_mib")
        mem_s = f"{int(mem):,} MiB" if isinstance(mem, (int, float)) else "n/a"
        process_path = str(u.get("process_name") or "?")
        process_name = os.path.basename(process_path.rstrip("/")) or process_path
        emit(
            f"{u.get('node') or '?'} · PID {u.get('pid') or '?'} · {process_name} · {mem_s}",
            initial_indent="  ",
            subsequent_indent="    ",
        )
        if verbose and process_path != process_name:
            field("path", process_path, indent=4)
else:
    print("UNMANAGED GPU  none observed")
PY
fi
