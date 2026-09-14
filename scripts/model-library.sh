#!/usr/bin/env bash
# Manifest-based storage boundary. Python owns data; shared Bash owns topology/SSH.
set -euo pipefail
SCRIPT_NAME=model-library
ORIGINAL_ARGS=("$@")
_MODEL_ARCHIVE_PROCESS_DEFINED=0
_MODEL_ARCHIVE_PROCESS_VALUE=""
if [ -n "${PULSAR_COLD_ROOT+x}" ]; then
  _MODEL_ARCHIVE_PROCESS_DEFINED=1
  _MODEL_ARCHIVE_PROCESS_VALUE="$PULSAR_COLD_ROOT"
fi
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
. "$REPO_DIR/scripts/acquire-source.sh"

usage() {
  cat <<'HELP' | python3 -c 'import sys; from scripts.terminal_format import TerminalWriter; out=TerminalWriter(); [out.emit(line.rstrip(),subsequent_indent="    " if line.startswith("  ") else "") for line in sys.stdin]'
Usage: model-library.sh OPERATION [SPEC_ID] [options]

  acquire       Download an exact snapshot or reuse verified files
  prepare       Prepare verified files on the exact serving nodes
  info          Inspect the selected prepared set
  check         Check known locations and save a catalog observation
  move          Move a home to an explicitly selected node
  restore       Restore the selected snapshot from its verified archive
  archive create|verify   Save or verify a recovery archive
  pin|unpin     Protect or release prepared copies
  purge         Remove unpinned, unused prepared copies
  remove        Remove an unused home, preserving catalog recovery
  budget        Inspect per-node managed storage usage

Options:
  --spec-file FILE       Explicit lab candidate spec
  --snapshot NAME       Select one declared snapshot for home/archive operations
  --node NODE_ID         Confirmed destination or one-node placement
  --manifest FILE       Retained source manifest for lab storage
  --model-id ORG/NAME --model-commit COMMIT   First acquisition before a spec
  --manifest-out FILE   Save the resulting verified source manifest
  --plan                Preview; do not change model files or records
  --yes                 Confirm a model-byte or retention mutation
  --discard-unpromoted   Acknowledge loss of unarchived lab-only bytes
  --json                Machine-readable output
  --full                Full SHA-256 verification (info/check)

Archive location is explicit PULSAR_COLD_ROOT. Configure an existing
location with ./pulsar configure archive-root. No archive deletion exists.
HELP
}

OP="${1:-help}"; [ $# -eq 0 ] || shift
ARCHIVE_ACTION=""
if [ "$OP" = archive ]; then ARCHIVE_ACTION="${1:-}"; [ $# -eq 0 ] || shift; fi
SPEC_ID="" SPEC_FILE="${PULSAR_SPEC_FILE:-}" MANIFEST_FILE="" MODEL_ID="" REVISION=""
SNAPSHOT="" SNAPSHOTS_JSON="" VIEW_SCHEMA=1
NODE="" YES=0 PLAN=0 JSON=0 FULL=0 DISCARD=0 MANIFEST_OUT=""
PREPARE_VERIFICATION_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --snapshot|--spec-file|--manifest|--model-id|--model-commit|--revision|--node|--manifest-out)
      [ $# -ge 2 ] || die "$1 needs a value"
      case "$1" in
        --spec-file) SPEC_FILE="$2" ;;
        --snapshot) SNAPSHOT="$2" ;;
        --manifest) MANIFEST_FILE="$2" ;;
        --model-id) MODEL_ID="$2" ;;
        --model-commit|--revision) REVISION="$2" ;;
        --node) NODE="$2" ;;
        --manifest-out) MANIFEST_OUT="$2" ;;
      esac; shift ;;
    --yes) YES=1 ;;
    --plan) PLAN=1 ;;
    --json) JSON=1 ;;
    --full) FULL=1 ;;
    --for-launch) ;;
    --discard-unpromoted) DISCARD=1 ;;
    --backend) [ "${2:-}" = copy ] || die "only local-file preparation is supported"; shift ;;
    --transport) [ "${2:-}" = ssh-roce ] || die "bulk copies require confirmed ssh-roce"; shift ;;
    --copy-streams) [ "${2:-}" = 8 ] || die "bulk copies use eight streams"; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) die "unknown option: $1" ;;
    *) [ -z "$SPEC_ID" ] || die "unexpected argument: $1"; SPEC_ID="$1" ;;
  esac
  shift
done
case "$OP" in help|-h|--help) usage; exit 0 ;; esac
[ "$YES" -eq 0 ] || [ "$PLAN" -eq 0 ] || die "--plan and --yes are separate operations"
case "$OP" in acquire|prepare|info|check|move|restore|archive|pin|unpin|purge|remove|budget) ;; *) die "unknown operation: $OP" ;; esac
if [ "$OP" = archive ]; then case "$ARCHIVE_ACTION" in create|verify) ;; *) die "archive requires create or verify" ;; esac; fi
MANIFEST_JSON="" SPEC_JSON="" MANIFEST_ID="" HOME_JSON="null"

. "$REPO_DIR/scripts/model-library-context.sh"
. "$REPO_DIR/scripts/model-library-acquisition.sh"
. "$REPO_DIR/scripts/model-library-recovery.sh"
. "$REPO_DIR/scripts/model-library-preparation.sh"
. "$REPO_DIR/scripts/model-library-retention.sh"
. "$REPO_DIR/scripts/model-library-observations.sh"

# Hold the chosen archive root fixed for the entire operation. On re-entry
# lib.sh preserves the frozen process value even if .env contains another value.
case "$OP" in archive|restore|remove|check)
  if [ "${PULSAR_ARCHIVE_CONFIG_LOCKED:-0}" != 1 ]; then
    archive_lock_options=()
    case "$OP" in check|remove) archive_lock_options+=(--allow-unconfigured) ;; esac
    if [ "$_MODEL_ARCHIVE_PROCESS_DEFINED" = 1 ]; then
      exec env PULSAR_COLD_ROOT="$_MODEL_ARCHIVE_PROCESS_VALUE" python3 -m model_library.configuration --repo-root "$REPO_DIR" run "${archive_lock_options[@]}" -- "$REPO_DIR/scripts/model-library.sh" "${ORIGINAL_ARGS[@]}"
    else
      exec env -u PULSAR_COLD_ROOT python3 -m model_library.configuration --repo-root "$REPO_DIR" run "${archive_lock_options[@]}" -- "$REPO_DIR/scripts/model-library.sh" "${ORIGINAL_ARGS[@]}"
    fi
  fi
  ;;
esac

if [ "$OP" != budget ]; then
  if [ "$OP" != acquire ] || [ -n "$SPEC_ID$SPEC_FILE$MANIFEST_FILE" ]; then
    resolve_selected || die "selected spec or manifest is invalid"
  fi
fi
if [ -n "$SNAPSHOT" ]; then
  case "$OP:$ARCHIVE_ACTION" in acquire:|restore:|move:|remove:|archive:*) ;; *) die "$OP always covers the complete recipe; omit --snapshot" ;; esac
elif [ "$VIEW_SCHEMA" = 2 ]; then
  case "$OP:$ARCHIVE_ACTION" in acquire:|restore:|move:|remove:|archive:create) die "$OP requires --snapshot NAME for a schema-3 recipe" ;; esac
fi
case "$OP" in
  prepare|info|check|pin|unpin|purge) [ -n "$SPEC_ID" ] || die "$OP requires a spec id" ;;
esac
# Preview commands and archive verification never write model-library records.
if [ "$PLAN" -eq 0 ]; then
  case "$OP:$ARCHIVE_ACTION" in
    info:|budget:|archive:verify) acquire_model_library_lifecycle_lock shared ;;
    *) acquire_model_library_lifecycle_lock exclusive ;;
  esac
fi
case "$OP" in
  acquire) acquire_model ;;
  prepare) prepare_model ;;
  info) result=$(prepared_info) || exit $?; emit_result "$result" ;;
  check) check_model ;;
  archive)
    if [ "$ARCHIVE_ACTION" = verify ]; then
      result=$(archive_verify) || die "archive verification failed"
      emit_result "$result"
    else archive_create; fi ;;

  restore) restore_model ;;
  pin|unpin|purge) retention_model ;;
  move) move_home ;;
  remove) remove_home ;;
  budget) show_budget ;;
esac
