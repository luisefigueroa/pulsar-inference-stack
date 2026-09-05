#!/usr/bin/env bash
# The wizard browses the catalog; every operation remains an explicit choice.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$root/scripts/model-storage.sh" menu "$@"
