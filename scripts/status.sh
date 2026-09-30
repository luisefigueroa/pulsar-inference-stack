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
  python3 "$ROOT/scripts/terminal_format.py" <<'HELP'
usage: pulsar status SPEC_ID [--node NODE] [--spec-file FILE] [--json]
       pulsar status

Observe the live service for the exact spec on every participating node.

  --node NODE       One-node spec: the recorded service's node, by hostname or
                    node ID; the recorded node is used when omitted
  --spec-file FILE  Name a workbench candidate; the service's recorded spec
                    is what status observes
  --json            Print the observation as JSON

Without SPEC_ID, list the catalog. When the service cannot be fully observed,
status reports the service inventory instead and says so.
HELP
  exit 0
fi
# A command-line mistake exits 2, or the public CLI's private usage status.
usage_error() { printf 'error: %s\n' "$1" >&2; exit "${PULSAR_USAGE_EXIT:-2}"; }
JSON=0 SPEC=""
observe_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1; observe_args+=("$1") ;;
    # --spec-file names a candidate for the spec ID; the recorded spec is what status observes.
    --spec-file)
      if [ $# -lt 2 ] || [ -z "$2" ]; then usage_error "--spec-file requires a file"; fi
      shift ;;
    --node|--service-id|--verification-jobs)
      if [ $# -lt 2 ] || [ -z "$2" ]; then usage_error "$1 requires a value"; fi
      observe_args+=("$1" "$2"); shift ;;
    -*) observe_args+=("$1") ;;
    *) [ -z "$SPEC" ] || usage_error "unexpected argument: $1"; SPEC="$1"; observe_args+=("$1") ;;
  esac
  shift
done
[ -n "$SPEC" ] || usage_error "status requires a spec ID; run ./pulsar status to list the catalog specs"
json_flag=()
[ "$JSON" != 1 ] || json_flag=(--json)

work=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-status.XXXXXX")
trap 'rm -rf "$work"' EXIT
# The complete observation reports its own usage mistakes with a private
# status, so every other failure falls back to the service inventory.
observe_rc=0
PULSAR_USAGE_EXIT=64 "$ROOT/scripts/observe-serving.sh" "${observe_args[@]}" \
  >"$work/observation.json" 2>"$work/observe.err" || observe_rc=$?
case "$observe_rc" in
  0)
    cat "$work/observe.err" >&2
    python3 "$ROOT/scripts/service_status.py" --spec "$SPEC" --observation "$work/observation.json" \
      ${json_flag[@]+"${json_flag[@]}"}
    exit
    ;;
  64)
    cat "$work/observe.err" >&2
    exit "${PULSAR_USAGE_EXIT:-2}"
    ;;
esac
inventory_args=(--inventory "$work/inventory.json")
if ! "$ROOT/scripts/inventory.sh" --json >"$work/inventory.json" 2>"$work/inventory.err"; then
  cat "$work/inventory.err" >&2
  inventory_args=()
fi
python3 "$ROOT/scripts/service_status.py" --spec "$SPEC" ${inventory_args[@]+"${inventory_args[@]}"} \
  --observe-error "$work/observe.err" ${json_flag[@]+"${json_flag[@]}"}
