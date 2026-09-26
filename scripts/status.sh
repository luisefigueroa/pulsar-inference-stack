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
  printf 'Usage: pulsar status SPEC_ID [--spec-file FILE] [--node NODE] [--json]\n'
  exit 0
fi
JSON=0
for arg in "$@"; do [ "$arg" != --json ] || JSON=1; done
if ! observation=$("$ROOT/scripts/observe-serving.sh" "$@"); then
  inventory=$("$ROOT/scripts/inventory.sh" --json) || exit 1
  observation=$(printf '%s' "$inventory" | python3 -c '
import json,sys
inventory=json.load(sys.stdin)
services=[row for row in inventory.get("services",[]) if row.get("conf")==sys.argv[1]]
if not services: raise SystemExit("No matching service could be observed")
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
