#!/usr/bin/env bash
# Explicit node-local migration; Python owns verification and plan comparison.
set -euo pipefail
STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$STACK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
case "${1:-}" in
  preview-view|apply-view) exec python3 -m model_library.migration_views "$@" ;;
  *) exec python3 -m model_library.migration "$@" ;;
esac
