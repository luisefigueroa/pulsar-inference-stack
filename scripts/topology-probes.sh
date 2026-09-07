#!/usr/bin/env bash
# Shared live probes. Caller sources lib.sh; no actions on source.

topology_control_ssh() {
  local host="$1" control="$2"; shift 2
  ssh_control_endpoint "$host" "$control" "$@"
}

topology_check_fabric() {
  local document="$1" plan source_rank source_host _target_rank target_ip _network control
  local failed=0
  plan=$(python3 "$REPO_DIR/scripts/topology_manifest.py" ping-plan "$document") || return 1
  while IFS=$'\t' read -r source_rank source_host _target_rank target_ip _network; do
    [ -n "$source_rank" ] || continue
    if [ "$source_rank" = 0 ]; then
      ping -c1 -W2 "$target_ip" >/dev/null 2>&1 || failed=1
    else
      control=$(python3 - "$document" "$source_rank" <<'PY'
import json, sys
document=json.load(open(sys.argv[1]))
topology=document.get('topology', document)
print(topology['nodes'][int(sys.argv[2])]['control']['ip'])
PY
) || return 1
      topology_control_ssh "$source_host" "$control" \
        "ping -c1 -W2 $(printf '%q' "$target_ip") >/dev/null 2>&1" </dev/null >/dev/null 2>&1 || failed=1
    fi
  done <<<"$plan"
  return "$failed"
}
