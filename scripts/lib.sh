#!/usr/bin/env bash
# Shared helpers for onboarding scripts. Source only — do not execute.
# shellcheck shell=bash

if [ -n "${_PULSAR_SCRIPTS_LIB:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
_PULSAR_SCRIPTS_LIB=1

_scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$_scripts_dir/.." && pwd)"
cd "$REPO_DIR"

_pulsar_cold_root_process_defined=0
_pulsar_cold_root_process_value=""
if [ -n "${PULSAR_COLD_ROOT+x}" ]; then
  _pulsar_cold_root_process_defined=1
  _pulsar_cold_root_process_value="${PULSAR_COLD_ROOT}"
fi
_pulsar_env_file="$REPO_DIR/.env"
if [ "${PULSAR_SELFTEST:-0}" = 1 ] \
    && [ -n "${PULSAR_COLD_STORAGE_TEST_DOTENV:-}" ]; then
  case "$PULSAR_COLD_STORAGE_TEST_DOTENV" in
    /*) _pulsar_env_file="$PULSAR_COLD_STORAGE_TEST_DOTENV" ;;
    *)
      printf '[%s] ERROR: PULSAR_COLD_STORAGE_TEST_DOTENV must be absolute\n' \
        "${SCRIPT_NAME:-pulsar}" >&2
      return 1 2>/dev/null || exit 1
      ;;
  esac
fi
if [ -e "$_pulsar_env_file" ] || [ -L "$_pulsar_env_file" ]; then
  if [ -L "$_pulsar_env_file" ]; then
    printf '[%s] ERROR: .env must not be a symlink\n' "${SCRIPT_NAME:-pulsar}" >&2
    unset _pulsar_cold_root_process_defined _pulsar_cold_root_process_value
    return 1 2>/dev/null || exit 1
  fi
  if [ ! -f "$_pulsar_env_file" ]; then
    printf '[%s] ERROR: .env is not a regular file\n' "${SCRIPT_NAME:-pulsar}" >&2
    unset _pulsar_cold_root_process_defined _pulsar_cold_root_process_value
    return 1 2>/dev/null || exit 1
  fi
  set -a
  # shellcheck disable=SC1091
  . "$_pulsar_env_file"
  set +a
fi
if [ "$_pulsar_cold_root_process_defined" = 1 ]; then
  PULSAR_COLD_ROOT="$_pulsar_cold_root_process_value"
  export PULSAR_COLD_ROOT
fi
# Keep the two non-exported origin markers for long-lived callers such as
# home.sh. They let child configuration commands discard a value that came
# from .env while preserving a genuine process override, including empty.

# shellcheck disable=SC1091
. "$REPO_DIR/cluster/cluster-env.sh"

VLLM_IMAGE_MAINLINE="${VLLM_IMAGE_MAINLINE:-vllm/vllm-openai:v0.26.0}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"

_platform_shell=$(python3 "$REPO_DIR/scripts/platform_reference.py" export-shell) || {
  printf '[%s] ERROR: platform reference is unavailable\n' \
    "${SCRIPT_NAME:-pulsar}" >&2
  unset _platform_shell
  return 1 2>/dev/null || exit 1
}
eval "$_platform_shell"
unset _platform_shell
[ -n "${PULSAR_PLATFORM_ID:-}" ] \
  && [ -n "${PULSAR_GPU_NAME:-}" ] \
  && [ -n "${PULSAR_PLATFORM_DISPLAY_NAME:-}" ] \
  && [ -n "${PULSAR_ARCHITECTURES:-}" ] \
  && [ -n "${PULSAR_RDMA_VERBS_DEVICE:-}" ] \
  && [ -n "${PULSAR_COLD_START_FOOTPRINT_SLACK:-}" ] \
  || {
    printf '[%s] ERROR: platform reference export is incomplete\n' \
      "${SCRIPT_NAME:-pulsar}" >&2
    return 1 2>/dev/null || exit 1
  }

# Memory policy defaults from the selected platform; process/.env overrides win.
MIN_OS_BUFFER_GIB="${MIN_OS_BUFFER_GIB:-$PULSAR_MIN_OS_BUFFER_GIB}"
HARD_FLOOR_AVAILABLE_GIB="${HARD_FLOOR_AVAILABLE_GIB:-$PULSAR_HARD_FLOOR_AVAILABLE_GIB}"
LAUNCH_SPIKE_GIB="${LAUNCH_SPIKE_GIB:-$PULSAR_LAUNCH_SPIKE_GIB}"
OVERHEAD_GIB_DEFAULT="${OVERHEAD_GIB_DEFAULT:-$PULSAR_OVERHEAD_GIB_DEFAULT}"

log()  { printf '[%s] %s\n' "${SCRIPT_NAME:-pulsar}" "$*"; }
warn() { printf '[%s] warn: %s\n' "${SCRIPT_NAME:-pulsar}" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "${SCRIPT_NAME:-pulsar}" "$1" >&2; exit "${2:-1}"; }

# Human-facing name for a confirmed node position: its saved hostname. Machine
# data and launcher arguments keep node_id and rank; people read hostnames.
human_node_name() {
  local index="${1:?node position required}" name
  name="${CLUSTER_NODE_HOSTNAMES[$index]:-${CLUSTER_NODE_SSH_HOSTS[$index]:-}}"
  if [ -z "$name" ] && [ "$index" = 0 ]; then
    name=$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)
  fi
  printf '%s\n' "${name:-node $index}"
}

terminal_width() {
  local width="${COLUMNS:-}"
  if ! [[ "$width" =~ ^[0-9]+$ ]] && [ -t 1 ] && command -v tput >/dev/null 2>&1; then
    width=$(tput cols 2>/dev/null || true)
  fi
  [[ "$width" =~ ^[0-9]+$ ]] || width=80
  [ "$width" -ge 32 ] || width=32
  [ "$width" -le 100 ] || width=100
  printf '%s\n' "$width"
}

print_hanging() {
  # print_hanging PREFIX MESSAGE — wrap MESSAGE with PREFIX-width continuation.
  local prefix="$1" message="$2" width content_width first line continuation
  width=$(terminal_width)
  content_width=$((width - ${#prefix}))
  [ "$content_width" -ge 8 ] || content_width=8
  printf -v continuation '%*s' "${#prefix}" ''
  first=1
  while IFS= read -r line; do
    line="${line%"${line##*[![:space:]]}"}"
    if [ "$first" = 1 ]; then
      printf '%s%s\n' "$prefix" "$line"
      first=0
    else
      printf '%s%s\n' "$continuation" "$line"
    fi
  done < <(printf '%s\n' "$message" | fold -s -w "$content_width")
}


# render_human_section TITLE LABEL VALUE [LABEL VALUE ...]
# Shared semantic section renderer for interactive, width-aware output.
render_human_section() {
  local title="${1:?section title required}"
  shift
  [ $(( $# % 2 )) -eq 0 ] \
    || die "render_human_section requires label/value pairs"
  python3 - "$title" "$@" <<'PY'
import sys

from scripts.terminal_format import TerminalWriter

title = sys.argv[1]
fields = sys.argv[2:]
term = TerminalWriter()
term.emit(title)
for index in range(0, len(fields), 2):
    term.field(fields[index], fields[index + 1])
PY
}

format_storage_gib() {
  local gib="${1:-0}"
  awk -v value="$gib" 'BEGIN {
    if (value + 0 >= 1024) printf "%.1f TiB", (value + 0) / 1024
    else printf "%.0f GiB", value + 0
  }'
}
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1 (install it and retry)"
}

# Close-on-exec so background children (archive workers) cannot inherit a
# held flock and deadlock on a second exclusive lock of the same file.
_fd_cloexec() {
  local fd="${1:-}"
  [[ "$fd" =~ ^[0-9]+$ ]] || return 0
  python3 -c 'import fcntl, sys; fcntl.fcntl(int(sys.argv[1]), fcntl.F_SETFD, fcntl.FD_CLOEXEC)' "$fd" \
    || die "cannot mark lock fd $fd close-on-exec"
}

# Serialize home changes against preparation and launch.
acquire_model_library_lifecycle_lock() {
  local mode="${1:-shared}" timeout lock_dir lock_path lock_parent lock_fd
  case "$mode" in
    shared|exclusive) ;;
    *) die "model library lifecycle lock mode must be shared or exclusive" ;;
  esac
  if [ -n "${PULSAR_MODEL_LIBRARY_LOCK_FD:-}" ]; then
    [ "${PULSAR_MODEL_LIBRARY_LOCK_MODE:-}" = "$mode" ] \
      || die "model library lifecycle lock is already held in another mode"
    return 0
  fi
  command -v flock >/dev/null 2>&1 \
    || die "flock is required for model library lifecycle safety"
  timeout="${PULSAR_MODEL_LIBRARY_LOCK_TIMEOUT_SECONDS:-30}"
  [[ "$timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    || die "PULSAR_MODEL_LIBRARY_LOCK_TIMEOUT_SECONDS must be numeric"
  lock_dir="$PULSAR_MODEL_LIBRARY_DIR"
  lock_path="${PULSAR_MODEL_LIBRARY_LOCK_FILE:-$lock_dir/lifecycle.lock}"
  lock_parent=$(dirname -- "$lock_path")
  mkdir -p "$lock_parent" \
    || die "cannot create model library lock directory: $lock_parent"
  exec {lock_fd}>"$lock_path" \
    || die "cannot open model library lifecycle lock: $lock_path"
  if [ "$mode" = exclusive ]; then
    flock -x -w "$timeout" "$lock_fd" || {
      exec {lock_fd}>&-
      die "model library is busy; exclusive model-library mutation lock timed out"
    }
  else
    flock -s -w "$timeout" "$lock_fd" || {
      exec {lock_fd}>&-
      die "durable-home removal is in progress; launch/library lock timed out"
    }
  fi
  _fd_cloexec "$lock_fd"
  PULSAR_MODEL_LIBRARY_LOCK_FD="$lock_fd"
  PULSAR_MODEL_LIBRARY_LOCK_MODE="$mode"
}

# Serialize supported hot-tree mutations so each all-rank capacity observation
# remains valid until the matching prepare/pin/purge operation finishes.
# Launch/readiness paths take the shared form; hot writers take exclusive.
acquire_model_library_hot_lock() {
  local mode="${1:-shared}" timeout lock_dir lock_path lock_parent lock_fd
  case "$mode" in
    shared|exclusive) ;;
    *) die "model library hot lock mode must be shared or exclusive" ;;
  esac
  if [ -n "${PULSAR_MODEL_LIBRARY_HOT_LOCK_FD:-}" ]; then
    [ "${PULSAR_MODEL_LIBRARY_HOT_LOCK_MODE:-}" = "$mode" ] \
      || die "model library hot lock is already held in another mode"
    return 0
  fi
  command -v flock >/dev/null 2>&1 \
    || die "flock is required for model library hot-state safety"
  timeout="${PULSAR_MODEL_LIBRARY_HOT_LOCK_TIMEOUT_SECONDS:-${PULSAR_MODEL_LIBRARY_LOCK_TIMEOUT_SECONDS:-30}}"
  [[ "$timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    || die "PULSAR_MODEL_LIBRARY_HOT_LOCK_TIMEOUT_SECONDS must be numeric"
  lock_dir="$PULSAR_MODEL_LIBRARY_DIR"
  lock_path="${PULSAR_MODEL_LIBRARY_HOT_LOCK_FILE:-$lock_dir/hot.lock}"
  lock_parent=$(dirname -- "$lock_path")
  mkdir -p "$lock_parent" \
    || die "cannot create model library hot lock directory: $lock_parent"
  exec {lock_fd}>"$lock_path" \
    || die "cannot open model library hot lock: $lock_path"
  if [ "$mode" = exclusive ]; then
    flock -x -w "$timeout" "$lock_fd" || {
      exec {lock_fd}>&-
      die "another hot mutation is in progress; hot lock timed out"
    }
  else
    flock -s -w "$timeout" "$lock_fd" || {
      exec {lock_fd}>&-
      die "hot preparation/pin/purge is in progress; hot read lock timed out"
    }
  fi
  _fd_cloexec "$lock_fd"
  PULSAR_MODEL_LIBRARY_HOT_LOCK_FD="$lock_fd"
  PULSAR_MODEL_LIBRARY_HOT_LOCK_MODE="$mode"
}

_reset_loaded_profile_defaults() {
  MODEL="" SERVED_NAME="" IMAGE="" NOTES="" STATUS="?"
  NODES=1 PORT=8000 GPU_MEM_UTIL=0.80
  ENGINE_ARGS=() CONTAINER_ENV=() SPEC_DECODE_ARGS=()
  WEIGHTS_GIB="" WEIGHTS_RAM_GIB="" KV_GIB="" OVERHEAD_GIB="" MEM_MIN_FREE_GIB=""
  RECOMMENDED_SPEC=0 FIRST_RUN_CANDIDATE=0
  PROFILE_FAMILY="" VARIANT_LABEL="" FAMILY_RECOMMENDED=0 PROFILE_PURPOSE="serving"
  TOPOLOGY_CLASS="" MIN_RAILS_PER_PAIR=""
  CONF_SOURCE=conf
  # shellcheck disable=SC2034  # consumed by model-library.sh and check-weights.sh
  SNAPSHOT_REVISION=""
  SPEC_MANIFEST_ID=""
  SPEC_PLATFORM_ID=""
  OVERLAY_PLACEMENT_NODE_ID=""
  OVERLAY_CACHE_ROOT=""
}

# Effective one-node placement selector for the loaded profile: for a released
# spec, an explicit --node wins, an overlay placement.node_id applies when no
# selector was given, and a conflict between the two fails. Prints the
# selector; callers assign it. Confs pass through unchanged.
# Print the topology rank a node selector names (node id, hostname, ssh
# host, control IP, key, or rank), or fail. The shared resolver runs in a
# subshell so the caller's placement variables are untouched.
single_node_index_for_selector() {
  local selector="${1:?}"
  (
    resolve_single_node_placement "$selector" >/dev/null 2>&1 || exit 1
    printf '%s\n' "$SINGLE_NODE_INDEX"
  )
}

# spec_overlay_node_selector <selector> [policy]
# Fill an empty selector from the overlay placement. A selector that names a
# different node than the overlay is a conflict for serving-side commands;
# under policy "cleanup" it is an explicit historical placement (a previous
# rank after home relocate) and is passed through unchanged.
spec_overlay_node_selector() {
  local selector="${1:-}" policy="${2:-serve}" selector_rank overlay_rank
  if [ "${CONF_SOURCE:-conf}" = spec ] && [ "${NODES:-1}" -eq 1 ]; then
    if [ "$policy" != cleanup ] && [ -n "$selector" ] && [ -n "${OVERLAY_PLACEMENT_NODE_ID:-}" ] \
        && [ "$selector" != "$OVERLAY_PLACEMENT_NODE_ID" ]; then
      # A rank number, hostname, or node id may all name the overlay's
      # node; compare the placement they resolve to, not the spelling.
      if ! selector_rank=$(single_node_index_for_selector "$selector") \
          || ! overlay_rank=$(single_node_index_for_selector "$OVERLAY_PLACEMENT_NODE_ID") \
          || [ "$selector_rank" != "$overlay_rank" ]; then
        die "--node '$selector' differs from overlay placement.node_id '$OVERLAY_PLACEMENT_NODE_ID'" 2
      fi
    fi
    if [ -z "$selector" ] && [ -n "${OVERLAY_PLACEMENT_NODE_ID:-}" ]; then
      selector="$OVERLAY_PLACEMENT_NODE_ID"
    fi
  fi
  printf '%s\n' "$selector"
}

# Shared schema/platform admission for launch, observation and resource sampling.
# Execution-only requirements belong in require_spec_launch_admission below.
require_spec_platform_admission() {
  local name="${1:-${CONF_NAME:-}}" active="${PULSAR_PLATFORM_ID:-dgx-spark-gb10}"
  [ "${CONF_SOURCE:-conf}" = spec ] || return 0
  python3 -c 'from release_spec.serving import load_spec; import sys; load_spec(sys.argv[1])' "$CONF_PATH" \
    || die "new serving operations require a valid spec using schema 2 or 3; historical services remain inspectable and stoppable" 2
  [ "${SPEC_PLATFORM_ID:-}" = "$active" ] \
    || die "selected spec $name targets platform '${SPEC_PLATFORM_ID:-?}'; this stack is '$active' (refusing to launch outside the spec's frozen geometry)" 2
}

# Every launcher calls this immediately after loading its selected spec, before
# staging an image or replacing a service. Read-only commands do not call it.
require_spec_launch_admission() {
  require_spec_platform_admission "$@"
  [ "${CONF_SOURCE:-conf}" = spec ] || return 0
  CONF_PATH="$CONF_PATH" python3 - <<'PY' || die 'selected recipe requires unsupported guard enforcement' 2
import os
from release_spec.serving import load_spec, load_json, apply_overrides
spec=load_spec(os.environ['CONF_PATH'])
if os.environ.get('PULSAR_OVERRIDE_FILE'):
    spec=apply_overrides(spec,load_json(os.environ['PULSAR_OVERRIDE_FILE']))
if 'guard' in spec['recipe']['container']:
    raise SystemExit('guard execution is not supported by this Stack')
PY
}

_finalize_loaded_profile() {
  local name="${1:?profile name required}" item recipe_nccl_qps=""
  [ -n "$MODEL" ] || die "$name: MODEL unset in conf"
  SERVED_NAME="${SERVED_NAME:-$name}"
  IMAGE="${IMAGE:-$VLLM_IMAGE_MAINLINE}"
  NODES="${NODES:-1}"
  PORT="${PORT:-8000}"
  GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.80}"
  STATUS="${STATUS:-?}"
  OVERHEAD_GIB="${OVERHEAD_GIB:-$OVERHEAD_GIB_DEFAULT}"
  MEM_MIN_FREE_GIB="${MEM_MIN_FREE_GIB:-$MIN_OS_BUFFER_GIB}"
  RECOMMENDED_SPEC="${RECOMMENDED_SPEC:-0}"
  FIRST_RUN_CANDIDATE="${FIRST_RUN_CANDIDATE:-0}"
  PROFILE_FAMILY="${PROFILE_FAMILY:-$SERVED_NAME}"
  VARIANT_LABEL="${VARIANT_LABEL:-${NODES}-node}"
  FAMILY_RECOMMENDED="${FAMILY_RECOMMENDED:-0}"
  PROFILE_PURPOSE="${PROFILE_PURPOSE:-serving}"
  if [ "$NODES" = 1 ]; then
    TOPOLOGY_CLASS="${TOPOLOGY_CLASS:-single}"
    MIN_RAILS_PER_PAIR="${MIN_RAILS_PER_PAIR:-0}"
  else
    TOPOLOGY_CLASS="${TOPOLOGY_CLASS:-roce-full-mesh}"
    MIN_RAILS_PER_PAIR="${MIN_RAILS_PER_PAIR:-2}"
  fi
  CONF_NAME="$name"
  if [ "$NODES" -gt 1 ]; then
    for item in ${CONTAINER_ENV[@]+"${CONTAINER_ENV[@]}"}; do
      case "$item" in
        NCCL_IB_QPS_PER_CONNECTION=*)
          recipe_nccl_qps="${item#*=}"
          ;;
      esac
    done
    NCCL_IB_QPS_PER_CONNECTION="${recipe_nccl_qps:-4}"
    [[ "$NCCL_IB_QPS_PER_CONNECTION" =~ ^[1-9][0-9]*$ ]] \
      || die "$name: NCCL_IB_QPS_PER_CONNECTION must be a positive integer"
    export NCCL_IB_QPS_PER_CONNECTION
  fi
  [[ "$NODES" =~ ^[1-9][0-9]*$ ]] || die "$name: NODES must be a positive integer"
  validate_profile_contract
}

# Load a catalog spec plus the site overlay into caller shell (no conf).
load_spec_profile() {
  local spec_id="${1:?load_spec_profile: spec_id required}"
  local releases_root="${PULSAR_RELEASES_ROOT:-$REPO_DIR/releases}"
  local overlay="${PULSAR_OVERLAY_PATH:-$REPO_DIR/.pulsar-overlay.json}"
  local image_repo="${VLLM_IMAGE_MAINLINE:-vllm/vllm-openai}"
  local output err
  local -a export_args
  # Keep a registry port: drop @digest, then only a :tag after the last slash.
  image_repo="${image_repo%%@*}"
  case "${image_repo##*/}" in
    *:*) image_repo="${image_repo%:*}" ;;
  esac
  [ -n "$image_repo" ] || image_repo=vllm/vllm-openai
  export_args=(
    shell-values "$spec_id"
    --repo-root "$REPO_DIR"
    --overlay "$overlay"
    --image-repo "$image_repo"
  )
  if [ -n "${PULSAR_RELEASES_ROOT:-}" ]; then
    export_args+=(--releases-root "$PULSAR_RELEASES_ROOT")
  fi
  # Lab path: a measured spec file (validate/baseline-v1.sh --spec) is the
  # profile before catalog publication; the file's spec_id must equal the profile.
  if [ -n "${PULSAR_SPEC_FILE:-}" ]; then
    export_args+=(--spec-file "$PULSAR_SPEC_FILE")
  fi
  err=$(mktemp "${TMPDIR:-/tmp}/pulsar-shell-values.XXXXXX")
  if ! output=$(python3 "$REPO_DIR/scripts/release_consumer.py" \
      "${export_args[@]}" 2>"$err"); then
    output=$(tr -d '\r' <"$err" | sed 's/^error: //')
    rm -f "$err"
    die "$output"
  fi
  rm -f "$err"
  # shellcheck disable=SC1090
  eval "$output"
  export PULSAR_EFFECTIVE_SPEC_ID
  CONF_SOURCE=spec
  CONF_PATH="${PULSAR_SPEC_FILE:-$releases_root/${spec_id}.json}"
  _finalize_loaded_profile "$spec_id"
}

# Load an exact catalog or explicitly selected candidate spec.
load_conf() {
  local name="${1:?load_conf: model name required}"
  case "$name" in
    *[!A-Za-z0-9._-]*|"")
      die "invalid model id '$name' (use letters, numbers, dot, underscore, or hyphen)"
      ;;
  esac
  _reset_loaded_profile_defaults
  if [[ "$name" =~ ^[0-9a-f]{64}$ ]]; then
    load_spec_profile "$name"
    return
  fi
  die "no spec named '$name': select a spec id from ./pulsar models list --json, or supply --spec-file for a candidate"
}

engine_arg_value() {
  local wanted="${1:?flag required}" fallback="${2:-}" i
  for ((i = 0; i < ${#ENGINE_ARGS[@]}; i++)); do
    case "${ENGINE_ARGS[$i]}" in
      "$wanted")
        [ $((i + 1)) -lt "${#ENGINE_ARGS[@]}" ] \
          || die "$CONF_NAME: $wanted requires a value"
        printf '%s\n' "${ENGINE_ARGS[$((i + 1))]}"
        return 0
        ;;
      "$wanted"=*)
        printf '%s\n' "${ENGINE_ARGS[$i]#*=}"
        return 0
        ;;
    esac
  done
  printf '%s\n' "$fallback"
}

validate_profile_contract() {
  local tp pp world
  case "$PROFILE_PURPOSE" in
    serving|diagnostic) ;;
    *) die "$CONF_NAME: PROFILE_PURPOSE must be serving or diagnostic" ;;
  esac
  tp=$(engine_arg_value --tensor-parallel-size 1)
  pp=$(engine_arg_value --pipeline-parallel-size 1)
  [[ "$tp" =~ ^[1-9][0-9]*$ ]] \
    || die "$CONF_NAME: tensor parallel size must be positive"
  [[ "$pp" =~ ^[1-9][0-9]*$ ]] \
    || die "$CONF_NAME: pipeline parallel size must be positive"
  world=$((tp * pp))
  if [ "$world" -ne "$NODES" ]; then
    die "$CONF_NAME: exact profile mismatch: TP=$tp × PP=$pp = $world, but NODES=$NODES"
  fi
  if [ "$NODES" -gt 1 ]; then
    [ "$TOPOLOGY_CLASS" = roce-full-mesh ] \
      || die "$CONF_NAME: multi-node profile requires TOPOLOGY_CLASS=roce-full-mesh"
    [[ "$MIN_RAILS_PER_PAIR" =~ ^[1-9][0-9]*$ ]] \
      || die "$CONF_NAME: MIN_RAILS_PER_PAIR must be positive"
    [ "$(engine_arg_value --distributed-executor-backend "")" = mp ] \
      || die "$CONF_NAME: validated multi-node profiles require --distributed-executor-backend mp"
  elif [ "$TOPOLOGY_CLASS" != single ]; then
    die "$CONF_NAME: single-node profile requires TOPOLOGY_CLASS=single"
  fi
}

append_loaded_profile_contract_args() {
  local target_name="${1:?target array name required}" item
  local -n target="$target_name"
  target+=(
    --model-id "$MODEL"
    --served-name "$SERVED_NAME"
    --image "$IMAGE"
    --nodes "$NODES"
    --port "$PORT"
    --gpu-mem-util "$GPU_MEM_UTIL"
    --recommended-spec "$RECOMMENDED_SPEC"
    --profile-purpose "$PROFILE_PURPOSE"
    --topology-class "$TOPOLOGY_CLASS"
    --min-rails-per-pair "$MIN_RAILS_PER_PAIR"
    "--weights-gib=$WEIGHTS_GIB"
    "--weights-ram-gib=$WEIGHTS_RAM_GIB"
    "--kv-gib=$KV_GIB"
    "--overhead-gib=$OVERHEAD_GIB"
    "--mem-min-free-gib=$MEM_MIN_FREE_GIB"
  )
  for item in ${ENGINE_ARGS[@]+"${ENGINE_ARGS[@]}"}; do
    target+=("--engine-arg=$item")
  done
  for item in ${CONTAINER_ENV[@]+"${CONTAINER_ENV[@]}"}; do
    target+=("--container-env=$item")
  done
  for item in ${SPEC_DECODE_ARGS[@]+"${SPEC_DECODE_ARGS[@]}"}; do
    target+=("--spec-decode-arg=$item")
  done
}

model_source_kind() {
  # hf | nfs
  case "${MODEL:-}" in
    /*) echo nfs ;;
    *)  echo hf ;;
  esac
}

hf_hub_dirname() {
  # org/name -> models--org--name
  local m="${1:-$MODEL}"
  echo "models--${m//\//--}"
}

hf_hub_cache_root() {
  echo "${HF_CACHE}/hub"
}

hf_hub_path() {
  echo "$(hf_hub_cache_root)/$(hf_hub_dirname "${1:-$MODEL}")"
}

# Compatibility location produced when `hf download --cache-dir "$HF_CACHE"`
# is used even though HF_CACHE is Pulsar's Hugging Face home directory.
hf_legacy_hub_path() {
  echo "${HF_CACHE}/$(hf_hub_dirname "${1:-$MODEL}")"
}

has_spec_args() {
  [ "${#SPEC_DECODE_ARGS[@]}" -gt 0 ]
}

# Set a caller-owned speculative-decode mode while rejecting contradictory
# overrides. Valid modes are auto (profile policy), on, and off.
set_spec_decode_mode() {
  local var_name="${1:?set_spec_decode_mode: variable name required}"
  local requested="${2:?set_spec_decode_mode: requested mode required}"
  local -n mode_ref="$var_name"
  case "$requested" in
    on|off) ;;
    *) die "invalid speculative-decode mode: $requested" ;;
  esac
  if [ "$mode_ref" != "auto" ] && [ "$mode_ref" != "$requested" ]; then
    die "--spec-decode and --no-spec-decode are mutually exclusive"
  fi
  mode_ref="$requested"
}

# Resolve profile policy plus an explicit CLI override. Call after load_conf.
# RECOMMENDED_SPEC is executable policy: 1 means the validated fast path is the
# default. SPEC_DECODE_ENABLED and SPEC_DECODE_SOURCE are set for the caller.
resolve_spec_decode() {
  [ "${1:-auto}" = auto ] || die "recipe variants require a new candidate spec" 2
  SPEC_DECODE_ENABLED=0
  SPEC_DECODE_SOURCE=profile-default
}

# Descriptive classifier only. A blocked/do-not-use label warns about recorded
# evidence; it is not launch authorization.
status_is_blocked() {
  case "${STATUS}" in
    blocked*|BLOCKED*|do-not-use|DO-NOT-USE*) return 0 ;;
    *) return 1 ;;
  esac
}

# Compatibility helpers retained for callers that sourced older versions of
# lib.sh. Validation status is advisory, so every parsed profile is launchable
# with respect to status and no status requires --force. Operational checks
# (identity, recipe, topology, capacity, lifecycle, and security) still fail
# closed in their owning code paths.
status_is_launchable() {
  return 0
}

status_requires_force() {
  return 1
}

mem_available_gib_local() {
  # PULSAR_MEMINFO_FILE is a deterministic-test override for /proc/meminfo.
  local meminfo="${PULSAR_MEMINFO_FILE:-/proc/meminfo}"
  if [ -r "$meminfo" ]; then
    local kb
    kb=$(awk '/MemAvailable:/ {print $2}' "$meminfo" 2>/dev/null || echo 0)
    awk -v kb="${kb:-0}" 'BEGIN { printf "%.2f", kb / 1048576 }'
    return
  fi
  # macOS authoring host — not a serve target; report high so dry-runs can proceed
  if command -v vm_stat >/dev/null 2>&1; then
    python3 - <<'PY' 2>/dev/null || echo "64.00"
import subprocess, re
out = subprocess.check_output(["vm_stat"], text=True)
page = 4096
m = re.search(r"page size of (\d+)", out)
if m:
    page = int(m.group(1))
free = speculative = inactive = 0
for line in out.splitlines():
    if line.startswith("Pages free"):
        free = int(line.split(":")[1].strip().rstrip("."))
    elif line.startswith("Pages speculative"):
        speculative = int(line.split(":")[1].strip().rstrip("."))
    elif line.startswith("Pages inactive"):
        inactive = int(line.split(":")[1].strip().rstrip("."))
# rough available ≈ free+speculative+inactive
print(f"{(free+speculative+inactive)*page/1073741824:.2f}")
PY
    return
  fi
  echo "0"
}


mem_available_gib_remote() {
  local host="${1:?}"
  "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
    "awk '/MemAvailable:/ {printf \"%.2f\", \$2/1048576}' /proc/meminfo" 2>/dev/null \
    || echo "0"
}

disk_free_gib() {
  local path="${1:-$HF_CACHE}"
  df -BG "$path" 2>/dev/null | awk 'NR==2 {gsub(/G/,""); print $4}' || echo 0
}

parse_kv_cache_bytes() {
  local i=0
  while [ $i -lt ${#ENGINE_ARGS[@]} ]; do
    case "${ENGINE_ARGS[$i]}" in
      --kv-cache-memory-bytes)
        i=$((i + 1))
        echo "${ENGINE_ARGS[$i]}"
        return 0
        ;;
      --kv-cache-memory-bytes=*)
        echo "${ENGINE_ARGS[$i]#--kv-cache-memory-bytes=}"
        return 0
        ;;
    esac
    i=$((i + 1))
  done
  echo ""
}

parse_max_model_len() {
  local i=0
  while [ $i -lt ${#ENGINE_ARGS[@]} ]; do
    case "${ENGINE_ARGS[$i]}" in
      --max-model-len)
        i=$((i + 1))
        echo "${ENGINE_ARGS[$i]}"
        return 0
        ;;
      --max-model-len=*)
        echo "${ENGINE_ARGS[$i]#--max-model-len=}"
        return 0
        ;;
    esac
    i=$((i + 1))
  done
  echo ""
}

bytes_to_gib() {
  awk -v b="${1:-0}" 'BEGIN { printf "%.2f", b / 1073741824 }'
}

# Best-effort directory size in GiB (du -sb or -sk).
dir_size_gib() {
  local p="$1"
  [ -e "$p" ] || { echo 0; return; }
  if du -sb "$p" >/dev/null 2>&1; then
    awk -v b="$(du -sb "$p" 2>/dev/null | awk '{print $1}')" 'BEGIN{printf "%.2f", b/1073741824}'
  else
    awk -v k="$(du -sk "$p" 2>/dev/null | awk '{print $1}')" 'BEGIN{printf "%.2f", k/1048576}'
  fi
}

estimate_weights_gib() {
  # Disk footprint for the loaded profile (WEIGHTS_GIB, else local tree size).
  if [ -n "${WEIGHTS_GIB}" ]; then
    echo "$WEIGHTS_GIB"
    return
  fi
  local kind sz
  kind=$(model_source_kind)
  if [ "$kind" = nfs ]; then
    sz=$(dir_size_gib "$MODEL")
  else
    sz=$(dir_size_gib "$(hf_hub_path)")
  fi
  if awk -v s="$sz" 'BEGIN{exit !(s>0.1)}'; then
    echo "$sz"
  else
    echo "5"
  fi
}

# Unified-memory weight footprint for check-memory (full model GiB).
# Prefer WEIGHTS_RAM_GIB when disk ≠ resident RAM (quantized / MoE).
estimate_weights_ram_gib() {
  if [ -n "${WEIGHTS_RAM_GIB:-}" ]; then
    echo "$WEIGHTS_RAM_GIB"
    return
  fi
  estimate_weights_gib
}

# Freeze a selected estimate into a readonly process value. Child admission
# commands receive it explicitly as one argv item; they never reread its source.
select_memory_estimate() {
  local file="${1:-}" frozen="${2:-}" expected="${3:-}" error_code="${4:-2}" value
  local -a args=(--spec-file "$CONF_PATH"
    --effective-spec-id "${PULSAR_EFFECTIVE_SPEC_ID:-$CONF_NAME}")
  if [ -n "$file" ] && [ -n "$frozen" ]; then
    die "select only one memory estimate input" "$error_code"
  fi
  if [ -n "$file" ]; then args+=(--file "$file")
  elif [ -n "$frozen" ]; then args+=(--frozen-json "$frozen")
  else die "memory estimate ID requires an estimate input" "$error_code"
  fi
  [ -z "${PULSAR_OVERRIDE_FILE:-}" ] || args+=(--override-file "$PULSAR_OVERRIDE_FILE")
  [ -z "$expected" ] || args+=(--estimate-id "$expected")
  value=$(python3 "$REPO_DIR/scripts/memory_estimate.py" "${args[@]}") \
    || die "memory estimate is invalid for the effective spec" "$error_code"
  declare -gr PULSAR_MEMORY_ESTIMATE_JSON="$value"
  export PULSAR_MEMORY_ESTIMATE_JSON
}

estimate_kv_gib() {
  if [ -n "${KV_GIB}" ]; then
    echo "$KV_GIB"
    return
  fi
  local b
  b=$(parse_kv_cache_bytes)
  if [ -n "$b" ]; then
    bytes_to_gib "$b"
    return
  fi
  # No fixed KV — modest default; confs should set KV_GIB when known
  if [ -n "${WEIGHTS_GIB}" ] && awk -v w="$WEIGHTS_GIB" 'BEGIN{exit !(w+0 < 10)}'; then
    echo "2"
  else
    echo "6"
  fi
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[:-1] if False else sys.argv[1]))' "$1" 2>/dev/null \
    || printf '"%s"' "${1//\"/\\\"}"
}

# Extract several fields from one JSON object with a single python3 start.
# Paths are dotted (home.rank). One line per path: scalars as text, null or
# missing as empty, booleans as true/false, objects and arrays as compact
# sorted JSON. Consume with: mapfile -t vals < <(json_fields "$json" a b.c)
json_fields() {
  local json="$1"
  shift
  printf '%s' "$json" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for path in sys.argv[1:]:
    value = doc
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None:
        print("")
    elif isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        print(value)
' "$@"
}

# One JSON field (stringified) from a JSON object string.
json_field() {
  json_fields "$1" "$2"
}

# Validate the stable inventory JSON boundary before callers make lifecycle
# decisions from it. Empty, malformed, or structurally incomplete output is an
# observability failure, never an empty-machine state.
inventory_json_is_valid() {
  local payload="${1:-}"
  [ -n "$payload" ] || return 1
  printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
ok = (
    isinstance(d, dict)
    and d.get("schema_version") == 1
    and isinstance(d.get("services"), list)
    and isinstance(d.get("worker"), dict)
    and isinstance(d.get("nodes"), dict)
    and isinstance(d["nodes"].get("head"), dict)
)
raise SystemExit(0 if ok else 1)
' >/dev/null 2>&1
}

# Validate both check-memory's JSON contract and its three documented statuses.
# This prevents crashes, signals, and malformed helper output from becoming an
# implicit PASS.
memory_preflight_json_is_valid() {
  local payload="${1:-}" rc="${2:-}"
  [ -n "$payload" ] || return 1
  case "$rc" in
    0|1|2) ;;
    *) return 1 ;;
  esac
  printf '%s' "$payload" | RC="$rc" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
expected = {"0": "pass", "1": "fail", "2": "warn"}[os.environ["RC"]]
required = ("result", "mode", "footprint_gib", "need_start_gib", "head_available_gib")
ok = (
    isinstance(d, dict)
    and all(k in d for k in required)
    and d.get("result") == expected
    and d.get("mode") in ("cold-start", "already-loaded")
)
raise SystemExit(0 if ok else 1)
' >/dev/null 2>&1
}

pulsar_api_key() {
  printf '%s' "${VLLM_API_KEY:-${API_KEY:-}}"
}

# Fill a caller-owned argv array with the OpenAI-compatible Authorization
# header when API auth is configured. Keeping the header as two argv elements
# avoids shell re-parsing and command injection.
api_auth_curl_args() {
  local destination="${1:?auth argv destination required}"
  local key
  key=$(pulsar_api_key)
  local -n destination_ref="$destination"
  destination_ref=()
  if [ -n "$key" ]; then
    destination_ref=(-H "Authorization: Bearer $key")
  fi
}

# Serialize argv for an SSH remote command without reinterpreting it locally.
shell_join_q() {
  local out="" arg
  for arg in "$@"; do
    out+="$(printf '%q' "$arg") "
  done
  printf '%s' "${out% }"
}

# Render diagnostic/dry-run argv without disclosing credentials. Execution
# paths must continue to use the original arrays.
shell_join_q_redacted() {
  local out="" arg shown redact_next=0
  for arg in "$@"; do
    shown="$arg"
    if [ "$redact_next" = 1 ]; then
      shown="<redacted>"
      redact_next=0
    else
      case "$arg" in
        --api-key)
          redact_next=1
          ;;
        HF_TOKEN=*|VLLM_API_KEY=*|API_KEY=*)
          shown="${arg%%=*}=<redacted>"
          ;;
      esac
    fi
    out+="$(printf '%q' "$shown") "
  done
  printf '%s' "${out% }"
}

print_shell_command_redacted() {
  shell_join_q_redacted "$@"
  printf '\n'
}

# Injectable docker/ssh for deterministic tests. Production leaves these default.
PULSAR_DOCKER="${PULSAR_DOCKER:-docker}"
PULSAR_SSH="${PULSAR_SSH:-ssh}"

# ADR 0006: the model library is the only weight-distribution mechanism.
WEIGHT_MODE_FLAG_REMOVED_MESSAGE='--weight-source/--weight-mode were removed (ADR 0006): the model library is the only weight mechanism, so there is no mode to select. Drop the flag. Live NFS serving is retired (ADR 0005).'

refuse_removed_weight_mode_flag() {
  die "$WEIGHT_MODE_FLAG_REMOVED_MESSAGE" 2
}

# ADR 0008: deprecated public no-ops and aliases fail without fallback with a
# named replacement. Parsers still recognize the token so operators do not
# get a generic "unknown arg".
REMOVED_FORCE_MESSAGE='--force was removed (ADR 0008): status labels never block serving. Drop the flag.'
REMOVED_ALLOW_UNVALIDATED_MESSAGE='--allow-unvalidated was removed (ADR 0008): drop the flag. Lab expected-identity files are not a live product (ADR 0012).'
REMOVED_LIST_VALIDATED_MESSAGE='--validated was removed (ADR 0008): profiles are catalog specs whose review.status is display-only (./pulsar models list). It does not mean ADR 0004 Validated.'
REMOVED_CATALOG_VALIDATED_MESSAGE='--validated was removed (ADR 0008): drop the flag. --reviewed-identity is retired (ADR 0012). It does not mean ADR 0004 Validated.'
REMOVED_ACTIVATE_MESSAGE='activate was removed (ADR 0008): use prepare.'
refuse_removed_force_flag() {
  die "$REMOVED_FORCE_MESSAGE" 2
}

PULSAR_MODEL_LIBRARY_DIR="${PULSAR_MODEL_LIBRARY_DIR:-${MODEL_LIBRARY_DIR:-$REPO_DIR/.model-library}}"
# shellcheck disable=SC2034  # consumed by model-library.sh and check-weights.sh
PULSAR_HOT_ROOT_EXPLICIT=0
[ -z "${PULSAR_HOT_ROOT+x}" ] || PULSAR_HOT_ROOT_EXPLICIT=1
PULSAR_HOT_ROOT="${PULSAR_HOT_ROOT:-$HOME/.cache/pulsar-inference-stack/views}"
PULSAR_SSH_CONNECT_TIMEOUT="${PULSAR_SSH_CONNECT_TIMEOUT:-8}"
PULSAR_SSH_OPTS=(
  -o BatchMode=yes
  -o "ConnectTimeout=${PULSAR_SSH_CONNECT_TIMEOUT}"
  -o ConnectionAttempts=1
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=2
)
PULSAR_TOPOLOGY_SSH_CONFIGURED=0

# Called by load_cluster_topology after schema/config validation. Schema 1 keeps
# the existing OpenSSH trust path; enrolled schema 2 pins aliases to exact
# control endpoints and keys for every shared SSH caller.
_pulsar_configure_topology_ssh() {
  [ "${CLUSTER_TOPOLOGY_SSH_TRUSTED:-0}" = 1 ] || return 0
  [ "$PULSAR_TOPOLOGY_SSH_CONFIGURED" = 0 ] || return 0
  PULSAR_SSH_OPTS+=(
    -F "$CLUSTER_SSH_CONFIG_FILE"
    -o AddressFamily=inet
    -o CanonicalizeHostname=no
    -o CheckHostIP=no
    -o ProxyCommand=none
    -o ProxyJump=none
    -o StrictHostKeyChecking=yes
    -o UpdateHostKeys=no
    -o VerifyHostKeyDNS=no
  )
  PULSAR_TOPOLOGY_SSH_CONFIGURED=1
}

# Build the remote-shell command consumed by rsync from the same injectable
# SSH binary and confirmed-topology options used by direct SSH callers.
pulsar_rsync_remote_shell() {
  shell_join_q "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}"
}

# Probe a known control address with its saved alias for identity and host keys.
# Command-line options take precedence over aliases that may resolve elsewhere.
ssh_control_endpoint() {
  local host="$1" control="$2"; shift 2
  "$PULSAR_SSH" -o "HostName=$control" -o "HostKeyAlias=$host" \
    -o StrictHostKeyChecking=yes -o UpdateHostKeys=no \
    -o CanonicalizeHostname=no -o ProxyCommand=none -o ProxyJump=none \
    "${PULSAR_SSH_OPTS[@]}" -- "$host" "$@"
}

ssh_node_command() {
  local rank="${1:?rank required}"
  [ "$rank" -gt 0 ] 2>/dev/null || die "ssh_node requires a remote rank (>0)"
  require_cluster_nodes "$((rank + 1))" \
    || die "rank $rank is not present in the confirmed topology"
  local host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  SSH_NODE_COMMAND=("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host")
}

ssh_node() {
  local rank="${1:?rank required}"; shift
  local -a SSH_NODE_COMMAND=()
  ssh_node_command "$rank"
  "${SSH_NODE_COMMAND[@]}" "$@"
}

# Resolve a physical target for a one-node profile. The selector is intentionally
# stable across topology reordering: callers should pass a topology node_id (the
# wizard does), although an exact hostname, SSH endpoint, control IP, or role key
# is also accepted for operator convenience. An empty selector preserves the
# historical local-node behavior.
#
# Sets:
#   SINGLE_NODE_INDEX       zero-based topology position
#   SINGLE_NODE_KEY         head|worker|rank-N inventory key
#   SINGLE_NODE_ID          stable topology node_id (may be empty standalone)
#   SINGLE_NODE_HOSTNAME    human-facing hostname
#   SINGLE_NODE_SSH_HOST    local or BatchMode SSH endpoint
#   SINGLE_NODE_CONTROL_IP  API/control address when known
#   SINGLE_NODE_REMOTE      0 local, 1 remote
#   SINGLE_NODE_TOPOLOGY_ID confirmed topology identity when known
single_node_key_for_index() {
  local index="${1:?node index required}"
  case "$index" in
    0) printf 'head\n' ;;
    1) printf 'worker\n' ;;
    *[!0-9]*|"") return 1 ;;
    *) printf 'rank-%s\n' "$index" ;;
  esac
}

single_node_index_for_key() {
  local key="${1:?node key required}" index
  case "$key" in
    head) printf '0\n' ;;
    worker) printf '1\n' ;;
    rank-*)
      index="${key#rank-}"
      [[ "$index" =~ ^[0-9]+$ ]] || return 1
      printf '%s\n' "$index"
      ;;
    *) return 1 ;;
  esac
}

resolve_single_node_placement() {
  local selector="${1:-}" index=-1 rank local_host
  load_cluster_topology || return 1

  if [ "$CLUSTER_TOPOLOGY_COUNT" -le 0 ]; then
    case "$selector" in
      ""|local|this-node|head) ;;
      *)
        warn "node '$selector' is not present: no confirmed topology is loaded"
        return 1
        ;;
    esac
    local_host=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo localhost)
    SINGLE_NODE_INDEX=0
    SINGLE_NODE_KEY=head
    SINGLE_NODE_ID=""
    SINGLE_NODE_HOSTNAME="$local_host"
    SINGLE_NODE_SSH_HOST=local
    SINGLE_NODE_CONTROL_IP=127.0.0.1
    SINGLE_NODE_REMOTE=0
    SINGLE_NODE_TOPOLOGY_ID=""
    return 0
  fi

  case "$selector" in
    ""|local|this-node|head)
      index=0
      ;;
    *)
      for ((rank = 0; rank < CLUSTER_TOPOLOGY_COUNT; rank++)); do
        if [ "$selector" = "${CLUSTER_NODE_IDS[$rank]:-}" ] \
            || [ "$selector" = "${CLUSTER_NODE_HOSTNAMES[$rank]:-}" ] \
            || [ "$selector" = "${CLUSTER_NODE_SSH_HOSTS[$rank]:-}" ] \
            || [ "$selector" = "${CLUSTER_NODE_CONTROL_IPS[$rank]:-}" ] \
            || [ "$selector" = "$(single_node_key_for_index "$rank")" ]; then
          { [ "$index" -eq -1 ] || [ "$index" -eq "$rank" ]; } || {
            warn "node selector '$selector' is ambiguous in the confirmed topology"
            return 1
          }
          index="$rank"
        fi
      done
      if [[ "$selector" =~ ^[0-9]+$ ]] \
          && [ "${#selector}" -le "${#CLUSTER_TOPOLOGY_COUNT}" ] \
          && [ "$selector" -lt "$CLUSTER_TOPOLOGY_COUNT" ]; then
        { [ "$index" -eq -1 ] || [ "$index" -eq "$selector" ]; } || {
          warn "node selector '$selector' is ambiguous in the confirmed topology"
          return 1
        }
        index="$selector"
      fi
      ;;
  esac

  if [ "$index" -lt 0 ] || [ "$index" -ge "$CLUSTER_TOPOLOGY_COUNT" ]; then
    warn "node '$selector' is not present in the confirmed topology"
    return 1
  fi

  SINGLE_NODE_INDEX="$index"
  SINGLE_NODE_KEY=$(single_node_key_for_index "$index") || return 1
  SINGLE_NODE_ID="${CLUSTER_NODE_IDS[$index]:-}"
  SINGLE_NODE_HOSTNAME="${CLUSTER_NODE_HOSTNAMES[$index]:-${CLUSTER_NODE_SSH_HOSTS[$index]:-unknown}}"
  SINGLE_NODE_SSH_HOST="${CLUSTER_NODE_SSH_HOSTS[$index]:-}"
  SINGLE_NODE_CONTROL_IP="${CLUSTER_NODE_CONTROL_IPS[$index]:-}"
  SINGLE_NODE_TOPOLOGY_ID="${CLUSTER_TOPOLOGY_ID:-}"
  if [ "$index" -eq 0 ]; then
    SINGLE_NODE_REMOTE=0
    [ -n "$SINGLE_NODE_SSH_HOST" ] || SINGLE_NODE_SSH_HOST=local
    [ -n "$SINGLE_NODE_CONTROL_IP" ] || SINGLE_NODE_CONTROL_IP=127.0.0.1
  else
    SINGLE_NODE_REMOTE=1
    [ -n "$SINGLE_NODE_ID" ] || {
      warn "$(human_node_name "$index") has no stable node_id"
      return 1
    }
    [ -n "$SINGLE_NODE_SSH_HOST" ] \
      && [ "$SINGLE_NODE_SSH_HOST" != local ] || {
      warn "$(human_node_name "$index") has no remote SSH endpoint"
      return 1
    }
  fi
}

single_node_display() {
  local suffix=""
  [ "${SINGLE_NODE_REMOTE:-0}" = 0 ] && suffix=" (this node)"
  printf '%s%s\n' "${SINGLE_NODE_HOSTNAME:-unknown}" "$suffix"
}

single_node_api_host() {
  local host="${SINGLE_NODE_CONTROL_IP:-}"
  [ -n "$host" ] || host="${SINGLE_NODE_SSH_HOST:-127.0.0.1}"
  host="${host#*@}"
  if [[ "$host" == *:* ]] && [[ "$host" != \[*\] ]]; then
    host="[$host]"
  fi
  printf '%s\n' "$host"
}

single_node_api_base_url() {
  local port="${1:?port required}"
  if [ "${SINGLE_NODE_REMOTE:-0}" = 0 ]; then
    printf 'http://127.0.0.1:%s\n' "$port"
  else
    printf 'http://%s:%s\n' "$(single_node_api_host)" "$port"
  fi
}

PULSAR_MANAGED_LABEL="io.pulsar.gb10.managed"
PULSAR_CONF_LABEL="io.pulsar.gb10.conf"
PULSAR_RANK_LABEL="io.pulsar.gb10.rank"
PULSAR_WORLD_SIZE_LABEL="io.pulsar.gb10.world-size"
PULSAR_TOPOLOGY_LABEL="io.pulsar.gb10.topology"
PULSAR_NODE_ID_LABEL="io.pulsar.gb10.node-id"
PULSAR_WEIGHT_SOURCE_LABEL="io.pulsar.gb10.weight-source"
PULSAR_WEIGHT_OWNER_LABEL="io.pulsar.gb10.weight-owner"
PULSAR_WEIGHT_CONFIG_LABEL="io.pulsar.gb10.weight-config"
PULSAR_MODEL_REVISION_LABEL="io.pulsar.gb10.model-revision"
PULSAR_MODEL_SEAL_LABEL="io.pulsar.gb10.model-seal"
PULSAR_VALIDATION_BUNDLE_LABEL="io.pulsar.gb10.validation-bundle"
PULSAR_MODEL_IDENTITY_STATUS_LABEL="io.pulsar.gb10.model-identity-status"
PULSAR_LAUNCH_CONTRACT_LABEL="io.pulsar.gb10.launch-contract"
PULSAR_SPEC_DECODE_LABEL="io.pulsar.gb10.spec-decode"




# Print find-hot JSON for the selected rank. Return 0 on success, 255 when
# that rank is SSH-unreachable, 2 when a found view fails verification, and
# 1 when no ready instance is present. Requires load_conf plus confirmed
# topology (or a resolved one-node topology id).
library_hot_info_for_profile() {
  local profile="${1:?spec required}"
  local -a args=(info "$profile" --json --for-launch --spec-file "$CONF_PATH")
  [ "${PULSAR_OBSERVE_FULL:-0}" != 1 ] || args+=(--full)
  [ -z "${PULSAR_OBSERVE_VERIFICATION_JOBS:-}" ] || args+=(--verification-jobs "$PULSAR_OBSERVE_VERIFICATION_JOBS")
  if [ "${NODES:-1}" = 1 ] && [ -n "${SINGLE_NODE_ID:-}" ]; then
    args+=(--node "$SINGLE_NODE_ID")
  fi
  "$REPO_DIR/scripts/model-library.sh" "${args[@]}"
}

resolve_library_hot_for_profile() {
  local profile="${1:?spec required}" info output rc=0
  info=$(library_hot_info_for_profile "$profile") || rc=$?
  [ "$rc" = 0 ] || die "model files cannot be verified on every serving rank (exit=$rc)" "$rc"
  output=$(printf '%s' "$info" | python3 "$REPO_DIR/scripts/runtime_binding.py" prepared-shell \
    --spec "$CONF_PATH" --topology-id "${CLUSTER_TOPOLOGY_ID:-${SINGLE_NODE_TOPOLOGY_ID:-}}") \
    || die "prepared model records do not match the selected spec"
  eval "$output"
  PULSAR_PREPARED_SET_JSON="$info"
}

json_encode_strings() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[3:], ensure_ascii=False))' _ -- "$@"
}

# Flags probe-node.py needs so remote stdin runs do not import this repo.
append_probe_node_platform_args() {
  local -n _pulsar_probe_args="$1"
  local arch
  _pulsar_probe_args+=(--expected-gpu "$PULSAR_GPU_NAME")
  _pulsar_probe_args+=(--min-active-rdma "$PULSAR_RDMA_MIN_ACTIVE_LINKS")
  # shellcheck disable=SC2086 # architectures are safe identifier tokens
  for arch in $PULSAR_ARCHITECTURES; do
    [ -n "$arch" ] || continue
    _pulsar_probe_args+=(--expected-arch "$arch")
  done
}

probe_node_remote_python_command() {
  local ssh_host="${1:?ssh host required}"
  local -a flags=()
  local flag
  append_probe_node_platform_args flags
  printf 'python3 - --ssh-host %q' "$ssh_host"
  for flag in "${flags[@]}"; do
    printf ' %q' "$flag"
  done
  printf '\n'
}

# Run probe-node.py on a confirmed rank. Rank 0 is local; other ranks receive
# the probe over SSH stdin so the remote host does not need a repo checkout.
probe_node_json_for_rank() {
  local rank="${1:?rank required}"
  local host
  local -a flags=()
  append_probe_node_platform_args flags
  if [ "$rank" = 0 ]; then
    python3 "$REPO_DIR/scripts/probe-node.py" --local --ssh-host local \
      "${flags[@]}"
    return
  fi
  require_cluster_nodes "$((rank + 1))" \
    || die "rank $rank is not present in the confirmed topology"
  host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
  "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
    "$(probe_node_remote_python_command "$host")" \
    <"$REPO_DIR/scripts/probe-node.py"
}

# Load docker run argv from a validated launch plan into a caller array.
# Secrets are applied from the current environment, not from the plan file.
load_docker_argv_from_plan() {
  local plan_file="${1:?plan file required}"
  local rank="${2:?rank required}"
  local dest="${3:?destination array name required}"
  local detach="${4:-0}"
  local json
  local -a argv_flags=(docker-argv "$plan_file" --rank "$rank")
  [ "$detach" = 1 ] && argv_flags+=(--detach)
  json=$(python3 "$REPO_DIR/scripts/launch_plan.py" "${argv_flags[@]}") \
    || die "launch-plan: docker argv failed for rank $rank"
  local -n dest_ref="$dest"
  dest_ref=()
  while IFS= read -r line; do
    dest_ref+=("$line")
  done < <(printf '%s' "$json" | python3 -c \
    'import json,sys; [print(x.replace("\n","\n")) for x in json.load(sys.stdin)]')
  [ "${#dest_ref[@]}" -gt 0 ] || die "launch-plan: empty docker argv for rank $rank"
}

# Build a validated launch-plan JSON file from the currently loaded profile,
# confirmed topology, and resolved model-library instance. Not a permit.
write_launch_plan_file() {
  local dest="${1:?destination required}" action="${2:-start}" ranks_json
  [ -n "${CONF_PATH:-}" ] || die "launch plan requires a selected spec"
  [ -n "${CLUSTER_TOPOLOGY_ID:-}" ] || die "launch plan requires confirmed topology"
  [ -z "${VLLM_EXTRA_ARGS:-}" ] && [ -z "${EXTRA_ENV:-}" ] \
    || die "use an explicit --override-file for execution changes"
  ranks_json=$(
    if [ "$NODES" -eq 1 ]; then
      python3 -c 'import json,sys; print(json.dumps([{
        "rank": 0,
        "node_id": sys.argv[1],
        "hostname": sys.argv[2],
        "ssh_host": sys.argv[3],
        "control_ip": sys.argv[4],
        "control_if": sys.argv[5],
        "hcas": sys.argv[6],
      }]))' \
        "${SINGLE_NODE_ID:-${SINGLE_NODE_HOSTNAME:-standalone}}" \
        "${SINGLE_NODE_HOSTNAME:-localhost}" \
        "${SINGLE_NODE_SSH_HOST:-local}" \
        "${SINGLE_NODE_CONTROL_IP:-127.0.0.1}" \
        "${CLUSTER_NODE_CONTROL_IFS[${SINGLE_NODE_INDEX:-0}]:-lan0}" \
        "${CLUSTER_PROFILE_HCAS[${SINGLE_NODE_INDEX:-0}]:-}"
    else
      for ((rank = 0; rank < NODES; rank++)); do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$rank" \
          "${CLUSTER_NODE_IDS[$rank]}" \
          "${CLUSTER_NODE_HOSTNAMES[$rank]}" \
          "${CLUSTER_NODE_SSH_HOSTS[$rank]}" \
          "${CLUSTER_NODE_CONTROL_IPS[$rank]}" \
          "${CLUSTER_NODE_CONTROL_IFS[$rank]}" \
          "${CLUSTER_PROFILE_HCAS[$rank]}"
      done | python3 -c '
import json,sys
ranks=[]
for line in sys.stdin:
    rank, node_id, hostname, ssh_host, ip, iface, hcas = line.rstrip("\n").split("\t")
    ranks.append({
        "rank": int(rank),
        "node_id": node_id,
        "hostname": hostname,
        "ssh_host": ssh_host,
        "control_ip": ip,
        "control_if": iface,
        "hcas": hcas,
    })
print(json.dumps(ranks))
'
    fi
  )
  PULSAR_PLAN_ACTION="$action" PULSAR_PLAN_RANKS_JSON="$ranks_json" \
  CONF_PATH="$CONF_PATH" CONF_NAME="$CONF_NAME" PORT="$PORT" SERVED_NAME="$SERVED_NAME" \
  CLUSTER_TOPOLOGY_ID="$CLUSTER_TOPOLOGY_ID" PULSAR_PREPARED_SET_JSON="$PULSAR_PREPARED_SET_JSON" \
  python3 - "$dest" <<'PYCODE'
import json, os, pathlib, sys
from release_spec.serving import load_spec, load_json, apply_overrides
from scripts.container_runtime import build_plan
spec=load_spec(os.environ['CONF_PATH'])
selected_document=spec
selected=spec['spec_id']
override=os.environ.get('PULSAR_OVERRIDE_FILE')
if override:
    spec=apply_overrides(spec,load_json(override))
expected=os.environ.get('PULSAR_EFFECTIVE_SPEC_ID')
if expected and expected != spec['spec_id']:
    raise SystemExit('effective recipe changed after selection; review the override again')
facts=dict(lifecycle_action=os.environ['PULSAR_PLAN_ACTION'],port=int(os.environ['PORT']),
           master_port=int(os.environ.get('MASTER_PORT') or '29500'),served_name=os.environ['SERVED_NAME'],
           topology_id=os.environ['CLUSTER_TOPOLOGY_ID'],ranks=json.loads(os.environ['PULSAR_PLAN_RANKS_JSON']))
if os.environ.get('PULSAR_MEMORY_ESTIMATE_JSON'):
    from release_spec.memory_estimate import parse, validate_frozen
    facts['memory_estimate']=validate_frozen(parse(os.environ['PULSAR_MEMORY_ESTIMATE_JSON'].encode()),spec)
plan=build_plan(spec,selected,facts,json.loads(os.environ['PULSAR_PREPARED_SET_JSON']),selected_spec=selected_document)
pathlib.Path(sys.argv[1]).write_text(json.dumps(plan,sort_keys=True,indent=2)+'\n')
result_path=os.environ.get('PULSAR_LAUNCH_RESULT_FILE')
if result_path:
    result=dict(service_id=plan['service_id'],
        selected_spec_id=selected,spec_id=plan['spec_id'],matches_selected_spec=plan['matches_selected_spec'],
        effective_spec=plan['spec'],lifecycle_action=plan['lifecycle_action'])
    if 'memory_estimate' in plan:
        result['memory_estimate_id']=plan['memory_estimate']['estimate_id']
    pathlib.Path(result_path).write_text(json.dumps(result))
PYCODE
}

persist_launch_plan_file() {
  python3 "$REPO_DIR/scripts/service_state.py" save --state-root "$PULSAR_MODEL_LIBRARY_DIR" --plan "$1"
}

retire_stopped_service_indexes() {
  local target="${1:?stopped spec or --all required}" selector="${2:-}" node
  local -a args=(retire --state-root "$PULSAR_MODEL_LIBRARY_DIR" --topology-id "$CLUSTER_TOPOLOGY_ID")
  [ "$target" = --all ] || args+=(--selected-spec-id "$target")
  if [ -n "$selector" ]; then
    resolve_single_node_placement "$selector" || return 1
    args+=(--node-id "$SINGLE_NODE_ID")
  else
    for node in "${CLUSTER_NODE_IDS[@]}"; do args+=(--node-id "$node"); done
  fi
  python3 "$REPO_DIR/scripts/service_state.py" "${args[@]}" >/dev/null
}

runtime_context_for_rank() {
  local index="${1:?physical rank required}"
  if [ "$index" = 0 ]; then
    python3 "$REPO_DIR/scripts/runtime-context.py"
  else
    ssh_node "$index" python3 - <"$REPO_DIR/scripts/runtime-context.py"
  fi
}


require_topology_rewrite_idle() {
  local manifest="${1:?topology manifest required}"
  local rows kind rank node_id hostname ssh_host control_ip control_if hcas rdma_ifs
  local running remote_query

  if ! rows=$(python3 "$REPO_DIR/scripts/topology_manifest.py" rows "$manifest"); then
    warn "cannot validate topology membership before rewrite: $manifest"
    return 1
  fi
  while IFS=$'\t' read -r kind rank node_id hostname ssh_host \
      control_ip control_if hcas rdma_ifs; do
    [ "$kind" = NODE ] || continue
    running=""
    if [ "$rank" = 0 ]; then
      if ! running=$("$PULSAR_DOCKER" ps -q \
          --filter "label=${PULSAR_MANAGED_LABEL}=true" 2>/dev/null); then
        warn "cannot query rank 0 Docker before topology rewrite"
        return 1
      fi
    else
      printf -v remote_query 'docker ps -q --filter %q' \
        "label=${PULSAR_MANAGED_LABEL}=true"
      if ! running=$(ssh_control_endpoint "$ssh_host" "$control_ip" \
          "$remote_query" 2>/dev/null); then
        warn "cannot query rank $rank Docker on $ssh_host before topology rewrite"
        return 1
      fi
    fi
    if [ -n "$running" ]; then
      warn "rank $rank ($hostname) has running stack-managed containers"
      return 1
    fi
  done <<<"$rows"
}

# Inspect / list probe codes (not remove codes):
#   0 = present / success (payload on stdout)
#   1 = operational error (SSH/Docker unreachable or failed)
#   3 = absent (healthy daemon, object not found)
# Remove codes: 0 ok, 1 operational error, 2 ownership refusal.

container_name_for() {
  local name="$1" nodes="${2:-1}"
  if [ "$nodes" -gt 1 ] 2>/dev/null; then
    echo "vllm-cluster-${name}"
  else
    echo "vllm-${name}"
  fi
}

# Expected rank label for a conf on a placement node (head|worker|rank).
expected_rank_for_nodes() {
  local nodes="${1:?}" placement="${2:-head}"
  if [ "$nodes" -gt 1 ] 2>/dev/null; then
    case "$placement" in
      head|0) echo 0 ;;
      worker) echo 1 ;;
      *[!0-9]*|"") die "expected_rank_for_nodes: bad placement $placement" ;;
      *)
        [ "$placement" -lt "$nodes" ] \
          || die "expected_rank_for_nodes: rank $placement outside NODES=$nodes"
        echo "$placement"
        ;;
    esac
  else
    echo single
  fi
}

# Node count of a profile, read from the catalog spec's geometry without
# loading it (a profile is a spec id, ADR 0017 Stage 4). Fails for anything
# else, including a conf name.
profile_nodes_for_conf() {
  local conf="${1:?}"
  [[ "$conf" =~ ^[0-9a-f]{64}$ ]] || return 1
  local path="${PULSAR_RELEASES_ROOT:-$REPO_DIR/releases}/${conf}.json"
  [ -f "$path" ] || return 1
  local nodes
  nodes=$(python3 - "$path" <<'PY'
import json
import sys

try:
    document = json.load(open(sys.argv[1], encoding="utf-8"))
    nodes = int((document["recipe"] if document.get("schema_version") in (2, 3) else document["identity"])["geometry"]["nodes"])
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(1)
if nodes < 1:
    raise SystemExit(1)
print(nodes)
PY
  ) || return 1
  printf '%s' "$nodes"
}

# True when conf exists and rank is valid for head, any worker, or a rank.
placement_rank_allowed() {
  local conf="${1:?}" rank="${2:?}" placement="${3:?}"
  local nodes
  nodes=$(profile_nodes_for_conf "$conf") || return 1
  if [ "$nodes" -eq 1 ]; then
    [ "$rank" = single ] || return 1
    placement_index_for_role "$placement" >/dev/null
    return
  fi
  case "$placement" in
    head) [ "$rank" = 0 ] ;;
    worker)
      [[ "$rank" =~ ^[1-9][0-9]*$ ]] \
        && [ "$rank" -lt "$nodes" ]
      ;;
    rank-*)
      local index
      index=$(placement_index_for_role "$placement") || return 1
      [ "$rank" = "$index" ] && [ "$index" -lt "$nodes" ]
      ;;
    *[!0-9]*|"") return 1 ;;
    *)
      [ "$rank" = "$placement" ] \
        && [ "$rank" -lt "$nodes" ]
      ;;
  esac
}

# docker-inspect --format payload for lifecycle ownership checks (id + labels only).
container_ownership_inspect_format() {
  printf '%s' \
    '{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}}}'
}

# Parse ownership inspect JSON → tab fields: id, name, managed, conf, rank
# name has leading '/' stripped. Empty fields when labels missing.
container_ownership_fields() {
  local metadata="${1:?}"
  printf '%s' "$metadata" | python3 -c '
import json, sys
meta = json.load(sys.stdin)
labels = meta.get("labels") or {}
if labels is None:
    labels = {}
cid = str(meta.get("id") or "")
name = str(meta.get("name") or "").lstrip("/")
managed = str(labels.get(sys.argv[1], "") or "")
conf = str(labels.get(sys.argv[2], "") or "")
rank = str(labels.get(sys.argv[3], "") or "")
print("\t".join([cid, name, managed, conf, rank]))
' "$PULSAR_MANAGED_LABEL" "$PULSAR_CONF_LABEL" "$PULSAR_RANK_LABEL"
}

# Declared world size from ownership metadata (multi-node launches only).
container_world_size_field() {
  local metadata="${1:?}"
  printf '%s' "$metadata" | python3 -c '
import json, sys
labels = json.load(sys.stdin).get("labels") or {}
print(str(labels.get(sys.argv[1], "") or ""))
' "$PULSAR_WORLD_SIZE_LABEL"
}

# Return the declared model-weight source from ownership metadata. The value
# is provenance, not a mode: current managed launches write local-files.
# Containers labeled replicated or unlabeled are historical observations.
# Malformed JSON is an observation failure.
container_weight_source_field() {
  local metadata="${1:?}"
  printf '%s' "$metadata" | python3 -c '
import json, sys
labels = json.load(sys.stdin).get("labels") or {}
value = str(labels.get(sys.argv[1], "") or "")
print(value or "replicated")
' "$PULSAR_WEIGHT_SOURCE_LABEL"
}

# Exactly managed="true" (launchers/inventory); never 1/yes/TRUE.
container_managed_label_is_true() {
  local managed="${1:-}"
  [ "$managed" = "true" ]
}

container_placement_identity_fields() {
  local metadata="${1:?}"
  printf '%s' "$metadata" | python3 -c '
import json, sys
labels = json.load(sys.stdin).get("labels") or {}
print("\t".join([
    str(labels.get(sys.argv[1], "") or ""),
    str(labels.get(sys.argv[2], "") or ""),
]))
' "$PULSAR_TOPOLOGY_LABEL" "$PULSAR_NODE_ID_LABEL"
}

placement_index_for_role() {
  local placement="${1:?}" index
  case "$placement" in
    head) printf '0\n' ;;
    worker) printf '1\n' ;;
    rank-*)
      index="${placement#rank-}"
      [[ "$index" =~ ^[0-9]+$ ]] || return 1
      printf '%s\n' "$index"
      ;;
    *[!0-9]*|"") return 1 ;;
    *) printf '%s\n' "$placement" ;;
  esac
}

# New remote one-node containers must prove the physical topology node where
# they were observed. Missing identity remains a compatibility path only for
# historical local one-node containers.
container_single_node_identity_is_proven() {
  local metadata="${1:?}" placement="${2:?}" index expected_id have_topology have_id
  index=$(placement_index_for_role "$placement") || return 1
  load_cluster_topology || return 1
  IFS=$'\t' read -r have_topology have_id \
    < <(container_placement_identity_fields "$metadata")

  if [ -z "$have_id" ]; then
    [ "$index" -eq 0 ]
    return
  fi
  [ "$index" -lt "$CLUSTER_TOPOLOGY_COUNT" ] || return 1
  expected_id="${CLUSTER_NODE_IDS[$index]:-}"
  [ -n "$expected_id" ] && [ "$have_id" = "$expected_id" ] || return 1
  if [ -n "$have_topology" ] && [ -n "${CLUSTER_TOPOLOGY_ID:-}" ]; then
    [ "$have_topology" = "$CLUSTER_TOPOLOGY_ID" ] || return 1
  fi
  return 0
}

container_single_node_identity_refuse_reason() {
  local metadata="${1:?}" placement="${2:?}" index expected_id have_topology have_id
  index=$(placement_index_for_role "$placement" 2>/dev/null || echo "?")
  IFS=$'\t' read -r have_topology have_id \
    < <(container_placement_identity_fields "$metadata")
  if [ -z "$have_id" ]; then
    if [ "$index" = 0 ]; then
      echo "local legacy single-node container has no node-id"
    else
      echo "remote single-node container has no ${PULSAR_NODE_ID_LABEL}"
    fi
    return
  fi
  load_cluster_topology >/dev/null 2>&1 || {
    echo "confirmed topology is unavailable"
    return
  }
  expected_id="${CLUSTER_NODE_IDS[$index]:-}"
  if [ -z "$expected_id" ] || [ "$have_id" != "$expected_id" ]; then
    echo "node-id mismatch (have='${have_id}' expected='${expected_id:-unknown}')"
    return
  fi
  if [ -n "$have_topology" ] && [ -n "${CLUSTER_TOPOLOGY_ID:-}" ] \
      && [ "$have_topology" != "$CLUSTER_TOPOLOGY_ID" ]; then
    echo "topology label mismatch"
    return
  fi
  echo "physical node identity not proven"
}

container_removal_is_proven() {
  local metadata="${1:?}" want_conf="${2:-}" want_rank="${3:-}" placement="${4:-}"
  container_ownership_is_proven "$metadata" "$want_conf" "$want_rank" || return 1
  if [ -n "$placement" ]; then
    container_single_node_identity_is_proven "$metadata" "$placement" || return 1
  fi
}

container_removal_refuse_reason() {
  local metadata="${1:?}" want_conf="${2:-}" want_rank="${3:-}" placement="${4:-}"
  if ! container_ownership_is_proven "$metadata" "$want_conf" "$want_rank"; then
    container_ownership_refuse_reason "$metadata" "$want_conf" "$want_rank"
  elif [ -n "$placement" ] \
      && ! container_single_node_identity_is_proven "$metadata" "$placement"; then
    container_single_node_identity_refuse_reason "$metadata" "$placement"
  else
    echo "ownership not proven"
  fi
}

# True when labels prove stack management for want_conf/want_rank.
# want_conf / want_rank empty means "not constrained by caller" for that field
# (still requires non-empty conf/rank and managed=true). Used by named stops.
# Lifecycle removal never accepts unlabeled/legacy argv fallbacks.
container_ownership_is_proven() {
  local metadata="${1:?}" want_conf="${2:-}" want_rank="${3:-}"
  local id name managed conf rank
  IFS=$'\t' read -r id name managed conf rank < <(container_ownership_fields "$metadata")
  [ -n "$id" ] || return 1
  container_managed_label_is_true "$managed" || return 1
  [ -n "$conf" ] || return 1
  [ -n "$rank" ] || return 1
  if [ -n "$want_conf" ] && [ "$conf" != "$want_conf" ]; then
    return 1
  fi
  if [ -n "$want_rank" ] && [ "$rank" != "$want_rank" ]; then
    return 1
  fi
  return 0
}

# True when a managed container may be stopped. Labels prove ownership.
# A live conf file is extra geometry when present; a missing conf still
# stops from labels (world-size / single-node identity).
container_all_candidate_is_safe() {
  local metadata="${1:?}" placement="${2:?}"
  local id name managed conf rank
  IFS=$'\t' read -r id name managed conf rank < <(container_ownership_fields "$metadata")
  [ -n "$id" ] || return 1
  container_managed_label_is_true "$managed" || return 1
  [ -n "$conf" ] || return 1
  [ -n "$rank" ] || return 1
  local nodes
  if ! profile_nodes_for_conf "$conf" >/dev/null 2>&1; then
    # Retired profile (no catalog spec under releases/, or a conf name from
    # before ADR 0017 Stage 4): ownership is still proven by labels, so the
    # container must remain stoppable. Geometry comes from its own labels.
    if [ "$rank" = single ]; then
      placement_index_for_role "$placement" >/dev/null || return 1
      container_single_node_identity_is_proven "$metadata" "$placement"
      return
    fi
    nodes=$(container_world_size_field "$metadata")
    [[ "$nodes" =~ ^[1-9][0-9]*$ ]] && [ "$nodes" -ge 2 ] || return 1
    [[ "$rank" =~ ^[0-9]+$ ]] && [ "$rank" -lt "$nodes" ] || return 1
    load_cluster_topology || return 1
    [ "$nodes" -le "$CLUSTER_TOPOLOGY_COUNT" ] || return 1
    [ "$rank" = "$(placement_index_for_role "$placement")" ] || return 1
    container_single_node_identity_is_proven "$metadata" "$placement" || return 1
    return 0
  fi
  placement_rank_allowed "$conf" "$rank" "$placement" || return 1
  nodes=$(profile_nodes_for_conf "$conf") || return 1
  if [ "$nodes" -eq 1 ]; then
    container_single_node_identity_is_proven "$metadata" "$placement"
  else
    # A multi-node rank may only be removed when every rank of its profile is
    # a confirmed, probeable member; otherwise --all could strand live remote
    # ranks (e.g. clusters launched before topology confirmation).
    load_cluster_topology || return 1
    [ "$nodes" -le "$CLUSTER_TOPOLOGY_COUNT" ] || return 1
    container_single_node_identity_is_proven "$metadata" "$placement" || return 1
  fi
}

# Describe why ownership failed (for refuse messages). Never claims safe to remove.
container_ownership_refuse_reason() {
  local metadata="${1:?}" want_conf="${2:-}" want_rank="${3:-}"
  local id name managed conf rank
  IFS=$'\t' read -r id name managed conf rank < <(container_ownership_fields "$metadata")
  if [ -z "$id" ]; then
    echo "missing container id"
    return
  fi
  if ! container_managed_label_is_true "$managed"; then
    if [ -z "$managed" ] && [ -z "$conf" ] && [ -z "$rank" ]; then
      echo "unlabeled/legacy container (no ${PULSAR_MANAGED_LABEL})"
    else
      echo "not stack-managed (managed='${managed}'; require exactly 'true')"
    fi
    return
  fi
  if [ -z "$conf" ] || [ -z "$rank" ]; then
    echo "managed label set but conf/rank incomplete (conf='${conf}' rank='${rank}')"
    return
  fi
  if [ -n "$want_conf" ] && [ "$conf" != "$want_conf" ]; then
    echo "conf label mismatch (have='${conf}' want='${want_conf}')"
    return
  fi
  if [ -n "$want_rank" ] && [ "$rank" != "$want_rank" ]; then
    echo "rank label mismatch (have='${rank}' want='${want_rank}')"
    return
  fi
  echo "ownership not proven"
}

container_all_refuse_reason() {
  local metadata="${1:?}" placement="${2:?}"
  local id name managed conf rank nodes
  IFS=$'\t' read -r id name managed conf rank < <(container_ownership_fields "$metadata")
  if ! container_managed_label_is_true "$managed"; then
    container_ownership_refuse_reason "$metadata" "" ""
    return
  fi
  if [ -z "$conf" ] || [ -z "$rank" ]; then
    echo "managed label set but conf/rank incomplete (conf='${conf}' rank='${rank}')"
    return
  fi
  if ! profile_nodes_for_conf "$conf" >/dev/null 2>&1; then
    if [ "$rank" = single ]; then
      container_single_node_identity_refuse_reason "$metadata" "$placement"
      return
    fi
    nodes=$(container_world_size_field "$metadata")
    if ! [[ "$nodes" =~ ^[1-9][0-9]*$ ]] || [ "$nodes" -lt 2 ]; then
      echo "retired conf '${conf}' with no usable world-size label"
      return
    fi
    load_cluster_topology >/dev/null 2>&1 || true
    if [ "$nodes" -gt "${CLUSTER_TOPOLOGY_COUNT:-0}" ]; then
      echo "retired conf ${conf} spans ${nodes} ranks but only ${CLUSTER_TOPOLOGY_COUNT:-0} confirmed — confirm topology before cleanup"
      return
    fi
    echo "ownership not proven for retired conf '${conf}'"
    return
  fi
  if ! nodes=$(profile_nodes_for_conf "$conf"); then
    echo "cannot read NODES for conf '${conf}'"
    return
  fi
  if ! placement_rank_allowed "$conf" "$rank" "$placement"; then
    echo "placement mismatch on ${placement}: conf=${conf} nodes=${nodes} rank=${rank}"
    return
  fi
  if [ "$nodes" -eq 1 ] \
      && ! container_single_node_identity_is_proven "$metadata" "$placement"; then
    container_single_node_identity_refuse_reason "$metadata" "$placement"
    return
  fi
  if [ "$nodes" -gt 1 ]; then
    load_cluster_topology >/dev/null 2>&1 || true
    if [ "$nodes" -gt "${CLUSTER_TOPOLOGY_COUNT:-0}" ]; then
      echo "conf ${conf} spans ${nodes} ranks but only ${CLUSTER_TOPOLOGY_COUNT:-0} confirmed — confirm topology before cleanup"
      return
    fi
  fi
  echo "ownership not proven"
}

# Validate docker run -d stdout: exactly one 64-hex id (optional trailing newline).
# Prints normalized id on success; returns 1 on garbage/extra stdout.
parse_docker_run_container_id() {
  local raw="${1-}"
  local id
  id=$(printf '%s' "$raw")
  id="${id%"${id##*[![:space:]]}"}"
  id="${id#"${id%%[![:space:]]*}"}"
  case "$id" in
    *$'\n'*|*$'\r'*|*' '*) return 1 ;;
  esac
  [[ "$id" =~ ^[0-9a-fA-F]{64}$ ]] || return 1
  printf '%s' "$id"
}

# Merge lifecycle remove exit codes. Severity: 1 (operational) > 2 (refuse) > 0.
# Used so a later refusal never masks an earlier operational error.
lifecycle_merge_rc() {
  local cur="${1:-0}" new="${2:-0}"
  if [ "$new" -eq 0 ]; then
    printf '%s' "$cur"
    return 0
  fi
  if [ "$cur" -eq 1 ] || [ "$new" -eq 1 ]; then
    printf '%s' 1
    return 0
  fi
  if [ "$cur" -eq 2 ] || [ "$new" -eq 2 ]; then
    printf '%s' 2
    return 0
  fi
  printf '%s' "$new"
}

# When docker run appears to succeed but stdout is not a valid container id,
# refuse arbitrary cleanup. Optionally read-only inspect exact name; if labels
# prove ownership for conf/rank, mention the short id — never remove it.
# Prints guidance: scripts/inventory.sh then scripts/down.sh <conf>.
# Usage: report_untracked_launch_container <role> <conf> <rank> <cname> [host]
# role is local|remote; head|worker remain compatibility aliases.
report_untracked_launch_container() {
  local role="${1:?}" conf="${2:?}" rank="${3:?}" cname="${4:?}" host="${5:-}"
  local meta rc=0 id short where

  case "$role" in
    head|local) where="local rank $rank" ;;
    worker|remote) where="rank $rank (${host:-?})" ;;
    *) where="$role" ;;
  esac

  warn "docker run on ${where} returned invalid container id output — refusing arbitrary cleanup"
  warn "a managed container for conf=${conf} may have been created and was deliberately left untouched"
  warn "safe remediation: ./pulsar inventory   then   ./pulsar stop ${conf}"

  meta=""
  if [ -n "$host" ]; then
    meta=$(container_ownership_inspect_remote "$host" "$cname") || rc=$?
  else
    meta=$(container_ownership_inspect_local "$cname") || rc=$?
  fi
  if [ "$rc" -eq 0 ] && container_ownership_is_proven "$meta" "$conf" "$rank"; then
    IFS=$'\t' read -r id _ < <(container_ownership_fields "$meta")
    short="${id:0:12}"
    warn "read-only inspect: exact name ${cname} on ${where} has proven labels id=${short} (not removed)"
  fi
  return 0
}

# Local inspect: 0 present (JSON stdout), 3 absent, 1 docker error.
container_ownership_inspect_local() {
  local ref="${1:?}"
  local format meta
  format=$(container_ownership_inspect_format)
  if meta=$("$PULSAR_DOCKER" inspect --format "$format" "$ref" 2>/dev/null); then
    [ -n "$meta" ] || return 1
    printf '%s' "$meta"
    return 0
  fi
  if ! "$PULSAR_DOCKER" info >/dev/null 2>&1; then
    return 1
  fi
  return 3
}

# Remote inspect with explicit error-vs-absent. Never masks SSH/Docker failure
# as absence. 0 present, 3 absent, 1 unreachable/error.
container_ownership_inspect_remote() {
  local host="${1:?}" ref="${2:?}"
  local format remote_cmd out status meta
  format=$(container_ownership_inspect_format)
  # Remote protocol (one status line, optional JSON body):
  #   PRESENT\n<json> | ABSENT | DOCKER_ERROR
  remote_cmd="if ! docker info >/dev/null 2>&1; then printf '%s\\n' DOCKER_ERROR; exit 0; fi; "
  remote_cmd+="if meta=\$(docker inspect --format $(printf '%q' "$format") $(printf '%q' "$ref") 2>/dev/null); then "
  remote_cmd+="printf 'PRESENT\\n%s\\n' \"\$meta\"; exit 0; fi; "
  remote_cmd+="printf '%s\\n' ABSENT; exit 0"

  if ! out=$("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$remote_cmd" 2>/dev/null); then
    return 1
  fi
  status=$(printf '%s\n' "$out" | head -n1 | tr -d '\r')
  case "$status" in
    PRESENT)
      meta=$(printf '%s\n' "$out" | tail -n +2)
      # Drop a single trailing newline from the body for JSON parse stability.
      meta="${meta%"${meta##*[![:space:]]}"}"
      [ -n "$meta" ] || return 1
      printf '%s' "$meta"
      return 0
      ;;
    ABSENT)
      return 3
      ;;
    DOCKER_ERROR|*)
      return 1
      ;;
  esac
}

# Verify immutable id is gone. 0 absent, 1 still present or probe error.
container_id_absent_local() {
  local id="${1:?}" rc=0
  container_ownership_inspect_local "$id" >/dev/null 2>&1 && return 1
  rc=$?
  [ "$rc" -eq 3 ]
}

container_id_absent_remote() {
  local host="${1:?}" id="${2:?}" rc=0
  container_ownership_inspect_remote "$host" "$id" >/dev/null 2>&1 && return 1
  rc=$?
  [ "$rc" -eq 3 ]
}

# Remove one local container by exact name only if labels prove ownership.
# Exit 0: removed or absent. Exit 2: present but refused. Exit 1: operational error.
remove_stack_owned_container_local() {
  local name="${1:?}" want_conf="${2:-}" want_rank="${3:-}" placement="${4:-}"
  local meta id name_have managed conf rank meta2 id2 reason short rc

  rc=0
  meta=$(container_ownership_inspect_local "$name") || rc=$?
  if [ "$rc" -eq 3 ]; then
    log "no container named $name"
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    warn "local docker error while inspecting $name"
    return 1
  fi

  IFS=$'\t' read -r id name_have managed conf rank < <(container_ownership_fields "$meta")
  if ! container_removal_is_proven "$meta" "$want_conf" "$want_rank" "$placement"; then
    reason=$(container_removal_refuse_reason "$meta" "$want_conf" "$want_rank" "$placement")
    warn "refusing to remove $name: $reason"
    return 2
  fi

  short="${id:0:12}"
  rc=0
  meta2=$(container_ownership_inspect_local "$id") || rc=$?
  if [ "$rc" -eq 3 ]; then
    log "container $name ($short) already gone before remove"
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    warn "local docker error during revalidation of $name id=$short"
    return 1
  fi
  IFS=$'\t' read -r id2 _ < <(container_ownership_fields "$meta2")
  if [ "$id2" != "$id" ] \
      || ! container_removal_is_proven "$meta2" "$want_conf" "$want_rank" "$placement"; then
    reason=$(container_removal_refuse_reason "$meta2" "$want_conf" "$want_rank" "$placement")
    warn "refusing to remove $name: ownership revalidation failed ($reason)"
    return 2
  fi

  log "removing $name id=$short (managed conf=${conf} rank=${rank})"
  if ! "$PULSAR_DOCKER" rm -f "$id" >/dev/null; then
    warn "docker rm -f failed for $name id=$short"
    return 1
  fi
  if ! container_id_absent_local "$id"; then
    warn "container id=$short still present or unverifiable after docker rm -f"
    return 1
  fi
  return 0
}

# Same as local, via SSH to host. Remote docker binary is always "docker".
remove_stack_owned_container_remote() {
  local host="${1:?}" name="${2:?}" want_conf="${3:-}" want_rank="${4:-}" placement="${5:-}"
  local meta id name_have managed conf rank meta2 id2 reason short remote_rm rc

  rc=0
  meta=$(container_ownership_inspect_remote "$host" "$name") || rc=$?
  if [ "$rc" -eq 3 ]; then
    log "no container named $name on $host"
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    warn "remote host unreachable or Docker error inspecting $name on $host"
    return 1
  fi

  IFS=$'\t' read -r id name_have managed conf rank < <(container_ownership_fields "$meta")
  if ! container_removal_is_proven "$meta" "$want_conf" "$want_rank" "$placement"; then
    reason=$(container_removal_refuse_reason "$meta" "$want_conf" "$want_rank" "$placement")
    warn "refusing to remove $name on $host: $reason"
    return 2
  fi

  short="${id:0:12}"
  rc=0
  meta2=$(container_ownership_inspect_remote "$host" "$id") || rc=$?
  if [ "$rc" -eq 3 ]; then
    log "container $name ($short) on $host already gone before remove"
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    warn "remote host unreachable during revalidation of $name id=$short on $host"
    return 1
  fi
  IFS=$'\t' read -r id2 _ < <(container_ownership_fields "$meta2")
  if [ "$id2" != "$id" ] \
      || ! container_removal_is_proven "$meta2" "$want_conf" "$want_rank" "$placement"; then
    reason=$(container_removal_refuse_reason "$meta2" "$want_conf" "$want_rank" "$placement")
    warn "refusing to remove $name on $host: ownership revalidation failed ($reason)"
    return 2
  fi

  log "removing $name on $host id=$short (managed conf=${conf} rank=${rank})"
  remote_rm="docker rm -f $(printf '%q' "$id") >/dev/null"
  if ! "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$remote_rm"; then
    warn "docker rm -f failed for $name id=$short on $host"
    return 1
  fi
  if ! container_id_absent_remote "$host" "$id"; then
    warn "container id=$short still present or unverifiable on $host after docker rm -f"
    return 1
  fi
  return 0
}

# Inspect an exact container reference on a topology node. Exit codes preserve
# the ownership probe contract: 0 present, 3 absent, 1 operational failure.
container_ownership_inspect_on_node() {
  local index="${1:?node index required}" ref="${2:?container ref required}"
  load_cluster_topology || return 1
  if [ "$index" -eq 0 ]; then
    container_ownership_inspect_local "$ref"
    return
  fi
  [ "$index" -lt "$CLUSTER_TOPOLOGY_COUNT" ] || return 1
  container_ownership_inspect_remote "${CLUSTER_NODE_SSH_HOSTS[$index]}" "$ref"
}

# Discover the unique physical node holding a one-node profile. Every confirmed
# node is probed before returning, so an SSH/Docker failure is never interpreted
# as absence. A conflicting exact name or duplicate managed copy is refused.
# Exit: 0 unique index on stdout; 3 absent everywhere; 2 refused; 1 probe error.
discover_single_node_index_for_conf() {
  local conf="${1:?conf required}" cname count index meta rc role found=-1
  local reason
  cname=$(container_name_for "$conf" 1)
  load_cluster_topology || return 1
  count="$CLUSTER_TOPOLOGY_COUNT"
  [ "$count" -gt 0 ] || count=1

  for ((index = 0; index < count; index++)); do
    rc=0
    meta=$(container_ownership_inspect_on_node "$index" "$cname") || rc=$?
    case "$rc" in
      3) continue ;;
      0) ;;
      *)
        warn "cannot inspect $cname on $(human_node_name "$index")"
        return 1
        ;;
    esac
    role=$(single_node_key_for_index "$index") || return 1
    if ! container_removal_is_proven "$meta" "$conf" single "$role"; then
      reason=$(container_removal_refuse_reason "$meta" "$conf" single "$role")
      warn "refusing $cname on $(human_node_name "$index"): $reason"
      return 2
    fi
    if [ "$found" -ge 0 ]; then
      warn "refusing ambiguous placement: $cname exists on $(human_node_name "$found") and $(human_node_name "$index")"
      return 2
    fi
    found="$index"
  done

  [ "$found" -ge 0 ] || return 3
  printf '%s\n' "$found"
}

# Remove a one-node profile at an explicitly resolved physical placement.
# The underlying helper captures the immutable ID and revalidates ownership,
# rank, and node identity immediately before removal.
remove_stack_owned_single_at_resolved_node() {
  local conf="${1:?conf required}" cname role
  cname=$(container_name_for "$conf" 1)
  role="${SINGLE_NODE_KEY:-head}"
  if [ "${SINGLE_NODE_REMOTE:-0}" = 1 ]; then
    remove_stack_owned_container_remote "$SINGLE_NODE_SSH_HOST" \
      "$cname" "$conf" single "$role"
  else
    remove_stack_owned_container_local "$cname" "$conf" single "$role"
  fi
}

# Best-effort remove by immutable ID only (current-launch cleanup). Never by name.
# Only accepts a validated 64-hex id — never arbitrary docker run stdout.
remove_container_id_local() {
  local id="${1:-}" normalized
  [ -n "$id" ] || return 0
  if ! normalized=$(parse_docker_run_container_id "$id"); then
    warn "refusing id cleanup: invalid container id"
    return 0
  fi
  "$PULSAR_DOCKER" rm -f "$normalized" >/dev/null 2>&1 || true
}

remove_container_id_remote() {
  local host="${1:-}" id="${2:-}" normalized
  [ -n "$host" ] && [ -n "$id" ] || return 0
  if ! normalized=$(parse_docker_run_container_id "$id"); then
    warn "refusing remote id cleanup: invalid container id"
    return 0
  fi
  "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
    "docker rm -f $(printf '%q' "$normalized") >/dev/null 2>&1 || true" 2>/dev/null || true
}

# List IDs of containers with managed=true.
# 0 success (stdout may be empty); 1 operational error — never mask as empty.
list_managed_container_ids_local() {
  if ! "$PULSAR_DOCKER" info >/dev/null 2>&1; then
    return 1
  fi
  "$PULSAR_DOCKER" ps -aq --filter "label=${PULSAR_MANAGED_LABEL}=true" 2>/dev/null || return 1
  return 0
}

list_managed_container_ids_remote() {
  local host="${1:?}" out status
  local remote_cmd filter_q
  filter_q=$(printf '%q' "label=${PULSAR_MANAGED_LABEL}=true")
  # Protocol status line (never print OK before a successful docker ps):
  #   OK\n[ids...] | DOCKER_INFO_ERROR | DOCKER_PS_ERROR
  # Both error statuses ⇒ operational failure (not empty success).
  remote_cmd="if ! docker info >/dev/null 2>&1; then printf '%s\\n' DOCKER_INFO_ERROR; exit 0; fi; "
  remote_cmd+="if ! ids=\$(docker ps -aq --filter ${filter_q}); then printf '%s\\n' DOCKER_PS_ERROR; exit 0; fi; "
  remote_cmd+="printf '%s\\n' OK; "
  remote_cmd+="if [ -n \"\$ids\" ]; then printf '%s\\n' \"\$ids\"; fi"

  if ! out=$("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$remote_cmd" 2>/dev/null); then
    return 1
  fi
  status=$(printf '%s\n' "$out" | head -n1 | tr -d '\r')
  case "$status" in
    OK)
      printf '%s\n' "$out" | tail -n +2
      return 0
      ;;
    DOCKER_INFO_ERROR|DOCKER_PS_ERROR)
      return 1
      ;;
    *)
      return 1
      ;;
  esac
}

list_managed_container_ids_on_node() {
  local index="${1:?node index required}"
  if [ "$index" -eq 0 ]; then
    list_managed_container_ids_local
    return
  fi
  load_cluster_topology || return 1
  [ "$index" -lt "$CLUSTER_TOPOLOGY_COUNT" ] || return 1
  list_managed_container_ids_remote "${CLUSTER_NODE_SSH_HOSTS[$index]}"
}

# Revalidate then remove one managed id on the node where it was observed.
# Uses the same stoppability predicate as --all.
# Exit: 0 removed or already gone; 2 refused; 1 operational error.
remove_safe_managed_id_on_node() {
  local index="${1:?node index required}" id="${2:?container id required}"
  local placement meta probe=0 reason conf rank short host
  placement=$(single_node_key_for_index "$index") || return 1
  meta=$(container_ownership_inspect_on_node "$index" "$id") || probe=$?
  if [ "$probe" -eq 3 ]; then
    log "container id=${id:0:12} already gone on $(human_node_name "$index")"
    return 0
  fi
  if [ "$probe" -ne 0 ]; then
    warn "$(human_node_name "$index") is unobservable during revalidation"
    return 1
  fi
  IFS=$'\t' read -r _ _ _ conf rank < <(container_ownership_fields "$meta")
  if ! container_all_candidate_is_safe "$meta" "$placement"; then
    reason=$(container_all_refuse_reason "$meta" "$placement")
    warn "refusing id=${id:0:12} on $(human_node_name "$index"): $reason"
    return 2
  fi
  short="${id:0:12}"
  log "removing stack-managed id=$short conf=$conf rank=$rank on $(human_node_name "$index")"
  if [ "$index" -eq 0 ]; then
    if ! "$PULSAR_DOCKER" rm -f "$id" >/dev/null; then
      warn "docker rm -f failed for id=$short"
      return 1
    fi
    if ! container_id_absent_local "$id"; then
      warn "container id=$short still present or unverifiable after docker rm -f"
      return 1
    fi
    return 0
  fi
  host="${CLUSTER_NODE_SSH_HOSTS[$index]}"
  if ! "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
      "docker rm -f $(printf '%q' "$id") >/dev/null"; then
    warn "docker rm -f failed for id=$short on $(human_node_name "$index")"
    return 1
  fi
  if ! container_id_absent_remote "$host" "$id"; then
    warn "container id=$short still present or unverifiable on $(human_node_name "$index") after docker rm -f"
    return 1
  fi
  return 0
}

# Named stop by proven labels. Conf file is extra geometry when present.
# Every confirmed Docker endpoint is probed before mutation. An unobservable
# node blocks removal (a live rank could be stranded).
# Exit: 0 stopped or absent; 2 refused; 1 unobservable/operational.
# Sets STOP_NAMED_NOTHING_FOUND=1 when no matching service was running.
stop_named_service_by_labels() {
  local conf="${1:?conf required}" node_selector="${2:-}"
  local count index ids id meta probe=0 placement reason rank have_conf
  local -a found_indices=() found_ids=() found_ranks=()
  local filtered_indices=() filtered_ids=() i
  STOP_NAMED_NOTHING_FOUND=0
  load_cluster_topology || return 1
  count="$CLUSTER_TOPOLOGY_COUNT"
  [ "$count" -gt 0 ] || count=1

  for ((index = 0; index < count; index++)); do
    if ! list_managed_container_ids_on_node "$index" >/dev/null; then
      warn "$(human_node_name "$index") is unobservable — not removing conf=$conf (an unobserved live rank could be stranded)"
      return 1
    fi
  done

  for ((index = 0; index < count; index++)); do
    placement=$(single_node_key_for_index "$index") || return 1
    ids=$(list_managed_container_ids_on_node "$index") || {
      warn "$(human_node_name "$index") is unobservable — not removing conf=$conf"
      return 1
    }
    for id in $ids; do
      [ -n "$id" ] || continue
      probe=0
      meta=$(container_ownership_inspect_on_node "$index" "$id") || probe=$?
      if [ "$probe" -eq 3 ]; then
        continue
      fi
      if [ "$probe" -ne 0 ]; then
        warn "$(human_node_name "$index") is unobservable — not removing conf=$conf"
        return 1
      fi
      IFS=$'\t' read -r _ _ _ have_conf rank < <(container_ownership_fields "$meta")
      [ "$have_conf" = "$conf" ] || continue
      if ! container_all_candidate_is_safe "$meta" "$placement"; then
        reason=$(container_all_refuse_reason "$meta" "$placement")
        warn "refusing conf=$conf id=${id:0:12} on $(human_node_name "$index"): $reason"
        return 2
      fi
      found_indices+=("$index")
      found_ids+=("$id")
      found_ranks+=("$rank")
    done
  done

  if [ -n "$node_selector" ]; then
    if [ "${#found_ranks[@]}" -gt 0 ]; then
      for rank in "${found_ranks[@]}"; do
        if [[ "$rank" =~ ^[0-9]+$ ]]; then
          warn "--node is only valid for one-node services"
          return 2
        fi
      done
    fi
    resolve_single_node_placement "$node_selector" || return 1
    if [ "${#found_indices[@]}" -gt 0 ]; then
      for i in "${!found_indices[@]}"; do
        if [ "${found_indices[$i]}" = "$SINGLE_NODE_INDEX" ]; then
          filtered_indices+=("${found_indices[$i]}")
          filtered_ids+=("${found_ids[$i]}")
        fi
      done
    fi
    found_indices=("${filtered_indices[@]+"${filtered_indices[@]}"}")
    found_ids=("${filtered_ids[@]+"${filtered_ids[@]}"}")
  fi

  if [ "${#found_ids[@]}" -eq 0 ]; then
    log "no stack-managed service is running for spec $conf; nothing to stop"
    STOP_NAMED_NOTHING_FOUND=1
    return 0
  fi

  for i in "${!found_ids[@]}"; do
    remove_safe_managed_id_on_node "${found_indices[$i]}" "${found_ids[$i]}" \
      || return
  done
  log "done"
  return 0
}

# Remove local --all candidates that are safe for head placement.
# Exit 0 all ok; 1 operational error; 2 any refused candidate left intact.
# Operational error (1) is sticky: later refusals must not overwrite it.
remove_all_stack_managed_local() {
  local ids id meta conf rank reason rc=0 short probe
  ids=$(list_managed_container_ids_local) || {
    warn "local docker error listing managed containers"
    return 1
  }
  if [ -z "${ids//[$' \t\r\n']/}" ]; then
    log "no stack-managed containers on local docker"
    return 0
  fi
  for id in $ids; do
    [ -n "$id" ] || continue
    probe=0
    meta=$(container_ownership_inspect_local "$id") || probe=$?
    if [ "$probe" -eq 3 ]; then
      continue
    fi
    if [ "$probe" -ne 0 ]; then
      warn "local docker error inspecting managed id=${id:0:12}"
      rc=$(lifecycle_merge_rc "$rc" 1)
      continue
    fi
    IFS=$'\t' read -r _ _ _ conf rank < <(container_ownership_fields "$meta")
    if ! container_all_candidate_is_safe "$meta" "head"; then
      reason=$(container_all_refuse_reason "$meta" "head")
      warn "refusing managed candidate id=${id:0:12} on head: $reason"
      rc=$(lifecycle_merge_rc "$rc" 2)
      continue
    fi
    short="${id:0:12}"
    probe=0
    meta=$(container_ownership_inspect_local "$id") || probe=$?
    if [ "$probe" -eq 3 ]; then
      continue
    fi
    if [ "$probe" -ne 0 ]; then
      warn "local docker error revalidating id=$short"
      rc=$(lifecycle_merge_rc "$rc" 1)
      continue
    fi
    if ! container_all_candidate_is_safe "$meta" "head"; then
      reason=$(container_all_refuse_reason "$meta" "head")
      warn "refusing id=$short on head after revalidation: $reason"
      rc=$(lifecycle_merge_rc "$rc" 2)
      continue
    fi
    log "removing stack-managed id=$short conf=$conf rank=$rank (head)"
    if ! "$PULSAR_DOCKER" rm -f "$id" >/dev/null; then
      warn "docker rm -f failed for id=$short"
      rc=$(lifecycle_merge_rc "$rc" 1)
      continue
    fi
    if ! container_id_absent_local "$id"; then
      warn "container id=$short still present or unverifiable after docker rm -f"
      rc=$(lifecycle_merge_rc "$rc" 1)
    fi
  done
  return "$rc"
}

remove_all_stack_managed_remote() {
  local host="${1:?}" placement="${2:-worker}"
  local ids id meta conf rank reason rc=0 short remote_rm probe
  ids=$(list_managed_container_ids_remote "$host") || {
    warn "remote host unreachable or Docker error listing managed containers on $host"
    return 1
  }
  if [ -z "${ids//[$' \t\r\n']/}" ]; then
    log "no stack-managed containers on $host"
    return 0
  fi
  for id in $ids; do
    [ -n "$id" ] || continue
    probe=0
    meta=$(container_ownership_inspect_remote "$host" "$id") || probe=$?
    if [ "$probe" -eq 3 ]; then
      continue
    fi
    if [ "$probe" -ne 0 ]; then
      warn "worker error inspecting managed id=${id:0:12} on $host"
      rc=$(lifecycle_merge_rc "$rc" 1)
      continue
    fi
    IFS=$'\t' read -r _ _ _ conf rank < <(container_ownership_fields "$meta")
    if ! container_all_candidate_is_safe "$meta" "$placement"; then
      reason=$(container_all_refuse_reason "$meta" "$placement")
      warn "refusing managed candidate id=${id:0:12} on $host: $reason"
      rc=$(lifecycle_merge_rc "$rc" 2)
      continue
    fi
    short="${id:0:12}"
    probe=0
    meta=$(container_ownership_inspect_remote "$host" "$id") || probe=$?
    if [ "$probe" -eq 3 ]; then
      continue
    fi
    if [ "$probe" -ne 0 ]; then
      warn "worker error revalidating id=$short on $host"
      rc=$(lifecycle_merge_rc "$rc" 1)
      continue
    fi
    if ! container_all_candidate_is_safe "$meta" "$placement"; then
      reason=$(container_all_refuse_reason "$meta" "$placement")
      warn "refusing id=$short on $host after revalidation: $reason"
      rc=$(lifecycle_merge_rc "$rc" 2)
      continue
    fi
    log "removing stack-managed on $host id=$short conf=$conf rank=$rank"
    remote_rm="docker rm -f $(printf '%q' "$id") >/dev/null"
    if ! "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$remote_rm"; then
      warn "docker rm -f failed for id=$short on $host"
      rc=$(lifecycle_merge_rc "$rc" 1)
      continue
    fi
    if ! container_id_absent_remote "$host" "$id"; then
      warn "container id=$short still present or unverifiable on $host after docker rm -f"
      rc=$(lifecycle_merge_rc "$rc" 1)
    fi
  done
  return "$rc"
}

# Two-node stale/replacement gate: prove ownership on every existing rank, then
# remove proven IDs. Worker probe errors are operational failures (never treated
# as absence). Refuse entirely (no partial remove) if any existing rank is
# unowned/mismatched. After each rm, verify the immutable ID is absent.
# Exit 0 removed/absent OK; 2 refuse; 1 error.
remove_stack_owned_cluster_pair() {
  local conf="${1:?}" cname="${2:?}" worker_ip="${3:?}"
  local head_meta worker_meta head_id="" worker_id="" reason
  local head_rc=0 worker_rc=0 refuse=0

  # Probe worker first so SSH/Docker failure never looks like "worker absent"
  # and never allows a partial head-only removal.
  worker_meta=$(container_ownership_inspect_remote "$worker_ip" "$cname") || worker_rc=$?
  if [ "$worker_rc" -eq 1 ]; then
    warn "remote host unreachable or Docker error inspecting $cname on $worker_ip — not removing any rank"
    return 1
  fi

  head_meta=$(container_ownership_inspect_local "$cname") || head_rc=$?
  if [ "$head_rc" -eq 1 ]; then
    warn "local docker error inspecting $cname — not removing any rank"
    return 1
  fi

  if [ "$head_rc" -eq 0 ]; then
    if container_ownership_is_proven "$head_meta" "$conf" "0"; then
      IFS=$'\t' read -r head_id _ < <(container_ownership_fields "$head_meta")
    else
      reason=$(container_ownership_refuse_reason "$head_meta" "$conf" "0")
      warn "head rank ownership not proven for $cname: $reason"
      refuse=1
    fi
  fi

  if [ "$worker_rc" -eq 0 ]; then
    if container_ownership_is_proven "$worker_meta" "$conf" "1"; then
      IFS=$'\t' read -r worker_id _ < <(container_ownership_fields "$worker_meta")
    else
      reason=$(container_ownership_refuse_reason "$worker_meta" "$conf" "1")
      warn "worker rank ownership not proven for $cname on $worker_ip: $reason"
      refuse=1
    fi
  fi

  if [ "$refuse" = 1 ]; then
    warn "refusing cluster replacement for conf=$conf: prove ownership on every existing rank (no partial remove)"
    return 2
  fi

  if [ "$head_rc" -eq 3 ] && [ "$worker_rc" -eq 3 ]; then
    log "no existing cluster containers named $cname"
    return 0
  fi

  # Revalidate both IDs before any mutation. Failure ⇒ refuse (no partial).
  if [ -n "$head_id" ]; then
    head_rc=0
    head_meta=$(container_ownership_inspect_local "$head_id") || head_rc=$?
    if [ "$head_rc" -eq 1 ]; then
      warn "local docker error revalidating head id for $cname"
      return 1
    fi
    if [ "$head_rc" -ne 0 ] || ! container_ownership_is_proven "$head_meta" "$conf" "0"; then
      warn "refusing: head id revalidation failed for $cname"
      return 2
    fi
  fi
  if [ -n "$worker_id" ]; then
    worker_rc=0
    worker_meta=$(container_ownership_inspect_remote "$worker_ip" "$worker_id") || worker_rc=$?
    if [ "$worker_rc" -eq 1 ]; then
      warn "worker error revalidating worker id for $cname — not removing any rank"
      return 1
    fi
    if [ "$worker_rc" -ne 0 ] || ! container_ownership_is_proven "$worker_meta" "$conf" "1"; then
      warn "refusing: worker id revalidation failed for $cname"
      return 2
    fi
  fi

  if [ -n "$worker_id" ]; then
    log "removing worker $cname id=${worker_id:0:12}"
    if ! "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$worker_ip" \
      "docker rm -f $(printf '%q' "$worker_id") >/dev/null"; then
      warn "failed to remove worker id=${worker_id:0:12}"
      return 1
    fi
    if ! container_id_absent_remote "$worker_ip" "$worker_id"; then
      warn "worker id=${worker_id:0:12} still present or unverifiable after rm — not removing head"
      return 1
    fi
  fi
  if [ -n "$head_id" ]; then
    log "removing head $cname id=${head_id:0:12}"
    if ! "$PULSAR_DOCKER" rm -f "$head_id" >/dev/null; then
      warn "failed to remove head id=${head_id:0:12}"
      return 1
    fi
    if ! container_id_absent_local "$head_id"; then
      warn "head id=${head_id:0:12} still present or unverifiable after rm"
      return 1
    fi
  fi
  return 0
}

# N-rank stale/replacement transaction. Probe and prove every existing rank,
# revalidate every immutable ID, then remove workers high-to-low and rank 0
# last. No mutation occurs after an unreachable/ambiguous initial probe.
# Exit 0 removed/absent; 1 operational error; 2 ownership/race refusal.
remove_stack_owned_cluster() {
  local conf="${1:?conf required}" cname="${2:?container required}"
  local nodes="${3:?node count required}"
  [ "$nodes" -gt 1 ] 2>/dev/null || {
    warn "remove_stack_owned_cluster requires NODES>1"
    return 2
  }
  require_cluster_nodes "$nodes" || return 1

  local -A ids=()
  local rank host meta rc reason present=0 container_id

  # Remote probes first: an unavailable rank must never permit head mutation.
  for ((rank = 1; rank < nodes; rank++)); do
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    rc=0
    meta=$(container_ownership_inspect_remote "$host" "$cname") || rc=$?
    if [ "$rc" -eq 1 ]; then
      warn "rank $rank unreachable or Docker error on $host — not removing any rank"
      return 1
    fi
    if [ "$rc" -eq 0 ]; then
      if ! container_removal_is_proven "$meta" "$conf" "$rank" "$rank"; then
        reason=$(container_ownership_refuse_reason "$meta" "$conf" "$rank")
        warn "rank $rank ownership not proven for $cname on $host: $reason"
        return 2
      fi
      IFS=$'\t' read -r container_id _ < <(container_ownership_fields "$meta")
      ids["$rank"]="$container_id"
      present=$((present + 1))
    fi
  done

  rc=0
  meta=$(container_ownership_inspect_local "$cname") || rc=$?
  if [ "$rc" -eq 1 ]; then
    warn "local Docker error inspecting $cname — not removing any rank"
    return 1
  fi
  if [ "$rc" -eq 0 ]; then
    if ! container_removal_is_proven "$meta" "$conf" 0 0; then
      reason=$(container_ownership_refuse_reason "$meta" "$conf" 0)
      warn "rank 0 ownership not proven for $cname: $reason"
      return 2
    fi
    IFS=$'\t' read -r container_id _ < <(container_ownership_fields "$meta")
    ids[0]="$container_id"
    present=$((present + 1))
  fi

  if [ "$present" -eq 0 ]; then
    log "no existing cluster containers named $cname across $nodes ranks"
    return 0
  fi

  # Revalidate every ID before the first rm. Any disappearance/replacement is a
  # race and therefore an ownership refusal, not permission to continue.
  for ((rank = 1; rank < nodes; rank++)); do
    [ -n "${ids[$rank]:-}" ] || continue
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    rc=0
    meta=$(container_ownership_inspect_remote "$host" "${ids[$rank]}") || rc=$?
    if [ "$rc" -eq 1 ]; then
      warn "rank $rank error revalidating $cname on $host"
      return 1
    fi
    if [ "$rc" -ne 0 ] \
        || ! container_removal_is_proven "$meta" "$conf" "$rank" "$rank"; then
      warn "rank $rank ownership changed while revalidating $cname"
      return 2
    fi
  done
  if [ -n "${ids[0]:-}" ]; then
    rc=0
    meta=$(container_ownership_inspect_local "${ids[0]}") || rc=$?
    if [ "$rc" -eq 1 ]; then
      warn "local Docker error revalidating rank 0 for $cname"
      return 1
    fi
    if [ "$rc" -ne 0 ] || ! container_removal_is_proven "$meta" "$conf" 0 0; then
      warn "rank 0 ownership changed while revalidating $cname"
      return 2
    fi
  fi

  for ((rank = nodes - 1; rank >= 1; rank--)); do
    [ -n "${ids[$rank]:-}" ] || continue
    host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
    log "removing rank $rank $cname id=${ids[$rank]:0:12} on $host"
    if ! "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
      "docker rm -f $(printf '%q' "${ids[$rank]}") >/dev/null"; then
      warn "failed to remove rank $rank id=${ids[$rank]:0:12}"
      return 1
    fi
    if ! container_id_absent_remote "$host" "${ids[$rank]}"; then
      warn "rank $rank id=${ids[$rank]:0:12} remains after rm; rank 0 left intact"
      return 1
    fi
  done

  if [ -n "${ids[0]:-}" ]; then
    log "removing rank 0 $cname id=${ids[0]:0:12}"
    if ! "$PULSAR_DOCKER" rm -f "${ids[0]}" >/dev/null; then
      warn "failed to remove rank 0 id=${ids[0]:0:12}"
      return 1
    fi
    if ! container_id_absent_local "${ids[0]}"; then
      warn "rank 0 id=${ids[0]:0:12} remains after rm"
      return 1
    fi
  fi
  return 0
}

# Append VLLM_EXTRA_ARGS onto a named bash array using shlex (handles
# spaces/quotes). Bare word-split of VLLM_EXTRA_ARGS is intentionally avoided.
# Usage: append_vllm_extra_args DEST_ARRAY_NAME
#   VLLM_EXTRA_ARGS='--foo "bar baz"' append_vllm_extra_args CMD
append_vllm_extra_args() {
  local dest="${1:?append_vllm_extra_args: array name required}"
  [ -n "${VLLM_EXTRA_ARGS:-}" ] || return 0
  local line
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    eval "$dest+=(\"\$line\")"
  done < <(VLLM_EXTRA_ARGS="$VLLM_EXTRA_ARGS" python3 - <<'PY'
import os, shlex
for a in shlex.split(os.environ.get("VLLM_EXTRA_ARGS", "")):
    print(a)
PY
)
}

# Exact container name match (Docker --filter name= is substring — unsafe for
# conf prefixes like qwen3-1.7b vs qwen3-1.7b-2node).
# Usage: echo "$names_one_per_line" | filter_exact_container_name "$want"
filter_exact_container_name() {
  local want="$1"
  local n
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    if [ "$n" = "$want" ]; then
      printf '%s\n' "$n"
    fi
  done
}

# True if a running container has this exact name (local docker).
container_running_exact() {
  local want="$1"
  "$PULSAR_DOCKER" ps --format '{{.Names}}' 2>/dev/null \
    | filter_exact_container_name "$want" | grep -q .
}
container_running_exact_remote() {
  local host="${1:?}" want="${2:?}"
  "$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" \
    "docker ps --format '{{.Names}}'" 2>/dev/null \
    | filter_exact_container_name "$want" | grep -q .
}


# Validate targeted docker-inspect metadata without exposing container env.
# Labeled containers are authoritative. Unlabeled containers are accepted only
# as a transition path when their exact vLLM argv matches the selected profile.
container_metadata_matches_profile() {
  local metadata="$1" conf="$2" model="$3" served="$4" rank="${5:-}"
  printf '%s' "$metadata" | python3 -c '
import json
import sys

conf, model, served, rank, managed_key, conf_key, rank_key = sys.argv[1:]
meta = json.load(sys.stdin)
if not meta.get("running"):
    raise SystemExit(1)

labels = meta.get("labels") or {}
if str(labels.get(managed_key, "")).lower() == "true":
    ok = labels.get(conf_key) == conf
    if rank:
        ok = ok and labels.get(rank_key) == rank
    raise SystemExit(0 if ok else 1)

cmd = meta.get("cmd") or []
def value(flag):
    try:
        return cmd[cmd.index(flag) + 1]
    except (ValueError, IndexError):
        return None

ok = value("--model") == model and value("--served-model-name") == served
if rank:
    ok = ok and value("--node-rank") == rank
raise SystemExit(0 if ok else 1)
' "$conf" "$model" "$served" "$rank" \
    "$PULSAR_MANAGED_LABEL" "$PULSAR_CONF_LABEL" "$PULSAR_RANK_LABEL"
}

container_profile_owned_local() {
  local name="$1" conf="$2" model="$3" served="$4" rank="${5:-}"
  local format metadata
  format='{"running":{{json .State.Running}},"labels":{{json .Config.Labels}},"cmd":{{json .Config.Cmd}}}'
  metadata=$(docker inspect --format "$format" "$name" 2>/dev/null) || return 1
  container_metadata_matches_profile "$metadata" "$conf" "$model" "$served" "$rank"
}

container_profile_owned_remote() {
  local host="$1" name="$2" conf="$3" model="$4" served="$5" rank="${6:-}"
  local format metadata remote_cmd
  format='{"running":{{json .State.Running}},"labels":{{json .Config.Labels}},"cmd":{{json .Config.Cmd}}}'
  remote_cmd="docker inspect --format $(printf '%q' "$format") $(printf '%q' "$name")"
  metadata=$("$PULSAR_SSH" "${PULSAR_SSH_OPTS[@]}" -- "$host" "$remote_cmd" 2>/dev/null) \
    || return 1
  container_metadata_matches_profile "$metadata" "$conf" "$model" "$served" "$rank"
}

container_profile_owned_worker() {
  local name="$1" conf="$2" model="$3" served="$4" rank="${5:-}"
  require_cluster_nodes 2 || return 1
  container_profile_owned_remote "${CLUSTER_NODE_SSH_HOSTS[1]}" \
    "$name" "$conf" "$model" "$served" "$rank"
}

# True only when every active rank of the selected exact profile is owned.
profile_service_is_stack_owned() {
  local conf="$1" selector="${2:-}" cname rank host
  cname=$(container_name_for "$conf" "$NODES")
  if [ "$NODES" -gt 1 ]; then
    require_cluster_nodes "$NODES" || return 1
    container_profile_owned_local "$cname" "$conf" "$MODEL" "$SERVED_NAME" 0 \
      || return 1
    for ((rank = 1; rank < NODES; rank++)); do
      host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
      container_profile_owned_remote "$host" "$cname" "$conf" \
        "$MODEL" "$SERVED_NAME" "$rank" || return 1
    done
    return 0
  fi
  profile_service_is_proven_running "$conf" "$selector"
}

# Strict loaded-state proof for memory exemptions. Unlike the transition
# classifier above, this accepts labels only; argv resemblance is insufficient.
profile_service_is_proven_running() {
  local conf="$1" selector="${2:-}" cname head_meta remote_meta host role
  local head_rc=0 remote_rc=0 rank
  cname=$(container_name_for "$conf" "$NODES")

  if [ "$NODES" -gt 1 ]; then
    container_running_exact "$cname" || return 1
    head_meta=$(container_ownership_inspect_local "$cname") || head_rc=$?
    [ "$head_rc" -eq 0 ] || return 1
    container_ownership_is_proven "$head_meta" "$conf" 0 || return 1
    require_cluster_nodes "$NODES" || return 1
    for ((rank = 1; rank < NODES; rank++)); do
      host="${CLUSTER_NODE_SSH_HOSTS[$rank]}"
      container_running_exact_remote "$host" "$cname" || return 1
      remote_rc=0
      remote_meta=$(container_ownership_inspect_remote "$host" "$cname") \
        || remote_rc=$?
      [ "$remote_rc" -eq 0 ] || return 1
      container_ownership_is_proven "$remote_meta" "$conf" "$rank" || return 1
    done
    return 0
  fi

  resolve_single_node_placement "$selector" || return 1
  role="$SINGLE_NODE_KEY"
  if [ "$SINGLE_NODE_REMOTE" = 1 ]; then
    container_running_exact_remote "$SINGLE_NODE_SSH_HOST" "$cname" || return 1
    remote_rc=0
    remote_meta=$(container_ownership_inspect_remote "$SINGLE_NODE_SSH_HOST" "$cname") \
      || remote_rc=$?
    [ "$remote_rc" -eq 0 ] || return 1
    container_removal_is_proven "$remote_meta" "$conf" single "$role"
  else
    container_running_exact "$cname" || return 1
    head_meta=$(container_ownership_inspect_local "$cname") || head_rc=$?
    [ "$head_rc" -eq 0 ] || return 1
    container_removal_is_proven "$head_meta" "$conf" single "$role"
  fi
}

container_exists_exact() {
  local want="$1"
  docker ps -a --format '{{.Names}}' 2>/dev/null | filter_exact_container_name "$want" | grep -q .
}

require_launch_operational_checks() {
  local rc=0
  local -a placement=() memory_args=(--cold-start)
  if [ "$NODES" = 1 ] && [ -n "${SINGLE_NODE_ID:-}" ]; then placement=(--node "$SINGLE_NODE_ID"); fi
  if [ -n "${PULSAR_MEMORY_ESTIMATE_JSON:-}" ]; then
    memory_args+=(--memory-estimate-frozen "$PULSAR_MEMORY_ESTIMATE_JSON")
  fi
  "$REPO_DIR/scripts/check-image.sh" "$CONF_NAME" "${placement[@]}" || die "image verification failed before launch"
  "$REPO_DIR/scripts/check-memory.sh" "$CONF_NAME" "${placement[@]}" "${memory_args[@]}" || rc=$?
  case "$rc" in
    0) ;;
    2) [ "${PULSAR_ACCEPT_MEMORY_WARN:-0}" = 1 ] || die "memory warning requires --accept-memory-warn" ;;
    *) die "memory verification failed before launch" ;;
  esac
  resolve_library_hot_for_profile "$CONF_NAME"
}
