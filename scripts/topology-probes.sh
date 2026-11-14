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
# With "loop", only the pairs a recipe that passes data around a loop of those
# ranks uses: each rank and the next, and the last and the first.
# POSITIONS, when given, are the saved ranks of the recipe's COUNT nodes in role
# order (a placement on named nodes): only links among them are pinged, a loop
# follows their role order, and rows still name saved ranks.
topology_ping_links() {
  local document="$1" count="$2" loop="${3:-}" positions="${4:-}" plan source_rank source_host target_rank target_ip
  local network interface control answered pair low high position role=0 a b
  local -A each_way=() saved=() role_of=()
  for position in $positions; do role_of[$position]=$role; role=$((role + 1)); done
  plan=$(python3 "$REPO_DIR/scripts/topology_manifest.py" ping-plan "$document") || return 1
  while IFS=$'\t' read -r source_rank source_host target_rank target_ip network interface; do
    [ -n "$source_rank" ] || continue
    if [ -n "$positions" ]; then
      [ -n "${role_of[$source_rank]+set}" ] && [ -n "${role_of[$target_rank]+set}" ] || continue
      [ "$loop" != loop ] || topology_loop_pair "${role_of[$source_rank]}" "${role_of[$target_rank]}" "$count" \
        || continue
    else
      [ "$source_rank" -lt "$count" ] && [ "$target_rank" -lt "$count" ] || continue
      [ "$loop" != loop ] || topology_loop_pair "$source_rank" "$target_rank" "$count" || continue
    fi
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
  local -a order=()
  if [ -n "$positions" ]; then read -r -a order <<<"$positions"; else for ((a = 0; a < count; a++)); do order+=("$a"); done; fi
  for ((a = 0; a < count; a++)); do
    for ((b = a + 1; b < count; b++)); do
      [ "$loop" != loop ] || topology_loop_pair "$a" "$b" "$count" || continue
      low="${order[$a]}" high="${order[$b]}"
      [ "$low" -lt "$high" ] || { low="${order[$b]}"; high="${order[$a]}"; }
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

# topology_loop_pair A B COUNT: whether ranks A and B are neighbours in the
# loop of ranks 0 … COUNT-1 in order: consecutive, or the last and the first.
topology_loop_pair() {
  local a="$1" b="$2" count="$3" gap
  gap=$((a > b ? a - b : b - a))
  [ "$gap" = 1 ] || { [ "$count" -gt 2 ] && [ "$gap" = $((count - 1)) ]; }
}

# topology_adapters_active HCAS: which of the adapters in HCAS (comma-separated:
# the ones a launch gives one rank) `rdma link show` on stdin reports as
# state ACTIVE. Prints the count active, the count selected, and the adapters
# that are not active, comma-separated, tab-separated on one line. An adapter
# missing from the listing, or no listing at all, is not active.
topology_adapters_active() {
  local hcas="$1" listing hca port_state active=0 total=0 missing=""
  local -A state=()
  listing=$(cat)
  while read -r _link name _state value _rest; do
    [ "$_link" = link ] && [ "$_state" = state ] || continue
    state["${name%%/*}"]="$value"
  done <<<"$listing"
  local IFS=,
  for hca in $hcas; do
    [ -n "$hca" ] || continue
    total=$((total + 1))
    port_state="${state[$hca]:-}"
    if [ "$port_state" = ACTIVE ]; then
      active=$((active + 1))
    else
      missing="${missing:+$missing,}$hca"
    fi
  done
  printf '%s\t%s\t%s\n' "$active" "$total" "$missing"
}
