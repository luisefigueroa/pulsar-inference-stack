#!/usr/bin/env bash
# Enroll and verify topology-bound SSH identities for control and RoCE endpoints.
set -euo pipefail
SCRIPT_NAME=topology-ssh-trust
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MANIFEST_TOOL="$REPO_DIR/scripts/topology_manifest.py"
TRUST_TOOL="$REPO_DIR/scripts/topology_ssh_trust.py"
PROBE_TOOL="$REPO_DIR/scripts/probe-node.py"

usage() {
  cat <<'EOF'
usage:
  scripts/topology-ssh-trust.sh enroll [--yes] [--accept-key-change]
  scripts/topology-ssh-trust.sh check [--json]

Enroll records each confirmed node's SSH host keys in topology schema 2. The
ceremony uses the operator's existing OpenSSH trust on the exact saved control
IP, verifies machine identity, then proves the same key and node identity on
every confirmed RoCE endpoint.

A key change is refused by default. After verifying a legitimate rotation out
of band and updating normal OpenSSH known_hosts, rerun with
--accept-key-change. No option bypasses normal SSH host-key verification.
EOF
}

safe_alias() {
  case "${1:-}" in
    ""|local|-*|*[!A-Za-z0-9._+-]*) return 1 ;;
    *) return 0 ;;
  esac
}

enrollment_ssh_options() {
  ENROLLMENT_SSH_OPTS=(
    -o BatchMode=yes
    -o "ConnectTimeout=${PULSAR_SSH_CONNECT_TIMEOUT}"
    -o ConnectionAttempts=1
    -o AddressFamily=inet
    -o CanonicalizeHostname=no
    -o CheckHostIP=no
    -o ProxyCommand=none
    -o ProxyJump=none
    -o StrictHostKeyChecking=yes
    -o UpdateHostKeys=no
    -o VerifyHostKeyDNS=no
  )
}

declare -a COLLECTED_PROBE_FILES=()
TRUST_TMPDIR=""

cleanup_trust_tmpdir() {
  [ -n "$TRUST_TMPDIR" ] || return 0
  [ -d "$TRUST_TMPDIR" ] || return 0
  rm -rf -- "$TRUST_TMPDIR"
}

collect_and_check_idle() {
  local tmpdir="$1" rows kind rank node_id hostname alias control_ip _rest
  local probe_file remote_command running remote_query host_key_alias
  COLLECTED_PROBE_FILES=()

  rows=$(python3 "$MANIFEST_TOOL" rows "$CLUSTER_TOPOLOGY_FILE") \
    || die "confirmed topology is invalid"
  enrollment_ssh_options
  while IFS=$'\t' read -r kind rank node_id hostname alias control_ip _rest; do
    [ "$kind" = NODE ] || continue
    probe_file="$tmpdir/probe-rank-${rank}.json"
    if [ "$rank" = 0 ]; then
      python3 "$PROBE_TOOL" --local --ssh-host local \
        --include-ssh-host-keys >"$probe_file" \
        || die "rank 0 identity probe failed"
      if ! running=$("$PULSAR_DOCKER" ps -q \
          --filter "label=${PULSAR_MANAGED_LABEL}=true" 2>/dev/null); then
        die "cannot query rank 0 Docker before topology trust rewrite"
      fi
    else
      safe_alias "$alias" \
        || die "rank $rank SSH host must be a plain alias before enrollment"
      host_key_alias="$alias"
      remote_command="python3 - --ssh-host $(printf '%q' "$alias") --include-ssh-host-keys"
      if ! "$PULSAR_SSH" "${ENROLLMENT_SSH_OPTS[@]}" \
          -o "HostName=$control_ip" -o "HostKeyAlias=$host_key_alias" \
          -- "$alias" "$remote_command" <"$PROBE_TOOL" >"$probe_file"; then
        die "rank $rank identity probe failed on saved control IP $control_ip; verify normal OpenSSH trust first"
      fi
      printf -v remote_query 'docker ps -q --filter %q' \
        "label=${PULSAR_MANAGED_LABEL}=true"
      if ! running=$("$PULSAR_SSH" "${ENROLLMENT_SSH_OPTS[@]}" \
          -o "HostName=$control_ip" -o "HostKeyAlias=$host_key_alias" \
          -- "$alias" "$remote_query" </dev/null 2>/dev/null); then
        die "cannot query rank $rank Docker on saved control IP $control_ip"
      fi
    fi
    if [ -n "$running" ]; then
      die "rank $rank ($hostname) has running stack-managed containers; stop them before enrollment"
    fi
    COLLECTED_PROBE_FILES+=("$probe_file")
  done <<<"$rows"

  [ "${#COLLECTED_PROBE_FILES[@]}" -gt 0 ] \
    || die "confirmed topology has no nodes"
}

cmd_check() {
  local json=0
  local -a args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --json) json=1 ;;
      -h|--help) usage; return 0 ;;
      *) die "unknown check option: $1" ;;
    esac
    shift
  done
  load_cluster_topology >/dev/null || die "trusted topology/config is unavailable"
  require_topology_ssh_trust \
    || die "topology-bound SSH identity is not enrolled"
  args=(
    check
    --topology "$CLUSTER_TOPOLOGY_FILE"
    --ssh-config "$CLUSTER_SSH_CONFIG_FILE"
    --probe "$PROBE_TOOL"
    --ssh-bin "$PULSAR_SSH"
  )
  [ "$json" = 0 ] || args+=(--json)
  exec python3 "$TRUST_TOOL" "${args[@]}"
}

cmd_enroll() {
  local yes=0 accept_key_change=0 tmpdir staged_topology staged_config
  local answer
  local -a probe_files=() enroll_args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes|-y) yes=1 ;;
      --accept-key-change) accept_key_change=1 ;;
      -h|--help) usage; return 0 ;;
      *) die "unknown enroll option: $1" ;;
    esac
    shift
  done

  require_cmd python3 "$PULSAR_SSH" "$PULSAR_DOCKER"
  [ -f "$CLUSTER_TOPOLOGY_FILE" ] \
    || die "confirmed topology is missing; run scripts/detect-fabric.sh --write-topology"
  [ -r "$PROBE_TOOL" ] || die "missing node probe: $PROBE_TOOL"
  [ -x "$MANIFEST_TOOL" ] || die "missing topology helper: $MANIFEST_TOOL"
  [ -x "$TRUST_TOOL" ] || die "missing SSH trust helper: $TRUST_TOOL"

  TRUST_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-ssh-trust.XXXXXX")
  tmpdir="$TRUST_TMPDIR"
  trap cleanup_trust_tmpdir EXIT
  collect_and_check_idle "$tmpdir"
  probe_files=("${COLLECTED_PROBE_FILES[@]}")

  staged_topology="$tmpdir/topology.json"
  staged_config="$tmpdir/ssh-config"
  enroll_args=(enroll-ssh-trust)
  [ "$accept_key_change" = 0 ] \
    || enroll_args+=(--accept-key-change)
  enroll_args+=("$CLUSTER_TOPOLOGY_FILE" "${probe_files[@]}")
  python3 "$MANIFEST_TOOL" "${enroll_args[@]}" >"$staged_topology" \
    || die "SSH trust enrollment candidate was rejected"
  python3 "$MANIFEST_TOOL" render-ssh-config "$staged_topology" \
    --topology-path "$staged_topology" >"$staged_config"

  python3 "$MANIFEST_TOOL" trust-diff \
    "$CLUSTER_TOPOLOGY_FILE" "$staged_topology"
  echo
  log "verifying exact control and RoCE endpoints before write"
  python3 "$TRUST_TOOL" check \
    --topology "$staged_topology" \
    --ssh-config "$staged_config" \
    --probe "$PROBE_TOOL" \
    --ssh-bin "$PULSAR_SSH" \
    || die "one or more exact topology endpoints failed identity verification"

  echo
  print_hanging "  Effect    " \
    "Writes topology schema 2 and .cluster-ssh-config. The topology ID changes, so prior catalog/hot preparation state must be refreshed before serving."
  if [ "$yes" = 0 ]; then
    printf 'Enroll these SSH identities? [y/N] '
    read -r answer
    case "$answer" in
      y|Y|yes|YES) ;;
      *) log "not enrolled"; return 0 ;;
    esac
  fi

  python3 "$MANIFEST_TOOL" write-trust-bundle \
    "$staged_topology" "$CLUSTER_TOPOLOGY_FILE" "$CLUSTER_SSH_CONFIG_FILE"
  log "topology-bound SSH identity enrolled"
  python3 "$TRUST_TOOL" check \
    --topology "$CLUSTER_TOPOLOGY_FILE" \
    --ssh-config "$CLUSTER_SSH_CONFIG_FILE" \
    --probe "$PROBE_TOOL" \
    --ssh-bin "$PULSAR_SSH"
  log "next: refresh the model-library catalog and prepare the diagnostic model again"
}

command_name="${1:-}"
[ -n "$command_name" ] || { usage; exit 2; }
shift
case "$command_name" in
  enroll) cmd_enroll "$@" ;;
  check) cmd_check "$@" ;;
  -h|--help|help) usage ;;
  *) die "unknown command: $command_name" ;;
esac
