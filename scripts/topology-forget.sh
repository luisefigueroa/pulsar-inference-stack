#!/usr/bin/env bash
# Forget this checkout's saved cluster state: membership, the generated SSH
# configuration and the saved observations of the cluster, after a private
# backup copy. Model files, archives, records, .env and the overlay stay.
#   scripts/topology-forget.sh [--plan] [--yes] [--json]
set -euo pipefail
# shellcheck disable=SC2034  # read by lib.sh log/warn/die
SCRIPT_NAME=topology-forget

PLAN=0
YES=0
JSON=0
usage() {
  python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/terminal_format.py" <<'EOF'
Usage: ./pulsar topology forget [--plan] [--yes] [--json]

Remove this checkout's saved cluster membership, its generated SSH configuration and the saved observations of the cluster, so setup can start again. The removed files are copied first to a private backup folder under .pulsar/. Model files on the nodes, archives, model-library records, .env and the site overlay are kept.

  --plan   list what would be removed and kept; change nothing
  --yes    forget without the question (needed off a terminal)
  --json   print the plan or the result as JSON
EOF
}
while [ $# -gt 0 ]; do
  case "$1" in
    --plan) PLAN=1 ;;
    --yes|-y) YES=1 ;;
    --json) JSON=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

# Tests name every file forget removes, so it never touches a checkout's own.
if [ "${PULSAR_SELFTEST:-0}" = 1 ]; then
  for variable in CLUSTER_TOPOLOGY_FILE CLUSTER_SSH_CONFIG_FILE PULSAR_SERVICE_INVENTORY_FILE \
      PULSAR_STORAGE_BUDGET_FILE PULSAR_CLUSTER_CHANGES_FILE PULSAR_CLUSTER_BACKUP_DIR; do
    [ -n "${!variable:-}" ] || { echo "error: selftest forget needs $variable" >&2; exit 2; }
  done
fi

# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if [ "$PLAN" = 0 ] && [ "$YES" = 0 ]; then
  # The question draws with Gum; check before asking any node.
  . "$REPO_DIR/scripts/ui.sh"
  gum_available \
    || die "forgetting this cluster asks for confirmation in an interactive terminal with Gum; review with ./pulsar topology forget --plan, then use ./pulsar topology forget --yes" 2
fi

TOOL="$REPO_DIR/scripts/topology_forget.py"
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-forget.XXXXXX")
trap 'rm -rf -- "$tmpdir"' EXIT
plan="$tmpdir/plan.json"
services="$tmpdir/services"
: >"$services"
python3 "$TOOL" plan --repo-root "$REPO_DIR" --topology "$CLUSTER_TOPOLOGY_FILE" \
  --ssh-config "$CLUSTER_SSH_CONFIG_FILE" --overlay "${PULSAR_OVERLAY_PATH:-$REPO_DIR/.pulsar-overlay.json}" \
  >"$plan" || exit 1

# ask_node_services HOSTNAME [SSH_HOST CONTROL_IP]: one row, hostname<TAB>
# running, none or not-observed. A node that cannot be asked does not block.
ask_node_services() {
  local hostname="$1" running="" query
  if [ $# = 1 ]; then
    running=$("$PULSAR_DOCKER" ps -q --filter "label=${PULSAR_MANAGED_LABEL}=true" 2>/dev/null) \
      || { printf '%s\tnot-observed\n' "$hostname" >>"$services"; return 0; }
  else
    printf -v query 'docker ps -q --filter %q' "label=${PULSAR_MANAGED_LABEL}=true"
    running=$(ssh_control_endpoint "$2" "$3" "$query" </dev/null 2>/dev/null) \
      || { printf '%s\tnot-observed\n' "$hostname" >>"$services"; return 0; }
  fi
  if [ -n "$running" ]; then
    printf '%s\trunning\n' "$hostname" >>"$services"
  else
    printf '%s\tnone\n' "$hostname" >>"$services"
  fi
}

checked_only_here=()
this_machine=$(hostname -s 2>/dev/null || hostname)
membership=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["membership"])' "$plan")
if [ "$membership" = saved ] && load_cluster_topology >/dev/null 2>&1; then
  for ((rank = 0; rank < CLUSTER_TOPOLOGY_COUNT; rank++)); do
    if [ "$rank" = 0 ]; then
      ask_node_services "${CLUSTER_NODE_HOSTNAMES[0]}"
    else
      ask_node_services "${CLUSTER_NODE_HOSTNAMES[$rank]}" "${CLUSTER_NODE_SSH_HOSTS[$rank]}" \
        "${CLUSTER_NODE_CONTROL_IPS[$rank]}"
    fi
  done
else
  # Invalid membership, or an SSH configuration that cannot be used: never
  # fall back to other SSH trust; only this machine can be asked.
  ask_node_services "$this_machine"
  [ "$membership" = missing ] || checked_only_here=(--checked-only-here)
fi

render_args=(render "$plan" --services "$services" "${checked_only_here[@]}")
nothing=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["result"] == "nothing")' "$plan")
refused=0
grep -q $'\trunning$' "$services" && refused=1

if [ "$PLAN" = 1 ] || [ "$nothing" = True ]; then
  if [ "$JSON" = 1 ]; then
    python3 "$TOOL" "${render_args[@]}" --json
  elif [ "$nothing" = True ]; then
    echo "Nothing to forget: this checkout has no saved cluster state."
  else
    python3 "$TOOL" "${render_args[@]}"
    echo
    if [ "$refused" = 1 ]; then
      print_hanging "  Result    " "Forget would refuse while a Pulsar service runs. No files changed."
    else
      print_hanging "  Result    " "Plan only. No files changed."
    fi
  fi
  exit 0
fi

[ "$JSON" = 1 ] || python3 "$TOOL" "${render_args[@]}"
if [ "$refused" = 1 ]; then
  [ "$JSON" = 0 ] || python3 "$TOOL" "${render_args[@]}" --json
  die "a Pulsar service runs on a node that answered; stop it first (./pulsar stop --all --yes), then forget again. No files changed." 1
fi

if [ "$YES" != 1 ]; then
  echo
  if confirm "Forget this cluster's saved membership and observations?"; then
    :
  else
    confirmation_rc=$?
    log "not forgotten — no files changed"
    [ "$confirmation_rc" -ne 130 ] || exit 130
    exit 0
  fi
fi

result=$(python3 "$TOOL" apply "$plan" --services "$services" "${checked_only_here[@]}") \
  || die "this cluster was not forgotten; the files are unchanged"
if [ "$JSON" = 1 ]; then
  printf '%s\n' "$result"
else
  backup=$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["backup"])')
  echo
  echo "FORGOTTEN"
  print_hanging "  Result    " "This checkout no longer has a saved cluster. Model files, archives and records are unchanged."
  print_hanging "  Backup    " "$backup"
  print_hanging "  Next      " "./pulsar topology setup"
fi
