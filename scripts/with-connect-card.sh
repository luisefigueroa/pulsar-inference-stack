#!/usr/bin/env bash
# with-connect-card.sh COMMAND... — runs a Start or Live status command for the
# menu. When it succeeds and names a running service's API, prints the Connect
# card (scripts/connect_card.py): the API URL, served model name, whether a key
# is required, and a first request that never contains the key. Exits with the
# command's status.
set -uo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
result=$(mktemp) || exec "$@"
trap 'rm -f -- "$result"' EXIT
rc=0
PULSAR_SERVICE_RESULT_FILE="$result" "$@" || rc=$?
if [ "$rc" -eq 0 ] && [ -s "$result" ]; then
  # The card reads the service's launch record from the model library. Load
  # the configuration in a subshell as every ./pulsar command does, so that a
  # library location set only in this checkout's .env is read.
  (. "$REPO_DIR/scripts/lib.sh" >/dev/null 2>&1; export PULSAR_MODEL_LIBRARY_DIR
   python3 "$REPO_DIR/scripts/connect_card.py" --result "$result") || true
fi
exit "$rc"
