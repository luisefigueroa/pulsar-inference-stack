#!/usr/bin/env bash
# Shared Docker image stream; source after lib.sh. No registry fallback.

stream_image_to_node() {
  local image="${1:?exact image required}" rank="${2:?confirmed rank required}"
  local limit="${3:-0}"
  [ "$rank" -gt 0 ] || { echo 'image stream requires a remote confirmed rank' >&2; return 2; }
  if [ "$limit" = 0 ]; then
    # Preserve the existing spec-oriented staging behavior.
    "$PULSAR_DOCKER" save "$image" | ssh_node "$rank" 'docker load'
  else
    local -a SSH_NODE_COMMAND=()
    [[ "$limit" =~ ^[1-9][0-9]*$ ]] || return 2
    ssh_node_command "$rank" || return $?
    # Keep local clients in the public command's owned process group. Default
    # timeout starts another group and could escape invocation cancellation.
    timeout --foreground --signal=TERM --kill-after=10 "$limit" "$PULSAR_DOCKER" save "$image" |
      timeout --foreground --signal=TERM --kill-after=10 "$((limit + 15))" "${SSH_NODE_COMMAND[@]}" \
        "$(shell_join_q timeout --foreground --signal=TERM --kill-after=10 "$limit" docker load)"
  fi
}
