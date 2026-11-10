#!/usr/bin/env bash
# Why will a model not start? (Help and troubleshooting…): pick a model, run
# Start's checks without starting, and show each blocker with its fix as a
# choice. The model is picked from the Serve list's cards, read from saved
# records only; a one-node recipe asks which machine, as Serve a model… does.
# The checks are one `./pulsar start SPEC --dry-run --json`. Each fix is an
# intent, not a single operation: Serve a model… plans the files, the copies
# and the serving image; Running services…; Check the cluster now; Run
# diagnostics (scripts/start_reasons.py). Nothing here starts or changes a
# service, and nothing passes --replace, --pull-image or --accept-memory-warn.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=ui.sh
. "$REPO_DIR/scripts/ui.sh"
require_gum "Why will a model not start?" \
  "./pulsar models list" "the recipes and their saved state" \
  "./pulsar start SPEC --dry-run" "Start's checks without starting; each blocker names its fix" \
  "./pulsar topology check" "check the cluster's nodes and links" \
  "./pulsar doctor" "diagnostics with every node's prerequisites" \
  "./pulsar inventory" "what runs on the nodes"
ui_keep_styles

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pulsar-why.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
COLUMNS_SEEN=$(_ui_terminal_columns) || COLUMNS_SEEN=""
serve_plan() { python3 -m scripts.serve_plan "$@" --repo-root "$REPO_DIR" ${COLUMNS_SEEN:+--width "$COLUMNS_SEEN"}; }
reasons() { python3 -m scripts.start_reasons "$@" ${COLUMNS_SEEN:+--width "$COLUMNS_SEEN"}; }
text() { python3 -m scripts.terminal_format --text; }
TITLE="Why will a model not start?"

if ! CATALOG=$(python3 -m model_library.catalog list --json); then
  printf '\n✗ The catalog could not be read; the reason is above.\n'
  wait_continue "Catalog error" || exit $?
  exit 0
fi

# The Serve list's cards, as KIND<TAB>TEXT lines (serve_plan.py list).
CARD_IDS=() CARD_OPTIONS=()
read_cards() {
  local line kind body heading=""
  CARD_IDS=() CARD_OPTIONS=()
  while IFS= read -r line; do
    kind=${line%%$'\t'*} body=${line#*$'\t'}
    case "$kind" in
      heading) heading=$body ;;
      spec) CARD_IDS+=("${body%%$'\t'*}"); CARD_OPTIONS+=("${heading:+$heading$'\n'}${body#*$'\t'}"); heading="" ;;
      line) [ "${#CARD_OPTIONS[@]}" -eq 0 ] || CARD_OPTIONS[-1]+=$'\n'"$body" ;;
      gap) [ "${#CARD_OPTIONS[@]}" -eq 0 ] || CARD_OPTIONS[-1]+=$'\n' ;;
    esac
  done
}

# choose_node SPEC: a one-node recipe's machine by hostname, as Start takes it; nothing for a multi-node one.
choose_node() {
  local spec="$1" kind first second title="" default="" start="" index
  local -a ids=() labels=() hosts=()
  while IFS=$'\t' read -r kind first second; do
    case "$kind" in
      title) title=$first ;;
      default) default=$first ;;
      node) ids+=("$first"); labels+=("$second"); hosts+=("${second%% — *}") ;;
    esac
  done < <(printf '%s' "$CATALOG" | serve_plan nodes --spec-id "$spec")
  [ "${#ids[@]}" -gt 0 ] || return 0
  for index in "${!ids[@]}"; do [ "${ids[$index]}" != "$default" ] || start=$index; done
  index=$(PULSAR_CHOOSE_DEFAULT="$start" choose_index "$title" "${labels[@]}" "Back") || return $?
  [ "$index" -lt "${#ids[@]}" ] || return 1
  printf '%s\n' "${hosts[$index]}"
}

# check SPEC HOST: the dry run, its answer above its fixes, until Back or a fix ran.
check() {
  local spec="$1" host="$2" rc choice action label
  local -a placement=() actions=() labels=()
  [ -z "$host" ] || placement=(--node "$host")
  while true; do
    rc=0
    spin "Running Start's checks without starting…" bash -c '"$@" 2>"$0"' "$WORK/start.err" \
      "$REPO_DIR/pulsar" start "$spec" "${placement[@]}" --dry-run --json >"$WORK/start.json" || rc=$?
    [ "$rc" -ne 130 ] || return 130
    echo
    printf '%s' "$CATALOG" | reasons screen --document "$WORK/start.json" --rc "$rc" --spec-id "$spec"
    printf '\nSame as: ./pulsar start %s%s --dry-run\n' "${spec:0:12}" "${host:+ --node $host}" | text
    actions=() labels=()
    while IFS=$'\t' read -r action label; do actions+=("$action"); labels+=("$label"); done \
      < <(reasons choices --document "$WORK/start.json" --rc "$rc")
    choice=$(choose_index "$TITLE" "${labels[@]}") || { rc=$?; [ "$rc" -ne 130 ] || return 130; return 0; }
    rc=0
    case "${actions[$choice]}" in
      serve) "$REPO_DIR/scripts/serve-menu.sh" || rc=$? ;;
      services) "$REPO_DIR/scripts/services-menu.sh" || rc=$? ;;
      cluster) "$REPO_DIR/scripts/cluster-overview.sh" || rc=$? ;;
      diagnostics) page_output "Running diagnostics…" "$REPO_DIR/pulsar" doctor || rc=$? ;;
      again) continue ;;
      *) return 0 ;;
    esac
    [ "$rc" -ne 130 ] || return 130
    # After a fix, its own screen has said what it did; the same question can be asked again.
    return 0
  done
}

while true; do
  read_cards < <(printf '%s' "$CATALOG" | serve_plan list)
  if [ "${#CARD_IDS[@]}" -eq 0 ]; then
    printf '\nThe catalog has no recipe this Stack can start.\n'
    wait_continue "$TITLE" || exit $?
    exit 0
  fi
  index=$(choose_index "$TITLE" "${CARD_OPTIONS[@]}" "Back") || { rc=$?; [ "$rc" -ne 130 ] || exit 130; exit 0; }
  [ "$index" -lt "${#CARD_IDS[@]}" ] || exit 0
  rc=0
  host=$(choose_node "${CARD_IDS[$index]}") || rc=$?
  [ "$rc" -ne 130 ] || exit 130
  [ "$rc" -eq 0 ] || continue
  check "${CARD_IDS[$index]}" "$host" || rc=$?
  [ "$rc" -ne 130 ] || exit 130
done
