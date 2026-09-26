#!/usr/bin/env bash
# Configure an existing operator-selected archive directory; no storage administration.
set -euo pipefail
STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$STACK_ROOT"
CONFIG_ROOT="${PULSAR_SETUP_ROOT:-$STACK_ROOT}"
export PYTHONPATH="$STACK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
command_name="${1:-show}"
if [ "$command_name" != menu ]; then
  if [ "$#" -eq 0 ]; then set -- show; fi
  exec python3 -m model_library.configuration --repo-root "$CONFIG_ROOT" "$@"
fi
if [ ! -t 0 ] && [ "${PULSAR_FORCE_MENU:-0}" != 1 ]; then
  echo 'Archive configuration menu needs a terminal.' >&2
  echo 'Use: ./pulsar configure archive-root show|set PATH --yes|disable --yes' >&2
  exit 2
fi
# shellcheck source=ui.sh
. "${PULSAR_HOME_UI:-$STACK_ROOT/scripts/ui.sh}"

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
