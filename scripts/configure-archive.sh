#!/usr/bin/env bash
# Configure an existing operator-selected archive directory; no storage administration.
set -euo pipefail
STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$STACK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
command_name="${1:-show}"
if [ "$command_name" != menu ]; then
  if [ "$#" -eq 0 ]; then set -- show; fi
  exec python3 -m model_library.configuration --repo-root "$STACK_ROOT" "$@"
fi
if [ ! -t 0 ]; then
  echo 'Archive configuration menu needs a terminal.' >&2
  echo 'Use: ./pulsar configure archive-root show|set PATH --yes|disable --yes' >&2
  exit 2
fi
python3 -m model_library.configuration --repo-root "$STACK_ROOT" show
if command -v gum >/dev/null 2>&1 && [ "${PULSAR_PLAIN:-0}" != 1 ]; then
  choice=$(gum choose 'Set archive location' 'Disable archives' 'Back') || exit 0
  case "$choice" in
    'Set archive location')
      path=$(gum input --placeholder 'Existing absolute directory') || exit 0
      gum confirm "Save this archive location? $path" || exit 0
      exec python3 -m model_library.configuration --repo-root "$STACK_ROOT" set "$path" --yes
      ;;
    'Disable archives')
      gum confirm 'Disable the saved archive location?' || exit 0
      exec python3 -m model_library.configuration --repo-root "$STACK_ROOT" disable --yes
      ;;
    *) exit 0 ;;
  esac
fi
printf '\n1. Set archive location\n2. Disable archives\n3. Back\nChoice: '
read -r choice
case "$choice" in
  1)
    printf 'Existing absolute directory: '
    read -r path
    printf 'Save this archive location? [y/N] '
    read -r confirm
    case "$confirm" in y|Y|yes|YES) exec python3 -m model_library.configuration --repo-root "$STACK_ROOT" set "$path" --yes ;; esac
    ;;
  2)
    printf 'Disable the saved archive location? [y/N] '
    read -r confirm
    case "$confirm" in y|Y|yes|YES) exec python3 -m model_library.configuration --repo-root "$STACK_ROOT" disable --yes ;; esac
    ;;
esac
