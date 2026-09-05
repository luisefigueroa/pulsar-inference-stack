#!/usr/bin/env bash
# Read catalog specs without loading topology or local serving state.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$ROOT/scripts/release_consumer.py" --repo-root "$ROOT" "$@"
