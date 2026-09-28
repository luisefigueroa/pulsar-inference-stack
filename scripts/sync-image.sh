#!/usr/bin/env bash
# Explicit image staging; inspection never pulls. Streaming falls back to pulling
# the exact digest only with --pull-if-stream-incomplete, which start passes when
# --pull-image grants pulling.
set -euo pipefail
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then echo "Usage: sync-image.sh SPEC [--spec-file FILE] [--node NODE] [--plan | --yes] [--pull | --pull-if-stream-incomplete]"; exit 0; fi
SCRIPT_NAME=sync-image
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
NAME="${1:?spec id required}"; shift
PULL=0 YES=0 PLAN=0 NODE_SELECTOR="" PULL_FALLBACK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pull) PULL=1 ;;
    --pull-if-stream-incomplete) PULL_FALLBACK=1 ;;
    --yes|-y) YES=1 ;;
    --plan) PLAN=1 ;;
    --node) NODE_SELECTOR="${2:?node required}"; shift ;;
    --spec-file) export PULSAR_SPEC_FILE="${2:?spec file required}"; shift ;;
    *) die "unknown argument: $1" 2 ;;
  esac
  shift
done
[ "$YES" = 0 ] || [ "$PLAN" = 0 ] || die "preview and apply are separate operations" 2
load_conf "$NAME"
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
  ok) log "Exact spec image already present on all $NODES serving ranks."; exit 0 ;;
  missing-on-head|missing-on-rank|missing-both) ;;
  *) die "all target nodes must be observable and image identities valid before staging ($state)" ;;
esac
mode=stream-from-controller
[ "$PULL" = 0 ] || mode=pull-exact-digest
if [ "$PLAN" = 1 ]; then
  printf '%s' "$report" | python3 -c 'import json,sys; d=json.load(sys.stdin);d["operation"]="stage-image";d["mode"]=sys.argv[1];print(json.dumps(d,indent=2))' "$mode"
  exit 0
fi
[ "$YES" = 1 ] || die "image staging requires --yes after reviewing --plan" 2
if [ "$PULL" = 0 ]; then
  # Docker save/load can omit registry references. A stream never falls back to
  # a registry pull unless the caller passed --pull-if-stream-incomplete.
  "$PULSAR_DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || die "controller lacks pinned image; choose --pull --yes or acquire it explicitly"
fi
mapfile -t missing < <(printf '%s' "$report" | python3 -c 'import json,sys; [print(r["topology_index"]) for r in json.load(sys.stdin)["ranks"] if r["state"]=="missing"]')
for physical in "${missing[@]}"; do
  if [ "$PULL" = 1 ]; then
    if [ "$physical" = 0 ]; then "$PULSAR_DOCKER" pull "$IMAGE"; else ssh_node "$physical" "$(shell_join_q docker pull "$IMAGE")"; fi
  else
    [ "$physical" != 0 ] || die "local missing image requires explicit --pull"
    "$PULSAR_DOCKER" save "$IMAGE" | ssh_node "$physical" 'docker load'
  fi
done
if ! "$REPO_DIR/scripts/check-image.sh" "$NAME" "${placement[@]}" --json >/dev/null; then
  if [ "$PULL" != 0 ] || [ "$PULL_FALLBACK" != 1 ]; then
    die "staging did not establish the pinned digest on every node; rerun with --pull to pull the exact digest"
  fi
  report=$("$REPO_DIR/scripts/check-image.sh" "$NAME" "${placement[@]}" --json || true)
  mapfile -t still < <(printf '%s' "$report" | python3 -c 'import json,sys; [print(r["topology_index"]) for r in json.load(sys.stdin)["ranks"] if r["state"]=="missing"]' 2>/dev/null)
  for physical in "${still[@]}"; do
    log "pulling the pinned image on $(human_node_name "$physical"); streaming did not keep its digest"
    if [ "$physical" = 0 ]; then "$PULSAR_DOCKER" pull "$IMAGE"; else ssh_node "$physical" "$(shell_join_q docker pull "$IMAGE")"; fi \
      || die "pulling $IMAGE failed on $(human_node_name "$physical"); check registry access on that node"
  done
  "$REPO_DIR/scripts/check-image.sh" "$NAME" "${placement[@]}" --json >/dev/null \
    || die "the pinned image is still missing after pulling it; run ./pulsar doctor"
fi
log "Pinned spec image verified on all $NODES serving ranks."
