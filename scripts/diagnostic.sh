#!/usr/bin/env bash
# Confirmed placement and supervised transport for model-free diagnostics.
set -euo pipefail
SCRIPT_NAME=diagnostic
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIAGNOSTIC_MODE=run
case "${1:-}" in run|stage-image) DIAGNOSTIC_MODE="$1"; shift ;; esac
DIAGNOSTIC_REQUEST="" DIAGNOSTIC_PAYLOAD="" DIAGNOSTIC_REQUEST_ID="" DIAGNOSTIC_OUTPUT="" DIAGNOSTIC_YES=0 DIAGNOSTIC_PLAN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --request) DIAGNOSTIC_REQUEST="${2:?request required}"; shift ;;
    --payload-dir) DIAGNOSTIC_PAYLOAD="${2:?payload directory required}"; shift ;;
    --request-id) DIAGNOSTIC_REQUEST_ID="${2:?reviewed request hash required}"; shift ;;
    --output-dir) DIAGNOSTIC_OUTPUT="${2:?fresh output directory required}"; shift ;;
    --yes) DIAGNOSTIC_YES=1 ;;
    --plan) DIAGNOSTIC_PLAN=1 ;;
    --help|-h)
      if [ "$DIAGNOSTIC_MODE" = stage-image ]; then
        echo 'usage: pulsar diagnostic stage-image --request FILE --payload-dir DIR --request-id SHA256 --output-dir NEW_DIR (--plan | --yes) [--json]'
        echo 'Preview or explicitly stream an exact local image to missing confirmed ranks. No pull fallback or GPU launch.'
      else
        echo 'usage: pulsar diagnostic run --request FILE --payload-dir DIR --request-id SHA256 --output-dir NEW_DIR --yes [--json]'
        echo 'Uses confirmed GB10 ranks and an already-present exact image. Never pulls images, replaces services, or loads model snapshots.'
      fi
      exit 0 ;;
    *) echo "diagnostic: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DIAGNOSTIC_REQUEST" ] && [ -n "$DIAGNOSTIC_PAYLOAD" ] && [ -n "$DIAGNOSTIC_REQUEST_ID" ] && [ -n "$DIAGNOSTIC_OUTPUT" ] || { echo 'diagnostic: complete reviewed inputs required' >&2; exit 2; }
if [ "$DIAGNOSTIC_MODE" = run ]; then
  [ "$DIAGNOSTIC_YES" = 1 ] && [ "$DIAGNOSTIC_PLAN" = 0 ] || { echo 'diagnostic run requires --yes and does not accept --plan' >&2; exit 2; }
else
  [ "$((DIAGNOSTIC_YES + DIAGNOSTIC_PLAN))" = 1 ] || { echo 'image staging requires exactly one of --plan or --yes' >&2; exit 2; }
fi
readonly DIAGNOSTIC_MODE DIAGNOSTIC_REQUEST DIAGNOSTIC_PAYLOAD DIAGNOSTIC_REQUEST_ID DIAGNOSTIC_OUTPUT DIAGNOSTIC_YES DIAGNOSTIC_PLAN
# The pure freezer runs before configuration, SSH or Docker is touched.
export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
python3 -m diagnostics.controller freeze --output "$DIAGNOSTIC_OUTPUT" --request "$DIAGNOSTIC_REQUEST" --payload-dir "$DIAGNOSTIC_PAYLOAD" --request-id "$DIAGNOSTIC_REQUEST_ID"
. "$repo/scripts/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
. "$REPO_DIR/scripts/image-transfer.sh"
[ "$PULSAR_PLATFORM_ID" = dgx-spark-gb10 ] || die 'diagnostic runner currently supports GB10 only'
acquire_model_library_lifecycle_lock exclusive
load_cluster_topology || die 'confirmed topology required'
mapfile -t requirements < <(python3 - "$DIAGNOSTIC_OUTPUT/input.json" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))['request'];print(v['topology_id']);print(v['geometry']['nodes'])
PY
)
[ "${#requirements[@]}" = 2 ] || die 'invalid diagnostic requirements'
[ "$CLUSTER_TOPOLOGY_ID" = "${requirements[0]}" ] || die 'confirmed topology differs from reviewed request'
[ "$CLUSTER_TOPOLOGY_COUNT" = "${requirements[1]}" ] || die 'exact confirmed rank coverage required'
require_profile_topology "$CLUSTER_TOPOLOGY_COUNT" roce-full-mesh 2 || die 'confirmed diagnostic fabric unavailable'
if [ "$CLUSTER_TOPOLOGY_COUNT" -gt 1 ]; then require_topology_ssh_trust >/dev/null || die 'SSH trust required'; fi
for ((rank=0;rank<CLUSTER_TOPOLOGY_COUNT;rank++)); do
  printf '%s\t%s\t%s\t%s\t%s\n' "$rank" "${CLUSTER_NODE_IDS[$rank]}" "${CLUSTER_NODE_CONTROL_IPS[$rank]}" \
    "${CLUSTER_NODE_CONTROL_IFS[$rank]}" "${CLUSTER_PROFILE_HCAS[$rank]:-${CLUSTER_NODE_HCAS[$rank]}}"
done | python3 -m diagnostics.controller bind --output "$DIAGNOSTIC_OUTPUT" --topology-id "$CLUSTER_TOPOLOGY_ID" --verbs-device "$PULSAR_RDMA_VERBS_DEVICE"
phase() {
  local action="$1" rank
  local -a MODEL_NODE_COMMAND=() batch_options=()
  if [ "$DIAGNOSTIC_MODE" = stage-image ]; then
    reload_cluster_topology || return 2
    [ "$CLUSTER_TOPOLOGY_ID" = "${requirements[0]}" ] && [ "$CLUSTER_TOPOLOGY_COUNT" = "${requirements[1]}" ] || return 2
    require_profile_topology "$CLUSTER_TOPOLOGY_COUNT" roce-full-mesh 2 || return 2
  fi
  for ((rank=0;rank<CLUSTER_TOPOLOGY_COUNT;rank++)); do
    model_node_command "$rank" || return $?
    python3 -m diagnostics.controller task --output "$DIAGNOSTIC_OUTPUT" --phase "$action" --rank "$rank" -- \
      python3 -m model_library.verification_process --owner "$$" -- "${MODEL_NODE_COMMAND[@]}" || return $?
  done
  python3 -m diagnostics.controller tasks --output "$DIAGNOSTIC_OUTPUT" --phase "$action" || return $?
  if [ "$action" = cleanup ]; then batch_options+=(--keep-going); fi
  python3 -m model_library.verification_process batch --tasks "$DIAGNOSTIC_OUTPUT/$action/tasks.json" \
    --directory "$DIAGNOSTIC_OUTPUT/$action" --jobs "$CLUSTER_TOPOLOGY_COUNT" "${batch_options[@]}" || return $?
  python3 -m diagnostics.controller check --output "$DIAGNOSTIC_OUTPUT" --phase "$action"
}
if [ "$DIAGNOSTIC_MODE" = stage-image ]; then
  if [ "$DIAGNOSTIC_PLAN" = 1 ]; then
    phase image-before
    python3 -m diagnostics.images plan --output "$DIAGNOSTIC_OUTPUT"
    exit 0
  fi
  image_finalized=0
  finish_image() {
    if [ "$image_finalized" = 0 ]; then
      image_finalized=1
      phase image-after || true
      python3 -m diagnostics.images finish --output "$DIAGNOSTIC_OUTPUT"
    fi
  }
  trap 'finish_image >/dev/null || true' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  phase image-before
  python3 -m diagnostics.images plan --output "$DIAGNOSTIC_OUTPUT" >"$DIAGNOSTIC_OUTPUT/image-plan.stdout"
  python3 -m diagnostics.images targets --output "$DIAGNOSTIC_OUTPUT" >"$DIAGNOSTIC_OUTPUT/receivers"
  image=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["image_id"])' "$DIAGNOSTIC_OUTPUT/image-plan.json")
  limit=$(python3 -c 'from diagnostics.images import TRANSFER_TIMEOUT_SECONDS; print(TRANSFER_TIMEOUT_SECONDS)')
  while IFS= read -r rank; do
    # Refresh every node before each stream; changing readiness cannot silently
    # add receivers or authorize a different image or topology.
    phase "image-check-$rank"
    state=$(python3 -m diagnostics.images recheck --output "$DIAGNOSTIC_OUTPUT" --rank "$rank")
    rc=0
    if [ "$state" = missing ]; then
      python3 -m diagnostics.images started --output "$DIAGNOSTIC_OUTPUT" --rank "$rank"
      stream_image_to_node "$image" "$rank" "$limit" >"$DIAGNOSTIC_OUTPUT/transfer-$rank.stdout" 2>"$DIAGNOSTIC_OUTPUT/transfer-$rank.stderr" || rc=$?
    fi
    python3 -m diagnostics.images finished --output "$DIAGNOSTIC_OUTPUT" --rank "$rank" --returncode "$rc"
    [ "$rc" = 0 ] || break
  done <"$DIAGNOSTIC_OUTPUT/receivers"
  finish_image
  exit 0
fi
cleanup_started=0
cleanup() {
  if [ "$cleanup_started" = 0 ]; then
    cleanup_started=1
    phase cleanup || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if phase preflight; then phase execute || true; fi
cleanup
python3 -m diagnostics.controller finish --output "$DIAGNOSTIC_OUTPUT"
