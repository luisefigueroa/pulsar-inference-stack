#!/usr/bin/env bash
# Read-only: print the recent output of a spec's Stack-owned containers on
# every rank, including exited ones. Takes no lock and changes nothing.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      python3 "$ROOT/scripts/terminal_format.py" <<'HELP'
usage: pulsar logs SPEC_ID [--node NODE | --rank N] [--tail N] [--follow]

Print the recent output of the spec's Stack-owned containers on every rank, including exited ones. Read-only.

  --node NODE  Only the container on this node, by hostname or node ID
  --rank N     Only the container of this rank; a one-node service is rank 0
  --tail N     Lines per container (default 200)
  --follow     Keep printing new output until Ctrl-C; needs exactly one container

Exits 1 when no container of the spec exists, and 3 when a node could not be observed.
HELP
      exit 0 ;;
  esac
done
usage_error() { printf 'error: %s\n' "$1" >&2; exit 2; }
SPEC="" NODE="" RANK="" TAIL=200 FOLLOW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) usage_error "logs prints the containers' own text and has no --json form; use ./pulsar status SPEC_ID --json for service state" ;;
    --node|--rank|--tail)
      case "${2:-}" in ""|-*) usage_error "$1 requires a value" ;; esac
      case "$1" in --node) NODE="$2" ;; --rank) RANK="$2" ;; --tail) TAIL="$2" ;; esac
      shift ;;
    --follow) FOLLOW=1 ;;
    -*) usage_error "unknown option: $1" ;;
    *) [ -z "$SPEC" ] || usage_error "unexpected argument: $1"; SPEC="$1" ;;
  esac
  shift
done
[ -n "$SPEC" ] || usage_error "logs requires a spec ID; see ./pulsar models list"
[[ "$SPEC" =~ ^[0-9a-f]{64}$ ]] \
  || usage_error "use a complete spec ID or a unique prefix of at least 12 characters; see ./pulsar models list"
[[ "$TAIL" =~ ^[1-9][0-9]*$ ]] || usage_error "--tail needs a positive number of lines"
[ -z "$RANK" ] || [[ "$RANK" =~ ^(0|[1-9][0-9]*)$ ]] || usage_error "--rank needs a rank number such as 0"
[ -z "$NODE" ] || [ -z "$RANK" ] || usage_error "use --node or --rank, not both"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"
load_cluster_topology || die "the saved cluster membership could not be read; run ./pulsar topology show"

spec_ref="${SPEC:0:12}"
node_index=""
if [ -n "$NODE" ]; then
  resolve_single_node_placement "$NODE" \
    || usage_error "--node '$NODE' does not select exactly one confirmed node; use a hostname from ./pulsar topology show"
  node_index="$SINGLE_NODE_INDEX"
fi

heading() {
  python3 -c 'import sys; from scripts.terminal_format import TerminalWriter; TerminalWriter().emit(sys.argv[1], role="heading")' "$1"
}

# Find the spec's containers. Reading needs proof of ownership, not stop's
# default-placement geometry: the labels must show a Stack-managed container
# of this spec with a rank, and its node-id label must be this confirmed node.
# A guarded trial may place its ranks off the default node order, and its
# containers are still read. Unlike stop, a node that cannot be observed is
# recorded and the others are still read.
count="$CLUSTER_TOPOLOGY_COUNT"
[ "$count" -gt 0 ] || count=1
found=() unobserved=()
refused=0
for ((index = 0; index < count; index++)); do
  [ -z "$node_index" ] || [ "$index" = "$node_index" ] || continue
  placement=$(single_node_key_for_index "$index")
  if ! ids=$(list_managed_container_ids_on_node "$index"); then
    unobserved+=("$index")
    continue
  fi
  for id in $ids; do
    probe=0
    meta=$(container_ownership_inspect_on_node "$index" "$id") || probe=$?
    # Removed since it was listed.
    [ "$probe" -ne 3 ] || continue
    if [ "$probe" -ne 0 ]; then
      unobserved+=("$index")
      break
    fi
    IFS=$'\t' read -r _ _ _ have_conf rank < <(container_ownership_fields "$meta")
    [ "$have_conf" = "$SPEC" ] || continue
    reason=""
    if ! container_ownership_is_proven "$meta" "$SPEC"; then
      reason=$(container_ownership_refuse_reason "$meta" "$SPEC")
    elif [ "$rank" != single ] && ! [[ "$rank" =~ ^(0|[1-9][0-9]*)$ ]]; then
      reason="its rank label '$rank' is not a rank"
    elif ! container_single_node_identity_is_proven "$meta" "$placement"; then
      reason=$(container_single_node_identity_refuse_reason "$meta" "$placement")
    fi
    if [ -n "$reason" ]; then
      warn "not reading container ${id:0:12} on $(human_node_name "$index"): $reason"
      refused=$((refused + 1))
      continue
    fi
    # A one-node service's container is labelled rank "single"; it is rank 0.
    number="$rank"
    [ "$number" != single ] || number=0
    [ -z "$RANK" ] || [ "$number" = "$RANK" ] || continue
    found+=("$number"$'\t'"$index"$'\t'"$id")
  done
done
if [ "${#found[@]}" -gt 0 ]; then
  mapfile -t found < <(printf '%s\n' "${found[@]}" | sort -t $'\t' -k1,1n -k2,2n)
fi

# Unknown is never reported as absent.
report_unobserved() {
  local index
  for index in "${unobserved[@]}"; do
    error_line "$(human_node_name "$index") could not be observed; its logs for spec $spec_ref are unknown. Next: ./pulsar topology check"
  done
}

if [ "${#found[@]}" -eq 0 ]; then
  if [ "${#unobserved[@]}" -gt 0 ]; then
    report_unobserved
    exit 3
  fi
  where="any confirmed node"
  [ -z "$node_index" ] || where=$(human_node_name "$node_index")
  [ -z "$RANK" ] || where="rank $RANK on $where"
  if [ "$refused" -gt 0 ]; then
    error_line "no container of spec $spec_ref on $where could be read; see the warning above. Next: ./pulsar inventory"
  else
    error_line "no container of spec $spec_ref exists on $where. Next: ./pulsar inventory shows what exists"
  fi
  exit 1
fi
if [ "$FOLLOW" = 1 ]; then
  if [ "${#unobserved[@]}" -gt 0 ]; then
    report_unobserved
    exit 3
  fi
  [ "${#found[@]}" -eq 1 ] \
    || usage_error "--follow reads one container, and spec $spec_ref has ${#found[@]}; add --node NODE or --rank N"
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-logs.XXXXXX")
trap 'rm -rf "$work"' EXIT
log_args=(--tail "$TAIL")
[ "$FOLLOW" = 0 ] || log_args+=(--follow)
status=0 first=1
for entry in "${found[@]}"; do
  IFS=$'\t' read -r number index id <<<"$entry"
  where="$(human_node_name "$index") (rank $number)"
  state=$(docker_on_node "$index" inspect --format '{{.State.Status}}' "$id" 2>/dev/null) || state="state unknown"
  [ "$first" = 1 ] || echo
  first=0
  heading "$where · $state"
  # The container's stdout and stderr are merged inside the docker command,
  # so an SSH failure stays on stderr and never reads as container output.
  rc=0
  if [ "$FOLLOW" = 1 ]; then
    if [ "$index" = 0 ]; then
      "$PULSAR_DOCKER" logs "${log_args[@]}" "$id" 2>&1 || rc=$?
    else
      ssh_node "$index" "$(shell_join_q docker logs "${log_args[@]}" "$id") 2>&1" </dev/null 2>"$work/ssh.err" || rc=$?
    fi
  else
    if [ "$index" = 0 ]; then
      "$PULSAR_DOCKER" logs "${log_args[@]}" "$id" >"$work/out" 2>&1 || rc=$?
    else
      ssh_node "$index" "$(shell_join_q docker logs "${log_args[@]}" "$id") 2>&1" </dev/null \
        >"$work/out" 2>"$work/ssh.err" || rc=$?
    fi
    if [ "$rc" = 0 ] && [ ! -s "$work/out" ]; then
      echo "(the container has printed nothing)"
    else
      cat "$work/out"
    fi
  fi
  if [ "$rc" = 255 ] && [ "$index" != 0 ]; then
    reason=$(head -n 1 "$work/ssh.err" 2>/dev/null || true)
    error_line "$where could not be observed${reason:+ ($reason)}; its logs are unknown. Next: ./pulsar topology check"
    status=3
  elif [ "$rc" = 130 ] || [ "$rc" = 143 ]; then
    exit "$rc"
  elif [ "$rc" != 0 ]; then
    error_line "the logs of $where could not be read (docker exited $rc)"
    status=3
  fi
done
if [ "${#unobserved[@]}" -gt 0 ]; then
  echo
  report_unobserved
  status=3
fi
exit "$status"
