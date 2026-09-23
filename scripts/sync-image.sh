#!/usr/bin/env bash
# Explicit image staging; inspection never pulls and streaming never falls back.
set -euo pipefail
if [ "${1:-}" = --diagnostic ]; then
  shift
  exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/diagnostic.sh" stage-image "$@"
fi
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then echo "Usage: pulsar image stage SPEC [--spec-file FILE] [--node NODE] [--plan | --yes] [--pull | --export-tag TAG] [--json]"; exit 0; fi
SCRIPT_NAME=sync-image
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
. "$REPO_DIR/scripts/image-transfer.sh"
NAME="${1:?spec id required}"; shift
PULL=0 YES=0 PLAN=0 JSON=0 NODE_SELECTOR="" EXPORT_TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pull) PULL=1 ;;
    --yes|-y) YES=1 ;;
    --plan) PLAN=1 ;;
    --json) JSON=1 ;;
    --export-tag) EXPORT_TAG="${2:?export tag required}"; shift ;;
    --node) NODE_SELECTOR="${2:?node required}"; shift ;;
    --spec-file) export PULSAR_SPEC_FILE="${2:?spec file required}"; shift ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done
[ "$YES" = 0 ] || [ "$PLAN" = 0 ] || die "preview and apply are separate operations" 2
[ "$PULL" = 0 ] || [ -z "$EXPORT_TAG" ] || die "export tag and registry pull are separate modes" 2
load_conf "$NAME"
export_image="$IMAGE"
source_image_id=""
verify_export_source() {
  local inspected
  inspected=$("$PULSAR_DOCKER" image inspect "$IMAGE" "$EXPORT_TAG") || die "export tag or pinned image is unavailable"
  printf '%s' "$inspected" | python3 -c '
import json,re,sys
image,tag=sys.argv[1:]
repository,digest=image.rsplit("@",1)
if not tag.startswith(repository+":") or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",tag[len(repository)+1:]):
 raise SystemExit("export tag must be an explicit tag in the pinned repository")
rows=json.load(sys.stdin)
if not isinstance(rows,list) or len(rows)!=2:raise SystemExit("export image observation incomplete")
for row in rows:
 if row.get("Architecture")!="arm64" or row.get("Os")!="linux" or not row.get("Id"):
  raise SystemExit("export requires exact ARM64 Linux images")
 if not any(ref.endswith("@"+digest) for ref in row.get("RepoDigests",[])):
  raise SystemExit("export tag does not resolve to the pinned digest")
if rows[0]["Id"]!=rows[1]["Id"]:raise SystemExit("export tag image differs from pinned image")
print(rows[0]["Id"])
' "$IMAGE" "$EXPORT_TAG"
}
if [ -n "$EXPORT_TAG" ]; then
  source_image_id=$(verify_export_source) || die "export tag identity check failed"
  export_image="$EXPORT_TAG"
fi
placement=()
if [ "$NODES" = 1 ]; then
  NODE_SELECTOR=$(spec_overlay_node_selector "$NODE_SELECTOR")
  resolve_single_node_placement "$NODE_SELECTOR" || die "selected node is not confirmed"
  placement=(--node "${SINGLE_NODE_ID:-$SINGLE_NODE_INDEX}")
elif [ -n "$NODE_SELECTOR" ]; then die "--node applies only to one-node specs" 2; fi
rc=0
report=$("$REPO_DIR/scripts/check-image.sh" "$NAME" "${placement[@]}" --json) || rc=$?
[ -n "$report" ] || die "image inspection failed"
state=$(printf '%s' "$report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
case "$state" in
  ok) ;;
  missing-on-head|missing-on-rank|missing-both) ;;
  *) die "all target nodes must be observable and image identities valid before staging ($state)" ;;
esac
mode=stream-from-controller
[ "$PULL" = 0 ] || mode=pull-exact-digest
if [ -n "$EXPORT_TAG" ]; then
  # Loading a named archive must never replace a different existing tag.
  mapfile -t destinations < <(printf '%s' "$report" | python3 -c 'import json,sys; [print(r["topology_index"]) for r in json.load(sys.stdin)["ranks"] if r["state"]=="missing"]')
  for physical in "${destinations[@]}"; do
    [ "$physical" != 0 ] || die "export tag requires the controller pinned image"
    ids=$(ssh_node "$physical" "$(shell_join_q docker image ls --all --quiet --no-trunc "$EXPORT_TAG")") || die "destination export tag observation failed"
    while IFS= read -r id; do
      [ -z "$id" ] || [ "$id" = "$source_image_id" ] || die "destination export tag belongs to another image"
    done <<<"$ids"
  done
fi
if [ "$PLAN" = 1 ]; then
  printf '%s' "$report" | python3 -c 'import json,sys; d=json.load(sys.stdin);d.update(operation="stage-image",mode=sys.argv[1],export_reference=sys.argv[2],source_image_id=sys.argv[3] or None);print(json.dumps(d,indent=2))' "$mode" "$export_image" "$source_image_id"
  exit 0
fi
if [ "$state" = ok ]; then
  if [ "$JSON" = 1 ]; then printf '%s\n' "$report"; else log "Exact spec image already present on all $NODES serving ranks."; fi
  exit 0
fi
[ "$YES" = 1 ] || die "image staging requires --yes after reviewing --plan" 2
if [ "$PULL" = 0 ]; then
  # Docker save/load can omit registry references. We deliberately do not pull
  # after a failed stream; registry acquisition is a separate explicit mode.
  "$PULSAR_DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || die "controller lacks pinned image; choose --pull --yes or acquire it explicitly"
fi
mapfile -t missing < <(printf '%s' "$report" | python3 -c 'import json,sys; [print(r["topology_index"]) for r in json.load(sys.stdin)["ranks"] if r["state"]=="missing"]')
for physical in "${missing[@]}"; do
  if [ "$PULL" = 1 ]; then
    if [ "$physical" = 0 ]; then "$PULSAR_DOCKER" pull "$IMAGE" >&2; else ssh_node "$physical" "$(shell_join_q docker pull "$IMAGE")" >&2; fi
  else
    [ "$physical" != 0 ] || die "local missing image requires explicit --pull"
    if [ -n "$EXPORT_TAG" ]; then
      [ "$(verify_export_source)" = "$source_image_id" ] || die "export source changed before transfer"
      ids=$(ssh_node "$physical" "$(shell_join_q docker image ls --all --quiet --no-trunc "$EXPORT_TAG")") || die "destination export tag observation failed"
      while IFS= read -r id; do
        [ -z "$id" ] || [ "$id" = "$source_image_id" ] || die "destination export tag changed before transfer"
      done <<<"$ids"
    fi
    stream_image_to_node "$export_image" "$physical" >&2
    if [ -n "$EXPORT_TAG" ]; then
      [ "$(verify_export_source)" = "$source_image_id" ] || die "export source changed during transfer"
    fi
  fi
done
report=$("$REPO_DIR/scripts/check-image.sh" "$NAME" "${placement[@]}" --json) \
  || die "staging did not establish pinned references on every rank; use explicit --pull if save/load omitted a digest reference"
if [ "$JSON" = 1 ]; then printf '%s\n' "$report"; else log "Pinned spec image verified on all $NODES serving ranks."; fi
