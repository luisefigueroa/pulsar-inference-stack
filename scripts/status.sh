#!/usr/bin/env bash
# Explicit exact-spec live status. Catalog browsing uses saved observations.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ $# = 0 ]; then
  "$ROOT/scripts/release.sh" list
  printf '\nSelect a spec for a live check: ./pulsar status <spec_id>\n'
  exit 0
fi
if [ "$1" = --help ] || [ "$1" = -h ]; then
  printf 'Usage: scripts/status.sh <spec_id> [--spec-file FILE] [--node NODE] [--json]\n'
  exit 0
fi
JSON=0
for arg in "$@"; do [ "$arg" != --json ] || JSON=1; done
observation=$("$ROOT/scripts/observe-serving.sh" "$@") || {
  printf 'Serving state could not be verified on every expected rank.\n' >&2
  exit 1
}
if [ "$JSON" = 1 ]; then printf '%s\n' "$observation"; else
  printf '%s' "$observation" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("Serving recipe and files verified on all",len(d["ranks"]),"ranks."); print("Spec:",d["spec_id"]); print("API:",d["api_url"])'
fi
