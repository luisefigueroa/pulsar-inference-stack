#!/usr/bin/env bash
# One complete prepared-set batch. Bash owns topology and transport; only the
# parent refreshes records, after the supplied verification workers are reaped.

inspect_prepared() {
  local work="$1" index slot request original candidate completed result outcome rc=0 batch_rc=0
  local -a MODEL_NODE_COMMAND=()
  selected_nodes || return 2
  model_ctl "$(model_json operation inspection-plan spec: "$SPEC_JSON" node_ids: "$NODE_IDS_JSON" topology_id "$CLUSTER_TOPOLOGY_ID" full: "$([ "$FULL" = 1 ] && echo true || echo false)" cache "${PREPARE_VERIFICATION_DIR:-}")" >"$work/plan.json" || return 2
  python3 -m model_library.inspection jobs "$work" >"$work/jobs.tsv" || return $?
  while IFS=$'\t' read -r index slot <&3; do
    request=$(cat "$work/jobs/$index.request.json") || return 2
    model_node_command "${SELECTED_RANKS[$slot]}" || return $?
    printf '%s' "$request" | python3 "$REPO_DIR/scripts/node-bundle.py" --supervised >"$work/jobs/$index.program" || return 2
    python3 - "$work" "$index" "$slot" python3 -m model_library.verification_process --owner "$$" -- "${MODEL_NODE_COMMAND[@]}" <<'PY' || return 2
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); index=int(sys.argv[2])
(root/'jobs'/f'{index}.task.json').write_text(json.dumps({
    'index':index,'node_slot':int(sys.argv[3]),'program':str(root/'jobs'/f'{index}.program'),
    'command':sys.argv[4:]}))
PY
  done 3<"$work/jobs.tsv"
  python3 - "$work" <<'PY' || return 2
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);plan=json.loads((root/'plan.json').read_text())
(root/'tasks.json').write_text(json.dumps([json.loads((root/'jobs'/f'{j["index"]}.task.json').read_text()) for j in plan['jobs']]))
PY
  python3 -m model_library.verification_process batch --tasks "$work/tasks.json" --directory "$work" --jobs "$VERIFICATION_JOBS" || batch_rc=$?
  [ -f "$work/batch.json" ] || return 2
  outcome=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["outcome"])' "$work/batch.json") || return 2
  # Only cancellation of the caller skips record updates and assembly. A
  # worker may independently exit with the same signal-style status codes.
  case "$outcome" in
    cancelled) return "$batch_rc" ;;
    complete|failed) ;;
    *) return 2 ;;
  esac
  python3 - "$work/batch.json" >"$work/succeeded.tsv" <<'PY' || return 2
import json,sys
for row in json.load(open(sys.argv[1]))['results']:
    if row['returncode']==0: print(str(row['index'])+'\t'+row['verified_at'])
PY
  while IFS=$'\t' read -r index completed <&3; do
    original=$(cat "$work/jobs/$index.record.json") || return 2
    candidate=$(cat "$work/jobs/$index.candidate.json") || return 2
    result=$(cat "$work/jobs/$index.out") || return 2
    rc=0
    refresh_verified_record "$original" "$candidate" "$result" "$completed" >"$work/jobs/$index.verified.json" || rc=$?
    if [ "$rc" -ne 0 ]; then
      python3 - "$work" "$index" "$rc" <<'PY' || return 2
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); index=int(sys.argv[2]);path=root/'batch.json';value=json.loads(path.read_text())
row=value['results'][index];row.update(returncode=int(sys.argv[3]),error='could not refresh verified record')
if value['first_error'] is None: value['first_error']=index
path.write_text(json.dumps(value))
PY
    fi
  done 3<"$work/succeeded.tsv"
  python3 -m model_library.inspection assemble "$work"
}
