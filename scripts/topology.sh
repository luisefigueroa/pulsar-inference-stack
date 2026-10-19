#!/usr/bin/env bash
# Operator topology boundary; discovery is never a serving action.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

usage() {
  PYTHONPATH="$REPO_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PYHELP'
from terminal_format import TerminalWriter
writer = TerminalWriter()
writer.emit("Usage: pulsar topology COMMAND [options]", role="command")
for command, description in [
    ("setup [--candidate HOST ...]", "Guided first use: confirm membership, enroll SSH identity, then check readiness. Candidates apply only when no valid membership is saved; setup refuses them otherwise (exit 2)."),
    ("show [--json]", "Read saved membership; no live probes."),
    ("check [--json]", "Check all saved nodes and fabric without saving."),
    ("detect [--json] [--candidate HOST ...]", "Discover without changing membership or SSH trust."),
    ("configure [--candidate HOST ...] [--yes] [--accept-new-host-keys]", "Review and explicitly save membership; no model actions."),
    ("menu", "Open the topology menu without probing; it needs a terminal with Gum."),
]:
    writer.emit(command, initial_indent="  ", subsequent_indent="    ", role="command")
    writer.emit(description, initial_indent="    ", subsequent_indent="    ")
writer.blank()
writer.emit("New SSH trust requires separate authorization. Existing low-level topology utilities remain available with their original arguments.")
PYHELP
}

# Shared by fresh setup and the menu's explicit membership reconfiguration.
# Candidate addresses live only in this interaction; discovery keeps its normal
# strict SSH policy. A successful preview is not permission to save membership.
review_candidates() {
  REVIEW_CANDIDATES=("$@")
  local detected choice rc candidate
  local -a labels actions
  while true; do
    if "$0" detect "${REVIEW_CANDIDATES[@]}"; then
      detected=1
    else
      rc=$?; [ "$rc" -ne 130 ] || return 130
      detected=0
    fi
    while true; do
      echo
      python3 "$REPO_DIR/scripts/terminal_format.py" <<'REVIEW'
Check that every machine you intend to use is listed. Added addresses are kept for retries until you leave this review.
REVIEW
      labels=("Add a machine by hostname or IP" "Retry discovery" "Machine still missing? SSH help" "Back")
      actions=(add retry help back)
      if [ "$detected" = 1 ]; then
        labels=("Continue to membership review" "${labels[@]}")
        actions=(continue "${actions[@]}")
      fi
      choice=$(choose_index "Review discovered machines" "${labels[@]}") \
        || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 1; }
      case "${actions[$choice]}" in
        continue) return 0 ;;
        add)
          if candidate=$(prompt_input "Management hostname or IP" "Hostname or IP"); then
            case "$candidate" in
              -*|*[!A-Za-z0-9._:@%+-]*)
                echo 'Enter one hostname or IP, without spaces or SSH options.'
                continue ;;
            esac
            REVIEW_CANDIDATES+=(--candidate "$candidate")
            break
          else
            rc=$?; [ "$rc" -ne 130 ] || return 130
          fi ;;
        retry) break ;;
        help)
          python3 "$REPO_DIR/scripts/terminal_format.py" <<'SSHHELP'
A missing machine can mean an address, network, SSH login or host-key problem. Discovery cannot reliably tell these apart.

Check its management hostname or IP and normal key-based SSH access with: ssh -o StrictHostKeyChecking=ask HOST

If OpenSSH presents a new host key, compare its fingerprint with an independently trusted source, such as the machine's console or its administrator, before accepting it. A changed key needs the same independent verification; do not bypass the warning.

Then return here and choose Retry discovery. Pulsar's later Enroll SSH trust confirmation separately records and verifies the confirmed machines' identities; it does not establish initial OpenSSH trust.
SSHHELP
          ;;
        back) return 1 ;;
      esac
    done
  done
}

action="${1:---help}"; [ $# = 0 ] || shift
case "$action" in
  --help|-h|help) usage; exit 0 ;;
  setup)
    setup_detect_flags=()
    setup_configure_flags=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --candidate)
          [ -n "${2:-}" ] || { usage >&2; exit 2; }
          setup_detect_flags+=("$1" "$2")
          shift
          ;;
        --accept-new-host-keys) setup_configure_flags+=("$1") ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
      esac
      shift
    done
    # Setup reuses valid saved membership and never adds a machine to it. An
    # explicit address would be ignored by a successful run, so it is refused
    # before anything runs, with or without a terminal.
    if [ "${#setup_detect_flags[@]}" -gt 0 ]; then
      . "$REPO_DIR/scripts/lib.sh"
      if [ -e "$CLUSTER_TOPOLOGY_FILE" ] && "$0" show >/dev/null 2>&1; then
        rejected=()
        for ((i = 1; i < ${#setup_detect_flags[@]}; i += 2)); do rejected+=("${setup_detect_flags[$i]}"); done
        hosts=$(printf '%s, ' "${rejected[@]}"); hosts=${hosts%, }
        candidate_args=$(printf -- '--candidate %s ' "${rejected[@]}"); candidate_args=${candidate_args% }
        {
          verb=was; [ "${#rejected[@]}" -eq 1 ] || verb=were
          printf "error: setup doesn't add machines to saved membership; %s %s not added.\n" "$hosts" "$verb"
          printf '  To add a machine, run ./pulsar topology configure %s\n' "$candidate_args"
          printf '  or choose Cluster topology → Configure membership or add a machine.\n'
          printf '  To discover without saving, run ./pulsar topology detect %s\n' "$candidate_args"
        } >&2
        exit 2
      fi
    fi
    # Its confirmations draw with Gum; without it, run the steps separately.
    . "$REPO_DIR/scripts/ui.sh"
    require_gum "guided setup" "pulsar topology detect, then topology configure --yes and ssh-trust enroll --yes after review"
    ui_keep_styles
    . "$REPO_DIR/scripts/lib.sh"
    if [ ! -e "$CLUSTER_TOPOLOGY_FILE" ]; then
      echo 'No saved membership. Detecting candidates without saving first.'
      while true; do
        # Leaving the review ends setup without membership, which is not success.
        review_candidates "${setup_detect_flags[@]}" || {
          rc=$?; [ "$rc" -ne 130 ] || exit 130
          echo 'Setup stopped before saving cluster membership.'
          exit 1
        }
        setup_detect_flags=("${REVIEW_CANDIDATES[@]}")
        # Standalone configure keeps exit 0 for a declined save. Only this
        # coordinator requests a distinct cancellation result; file existence
        # cannot tell a save from a concurrent writer after a declined prompt.
        if _PULSAR_SETUP_CONFIGURE=1 "$0" configure \
            "${setup_detect_flags[@]}" "${setup_configure_flags[@]}"; then
          break
        else
          rc=$?
          [ "$rc" -ne 130 ] || exit 130
          if [ "$rc" != 125 ]; then
            echo 'Configuration did not complete. Review the error above before retrying.'
          fi
        fi
      done
    elif "$0" show >/dev/null 2>&1; then
      :
    else
      rc=$?; [ "$rc" -ne 130 ] || exit 130
      "$0" show || { rc=$?; [ "$rc" -ne 130 ] || exit 130; }
      echo 'Saved membership is invalid. Running diagnostic discovery without replacing it.'
      "$0" detect "${setup_detect_flags[@]}" || { rc=$?; [ "$rc" -ne 130 ] || exit 130; }
      echo 'Saved membership was not changed. Inspect it before explicit topology configure.'
      exit 1
    fi
    # A cancelled configuration, invalid membership or failed enrollment cannot
    # fall through to a success message or trigger a model operation.
    "$0" show || exit $?
    if "$REPO_DIR/scripts/topology-ssh-trust.sh" check; then
      :
    else
      rc=$?; [ "$rc" -ne 130 ] || exit 130
      echo 'SSH enrollment is missing or needs attention. Review the identities before confirming enrollment.'
      "$REPO_DIR/scripts/topology-ssh-trust.sh" enroll || exit $?
      "$REPO_DIR/scripts/topology-ssh-trust.sh" check || exit $?
    fi
    if "$0" check; then
      exit 0
    else
      rc=$?; [ "$rc" -ne 130 ] || exit 130
    fi
    echo 'Saved membership is not ready. Running diagnostic discovery without saving.'
    "$0" detect "${setup_detect_flags[@]}" || { rc=$?; [ "$rc" -ne 130 ] || exit 130; }
    echo 'Saved membership was not changed. Review discovery before explicit topology configure.'
    exit 1 ;;
  menu)
    [ $# = 0 ] || { echo "error: the topology menu is interactive; use ./pulsar topology show --json" >&2; exit 2; }
    . "$REPO_DIR/scripts/ui.sh"
    require_gum "the topology menu" "pulsar topology show | check | setup | detect | configure"
    ui_keep_styles
    # Each action runs as a child and returns here; Back leaves this menu.
    # Saved membership and the check's report open in the pager when long.
    while true; do
      choice=$(choose_index "Cluster topology" "First-use setup" "Show saved membership" \
        "Check saved nodes and fabric" "Detect cluster candidates" \
        "Configure membership or add a machine" "Check SSH trust" "Enroll SSH trust" "Back") \
        || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
      set +e
      case "$choice" in
        0) "$0" setup ;;
        1) page_output "" "$0" show ;;
        2) page_output "Checking saved nodes and fabric…" "$0" check ;;
        3) "$0" detect ;;
        4)
          if review_candidates; then
            # Always use configure here: setup reuses valid saved membership.
            # Return to the menu even on exit 0; configure may have been
            # cancelled, and enrollment remains a separate explicit action.
            "$0" configure "${REVIEW_CANDIDATES[@]}"
          else
            rc=$?; [ "$rc" -ne 130 ] || exit 130
          fi ;;
        5) "$REPO_DIR/scripts/topology-ssh-trust.sh" check ;;
        6) "$REPO_DIR/scripts/topology-ssh-trust.sh" enroll ;;
        *) exit 0 ;;
      esac
      rc=$?
      set -e
      [ "$rc" -ne 130 ] || exit 130
      echo
    done ;;
  detect|configure)
    flags=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --candidate) [ -n "${2:-}" ] || { usage >&2; exit 2; }; flags+=("$1" "$2"); shift ;;
        --json) [ "$action" = detect ] || { usage >&2; exit 2; }; flags+=("$1") ;;
        --yes|-y|--accept-new-host-keys) [ "$action" = configure ] || { usage >&2; exit 2; }; flags+=("$1") ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
      esac
      shift
    done
    [ "$action" != configure ] || flags+=(--write-topology)
    exec "$REPO_DIR/scripts/detect-fabric.sh" "${flags[@]}" ;;
  show|check) ;;
  *) exec python3 "$REPO_DIR/scripts/topology_manifest.py" "$action" "$@" ;;
esac

json=()
case "$*" in
  "") ;;
  --json) json=(--json) ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
. "$REPO_DIR/scripts/lib.sh"
tool="$REPO_DIR/scripts/topology_actions.py"
if [ "$action" = show ]; then
  exec python3 "$tool" show "$CLUSTER_TOPOLOGY_FILE" "${json[@]}"
fi
# Validate before any probes. Missing and invalid state produce structured output.
if ! python3 "$tool" show "$CLUSTER_TOPOLOGY_FILE" --json >/dev/null; then
  exec python3 "$tool" show "$CLUSTER_TOPOLOGY_FILE" "${json[@]}"
fi
. "$REPO_DIR/scripts/topology-probes.sh"
temporary=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-topology-check.XXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
if load_cluster_topology; then
  for ((rank=0; rank<CLUSTER_TOPOLOGY_COUNT; rank++)); do
    if [ "$rank" = 0 ]; then
      probe_node_json_for_rank "$rank" >"$temporary/rank-$rank.json" 2>/dev/null || rm -f "$temporary/rank-$rank.json"
    else
      host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
      topology_control_ssh "$host" "${CLUSTER_NODE_CONTROL_IPS[$rank]}" \
        "$(probe_node_remote_python_command "$host")" <"$REPO_DIR/scripts/probe-node.py" \
        >"$temporary/rank-$rank.json" 2>/dev/null || rm -f "$temporary/rank-$rank.json"
    fi
  done
  topology_check_fabric "$CLUSTER_TOPOLOGY_FILE" || touch "$temporary/fabric-failed"
  touch "$temporary/fabric-checked"
else
  touch "$temporary/configuration-error"
fi
python3 "$tool" check "$CLUSTER_TOPOLOGY_FILE" --observations "$temporary" "${json[@]}"
