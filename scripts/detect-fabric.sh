#!/usr/bin/env bash
# Discover hostname-agnostic GB10 peers, verify their RoCE mesh, and optionally
# persist the user-confirmed membership in .cluster-topology.json.
set -euo pipefail
# shellcheck disable=SC2034  # read by lib.sh log/warn/die
SCRIPT_NAME=detect-fabric
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

JSON=0
WRITE=0
YES=0
ACCEPT_NEW=0
REMOVE_MISSING=0
declare -a CLI_CANDIDATES=()

usage() {
  cat <<'EOF'
usage: scripts/detect-fabric.sh [options]

Discover SSH services with mDNS, then retain only nodes that independently
prove aarch64 + NVIDIA GB10 + Docker NVIDIA + active addressed RDMA links.
Hostnames are descriptive only; cluster identity comes from each machine ID.

  --json                    emit the discovery document as JSON
  --write-topology          confirm and atomically write .cluster-topology.json
  --write-env               deprecated alias for --write-topology
  --candidate HOST          probe an additional hostname or IP (repeatable)
  --yes, -y                 skip membership confirmation (automation)
  --accept-new-host-keys    explicitly trust a new SSH host key during discovery
  --remove-missing          with --write-topology: save without confirmed nodes
                            that did not answer (never one that answered)
  -h, --help                show this help

Candidate sources are combined and de-duplicated:
  * this node (always included)
  * Avahi/mDNS _ssh._tcp IPv4 advertisements
  * CLUSTER_CANDIDATES (comma- or whitespace-separated)
  * --candidate HOST
  * nodes from an existing confirmed topology

SSH checks are non-interactive and use saved host keys by default. Discovery
never chooses model geometry; each profile remains an exact configured deployment.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --write-topology) WRITE=1 ;;
    --write-env)
      WRITE=1
      [ "$JSON" = 1 ] || warn "--write-env is now an alias for --write-topology; per-node fabric is stored in .cluster-topology.json"
      ;;
    --candidate)
      [ -n "${2:-}" ] || die "--candidate requires a hostname or IP"
      CLI_CANDIDATES+=("$2")
      shift
      ;;
    --yes|-y) YES=1 ;;
    --accept-new-host-keys) ACCEPT_NEW=1 ;;
    --remove-missing) REMOVE_MISSING=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

if [ "$JSON" = 1 ] && [ "$WRITE" = 1 ]; then
  die "--json and --write-topology are separate operations"
fi

if [ "$REMOVE_MISSING" = 1 ] && [ "$WRITE" != 1 ]; then
  die "--remove-missing applies only when saving membership: pulsar topology configure --remove-missing" 2
fi

if [ "$WRITE" = 1 ] && [ "$YES" != 1 ]; then
  # The save confirmation draws with Gum; check before any discovery work.
  . "$REPO_DIR/scripts/ui.sh"
  gum_available \
    || die "saving membership asks for confirmation in an interactive terminal with Gum; review with pulsar topology detect, then use: pulsar topology configure --yes"
fi
require_cmd python3
. "$REPO_DIR/scripts/topology-probes.sh"
PROBE="$REPO_DIR/scripts/probe-node.py"
MANIFEST_TOOL="$REPO_DIR/scripts/topology_manifest.py"
[ -r "$PROBE" ] || die "missing node probe: $PROBE"
[ -x "$MANIFEST_TOOL" ] || die "missing topology helper: $MANIFEST_TOOL"

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-discovery.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT
if [ -e "$CLUSTER_TOPOLOGY_FILE" ]; then
  cp -- "$CLUSTER_TOPOLOGY_FILE" "$tmpdir/saved-before.json"
fi
candidates_file="$tmpdir/candidates"
: >"$candidates_file"
# Candidates that answered but whose host key SSH on this machine does not trust.
untrusted_file="$tmpdir/untrusted"
: >"$untrusted_file"
existing_valid=0

# save_noticed_changes: a changes-noticed body on stdin becomes the saved
# record (scripts/saved_observations.py). Tests save only where
# PULSAR_CLUSTER_CHANGES_FILE points, so they never write into a checkout.
# Saving never changes this command's output or exit status.
save_noticed_changes() {
  if [ "${PULSAR_SELFTEST:-0}" != 1 ] || [ -n "${PULSAR_CLUSTER_CHANGES_FILE:-}" ]; then
    python3 "$REPO_DIR/scripts/saved_observations.py" save-cluster-changes --repo-root "$REPO_DIR" >&2 || true
  else
    cat >/dev/null
  fi
}

# notice_changes DOCUMENT [ISSUE]: every discovery against valid saved
# membership adds `changes_noticed` to its document and saves it. ISSUE says
# why discovery itself failed (the result is then not-checked).
notice_changes() {
  local document="$1" issue="${2:-}" noticed="$tmpdir/noticed.json"
  local -a args=(notice "$tmpdir/saved-before.json" --document "$document" --untrusted "$untrusted_file")
  [ "$existing_valid" = 1 ] && [ -f "$tmpdir/saved-before.json" ] || return 0
  [ -z "$issue" ] || args+=(--failure "$issue")
  python3 "$REPO_DIR/scripts/topology_actions.py" "${args[@]}" >"$noticed" 2>/dev/null || return 0
  mv "$noticed" "$document"
  python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["changes_noticed"]))' "$document" \
    | save_noticed_changes || true
}

safe_candidate() {
  case "${1:-}" in
    ""|-*|*[!A-Za-z0-9._:@%+-]*) return 1 ;;
    *) return 0 ;;
  esac
}

add_candidate() {
  local candidate="${1:-}"
  [ -n "$candidate" ] || return 0
  if safe_candidate "$candidate"; then
    printf '%s\n' "$candidate" >>"$candidates_file"
  else
    warn "skip unsafe SSH candidate '$candidate'"
  fi
}

for candidate in "${CLI_CANDIDATES[@]}"; do
  add_candidate "$candidate"
done

if [ -n "${CLUSTER_CANDIDATES:-}" ]; then
  candidate_words=${CLUSTER_CANDIDATES//,/ }
  # shellcheck disable=SC2086 # intentional word splitting for documented list
  for candidate in $candidate_words; do
    add_candidate "$candidate"
  done
fi

if command -v avahi-browse >/dev/null 2>&1; then
  while IFS=$'\t' read -r mdns_host mdns_ip; do
    add_candidate "$mdns_host"
    add_candidate "$mdns_ip"
  done < <(
    avahi-browse --resolve --terminate --parsable _ssh._tcp 2>/dev/null \
      | awk -F';' '$1 == "=" && $3 == "IPv4" {print $7 "\t" $8}' \
      || true
  )
elif [ "$JSON" != 1 ]; then
  warn "avahi-browse unavailable — probing explicit/existing candidates only"
fi

# Pin saved aliases to their confirmed control endpoints, even before enrollment.
declare -A saved_controls=()
if [ -f "$CLUSTER_TOPOLOGY_FILE" ]; then
  if saved_rows=$("$MANIFEST_TOOL" rows "$CLUSTER_TOPOLOGY_FILE" 2>/dev/null); then
    existing_valid=1
    while IFS=$'\t' read -r kind _rank _id _hostname ssh_host control _rest; do
      [ "$kind" = NODE ] || continue
      if [ "$_rank" != 0 ]; then
        add_candidate "$ssh_host"
        saved_controls["$ssh_host"]="$control"
      fi
    done <<<"$saved_rows"
    # An unusable enrolled config must not fall back to ordinary SSH trust.
    if ! load_cluster_topology; then
      python3 "$REPO_DIR/scripts/topology_actions.py" discovery "$CLUSTER_TOPOLOGY_FILE" \
        --failure "Saved SSH configuration is unusable. Run pulsar ssh-trust check and repair enrollment before discovery." \
        >"$tmpdir/access-failure.json" || :
      notice_changes "$tmpdir/access-failure.json" \
        "Saved SSH configuration is unusable. Run pulsar ssh-trust check and repair enrollment before discovery."
      if [ "$JSON" = 1 ]; then
        cat "$tmpdir/access-failure.json"
      else
        python3 "$REPO_DIR/scripts/topology_actions.py" changes "$CLUSTER_TOPOLOGY_FILE" \
          --document "$tmpdir/access-failure.json"
      fi
      exit 1
    fi
  fi
fi
sort -u "$candidates_file" -o "$candidates_file"

local_probe="$tmpdir/probe-0000.json"
local_probe_flags=()
append_probe_node_platform_args local_probe_flags
python3 "$PROBE" --local --ssh-host local "${local_probe_flags[@]}" \
  >"$local_probe"
declare -a probe_files=("$local_probe")
declare -a probe_pids=()
declare -a probe_outputs=()
declare -a probe_errors=()
declare -a probe_hosts=()

ssh_opts=(-o StrictHostKeyChecking=yes -o UpdateHostKeys=no "${PULSAR_SSH_OPTS[@]}")
if [ "$ACCEPT_NEW" = 1 ]; then
  ssh_opts=(-o StrictHostKeyChecking=accept-new -o UpdateHostKeys=no "${PULSAR_SSH_OPTS[@]}")
fi

index=0
while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  index=$((index + 1))
  output=$(printf '%s/probe-%04d.json' "$tmpdir" "$index")
  error_output=$(printf '%s/probe-%04d.err' "$tmpdir" "$index")
  remote_command=$(probe_node_remote_python_command "$candidate")
  (
    if [ -n "${saved_controls[$candidate]:-}" ]; then
      topology_control_ssh "$candidate" "${saved_controls[$candidate]}" "$remote_command" \
        <"$PROBE" >"$output" 2>"$error_output"
    else
      "$PULSAR_SSH" "${ssh_opts[@]}" -- "$candidate" "$remote_command" \
        <"$PROBE" >"$output" 2>"$error_output"
    fi
  ) &
  probe_pids+=("$!")
  probe_outputs+=("$output")
  probe_errors+=("$error_output")
  probe_hosts+=("$candidate")
done <"$candidates_file"

skipped_probe_count=0
for position in "${!probe_pids[@]}"; do
  if wait "${probe_pids[$position]}"; then
    if [ -s "${probe_outputs[$position]}" ]; then
      probe_files+=("${probe_outputs[$position]}")
    fi
  else
    skipped_probe_count=$((skipped_probe_count + 1))
    # OpenSSH refused the host key: the machine answered, but SSH on this
    # machine does not trust it yet. Anything else is not told apart.
    if grep -qE 'Host key verification failed|host key is known|REMOTE HOST IDENTIFICATION HAS CHANGED' \
        "${probe_errors[$position]}" 2>/dev/null; then
      printf '%s\n' "${probe_hosts[$position]}" >>"$untrusted_file"
    fi
  fi
done
report_skipped_candidates() {
  local noun=addresses
  [ "$skipped_probe_count" -gt 0 ] || return 0
  [ "$skipped_probe_count" -ne 1 ] || noun=address
  echo "DISCOVERY NOTES"
  print_hanging "  SSH       " "$skipped_probe_count advertised SSH $noun could not be checked with saved keys."
  if [ "${_PULSAR_SETUP_REVIEW:-0}" = 1 ]; then
    print_hanging "  Help      " "These are usually unrelated devices. If a GB10 is missing, choose Add a machine by hostname or IP below."
  else
    print_hanging "  Help      " "These are usually unrelated devices. If a GB10 is missing, set up key-based SSH and rerun with --candidate HOST."
  fi
}



assemble_args=(assemble)
if [ "$existing_valid" = 1 ]; then
  assemble_args+=(--existing "$CLUSTER_TOPOLOGY_FILE")
fi
assemble_args+=("${probe_files[@]}")
discovery="$tmpdir/discovery.json"
"$MANIFEST_TOOL" "${assemble_args[@]}" >"$discovery"
membership_ok=1
membership_args=(discovery "$CLUSTER_TOPOLOGY_FILE" --document "$discovery")
[ "$REMOVE_MISSING" != 1 ] || membership_args+=(--remove-missing)
python3 "$REPO_DIR/scripts/topology_actions.py" "${membership_args[@]}" \
  >"$tmpdir/membership.json" || membership_ok=0
mv "$tmpdir/membership.json" "$discovery"
if [ "$JSON" != 1 ]; then
  python3 "$REPO_DIR/scripts/topology_actions.py" changes "$CLUSTER_TOPOLOGY_FILE" --document "$discovery"
fi

if ! "$MANIFEST_TOOL" validate "$discovery" >/dev/null 2>&1; then
  notice_changes "$discovery"
  if [ "$JSON" = 1 ]; then
    cat "$discovery"
  else
    warn "no usable GB10 RoCE topology found"
    report_skipped_candidates
    python3 - "$discovery" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
for item in document.get("rejected") or []:
    reasons = "; ".join(item.get("reasons") or ["not qualified"])
    print(f"  skip {item.get('hostname') or '?'} ({item.get('ssh_host') or '?'}): {reasons}")
PY
  fi
  exit 1
fi

# Verify every advertised rail in both directions. A structurally matching
# subnet is not enough to become confirmed cluster state.
connectivity_ok=1
topology_check_fabric "$discovery" || connectivity_ok=0

if [ "$connectivity_ok" != 1 ]; then
  notice_changes "$discovery" "Pairwise RoCE connectivity failed."
  if [ "$JSON" = 1 ]; then
    python3 - "$discovery" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document['result'] = 'incomplete'
document['discovery_issues'] = ['Pairwise RoCE connectivity failed.']
print(json.dumps(document, sort_keys=True))
PY
  fi
  [ "$JSON" = 1 ] || warn "pairwise RoCE verification failed; topology will not be written"
  exit 1
fi

verified="$tmpdir/verified.json"
"$MANIFEST_TOOL" mark-verified "$discovery" >"$verified"
notice_changes "$verified"

if [ "$JSON" = 1 ]; then
  cat "$verified"
  [ "$membership_ok" = 1 ]
  exit
fi

echo
# Inside the setup review (private marker), the review's menu offers the next
# steps, so detect names them instead of the CLI's.
review_flag=()
[ "${_PULSAR_SETUP_REVIEW:-0}" != 1 ] || review_flag=(--in-review)
"$MANIFEST_TOOL" render "$verified" --skipped-ssh "$skipped_probe_count" "${review_flag[@]}"
if [ "$membership_ok" != 1 ]; then
  warn "discovery is incomplete; confirmed membership was not changed"
  exit 1
fi

if [ "$WRITE" != 1 ]; then
  echo
  echo "REVIEW ONLY"
  print_hanging "  Result    " "No files changed."
  [ "${_PULSAR_SETUP_REVIEW:-0}" = 1 ] \
    || print_hanging "  Next      " "Save this membership later with pulsar topology configure."
  exit 0
fi

# --remove-missing: the confirmed nodes that did not answer, which this save
# leaves out. Discovery already refused any missing node that answered.
declare -a unanswered_ids=() unanswered_names=()
if [ "$REMOVE_MISSING" = 1 ]; then
  while IFS=$'\t' read -r unanswered_id unanswered_name; do
    [ -n "$unanswered_id" ] || continue
    unanswered_ids+=("$unanswered_id")
    unanswered_names+=("$unanswered_name")
  done < <(python3 -c 'import json,sys
for item in json.load(open(sys.argv[1]))["saved_membership"]["missing"]:
    if item.get("answered") is False: print(item["node_id"] + "\t" + item["hostname"])
' "$verified")
fi
unanswered_list=""
if [ "${#unanswered_names[@]}" -gt 0 ]; then
  unanswered_list=$(python3 -c 'import sys
names = sys.argv[1:]
print(names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])' "${unanswered_names[@]}")
fi

# Replacing membership while any old or proposed rank is active would make
# lifecycle ownership ambiguous. Query every rank and fail closed on probe error;
# only a node left out under --remove-missing may fail to answer.
if [ -f "$CLUSTER_TOPOLOGY_FILE" ]; then
  require_topology_rewrite_idle "$CLUSTER_TOPOLOGY_FILE" "${unanswered_ids[@]}" \
    || die "existing cluster is not idle; stop managed services and restore access to every node"
fi
require_topology_rewrite_idle "$verified" \
  || die "proposed cluster is not idle; stop managed services and restore access to every node"

echo
impact_args=(--new "$verified" --state-root "$PULSAR_MODEL_LIBRARY_DIR")
[ ! -f "$CLUSTER_TOPOLOGY_FILE" ] || impact_args+=(--old "$CLUSTER_TOPOLOGY_FILE")
python3 "$REPO_DIR/scripts/topology_storage_impact.py" "${impact_args[@]}" \
  || die "managed storage impact could not be inspected; topology was not changed"
echo
"$MANIFEST_TOOL" render-save "$verified" "$CLUSTER_TOPOLOGY_FILE"
if [ -n "$unanswered_list" ]; then
  print_hanging "  Left out  " "$unanswered_list, which did not answer."
  unanswered_pronoun=it
  [ "${#unanswered_names[@]}" = 1 ] || unanswered_pronoun=them
  print_hanging "  Services  " "Pulsar cannot ask $unanswered_list whether a Pulsar service runs there. It saves without $unanswered_pronoun only because --remove-missing was given."
fi

node_count=$(python3 -c 'import json,sys
document=json.load(open(sys.argv[1],encoding="utf-8"))
topology=document["topology"] if isinstance(document.get("topology"),dict) else document
print(len(topology.get("nodes") or []))
' "$verified")
if [ "$YES" != 1 ]; then
  # ui.sh was loaded, and Gum found, before discovery started.
  machine_word=machines
  [ "$node_count" != 1 ] || machine_word=machine
  question="Save cluster membership with $node_count $machine_word?"
  [ -z "$unanswered_list" ] \
    || question="Save cluster membership with $node_count $machine_word, without $unanswered_list, which did not answer?"
  if confirm "$question"; then
    :
  else
    confirmation_rc=$?
    log "aborted — topology not modified"
    [ "$confirmation_rc" -ne 130 ] || exit 130
    # Private guided-setup result; ordinary configure cancellation stays exit 0.
    [ "${_PULSAR_SETUP_CONFIGURE:-0}" != 1 ] || exit 125
    exit 0
  fi
fi

# Recheck after the human prompt so a newly started service blocks saving.
if [ -f "$CLUSTER_TOPOLOGY_FILE" ]; then
  require_topology_rewrite_idle "$CLUSTER_TOPOLOGY_FILE" "${unanswered_ids[@]}" || die "existing cluster is not idle"
fi
require_topology_rewrite_idle "$verified" || die "proposed cluster is not idle"
if [ -f "$tmpdir/saved-before.json" ]; then
  cmp -s "$tmpdir/saved-before.json" "$CLUSTER_TOPOLOGY_FILE" \
    || die "saved membership changed during discovery; review it and retry"
else
  [ ! -e "$CLUSTER_TOPOLOGY_FILE" ] || die "membership was configured concurrently; review it and retry"
fi
"$MANIFEST_TOOL" write "$verified" "$CLUSTER_TOPOLOGY_FILE"
log "wrote $CLUSTER_TOPOLOGY_FILE"
# The saved membership is now what discovery saw: the record reads unchanged.
python3 "$REPO_DIR/scripts/topology_actions.py" notice-saved "$CLUSTER_TOPOLOGY_FILE" \
  --untrusted "$untrusted_file" 2>/dev/null | save_noticed_changes || true
if [ "$node_count" -gt 1 ]; then
  print_hanging "  Next      " "./pulsar ssh-trust enroll"
else
  print_hanging "  Next      " "./pulsar doctor"
fi
