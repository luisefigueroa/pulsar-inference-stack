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

# topology_ping_links DOCUMENT COUNT: ping every saved link among the ranks
# below COUNT, the ranks a recipe on COUNT nodes serves on, in both directions,
# each bound to the source's own interface on that link. Prints one row per
# ping, "ok" or "fail" with source rank, target rank, address, network and
# interface, then one "pair" row per pair of those ranks with saved links: the
# lower and higher rank, the links that answered both ways, and the links saved.
topology_ping_links() {
  local document="$1" count="$2" plan source_rank source_host target_rank target_ip network interface control
  local answered pair low high
  local -A each_way=() saved=()
  plan=$(python3 "$REPO_DIR/scripts/topology_manifest.py" ping-plan "$document") || return 1
  while IFS=$'\t' read -r source_rank source_host target_rank target_ip network interface; do
    [ -n "$source_rank" ] || continue
    [ "$source_rank" -lt "$count" ] && [ "$target_rank" -lt "$count" ] || continue
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
    low="$source_rank" high="$target_rank"; [ "$low" -lt "$high" ] || { low="$target_rank"; high="$source_rank"; }
    pair="$low:$high"
    saved["$pair:$network"]=1
    each_way["$pair:$network"]=$(( ${each_way["$pair:$network"]:-0} + answered ))
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$([ "$answered" = 1 ] && echo ok || echo fail)" \
      "$source_rank" "$target_rank" "$target_ip" "$network" "$interface"
  done <<<"$plan"
  local key total both
  for ((low = 0; low < count; low++)); do
    for ((high = low + 1; high < count; high++)); do
      total=0 both=0
      for key in "${!saved[@]}"; do
        case "$key" in
          "$low:$high:"*)
            total=$((total + 1))
            [ "${each_way[$key]}" != 2 ] || both=$((both + 1))
            ;;
        esac
      done
      [ "$total" = 0 ] || printf 'pair\t%s\t%s\t%s\t%s\n' "$low" "$high" "$both" "$total"
    done
  done
}
