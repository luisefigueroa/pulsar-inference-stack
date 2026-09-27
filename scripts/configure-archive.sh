#!/usr/bin/env bash
# Configure an existing operator-selected archive directory; no storage administration.
set -euo pipefail
STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$STACK_ROOT"
CONFIG_ROOT="${PULSAR_SETUP_ROOT:-$STACK_ROOT}"
export PYTHONPATH="$STACK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
command_name="${1:-show}"
case "$command_name" in
  -h|--help|help)
    python3 "$STACK_ROOT/scripts/terminal_format.py" <<'HELP'
usage: pulsar configure archive-root [show] [--json]
       pulsar configure archive-root set PATH --yes [--json]
       pulsar configure archive-root disable --yes [--json]
       pulsar configure archive-root menu

Select the existing directory that holds archives, saved as PULSAR_COLD_ROOT in this checkout's .env.

  show            Show the archive location, its source and observed access (the default)
  set PATH --yes  Save an existing absolute directory; Pulsar never creates or mounts it
  disable --yes   Save an empty location, which disables archives; no archive is deleted
  menu            Choose in a menu; it needs an interactive terminal with Gum
  --json          Print the resulting configuration as JSON

A PULSAR_COLD_ROOT process value, including empty, takes precedence over the saved one.
HELP
    exit 0 ;;
esac
if [ "$command_name" != menu ]; then
  if [ "$#" -eq 0 ]; then set -- show; fi
  exec python3 -m model_library.configuration --repo-root "$CONFIG_ROOT" "$@"
fi
# shellcheck source=ui.sh
. "${PULSAR_HOME_UI:-$STACK_ROOT/scripts/ui.sh}"
require_gum "the archive storage menu" "pulsar configure archive-root show | set PATH --yes | disable --yes"

archive_cli() {
  python3 -m model_library.configuration --repo-root "$CONFIG_ROOT" "$@"
}

archive_cli show
choice=$(choose_index "Archive storage" "Set archive location" "Disable archives" "Back") \
  || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
case "$choice" in
  0)
    while true; do
      path=$(prompt_input "Existing absolute directory:" "/existing/absolute/directory") || exit 0
      if [ ! -d "$path" ]; then
        printf '%s\n' "$path is not an existing directory. Pulsar does not create archive storage." | emit_error
        confirm "Try another path?" yes || exit 0
        continue
      fi
      confirm "Save this archive location? $path" no || exit 0
      if ! output=$(archive_cli set "$path" --yes 2>&1); then
        printf '%s\n' "$output" | emit_error
        confirm "Try another path?" yes || exit 0
        continue
      fi
      printf '%s\n' "$output"
      exit 0
    done
    ;;
  1)
    confirm "Disable the saved archive location?" no || exit 0
    archive_cli disable --yes
    ;;
  *) exit 0 ;;
esac
