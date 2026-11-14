# shellcheck shell=bash
# Sourced by the menus after REPO_DIR is set. Their Python helpers read the
# model library, the saved membership, the saved records and the catalog
# from the locations below. Every ./pulsar command takes those from lib.sh,
# which loads this checkout's .env over the process environment, so a
# location set only in .env must reach the helpers too.
#
# lib.sh is loaded in a subshell: its functions, its change of directory and
# the rest of .env stay out of the menu. Only these locations are exported;
# the ./pulsar commands that a menu runs load .env themselves. Left out on
# purpose: PULSAR_COLD_ROOT, whose process value lib.sh keeps over .env and
# which the archive helpers read from .env as data, and the API key, which
# the menus never take. When lib.sh cannot be loaded, the process values
# stay and each ./pulsar command reports why.
_pulsar_menu_settings=$(
  . "$REPO_DIR/scripts/lib.sh" >/dev/null 2>&1 || exit 1
  for _pulsar_name in PULSAR_MODEL_LIBRARY_DIR CLUSTER_TOPOLOGY_FILE PULSAR_SERVICE_INVENTORY_FILE \
      PULSAR_STORAGE_BUDGET_FILE PULSAR_CLUSTER_CHANGES_FILE PULSAR_RELEASES_ROOT; do
    [ -z "${!_pulsar_name+x}" ] || printf 'export %s=%q\n' "$_pulsar_name" "${!_pulsar_name}"
  done
) && eval "$_pulsar_menu_settings"
unset _pulsar_menu_settings
