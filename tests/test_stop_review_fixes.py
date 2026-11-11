"""Stop under its lock, stop --all beside records of an earlier saved cluster,
and what Stop says it did, on a saved four-node ring with the doubles of
tests/support/service_cluster.py. Nothing here contacts a node."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.service_cluster import ServiceCluster  # noqa: E402

# Saves the plan SERVICE_SAVE_ON_LOCK names just before Stop takes the
# exclusive lifecycle lock: a Start that recorded it in between.
LOCK_HOOK = r'''
. "$SERVICE_REPO/scripts/lib.sh"
eval "_service_lock() $(declare -f acquire_model_library_lifecycle_lock | tail -n +2)"
acquire_model_library_lifecycle_lock() {
  if [ "${1:-}" = exclusive ] && [ -n "${SERVICE_SAVE_ON_LOCK:-}" ]; then
    python3 "$SERVICE_REPO/scripts/service_state.py" save --state-root "$PULSAR_MODEL_LIBRARY_DIR" \
      --plan "$SERVICE_SAVE_ON_LOCK"
    unset SERVICE_SAVE_ON_LOCK
  fi
  _service_lock "$@"
}
'''


class Cluster(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))


class StopReadsTheRecordsUnderItsLock(Cluster):
    def test_a_record_saved_between_the_two_reads_is_stopped_on_its_nodes(self):
        import json
        c = self.cluster
        spec, manifest = c.spec(2)
        plan = c.plan(spec, manifest, [2, 3], record=False)
        c.run(plan)
        path = c.root / 'late-plan.json'
        path.write_text(json.dumps(plan))
        hook = c.root / 'lock-hook.sh'
        hook.write_text(LOCK_HOOK)
        result = c.pulsar('stop', spec['spec_id'], BASH_ENV=str(hook), SERVICE_REPO=str(ROOT),
                          SERVICE_SAVE_ON_LOCK=str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Stopped spec {spec['spec_id'][:12]} on {c.hostname(2)} (rank 0), {c.hostname(3)} (rank 1): "
                      'removed 2 container(s) and retired its service record.', result.stdout)
        self.assertEqual(c.remaining(), [])
        self.assertFalse(c.recorded(plan))


if __name__ == '__main__':
    unittest.main()
