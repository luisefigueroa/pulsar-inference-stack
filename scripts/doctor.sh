#!/usr/bin/env bash
# Read-only host diagnostics; selected-spec launch checks remain separate.
#   scripts/doctor.sh [--json]
# exit 0 when no blocking issue is found
set -euo pipefail
# shellcheck disable=SC2034  # read by lib.sh log/warn/die
SCRIPT_NAME=doctor
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --help|-h) python3 "$REPO_DIR/scripts/terminal_format.py" <<<'Usage: ./pulsar doctor [--json]'; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done

FAIL=0
WARN=0
FAIL_COUNT=0
WARN_COUNT=0
# Parallel arrays for JSON: level|id|message
CHECKS=()
# People read the checks in sections: this node, cluster membership, each
# other node by hostname, then the catalog. A section prints when the next one
# starts, so slow remote probes still show progress.
SECTION_TITLE=""
SECTION_ROWS=()
SECTIONS_SHOWN=0

# record LEVEL ID MESSAGE [TEXT]
# MESSAGE marks commands and paths with backticks for the terminal; JSON
# carries it without them. TEXT, when given, is what people read instead.
record() {
  local level="$1" id="$2" msg="$3" text="${4:-$3}"
  CHECKS+=("${level}|${id}|${msg//\`/}")
  SECTION_ROWS+=("$level" "$text")
  case "$level" in
    warn) WARN=1; WARN_COUNT=$((WARN_COUNT + 1)) ;;
    fail) FAIL=1; FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
  esac
}

section() {
  flush_section
  SECTION_TITLE="$1"
}

flush_section() {
  if [ "$JSON" = 0 ] && [ "${#SECTION_ROWS[@]}" -gt 0 ]; then
    [ "$SECTIONS_SHOWN" = 0 ] || echo
    python3 "$REPO_DIR/scripts/terminal_format.py" --checks "$SECTION_TITLE" "${SECTION_ROWS[@]}"
    SECTIONS_SHOWN=1
  fi
  SECTION_ROWS=()
}

# Human text shows a 64-character ID by its 12-character prefix.
short_ids() { sed -E 's/([0-9a-f]{12})[0-9a-f]{52}/\1…/g' <<<"$1"; }

plural() { if [ "$1" = 1 ]; then printf '1 %s' "$2"; else printf '%s %ss' "$1" "$2"; fi; }
them() { if [ "$1" = 1 ]; then printf it; else printf them; fi; }

doctor_ready_line() {
  python3 "$REPO_DIR/scripts/terminal_format.py" --status pass READY "$1"
}

section "$(hostname -s 2>/dev/null || hostname) (this node)"

arch=$(uname -m)
arch_ok=0
# shellcheck disable=SC2086 # architectures are safe identifier tokens
for expected_arch in $PULSAR_ARCHITECTURES; do
  if [ "$arch" = "$expected_arch" ]; then
    arch_ok=1
    break
  fi
done
if [ "$arch_ok" = 1 ]; then
  record ok arch "arch=$arch" "arch $arch"
else
  primary_arch="${PULSAR_ARCHITECTURES%% *}"
  record fail arch \
    "arch=$arch (selected platform requires ${primary_arch} ${PULSAR_PLATFORM_DISPLAY_NAME}); run doctor on a ${PULSAR_PLATFORM_DISPLAY_NAME} node"
fi

if [ ! -r /proc/meminfo ]; then
  record warn host "not a Linux serve host (/proc/meminfo missing) — doctor is informational here"
fi

if command -v "${PULSAR_NVIDIA_SMI:-nvidia-smi}" >/dev/null 2>&1; then
  gpu=$("${PULSAR_NVIDIA_SMI:-nvidia-smi}" --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | sed 's/^ *//' || true)
  if [ "$gpu" = "$PULSAR_GPU_NAME" ]; then
    record ok gpu "GPU $gpu"
  else
    record fail gpu "GPU '$gpu' (want ${PULSAR_GPU_NAME}); this host does not match the selected platform"
  fi
else
  record fail gpu "nvidia-smi not found; install the NVIDIA driver utilities (\`docs/OPERATIONS.md#installation-prerequisites\`)"
fi

if command -v "$PULSAR_DOCKER" >/dev/null 2>&1; then
  if docker_info=$("$PULSAR_DOCKER" info 2>/dev/null); then
    record ok docker "docker present; daemon reachable" "Docker reachable"
    runtimes=$("$PULSAR_DOCKER" info -f '{{range $k,$v := .Runtimes}}{{$k}} {{end}}' 2>/dev/null || true)
    if echo " $runtimes " | grep -Eq '[[:space:]]nvidia[[:space:]]'; then
      record ok docker_nvidia "docker nvidia runtime registered (runtimes: $runtimes)" \
        "Docker NVIDIA runtime registered (runtimes: ${runtimes% })"
    elif printf '%s' "$docker_info" | grep -q 'nvidia.com/gpu'; then
      record ok docker_nvidia "docker nvidia CDI devices present" "Docker NVIDIA CDI devices present"
    elif command -v nvidia-container-runtime >/dev/null 2>&1 \
      || command -v nvidia-container-cli >/dev/null 2>&1; then
      record warn docker_nvidia "nvidia container tools installed but runtime is not listed in docker info; inspect the Docker NVIDIA configuration"
    else
      record fail docker_nvidia "docker nvidia runtime missing (expected Runtimes includes nvidia); install and configure the NVIDIA Container Toolkit (\`docs/OPERATIONS.md#installation-prerequisites\`)"
    fi
  else
    record fail docker "docker present but daemon unavailable (start/fix Docker, then retry)"
  fi
else
  record fail docker "docker not found; install Docker with NVIDIA GPU access (\`docs/OPERATIONS.md#installation-prerequisites\`)"
fi

port="${PORT:-8000}"
# Identify port owner: published ports, then container carrying Pulsar labelss
# (labels + /v1/models). Unknown ownership stays a blocking-style warn (read-only).
port_owner_msg=""
port_owner_text=""
if command -v ss >/dev/null 2>&1; then
  port_listening=0
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${port}\$"; then
    port_listening=1
  fi
elif command -v lsof >/dev/null 2>&1; then
  port_listening=0
  if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    port_listening=1
  fi
else
  port_listening=-1
fi

if [ "$port_listening" = 1 ]; then
  owner=$("$PULSAR_DOCKER" ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
    | grep -E ":${port}->|0.0.0.0:${port}" | head -1 || true)
  if [ -n "$owner" ]; then
    port_owner_msg="port $port in use by container: $owner (expected if a model is already up — do not up another on same port)"
    port_owner_text="port $port in use by container $(short_ids "$owner"); another service cannot use this port"
  else
    # Host-network managed containers do not show published ports. Prefer labels.
    managed_hit=""
    while IFS= read -r cname; do
      [ -n "$cname" ] || continue
      meta=$("$PULSAR_DOCKER" inspect --format \
        '{{index .Config.Labels "io.pulsar.gb10.managed"}} {{index .Config.Labels "io.pulsar.gb10.conf"}} {{.HostConfig.NetworkMode}}' \
        "$cname" 2>/dev/null || true)
      # shellcheck disable=SC2086
      set -- $meta
      mflag="${1:-}" conf_l="${2:-}" net="${3:-}"
      if [ "$mflag" = "true" ] && [ -n "$conf_l" ]; then
        if [ "$net" = "host" ] || [ "$net" = "default" ]; then
          managed_hit="$cname conf label=$conf_l net=$net"
          managed_name="$cname"
          break
        fi
      fi
    done < <("$PULSAR_DOCKER" ps --format '{{.Names}}' 2>/dev/null || true)

    api_ids=""
    api_auth_args=()
    api_auth_curl_args api_auth_args
    if api_json=$(curl -fsS --max-time 2 "${api_auth_args[@]}" "http://127.0.0.1:${port}/v1/models" 2>/dev/null); then
      api_ids=$(printf '%s' "$api_json" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(",".join(x.get("id","") for x in d.get("data",[])))' \
        2>/dev/null || true)
    fi

    if [ -n "$managed_hit" ] && [ -n "$api_ids" ]; then
      port_owner_msg="port $port in use by container carrying Pulsar labels ($managed_hit; API models=$api_ids)"
      port_owner_text="port $port in use by Pulsar container $(short_ids "$managed_name"), serving $api_ids; another service cannot use this port"
    elif [ -n "$managed_hit" ]; then
      port_owner_msg="port $port in use by container carrying Pulsar labels ($managed_hit; API not confirmed)"
      port_owner_text="port $port in use by Pulsar container $(short_ids "$managed_name"); its API did not answer"
    elif [ -n "$api_ids" ]; then
      port_owner_msg="port $port listening with OpenAI API models=$api_ids (ownership not proven via stack labels — inventory before replace)"
      port_owner_text="port $port serves $api_ids, but no Pulsar container owns it; check \`./pulsar inventory\` before replacing anything"
    else
      port_owner_msg="port $port already listening — owner unknown (not a proven stack-managed service); identify before up; wizard will not stop unknown owners"
      port_owner_text="port $port is in use by an unknown owner; identify it before starting a service, Pulsar never stops it"
    fi
  fi
  record warn port "$port_owner_msg" "$port_owner_text"
elif [ "$port_listening" = 0 ]; then
  record ok port "port $port free"
else
  record warn port "cannot probe port $port (no ss/lsof)"
fi

for storage_path in "${PULSAR_HOME_ROOT:-$HOME/.cache/pulsar-inference-stack}" "$PULSAR_HOT_ROOT"; do
  if [ -d "$storage_path" ] && [ -r "$storage_path" ] && [ -w "$storage_path" ] && [ -x "$storage_path" ]; then
    record ok model_storage "Managed storage directory accessible: \`$storage_path\`"
  elif [ -e "$storage_path" ]; then
    record fail model_storage "Managed storage path is not an accessible directory: \`$storage_path\`; fix its ownership or permissions"
  else
    record warn model_storage "Managed storage directory does not exist yet: \`$storage_path\`"
  fi
done
if [ -n "${VLLM_API_KEY:-${API_KEY:-}}" ]; then
  record ok api_auth "API key authentication is configured"
else
  record warn api_auth "API key authentication is not configured"
fi

if [ -n "${PULSAR_COLD_ROOT+x}" ]; then
  if [ -z "${PULSAR_COLD_ROOT}" ]; then
    record ok cold_storage "archives are disabled"
  elif [ ! -d "${PULSAR_COLD_ROOT}" ]; then
    record fail cold_storage "archive location is missing or not a directory; choose one with \`./pulsar configure archive-root set PATH --yes\`"
  elif [ ! -r "${PULSAR_COLD_ROOT}" ] || [ ! -x "${PULSAR_COLD_ROOT}" ]; then
    record fail cold_storage \
      "archive location \`${PULSAR_COLD_ROOT}\` is not readable and searchable; fix its permissions or choose another with \`./pulsar configure archive-root set PATH --yes\`"
  elif [ ! -w "${PULSAR_COLD_ROOT}" ]; then
    record warn cold_storage \
      "archive location is configured · current write access unavailable"
  else
    record ok cold_storage "archive location is configured"
  fi
else
  record warn cold_storage \
    "archive location is not configured; choose one with \`./pulsar configure archive-root set PATH --yes\`"
fi

avail=$(mem_available_gib_local)
if awk -v a="$avail" -v f="$HARD_FLOOR_AVAILABLE_GIB" 'BEGIN{exit !(a+0 < f)}'; then
  record fail memory "MemAvailable ${avail} GiB < hard floor ${HARD_FLOOR_AVAILABLE_GIB} GiB; free memory or stop other GPU work (\`./pulsar inventory\`)" \
    "${avail} GiB memory available, below the ${HARD_FLOOR_AVAILABLE_GIB} GiB floor; free memory or stop other GPU work (\`./pulsar inventory\`)"
else
  record ok memory "MemAvailable ${avail} GiB" "${avail} GiB memory available"
fi

section "Cluster membership"
if ! load_cluster_topology; then
  record fail topology "Confirmed topology or saved SSH identity configuration is invalid; inspect it with \`./pulsar topology show\`"
elif [ "$CLUSTER_TOPOLOGY_COUNT" -lt 1 ] || [ -z "$CLUSTER_TOPOLOGY_ID" ]; then
  record fail topology "No confirmed topology; run \`./pulsar topology setup\` to confirm cluster membership"
else
  record ok topology "Confirmed membership: $CLUSTER_TOPOLOGY_COUNT nodes"
  if [ "$CLUSTER_TOPOLOGY_COUNT" -gt 1 ] && ! require_topology_ssh_trust >/dev/null 2>&1; then
    record fail ssh_trust "Multi-node operation requires enrolled SSH identities; run \`./pulsar ssh-trust enroll\`"
  fi
  if [ "$CLUSTER_TOPOLOGY_COUNT" -gt 1 ]; then
    if local_fabric=$(probe_node_json_for_rank 0 2>/dev/null) && printf '%s' "$local_fabric" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("qualified") is True else 1)'; then
      record ok local_fabric "Local platform and active RDMA prerequisites are available"
    else
      record fail local_fabric "Local platform or active RDMA prerequisites could not be verified; run \`./pulsar topology check\`"
    fi
  fi
  for ((rank=1; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    node_name=$(human_node_name "$rank")
    section "$node_name"
    if rank_probe=$(probe_node_json_for_rank "$rank" 2>/dev/null); then
      probe_rows=$(printf '%s' "$rank_probe" | python3 -c '
import json,sys
p=json.load(sys.stdin)
if not isinstance(p,dict): raise SystemExit(1)
words={"gpu":("GPU {}","GPU {} (want "+sys.argv[1]+"); this node does not match the selected platform"),
 "docker_ok":("Docker reachable","Docker unavailable; start or install Docker on this node"),
 "docker_nvidia":("Docker NVIDIA runtime present","Docker NVIDIA runtime missing; install the NVIDIA Container Toolkit"),
 "qualified":("platform and RDMA prerequisites verified","platform or RDMA prerequisites not verified; run `./pulsar topology check`"),
 "node_id":("node identity matches saved membership","node identity differs from saved membership; run `./pulsar topology check`")}
for field,expected in (("gpu",sys.argv[1]),("docker_ok",True),("docker_nvidia",True),("qualified",True),("node_id",sys.argv[3])):
 value=p.get(field); valid=value==expected and type(value) is type(expected)
 if field=="docker_nvidia" and p.get("docker_ok") is not True:
  # The runtime query needs Docker, so its absence is not observed.
  print("warn|docker_nvidia|Docker NVIDIA runtime not observed because Docker is unavailable"); continue
 print(("ok" if valid else "fail")+"|"+field+"|"+words[field][0 if valid else 1].format(value))
arch_ok=p.get("arch") in sys.argv[2].split()
print(("ok" if arch_ok else "fail")+"|arch|arch "+str(p.get("arch"))+("" if arch_ok else " (selected platform requires "+sys.argv[2].split()[0]+"); this node does not match the selected platform"))
' "$PULSAR_GPU_NAME" "$PULSAR_ARCHITECTURES" "${CLUSTER_NODE_IDS[$rank]}") || probe_rows="fail|probe|Unreadable remote hardware probe; run \`./pulsar topology check\`"
      while IFS='|' read -r level id message; do
        record "$level" "rank_${rank}_${id}" "$node_name: $message" "$message"
      done <<<"$probe_rows"
      remote_available=$(mem_available_gib_remote "${CLUSTER_NODE_SSH_HOSTS[$rank]}") || remote_available=""
      if awk -v a="$remote_available" -v f="$HARD_FLOOR_AVAILABLE_GIB" 'BEGIN{exit !(a+0 >= f)}'; then
        record ok "rank_${rank}_memory" "$node_name: MemAvailable $remote_available GiB" "$remote_available GiB memory available"
      else
        record fail "rank_${rank}_memory" "$node_name: memory unavailable or below the hard floor; run \`./pulsar inventory\`" \
          "memory unavailable or below the hard floor; run \`./pulsar inventory\`"
      fi
    else
      record fail "rank_${rank}_probe" "$node_name cannot be inspected through its confirmed control endpoint; run \`./pulsar topology check\`" \
        "cannot be inspected through its confirmed control endpoint; run \`./pulsar topology check\`"
    fi
  done
fi

# Catalog integrity is independent of local readiness; an empty catalog is valid.
section "Catalog"
if catalog=$("$REPO_DIR/scripts/release.sh" list --json 2>/dev/null); then
  count=$(printf '%s' "$catalog" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["releases"]))')
  record ok catalog "$count catalog specs readable; use models check for selected storage readiness" \
    "$count catalog specs readable; \`./pulsar models check SPEC\` checks a spec's storage"
else
  record fail catalog "Catalog contains an unreadable or invalid spec; \`scripts/check-catalog.py\` names it"
fi

result=pass
[ "$WARN" = 1 ] && result=pass_with_warnings
[ "$FAIL" = 1 ] && result=fail

if [ "$JSON" = 1 ]; then
  python3 - "$result" "$FAIL" "$WARN" "$arch" "$avail" "$port" "${CLUSTER_TOPOLOGY_COUNT:-0}" "${CHECKS[@]}" <<'PY'
import json, os, sys
result, fail, warn, arch, avail, port, confirmed = sys.argv[1:8]
checks = []
for item in sys.argv[8:]:
    level, cid, msg = item.split("|", 2)
    checks.append({"level": level, "id": cid, "message": msg})
print(json.dumps({
    "schema_version": 1,
    "kind": "pulsar-doctor",
    "result": result,
    "fail": int(fail),
    "warn": int(warn),
    "arch": arch,
    "mem_available_gib": float(avail) if avail not in ("", "n/a") else None,
    "port": int(port) if str(port).isdigit() else port,
    "worker_confirmed": int(confirmed) >= 2,
    "platform_id": os.environ.get("PULSAR_PLATFORM_ID") or "",
    "checks": checks,
}, indent=2))
PY
else
  flush_section
  echo
  if [ "$FAIL" = 0 ]; then
    if [ "${count:-1}" = 0 ]; then
      doctor_ready_line "host checks passed; catalog is empty until a spec is published"
    elif [ "$WARN" = 1 ]; then
      doctor_ready_line "host checks passed with $(plural "$WARN_COUNT" warning); review $(them "$WARN_COUNT") and run selected-spec launch checks"
    else
      doctor_ready_line "host checks passed; selected-spec launch checks still apply"
    fi
  else
    python3 "$REPO_DIR/scripts/terminal_format.py" --status fail "NOT READY" \
      "$(plural "$FAIL_COUNT" "blocking issue") above; fix $(them "$FAIL_COUNT") before serving"
  fi
fi

[ "$FAIL" = 0 ]
