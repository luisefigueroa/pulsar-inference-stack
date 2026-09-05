#!/usr/bin/env bash
# Discover hostname-agnostic GB10 peers, verify their RoCE mesh, and optionally
# persist the user-confirmed membership in .cluster-topology.json.
set -euo pipefail
# shellcheck disable=SC2034  # read by lib.sh log/warn/die
SCRIPT_NAME=detect-fabric
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

JSON=0
WRITE=0
YES=0
ACCEPT_NEW=0
declare -a CLI_CANDIDATES=()

usage() {
  cat <<'EOF'
usage: scripts/detect-fabric.sh [options]

Discover SSH services with mDNS, then retain only nodes that independently
prove aarch64 + NVIDIA GB10 + Docker NVIDIA + active addressed RDMA links.
Hostnames are descriptive only; cluster identity comes from each machine ID.

  --json                    emit the discovery document as JSON
  --write-topology          confirm and atomically write .cluster-topology.json
  --write-env               deprecated alias for --write-topology
  --candidate HOST          probe an additional hostname or IP (repeatable)
  --yes, -y                 skip membership confirmation (automation)
  --accept-new-host-keys    explicitly trust a new SSH host key during discovery
  -h, --help                show this help

Candidate sources are combined and de-duplicated:
  * this node (always included)
  * Avahi/mDNS _ssh._tcp IPv4 advertisements
  * CLUSTER_CANDIDATES (comma- or whitespace-separated)
  * --candidate HOST
  * nodes from an existing confirmed topology

SSH checks are non-interactive and use saved host keys by default. Discovery
never chooses model geometry; each profile remains an exact configured deployment.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --write-topology) WRITE=1 ;;
    --write-env)
      WRITE=1
      [ "$JSON" = 1 ] || warn "--write-env is now an alias for --write-topology; per-node fabric is stored in .cluster-topology.json"
      ;;
    --candidate)
      [ -n "${2:-}" ] || die "--candidate requires a hostname or IP"
      CLI_CANDIDATES+=("$2")
      shift
      ;;
    --yes|-y) YES=1 ;;
    --accept-new-host-keys) ACCEPT_NEW=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

if [ "$JSON" = 1 ] && [ "$WRITE" = 1 ]; then
  die "--json and --write-topology are separate operations"
fi

require_cmd python3
PROBE="$REPO_DIR/scripts/probe-node.py"
MANIFEST_TOOL="$REPO_DIR/scripts/topology_manifest.py"
[ -r "$PROBE" ] || die "missing node probe: $PROBE"
[ -x "$MANIFEST_TOOL" ] || die "missing topology helper: $MANIFEST_TOOL"

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-discovery.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT
candidates_file="$tmpdir/candidates"
: >"$candidates_file"

safe_candidate() {
  case "${1:-}" in
    ""|-*|*[!A-Za-z0-9._:@%+-]*) return 1 ;;
    *) return 0 ;;
  esac
}

add_candidate() {
  local candidate="${1:-}"
  [ -n "$candidate" ] || return 0
  if safe_candidate "$candidate"; then
    printf '%s\n' "$candidate" >>"$candidates_file"
  else
    warn "skip unsafe SSH candidate '$candidate'"
  fi
}

for candidate in "${CLI_CANDIDATES[@]}"; do
  add_candidate "$candidate"
done

if [ -n "${CLUSTER_CANDIDATES:-}" ]; then
  candidate_words=${CLUSTER_CANDIDATES//,/ }
  # shellcheck disable=SC2086 # intentional word splitting for documented list
  for candidate in $candidate_words; do
    add_candidate "$candidate"
  done
fi

if command -v avahi-browse >/dev/null 2>&1; then
  while IFS=$'\t' read -r mdns_host mdns_ip; do
    add_candidate "$mdns_host"
    add_candidate "$mdns_ip"
  done < <(
    avahi-browse --resolve --terminate --parsable _ssh._tcp 2>/dev/null \
      | awk -F';' '$1 == "=" && $3 == "IPv4" {print $7 "\t" $8}' \
      || true
  )
elif [ "$JSON" != 1 ]; then
  warn "avahi-browse unavailable — probing explicit/existing candidates only"
fi

# Existing confirmed nodes remain candidates, so regeneration preserves ranks
# and does not depend on their naming convention.
if [ -f "$CLUSTER_TOPOLOGY_FILE" ]; then
  while IFS=$'\t' read -r kind _rank _id _hostname ssh_host _rest; do
    [ "$kind" = NODE ] || continue
    [ "$ssh_host" = local ] || add_candidate "$ssh_host"
  done < <("$MANIFEST_TOOL" rows "$CLUSTER_TOPOLOGY_FILE" 2>/dev/null || true)
fi
sort -u "$candidates_file" -o "$candidates_file"

local_probe="$tmpdir/probe-0000.json"
local_probe_flags=()
append_probe_node_platform_args local_probe_flags
python3 "$PROBE" --local --ssh-host local "${local_probe_flags[@]}" \
  >"$local_probe"
declare -a probe_files=("$local_probe")
declare -a probe_pids=()
declare -a probe_outputs=()
declare -a probe_hosts=()

ssh_opts=("${PULSAR_SSH_OPTS[@]}")
if [ "$ACCEPT_NEW" = 1 ] || [ "${DETECT_FABRIC_ACCEPT_NEW:-0}" = 1 ]; then
  ssh_opts+=(-o StrictHostKeyChecking=accept-new)
fi

index=0
while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  index=$((index + 1))
  output=$(printf '%s/probe-%04d.json' "$tmpdir" "$index")
  error_output=$(printf '%s/probe-%04d.err' "$tmpdir" "$index")
  remote_command=$(probe_node_remote_python_command "$candidate")
  (
    "$PULSAR_SSH" "${ssh_opts[@]}" -- "$candidate" "$remote_command" \
      <"$PROBE" >"$output" 2>"$error_output"
  ) &
  probe_pids+=("$!")
  probe_outputs+=("$output")
  probe_hosts+=("$candidate")
done <"$candidates_file"

skipped_probe_count=0
for position in "${!probe_pids[@]}"; do
  if wait "${probe_pids[$position]}"; then
    if [ -s "${probe_outputs[$position]}" ]; then
      probe_files+=("${probe_outputs[$position]}")
    fi
  elif [ "$JSON" != 1 ]; then
    skipped_probe_count=$((skipped_probe_count + 1))
  fi
done
report_skipped_candidates() {
  local noun=addresses
  [ "$skipped_probe_count" -gt 0 ] || return 0
  [ "$skipped_probe_count" -ne 1 ] || noun=address
  echo "DISCOVERY NOTES"
  print_hanging "  SSH       " "$skipped_probe_count advertised SSH $noun could not be checked with saved keys."
  print_hanging "  Help      " "These are usually unrelated devices. If a GB10 is missing, set up key-based SSH and rerun with --candidate HOST."
}



assemble_args=(assemble)
if [ -f "$CLUSTER_TOPOLOGY_FILE" ]; then
  assemble_args+=(--existing "$CLUSTER_TOPOLOGY_FILE")
fi
assemble_args+=("${probe_files[@]}")
discovery="$tmpdir/discovery.json"
"$MANIFEST_TOOL" "${assemble_args[@]}" >"$discovery"

if ! "$MANIFEST_TOOL" validate "$discovery" >/dev/null 2>&1; then
  if [ "$JSON" = 1 ]; then
    cat "$discovery"
  else
    warn "no usable GB10 RoCE topology found"
    report_skipped_candidates
    python3 - "$discovery" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
for item in document.get("rejected") or []:
    reasons = "; ".join(item.get("reasons") or ["not qualified"])
    print(f"  skip {item.get('hostname') or '?'} ({item.get('ssh_host') or '?'}): {reasons}")
PY
  fi
  exit 1
fi

# Verify every advertised rail in both directions. A structurally matching
# subnet is not enough to become confirmed cluster state.
ping_plan="$tmpdir/ping-plan"
"$MANIFEST_TOOL" ping-plan "$discovery" >"$ping_plan"
connectivity_ok=1
ping_checks=0
while IFS=$'\t' read -r source_rank source_host target_rank target_ip network; do
  [ -n "$source_rank" ] || continue
  ping_checks=$((ping_checks + 1))
  source_label=$(human_cluster_node "$source_rank")
  target_label=$(human_cluster_node "$target_rank")
  if [ "$source_rank" = 0 ]; then
    if ! ping -c1 -W2 "$target_ip" >/dev/null 2>&1; then
      connectivity_ok=0
      [ "$JSON" = 1 ] || warn "$source_label cannot reach $target_label at $target_ip ($network)"
    fi
  else
    remote_ping="ping -c1 -W2 $(printf '%q' "$target_ip") >/dev/null 2>&1"
    if ! "$PULSAR_SSH" "${ssh_opts[@]}" -- "$source_host" "$remote_ping" </dev/null; then
      connectivity_ok=0
      [ "$JSON" = 1 ] || warn "$source_label cannot reach $target_label at $target_ip ($network)"
    fi
  fi
done <"$ping_plan"

if [ "$connectivity_ok" != 1 ]; then
  [ "$JSON" = 1 ] && cat "$discovery"
  [ "$JSON" = 1 ] || warn "pairwise RoCE verification failed; topology will not be written"
  exit 1
fi

verified="$tmpdir/verified.json"
"$MANIFEST_TOOL" mark-verified "$discovery" >"$verified"

if [ "$JSON" = 1 ]; then
  cat "$verified"
  exit 0
fi

echo
"$MANIFEST_TOOL" render "$verified" --skipped-ssh "$skipped_probe_count"

if [ "$WRITE" != 1 ]; then
  echo
  echo "REVIEW ONLY"
  print_hanging "  Result    " "No files changed."
  print_hanging "  Next      " "Save this membership later with scripts/detect-fabric.sh --write-topology."
  exit 0
fi

# Replacing membership while any old or proposed rank is active would make
# lifecycle ownership ambiguous. Query every rank and fail closed on probe error.
if [ -f "$CLUSTER_TOPOLOGY_FILE" ]; then
  require_topology_rewrite_idle "$CLUSTER_TOPOLOGY_FILE" \
    || die "existing cluster is not idle; stop managed services and restore access to every node"
fi
require_topology_rewrite_idle "$verified" \
  || die "proposed cluster is not idle; stop managed services and restore access to every node"

echo
"$MANIFEST_TOOL" render-save "$verified" "$CLUSTER_TOPOLOGY_FILE"

if [ "$YES" != 1 ]; then
  if [ ! -t 0 ]; then
    die "refusing write without a TTY; rerun interactively or pass --yes"
  fi
  printf 'Save this cluster membership? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *)
      log "aborted — topology not modified"
      exit 0
      ;;
  esac
fi

"$MANIFEST_TOOL" write "$verified" "$CLUSTER_TOPOLOGY_FILE"
log "wrote $CLUSTER_TOPOLOGY_FILE"
