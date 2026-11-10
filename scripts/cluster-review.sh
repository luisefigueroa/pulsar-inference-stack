#!/usr/bin/env bash
# Review what changed… (Cluster menu): saved membership against what the last
# discovery saw (scripts/cluster_status.py review), then one question:
#   Save the cluster as it is now          ./pulsar topology configure
#   Save the cluster without NODE          ./pulsar topology configure --remove-missing
#   Keep NODE; it is only down             saved beside the changes; asked again
#                                          only when a later check sees others
# A save asks its own question before it writes; this menu's choice leads
# there and is not a second question. After a save, recording the nodes' SSH
# identities keeps its own confirmation with the fingerprints
# (./pulsar ssh-trust enroll). Nothing here probes a node until a choice runs.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "the review of cluster changes" \
  "./pulsar topology detect" "see what changed; saves nothing" \
  "./pulsar topology configure" "save the cluster as it is now; asks first" \
  "./pulsar topology configure --remove-missing" "save it without a node that did not answer; asks first" \
  "./pulsar ssh-trust enroll" "then record the nodes' SSH identities; asks first"
ui_keep_styles

status() { python3 -m scripts.cluster_status "$@" --repo-root "$REPO_DIR"; }

# saved_id: the saved membership's topology_id, or nothing.
saved_id() { python3 -m scripts.cluster_status saved-id --repo-root "$REPO_DIR" 2>/dev/null || true; }

# save ARGS...: configure asks its own question; identities follow a save.
save() {
  local before after rc=0 nodes
  before=$(saved_id)
  "$REPO_DIR/pulsar" topology configure "$@" || rc=$?
  [ "$rc" -ne 130 ] || return 130
  after=$(saved_id)
  if [ "$rc" -ne 0 ]; then
    printf '\n✗ The cluster was not saved; the reason is above. Nothing changed.\n'
    wait_continue "Cluster not saved" || return
    return 0
  fi
  if [ -z "$after" ] || [ "$before" = "$after" ]; then
    printf '\nNothing was saved; the cluster is unchanged.\n'
    return 0
  fi
  nodes=$(python3 -m scripts.cluster_status node-count --repo-root "$REPO_DIR" 2>/dev/null || echo 0)
  if [ "$nodes" -gt 1 ]; then
    printf '\nThe cluster is saved. Next, Pulsar records each node'"'"'s SSH identity; it shows the fingerprints and asks first.\n'
    rc=0
    "$REPO_DIR/pulsar" ssh-trust enroll || rc=$?
    [ "$rc" -ne 130 ] || return 130
    if [ "$rc" -ne 0 ]; then
      printf '\n✗ The SSH identities were not recorded; the reason is above. Choose Check the cluster now to see what is left.\n'
    fi
  fi
  python3 -m scripts.terminal_format --text <<'SAVED'

Models prepared before this save keep their files. When one is next prepared, Prepare takes over each copy that still verifies on the same node, and nothing is copied again.
SAVED
  wait_continue "Cluster saved" || return
}

# The screen is rendered where it is shown, so it fits the pager's width.
rc=0
page_output "" python3 -m scripts.cluster_status review --repo-root "$REPO_DIR" || rc=$?
[ "$rc" -ne 130 ] || exit 130
[ "$rc" -eq 0 ] || { printf '✗ The review could not be shown; review the error above.\n'; exit 1; }
labels=() actions=()
while IFS=$'\t' read -r action label; do
  [ -n "$action" ] || continue
  actions+=("$action"); labels+=("$label")
done < <(status review --choices)
choice=$(choose_index "Review what changed" "${labels[@]}") \
  || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
case "${actions[$choice]}" in
  save) save ;;
  drop) save --remove-missing ;;
  keep)
    if python3 -m scripts.saved_observations keep-cluster-changes --repo-root "$REPO_DIR"; then
      printf '\nKept as a member. It stays under Attention until it answers, and Pulsar asks again only when a later check sees different changes.\n' \
        | python3 -m scripts.terminal_format --text
    fi ;;
  check)
    # Inside the check, the check runs again there; elsewhere it runs here.
    [ "${_PULSAR_REVIEW_FROM_CHECK:-0}" != 1 ] || exit 10
    exec "$REPO_DIR/scripts/cluster-overview.sh" ;;
  *) exit 0 ;;
esac
