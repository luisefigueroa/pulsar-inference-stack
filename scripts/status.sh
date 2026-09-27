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
usage: pulsar status SPEC_ID [--node NODE_ID] [--spec-file FILE] [--json]
       pulsar status

Observe the live service for the exact spec on every participating node.

  --node NODE_ID    One-node spec: the node ID of its recorded service; the
                    recorded node is used when omitted
  --spec-file FILE  Name a workbench candidate; the service's recorded spec
                    is what status observes
  --json            Print the observation as JSON

Without SPEC_ID, list the catalog. When the service cannot be fully observed,
status reports the service inventory instead and says so.
HELP
  exit 0
fi
JSON=0
for arg in "$@"; do [ "$arg" != --json ] || JSON=1; done
observe_rc=0
observation=$("$ROOT/scripts/observe-serving.sh" "$@") || observe_rc=$?
# A --json usage error is reported as such, not replaced by the inventory view.
if [ -n "${PULSAR_USAGE_EXIT:-}" ] && [ "$observe_rc" = "$PULSAR_USAGE_EXIT" ]; then exit "$observe_rc"; fi
if [ "$observe_rc" != 0 ]; then
  inventory=$("$ROOT/scripts/inventory.sh" --json) || exit 1
  observation=$(printf '%s' "$inventory" | python3 -c '
import json,sys
inventory=json.load(sys.stdin)
services=[row for row in inventory.get("services",[]) if row.get("conf")==sys.argv[1]]
if not services:
    worker=inventory.get("worker") or {}
    # Absence is established only when every remote node was observed.
    if worker.get("status") not in ("ok","unset"):
        reason=worker.get("reason") or "a node could not be observed"
        raise SystemExit(f"Service state for spec {sys.argv[1][:12]} is unknown: {reason}. Run ./pulsar topology check")
    raise SystemExit(f"No running service for spec {sys.argv[1][:12]} was observed. Start it with ./pulsar start {sys.argv[1][:12]}")
print(json.dumps(dict(schema_version=1,kind="pulsar-service-status",selected_spec_id=sys.argv[1],
    configuration_verified=False,services=services,
    message="Service inventory only; complete current-spec observation is unavailable.")))
' "$1") || exit 1
fi
if [ "$JSON" = 1 ]; then printf '%s\n' "$observation"; else
  printf '%s' "$observation" | python3 -c '
import json,sys
value=json.load(sys.stdin)
if value.get("kind")=="pulsar-service-status":
 print(value["message"])
 for service in value["services"]: print("State:",service["state"],"Ownership:",service["ownership"])
else:
 print("Serving recipe and files verified on all",len(value["ranks"]),"ranks.")
 print("Spec:",value["spec_id"])
 if not value["matches_selected_spec"]:
  print("Modified recipe. Selected catalog spec:",value["selected_spec_id"])
  print("Selected-recipe measurements are reference only.")
 print("API:",value["api_url"])
'
fi
