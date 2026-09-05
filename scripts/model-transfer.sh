#!/usr/bin/env bash
# Eight-stream flat snapshot transfer over one exact confirmed RoCE rail.
# Source this file after lib.sh and model-library-common.sh.
model_transfer() (
  set -euo pipefail
  set +m  # each setsid child keeps the PID tracked below
  local source_rank="${1:?source physical rank}" source_path="${2:?source snapshot}"
  local destination_rank="${3:?destination physical rank}" destination_path="${4:?destination staging snapshot}"
  local manifest="${5:?canonical manifest JSON}" transfer_map remote_rank alias expected_alias
  local local_ip local_dev remote_ip remote_dev route shell command revision temp effective pid rc=0
  local -a children=() ssh_argv=() fields=()
  [[ "$source_rank" =~ ^[0-9]+$ ]] && [[ "$destination_rank" =~ ^[0-9]+$ ]] || die "physical ranks must be nonnegative integers"
  [ "$source_rank" != "$destination_rank" ] || die "same-node copies use the local storage copy operation"
  if [ "$source_rank" != 0 ] && [ "$destination_rank" != 0 ]; then
    model_transfer_relay "$source_rank" "$source_path" "$destination_rank" "$destination_path" "$manifest"
    return
  fi
  require_topology_ssh_trust >/dev/null || die "transfer requires enrolled confirmed SSH identities"
  transfer_map=$(python3 -m model_library.transfer map --topology "$CLUSTER_TOPOLOGY_FILE" --source-rank "$source_rank" --destination-rank "$destination_rank") || die "transfer topology is invalid"
  mapfile -t fields < <(printf '%s' "$transfer_map" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(d[k]) for k in ("remote_rank","control_ssh_host","local_ip","local_netdev","remote_ip","remote_netdev","topology_id")]')
  [ "${#fields[@]}" = 7 ] || die "transfer map is incomplete"
  remote_rank="${fields[0]}" alias="${fields[1]}" local_ip="${fields[2]}" local_dev="${fields[3]}" remote_ip="${fields[4]}" remote_dev="${fields[5]}"
  expected_alias="${CLUSTER_NODE_SSH_HOSTS[$remote_rank]:-}"
  [ -n "$expected_alias" ] && [ "$alias" = "$expected_alias" ] && [ "${fields[6]}" = "$CLUSTER_TOPOLOGY_ID" ] || die "transfer SSH identity or topology changed"
  temp=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-transfer.XXXXXX")
  cleanup_transfer() {
    local child
    for child in "${children[@]}"; do
      # Each still-unwaited child owns the session created below.
      kill -TERM -- "-$child" 2>/dev/null || true
    done
    for child in "${children[@]}"; do wait "$child" 2>/dev/null || true; done
    rm -rf -- "$temp"
  }
  trap cleanup_transfer EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  effective=$(printf '%s' "$manifest" | python3 -m model_library.transfer partition --out "$temp") || die "invalid transfer manifest"
  revision=$(printf '%s' "$manifest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["snapshot_revision"])')
  # Prove both endpoint routes using the control plane before starting bulk SSH.
  route=$("${PULSAR_IP:-ip}" -j route get "$remote_ip") || die "local RoCE route is unavailable"
  printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$remote_ip" --netdev "$local_dev" --source-ip "$local_ip" || die "local route differs from confirmed rail"
  command=$(shell_join_q ip -j route get "$local_ip")
  route=$(ssh_node "$remote_rank" "$command") || die "remote RoCE route is unavailable"
  printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$local_ip" --netdev "$remote_dev" --source-ip "$remote_ip" || die "remote route differs from confirmed rail"
  # Verify source bytes through the existing node service; no parallel schema.
  model_node "$source_rank" "$(model_json operation verify manifest: "$manifest" path "$source_path" full: true)" >/dev/null || die "transfer source does not match manifest"
  command=$(python3 -m model_library.transfer staging-code)
  if [ "$destination_rank" = 0 ]; then
    python3 -c "$command" "$destination_path" "$revision" || die "destination staging is unsafe"
  else
    command=$(shell_join_q python3 -c "$command" "$destination_path" "$revision")
    ssh_node "$destination_rank" "$command" || die "destination staging is unsafe"
  fi
  ssh_argv=("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -o "HostName=$remote_ip" -o "HostKeyAlias=$alias" -o "BindAddress=$local_ip" -o StrictHostKeyChecking=yes -o CheckHostIP=no -o UpdateHostKeys=no)
  shell=$(shell_join_q "${ssh_argv[@]}")
  # --from0 and --protect-args preserve literal filenames and remote paths.
  # No --delete: only named files enter an empty operation-owned snapshot.
  for ((stream=0; stream<effective; stream++)); do
    if [ "$source_rank" = 0 ]; then
      setsid "${PULSAR_RSYNC:-rsync}" -rt --protect-args --from0 --files-from="$temp/stream-$stream.list" --timeout=120 -e "$shell" -- "$source_path/" "$alias:$destination_path/" &
    else
      setsid "${PULSAR_RSYNC:-rsync}" -rt --protect-args --from0 --files-from="$temp/stream-$stream.list" --timeout=120 -e "$shell" -- "$alias:$source_path/" "$destination_path/" &
    fi
    children+=("$!")
  done
  while [ "${#children[@]}" -gt 0 ]; do
    pid="${children[0]}"
    wait "$pid" || rc=$?
    children=("${children[@]:1}")
    [ "$rc" = 0 ] || die "snapshot stream failed (exit=$rc); pending staging retained" "$rc"
  done
  model_node "$destination_rank" "$(model_json operation verify manifest: "$manifest" path "$destination_path" full: true)" >/dev/null || die "transferred snapshot differs from manifest"
  log "Snapshot transferred and verified across $effective streams on the confirmed rail." >&2
)

# Selected explicitly for a move between two remote nodes. The controller holds
# only bounded pipe buffers; no snapshot or temporary model files are written.
model_transfer_relay() (
  set -euo pipefail
  set +m
  local source_rank="${1:?}" source_path="${2:?}" destination_rank="${3:?}" destination_path="${4:?}" manifest="${5:?}"
  local temp effective revision role rank mapped route command local_ip local_dev remote_ip remote_dev alias rshell stream send receive pipeline pid rc=0
  local -a children=() fields=()
  local -A shells=() aliases=()
  [ "$source_rank" != 0 ] && [ "$destination_rank" != 0 ] && [ "$source_rank" != "$destination_rank" ] || die "relay requires two different remote nodes"
  require_topology_ssh_trust >/dev/null || die "relay requires enrolled confirmed SSH identities"
  temp=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-relay.XXXXXX")
  cleanup_relay() {
    local child
    for child in "${children[@]}"; do kill -TERM -- "-$child" 2>/dev/null || true; done
    for child in "${children[@]}"; do wait "$child" 2>/dev/null || true; done
    rm -rf -- "$temp"
  }
  trap cleanup_relay EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  effective=$(printf '%s' "$manifest" | python3 -m model_library.transfer partition --out "$temp") || die "invalid relay manifest"
  revision=$(printf '%s' "$manifest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["snapshot_revision"])')
  for role in source destination; do
    if [ "$role" = source ]; then rank="$source_rank"; else rank="$destination_rank"; fi
    mapped=$(python3 -m model_library.transfer map --topology "$CLUSTER_TOPOLOGY_FILE" --source-rank 0 --destination-rank "$rank") || die "relay leg has no confirmed rail"
    mapfile -t fields < <(printf '%s' "$mapped" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(d[k]) for k in ("control_ssh_host","local_ip","local_netdev","remote_ip","remote_netdev","topology_id")]')
    [ "${#fields[@]}" = 6 ] || die "relay leg map is incomplete"
    alias="${fields[0]}" local_ip="${fields[1]}" local_dev="${fields[2]}" remote_ip="${fields[3]}" remote_dev="${fields[4]}"
    [ "$alias" = "${CLUSTER_NODE_SSH_HOSTS[$rank]:-}" ] && [ "${fields[5]}" = "$CLUSTER_TOPOLOGY_ID" ] || die "relay leg SSH identity or topology changed"
    route=$("${PULSAR_IP:-ip}" -j route get "$remote_ip") || die "relay local route is unavailable"
    printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$remote_ip" --netdev "$local_dev" --source-ip "$local_ip" || die "relay local route differs from confirmed rail"
    route=$(ssh_node "$rank" "$(shell_join_q ip -j route get "$local_ip")") || die "relay remote route is unavailable"
    printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$local_ip" --netdev "$remote_dev" --source-ip "$remote_ip" || die "relay remote route differs from confirmed rail"
    shells[$role]=$(shell_join_q "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -o "HostName=$remote_ip" -o "HostKeyAlias=$alias" -o "BindAddress=$local_ip" -o StrictHostKeyChecking=yes -o CheckHostIP=no -o UpdateHostKeys=no)
    aliases[$role]="$alias"
  done
  log "Move transfer route: physical node $source_rank → controller pipe → physical node $destination_rank; verified RoCE rail on each leg, no controller model copy." >&2
  model_node "$source_rank" "$(model_json operation verify manifest: "$manifest" path "$source_path" full: true)" >/dev/null || die "relay source does not match manifest"
  command=$(python3 -m model_library.transfer staging-code)
  ssh_node "$destination_rank" "$(shell_join_q python3 -c "$command" "$destination_path" "$revision")" || die "relay destination staging is unsafe"
  for ((stream=0; stream<effective; stream++)); do
    send=$(printf '%s' "$manifest" | python3 -m model_library.transfer stream-command --mode send --path "$source_path" --stream "$stream") || die "cannot build source stream"
    receive=$(printf '%s' "$manifest" | python3 -m model_library.transfer stream-command --mode receive --path "$destination_path" --stream "$stream") || die "cannot build destination stream"
    pipeline="${shells[source]} -n -- $(shell_join_q "${aliases[source]}" "$send") | ${shells[destination]} -- $(shell_join_q "${aliases[destination]}" "$receive")"
    setsid bash -o pipefail -c "$pipeline" &
    children+=("$!")
  done
  while [ "${#children[@]}" -gt 0 ]; do
    pid="${children[0]}"; wait "$pid" || rc=$?
    children=("${children[@]:1}")
    [ "$rc" = 0 ] || die "relay stream failed (exit=$rc); pending staging retained" "$rc"
  done
  model_node "$destination_rank" "$(model_json operation verify manifest: "$manifest" path "$destination_path" full: true)" >/dev/null || die "relayed snapshot differs from manifest"
  log "Snapshot relayed and verified across $effective streams; controller stored no model files." >&2
)

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  SCRIPT_NAME=model-transfer
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
  . "$REPO_DIR/scripts/model-library-common.sh"
  [ "$#" = 6 ] && [ "${6:-}" = --yes ] || die "standalone transfer requires five arguments followed by --yes; use the model-library command for a reviewable plan" 2
  model_transfer "${@:1:5}"
fi
