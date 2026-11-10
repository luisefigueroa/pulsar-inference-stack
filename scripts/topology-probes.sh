#!/usr/bin/env bash
# Shared live probes. Caller sources lib.sh; no actions on source.

topology_control_ssh() {
  local host="$1" control="$2"; shift 2
  ssh_control_endpoint "$host" "$control" "$@"
}

# topology_check_fabric DOCUMENT [FAILURES]: ping every saved link in both
# directions, each bound to the source's own interface on that link, so a
# ping cannot pass over another path. Each link that does not answer is
# appended to FAILURES as source rank, target rank, target address, network
# and interface, tab-separated.
topology_check_fabric() {
  local document="$1" failures="${2:-}" plan source_rank source_host target_rank target_ip network interface control
  local failed=0 answered
  plan=$(python3 "$REPO_DIR/scripts/topology_manifest.py" ping-plan "$document") || return 1
  while IFS=$'\t' read -r source_rank source_host target_rank target_ip network interface; do
    [ -n "$source_rank" ] || continue
    answered=1
    if [ "$source_rank" = 0 ]; then
      ping -I "$interface" -c1 -W2 "$target_ip" >/dev/null 2>&1 || answered=0
    else
      control=$(python3 - "$document" "$source_rank" <<'PY'
import json, sys
document=json.load(open(sys.argv[1]))
topology=document.get('topology', document)
print(topology['nodes'][int(sys.argv[2])]['control']['ip'])
PY
) || return 1
      topology_control_ssh "$source_host" "$control" \
        "ping -I $(printf '%q' "$interface") -c1 -W2 $(printf '%q' "$target_ip") >/dev/null 2>&1" \
        </dev/null >/dev/null 2>&1 || answered=0
    fi
    if [ "$answered" = 0 ]; then
      failed=1
      [ -z "$failures" ] \
        || printf '%s\t%s\t%s\t%s\t%s\n' "$source_rank" "$target_rank" "$target_ip" "$network" "$interface" >>"$failures"
    fi
  done <<<"$plan"
  return "$failed"
}
