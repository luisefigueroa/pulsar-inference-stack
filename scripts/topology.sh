#!/usr/bin/env bash
# Operator topology boundary; discovery is never a serving action.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

usage() {
  PYTHONPATH="$REPO_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PYHELP'
from terminal_format import TerminalWriter
writer = TerminalWriter()
writer.emit("Usage: pulsar topology COMMAND [options]")
for command, description in [
    ("setup [--candidate HOST ...]", "Guided first use: confirm membership, enroll SSH identity, then check readiness."),
    ("show [--json]", "Read saved membership; no live probes."),
    ("check [--json]", "Check all saved nodes and fabric without saving."),
    ("detect [--json] [--candidate HOST ...]", "Discover without changing membership or SSH trust."),
    ("configure [--candidate HOST ...] [--yes] [--accept-new-host-keys]", "Review and explicitly save membership; no model actions."),
    ("menu", "Open the Gum/plain topology menu without probing."),
]:
    writer.emit(command, initial_indent="  ", subsequent_indent="    ")
    writer.emit(description, initial_indent="    ", subsequent_indent="    ")
writer.blank()
writer.emit("New SSH trust requires separate authorization. Existing low-level topology utilities remain available with their original arguments.")
PYHELP
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
          setup_configure_flags+=("$1" "$2")
          shift
          ;;
        --accept-new-host-keys) setup_configure_flags+=("$1") ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
      esac
      shift
    done
    if [ ! -t 0 ]; then
      echo 'First-use setup is interactive. Use topology configure and ssh-trust enroll separately with explicit approval.' >&2
      exit 2
    fi
    . "$REPO_DIR/scripts/lib.sh"
    if [ ! -e "$CLUSTER_TOPOLOGY_FILE" ]; then
      echo 'No saved membership. Detecting candidates without saving first.'
      "$0" detect "${setup_detect_flags[@]}" || exit $?
      "$0" configure "${setup_configure_flags[@]}" || exit $?
    elif ! "$0" show >/dev/null 2>&1; then
      "$0" show || true
      echo 'Saved membership is invalid. Running diagnostic discovery without replacing it.'
      "$0" detect "${setup_detect_flags[@]}" || true
      echo 'Saved membership was not changed. Inspect it before explicit topology configure.'
      exit 1
    fi
    # A cancelled configuration, invalid membership or failed enrollment cannot
    # fall through to a success message or trigger a model operation.
    "$0" show || exit $?
    if ! "$REPO_DIR/scripts/topology-ssh-trust.sh" check; then
      echo 'SSH enrollment is missing or needs attention. Review the identities before confirming enrollment.'
      "$REPO_DIR/scripts/topology-ssh-trust.sh" enroll || exit $?
      "$REPO_DIR/scripts/topology-ssh-trust.sh" check || exit $?
    fi
    if "$0" check; then
      exit 0
    fi
    echo 'Saved membership is not ready. Running diagnostic discovery without saving.'
    "$0" detect "${setup_detect_flags[@]}" || true
    echo 'Saved membership was not changed. Review discovery before explicit topology configure.'
    exit 1 ;;
  menu)
    [ $# = 0 ] || { usage >&2; exit 2; }
    . "$REPO_DIR/scripts/ui.sh"
    # Each action runs as a child and returns here; Back leaves this menu.
    while true; do
      choice=$(choose_index "Cluster topology" "First-use setup" "Show saved membership" \
        "Check saved nodes and fabric" "Detect cluster candidates" \
        "Configure cluster membership" "Check SSH trust" "Enroll SSH trust" "Back") \
        || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
      set +e
      case "$choice" in
        0) "$0" setup ;;
        1) "$0" show ;;
        2) "$0" check ;;
        3) "$0" detect ;;
        4) "$0" configure ;;
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
