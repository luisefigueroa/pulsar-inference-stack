#!/usr/bin/env bash
# Source helpers used by model-library.sh. No actions occur when this is sourced.

source_inventory_on_rank() {
  local rank="${1:?physical rank required}" model="${2:?model required}"
  local selector="${3:?revision selector required}" command raw expected=""
  [[ "$selector" =~ ^[0-9a-f]{40}$ ]] && expected="$selector"
  command=$(cat <<'SH'
set -eu
cli=$(command -v hf || true)
if [ -z "$cli" ] && [ -x "$HOME/.hf-cli/venv/bin/hf" ]; then
  cli="$HOME/.hf-cli/venv/bin/hf"
fi
[ -n "$cli" ] || { echo 'modern hf CLI is required on selected node' >&2; exit 64; }
resolved=$(readlink -f -- "$cli")
first=$(head -n 1 -- "$resolved")
case "$first" in
  '#!/usr/bin/env python3') py=$(command -v python3) ;;
  '#!'/*) py=${first#\#!} ;;
  *) echo 'cannot identify hf Python environment' >&2; exit 64 ;;
esac
case "$py" in *[[:space:]]*) exit 64 ;; esac
case "${py##*/}" in python|python3|python3.*) ;; *) exit 64 ;; esac
[ -x "$py" ] || exit 64
export HF_HUB_OFFLINE=0 PYTHONDONTWRITEBYTECODE=1
exec "$py" -
SH
  ) || return 2
  command+=" $(shell_join_q --model-id "$model" --selector "$selector")"
  if [ "$rank" -eq 0 ]; then
    raw=$(bash -c "$command" <"$REPO_DIR/scripts/hf_source_inventory.py") || return $?
  else
    require_topology_ssh_trust >/dev/null || return 2
    raw=$(ssh_node "$rank" "$command" <"$REPO_DIR/scripts/hf_source_inventory.py") || return $?
  fi
  printf '%s' "$raw" | PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json,sys
from model_library.source import normalize_inventory
from release_spec import pretty_json_bytes
value=normalize_inventory(json.load(sys.stdin), sys.argv[1], sys.argv[2] or None)
sys.stdout.buffer.write(pretty_json_bytes(value))
' "$model" "$expected"
}

source_download_on_rank() {
  local rank="${1:?physical rank required}" stage="${2:?owned staging path required}"
  local source_json="${3:?validated source JSON required}" request prepared command
  local -a values=()
  request=$(printf '%s' "$source_json" | python3 -c '
import json,sys
print(json.dumps({"operation":"source-prepare","stage":sys.argv[1],"source":json.load(sys.stdin)}))
' "$stage") || return 2
  prepared=$(model_node "$rank" "$request") || return $?
  mapfile -d '' -t values < <(printf '%s' "$prepared" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for key in ("snapshot_path","cache_root"):sys.stdout.write(d[key]+"\0")
')
  [ "${#values[@]}" -eq 2 ] || return 2
  local snapshot="${values[0]}" cache="${values[1]}"
  values=()
  mapfile -d '' -t values < <(printf '%s' "$source_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for key in ("model_id","snapshot_revision"):sys.stdout.write(d[key]+"\0")
')
  [ "${#values[@]}" -eq 2 ] || return 2
  local model="${values[0]}" revision="${values[1]}"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || return 2
  # Preserve the selected user's authentication location. Confine model data,
  # transient object caches and temporary downloads to this private stage.
  command=$(cat <<'SH'
set -eu
cli=$(command -v hf || true)
if [ -z "$cli" ] && [ -x "$HOME/.hf-cli/venv/bin/hf" ]; then cli="$HOME/.hf-cli/venv/bin/hf"; fi
[ -n "$cli" ] || { echo 'modern hf CLI is required on selected node' >&2; exit 64; }
SH
  ) || return 2
  command+=$'\n'
  command+="export HF_HUB_OFFLINE=0 PYTHONDONTWRITEBYTECODE=1; "
  command+="export HF_HUB_CACHE=$(printf '%q' "$cache/hub") HF_XET_CACHE=$(printf '%q' "$cache/xet"); "
  command+="export HF_ASSETS_CACHE=$(printf '%q' "$cache/assets") TMPDIR=$(printf '%q' "$cache/tmp"); "
  command+='exec "$cli" '
  # hf treats local-dir and cache-dir as mutually exclusive. The environment
  # above still confines auxiliary caches without changing authentication.
  command+="$(shell_join_q download "$model" --revision "$revision" --local-dir "$snapshot" --quiet)"
  if [ "$rank" -eq 0 ]; then
    bash -c "$command" >&2 || return $?
  else
    require_topology_ssh_trust >/dev/null || return 2
    ssh_node "$rank" "$command" >&2 || return $?
  fi
  request=$(printf '%s' "$source_json" | python3 -c '
import json,sys
print(json.dumps({"operation":"source-clean","stage":sys.argv[1],"source":json.load(sys.stdin)}))
' "$stage") || return 2
  model_node "$rank" "$request" >/dev/null
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  acquire_script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  exec "$acquire_script_root/scripts/model-library.sh" acquire "$@"
fi
