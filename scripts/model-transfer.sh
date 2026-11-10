#!/usr/bin/env bash
# Eight-stream flat snapshot transfer over one exact confirmed RoCE rail.
# Source this file after lib.sh and model-library-common.sh.

# model_transfer_prove_hops MAP: for a copy route through neighbours (the
# transfer map's hops), prove on both ends of every hop, over control SSH,
# that the far end's address on their cable goes out through that cable's
# interface from the node's own address on it. Dies naming the hop.
model_transfer_prove_hops() {
  local mapped="$1" from_rank from_name from_ip from_dev to_rank to_name to_ip to_dev _to_alias network route
  while IFS=$'\t' read -r from_rank from_name from_ip from_dev to_rank to_name to_ip to_dev _to_alias network; do
    [ -n "$from_rank" ] || continue
    if [ "$from_rank" = 0 ]; then
      route=$("${PULSAR_IP:-ip}" -j route get "$to_ip" </dev/null) \
        || die "the copy route is not proven: this machine has no route to $to_name's address $to_ip on their cable; nothing was copied"
    else
      route=$(ssh_node "$from_rank" "$(shell_join_q ip -j route get "$to_ip")" </dev/null) \
        || die "the copy route is not proven: $from_name did not say how it reaches $to_name's address $to_ip on their cable; nothing was copied"
    fi
    printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$to_ip" --netdev "$from_dev" --source-ip "$from_ip" \
      || die "the copy route is not proven: on $from_name, $to_name's address $to_ip ($network) does not go out through $from_dev from $from_ip; nothing was copied"
    route=$(ssh_node "$to_rank" "$(shell_join_q ip -j route get "$from_ip")" </dev/null) \
      || die "the copy route is not proven: $to_name did not say how it reaches $from_name's address $from_ip on their cable; nothing was copied"
    printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$from_ip" --netdev "$to_dev" --source-ip "$to_ip" \
      || die "the copy route is not proven: on $to_name, $from_name's address $from_ip ($network) does not go out through $to_dev from $to_ip; nothing was copied"
  done < <(printf '%s' "$mapped" | python3 -m model_library.transfer hops)
}

# model_transfer_jump MAP DIR: write one SSH wrapper per hop of the route in
# DIR, then prove each in order with one short command: this machine to its
# neighbour over their cable, then each next node through the one before.
# Sets JUMP_WRAPPER, the wrapper that reaches the far end, and JUMP_THROUGH,
# the machines the copy passes through. Dies naming the hop that failed.
model_transfer_jump() {
  local mapped="$1" dir="$2" options path alias to_name from_name to_ip index=0
  local -a through=()
  JUMP_WRAPPER="" JUMP_THROUGH=""
  mkdir -m 700 "$dir" || die "cannot prepare the copy route"
  options=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${PULSAR_SSH_OPTS[@]}")
  while IFS=$'\t' read -r path alias to_name from_name to_ip; do
    [ -n "$path" ] || continue
    if ! "$path" -n -- "$alias" true </dev/null >/dev/null 2>"$dir/proof.err"; then
      if [ "$index" = 0 ]; then
        die "the copy route is not proven: SSH from this machine to $to_name over their cable ($to_ip) did not connect: $(tail -n1 "$dir/proof.err"); nothing was copied"
      fi
      die "the copy route is not proven: SSH to $to_name through $from_name did not connect: $(tail -n1 "$dir/proof.err"). $from_name's SSH server must allow it to forward the connection to $to_ip (AllowTcpForwarding); nothing was copied"
    fi
    [ "$index" = 0 ] || through+=("$from_name")
    JUMP_WRAPPER="$path"
    index=$((index + 1))
  done < <(printf '%s' "$mapped" | python3 -m model_library.transfer jump --dir "$dir" --ssh "$PULSAR_SSH" --options-json "$options")
  [ -n "$JUMP_WRAPPER" ] || die "the copy route has no hop to prove"
  JUMP_THROUGH=$(python3 -c 'import sys; names=sys.argv[1:]; print(names[0] if len(names)<2 else ", ".join(names[:-1])+" and "+names[-1])' "${through[@]}")
}

model_transfer() (
  set -euo pipefail
  set +m  # each setsid child keeps the PID tracked below
  local source_rank="${1:?source physical rank}" source_path="${2:?source snapshot}"
  local destination_rank="${3:?destination physical rank}" destination_path="${4:?destination staging snapshot}"
  local manifest="${5:?canonical manifest JSON}" transfer_map remote_rank alias expected_alias
  # Preparation supplies a stamp from this invocation and immediately publishes
  # the stage; publication owns its destination hash. Other callers stay full.
  local source_stamp="${6:-null}" destination_verification="${7:-transfer}"
  case "$destination_verification" in transfer|publication) ;; *) die "invalid destination verification owner" ;; esac
  local local_ip local_dev remote_ip remote_dev route shell command revision temp effective pid rc=0 hops
  local -a children=() ssh_argv=() fields=()
  [[ "$source_rank" =~ ^[0-9]+$ ]] && [[ "$destination_rank" =~ ^[0-9]+$ ]] || die "physical ranks must be nonnegative integers"
  [ "$source_rank" != "$destination_rank" ] || die "same-node copies use the local storage copy operation"
  if [ "$source_rank" != 0 ] && [ "$destination_rank" != 0 ]; then
    model_transfer_relay "$source_rank" "$source_path" "$destination_rank" "$destination_path" "$manifest" "$source_stamp" "$destination_verification"
    return
  fi
  require_topology_ssh_trust >/dev/null || die "transfer requires enrolled confirmed SSH identities"
  transfer_map=$(python3 -m model_library.transfer map --topology "$CLUSTER_TOPOLOGY_FILE" --source-rank "$source_rank" --destination-rank "$destination_rank") || die "transfer topology is invalid"
  mapfile -t fields < <(printf '%s' "$transfer_map" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(d[k]) for k in ("remote_rank","control_ssh_host","local_ip","local_netdev","remote_ip","remote_netdev","topology_id")]')
  [ "${#fields[@]}" = 7 ] || die "transfer map is incomplete"
  remote_rank="${fields[0]}" alias="${fields[1]}" local_ip="${fields[2]}" local_dev="${fields[3]}" remote_ip="${fields[4]}" remote_dev="${fields[5]}"
  expected_alias="${CLUSTER_NODE_SSH_HOSTS[$remote_rank]:-}"
  [ -n "$expected_alias" ] && [ "$alias" = "$expected_alias" ] && [ "${fields[6]}" = "$CLUSTER_TOPOLOGY_ID" ] || die "transfer SSH identity or topology changed"
  hops=$(printf '%s' "$transfer_map" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("hops") or []))')
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
  if [ "$hops" -gt 0 ]; then
    # No shared link: prove every hop's cable on both of its ends.
    model_transfer_prove_hops "$transfer_map"
  else
  route=$("${PULSAR_IP:-ip}" -j route get "$remote_ip") || die "local RoCE route is unavailable"
  printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$remote_ip" --netdev "$local_dev" --source-ip "$local_ip" || die "local route differs from confirmed rail"
  command=$(shell_join_q ip -j route get "$local_ip")
  route=$(ssh_node "$remote_rank" "$command") || die "remote RoCE route is unavailable"
  printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$local_ip" --netdev "$remote_dev" --source-ip "$remote_ip" || die "remote route differs from confirmed rail"
  fi
  # Verify source bytes through the existing node service; no parallel schema.
  model_node "$source_rank" "$(model_json operation verify manifest: "$manifest" path "$source_path" stamp: "$source_stamp" full: false)" >/dev/null || die "transfer source does not match manifest"
  command=$(python3 -m model_library.transfer staging-code)
  if [ "$destination_rank" = 0 ]; then
    python3 -c "$command" "$destination_path" "$revision" || die "destination staging is unsafe"
  else
    command=$(shell_join_q python3 -c "$command" "$destination_path" "$revision")
    ssh_node "$destination_rank" "$command" || die "destination staging is unsafe"
  fi
  if [ "$hops" -gt 0 ]; then
    # Each hop is one SSH connection on its own cable; the proofs run the
    # same wrappers rsync uses. rsync splits -e itself: one quoted path.
    model_transfer_jump "$transfer_map" "$temp/route"
    if [ "$destination_rank" = 0 ]; then
      log "Copies from $(human_node_name "$remote_rank") pass through $JUMP_THROUGH." >&2
    else
      log "Copies to $(human_node_name "$remote_rank") pass through $JUMP_THROUGH." >&2
    fi
    shell="'$JUMP_WRAPPER'"
  else
  ssh_argv=("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -o "HostName=$remote_ip" -o "HostKeyAlias=$alias" -o "BindAddress=$local_ip" -o StrictHostKeyChecking=yes -o CheckHostIP=no -o UpdateHostKeys=no)
  shell=$(shell_join_q "${ssh_argv[@]}")
  fi
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
  if [ "$destination_verification" = transfer ]; then
    model_node "$destination_rank" "$(model_json operation verify manifest: "$manifest" path "$destination_path" full: true)" >/dev/null || die "transferred snapshot differs from manifest"
    log "Snapshot transferred and verified across $effective streams on the confirmed rail." >&2
  else
    log "Snapshot transferred across $effective streams; staging publication will verify received bytes." >&2
  fi
)

# Selected explicitly for a move between two remote nodes. The controller holds
# only bounded pipe buffers; no snapshot or temporary model files are written.
model_transfer_relay() (
  set -euo pipefail
  set +m
  local source_rank="${1:?}" source_path="${2:?}" destination_rank="${3:?}" destination_path="${4:?}" manifest="${5:?}"
  local source_stamp="${6:-null}" destination_verification="${7:-transfer}"
  case "$destination_verification" in transfer|publication) ;; *) die "invalid destination verification owner" ;; esac
  local temp effective revision role rank mapped route command local_ip local_dev remote_ip remote_dev alias rshell stream send receive pipeline pid rc=0
  local -a children=() fields=()
  local -A shells=() aliases=() through=()
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
    aliases[$role]="$alias"
    if [ "$(printf '%s' "$mapped" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("hops") or []))')" -gt 0 ]; then
      # This leg shares no link with this machine: it passes through neighbours.
      model_transfer_prove_hops "$mapped"
      model_transfer_jump "$mapped" "$temp/route-$role"
      shells[$role]=$(shell_join_q "$JUMP_WRAPPER")
      through[$role]="$JUMP_THROUGH"
      continue
    fi
    route=$("${PULSAR_IP:-ip}" -j route get "$remote_ip") || die "relay local route is unavailable"
    printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$remote_ip" --netdev "$local_dev" --source-ip "$local_ip" || die "relay local route differs from confirmed rail"
    route=$(ssh_node "$rank" "$(shell_join_q ip -j route get "$local_ip")") || die "relay remote route is unavailable"
    printf '%s' "$route" | python3 -m model_library.transfer route --remote-ip "$local_ip" --netdev "$remote_dev" --source-ip "$remote_ip" || die "relay remote route differs from confirmed rail"
    shells[$role]=$(shell_join_q "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -o "HostName=$remote_ip" -o "HostKeyAlias=$alias" -o "BindAddress=$local_ip" -o StrictHostKeyChecking=yes -o CheckHostIP=no -o UpdateHostKeys=no)
    aliases[$role]="$alias"
  done
  log "Move transfer route: $(human_node_name "$source_rank") → controller pipe → $(human_node_name "$destination_rank"); verified RoCE rail on each leg, no controller model copy." >&2
  [ -z "${through[source]:-}" ] || log "Copies from $(human_node_name "$source_rank") pass through ${through[source]}." >&2
  [ -z "${through[destination]:-}" ] || log "Copies to $(human_node_name "$destination_rank") pass through ${through[destination]}." >&2
  model_node "$source_rank" "$(model_json operation verify manifest: "$manifest" path "$source_path" stamp: "$source_stamp" full: false)" >/dev/null || die "relay source does not match manifest"
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
  if [ "$destination_verification" = transfer ]; then
    model_node "$destination_rank" "$(model_json operation verify manifest: "$manifest" path "$destination_path" full: true)" >/dev/null || die "relayed snapshot differs from manifest"
    log "Snapshot relayed and verified across $effective streams; controller stored no model files." >&2
  else
    log "Snapshot relayed across $effective streams; staging publication will verify received bytes." >&2
  fi
)

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  SCRIPT_NAME=model-transfer
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
  . "$REPO_DIR/scripts/model-library-common.sh"
  [ "$#" = 6 ] && [ "${6:-}" = --yes ] || die "standalone transfer requires five arguments followed by --yes; use the model-library command for a reviewable plan" 2
  model_transfer "${@:1:5}"
fi
