"""Stop under its lock, stop --all beside records of an earlier saved cluster,
and what Stop says it did, on a saved four-node ring with the doubles of
tests/support/service_cluster.py. Nothing here contacts a node."""
import json
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


def lines(result):
    return (result.stdout + result.stderr).splitlines()


class StopAllBesideRecordsOfAnEarlierCluster(Cluster):
    """stop --all is never refused or cut short by a record of an earlier saved
    cluster. Its sweep is pinned from 8c0c673, the commit before named nodes,
    run on these doubles: a service of this cluster on the default order is
    removed and a container labelled for the earlier cluster is refused, with
    the lines below, which now name the other nodes by hostname. Only the
    record's own pass is new."""

    def setUp(self):
        super().setUp()
        c = self.cluster
        spec, manifest = c.spec(2)
        self.running = c.run(c.plan(spec, manifest, [0, 1]))
        self.sweep = ['removing stack-managed containers across 4 confirmed ranks',
                      'no stack-managed containers on fixture-ring-alpha',
                      'no stack-managed containers on fixture-ring-echo',
                      f"removing stack-managed on fixture-ring-bravo id={self.running[1]['id'][:12]} spec "
                      f"{spec['spec_id'][:12]} rank=1",
                      f"removing stack-managed id={self.running[0]['id'][:12]} spec {spec['spec_id'][:12]} rank=0 "
                      'on fixture-ring-dorado']

    def assert_swept(self, result, *, refused=None):
        for line in self.sweep:
            self.assertIn(line, lines(result))
        if refused:
            # As 8c0c673 refused it. The record's pass named it already, so
            # the sweep says it once more only in its closing line.
            self.assertIn(refused, lines(result))
            self.assertIn('warning: the containers left from an earlier saved cluster named above were not removed',
                          lines(result))
            self.assertNotIn('left from an earlier saved cluster. Pulsar', result.stderr)
        else:
            self.assertIn('clean — no stack-managed containers remain', lines(result))

    def test_records_with_no_container_left_are_retired_and_the_sweep_runs(self):
        c = self.cluster
        guarded, guarded_manifest = c.spec(2, 1, catalog=False, guarded=True)
        ordinary, ordinary_manifest = c.spec(2, 2)
        records = [c.plan(guarded, guarded_manifest, [2, 3], topology_id='e' * 64),
                   c.plan(ordinary, ordinary_manifest, [0, 1], topology_id='e' * 64)]
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_swept(result)
        text = ' '.join(result.stdout.split())
        self.assertIn(f"Guarded trial {records[0]['guard_run_id'][:12]} of spec {guarded['spec_id'][:12]} was "
                      'recorded under an earlier saved cluster and has no container on any node of this one; retired '
                      "its stale service record. The trial's run directory keeps its own result.", text)
        self.assertIn(f"Spec {ordinary['spec_id'][:12]}'s service was recorded under an earlier saved cluster and has "
                      'no container on any node of this one; retired its stale service record.', text)
        self.assertEqual(c.store.records('services'), [])
        self.assertEqual(c.remaining(), [])

    def test_a_container_still_there_is_reported_and_the_sweep_judges_it_as_before(self):
        c = self.cluster
        guarded, guarded_manifest = c.spec(2, 1, catalog=False, guarded=True)
        ordinary, ordinary_manifest = c.spec(2, 2)
        for spec, manifest, refusal in (
                (ordinary, ordinary_manifest, 'ownership not proven'),
                (guarded, guarded_manifest, f"ownership not proven for retired spec {guarded['spec_id'][:12]}")):
            with self.subTest(guarded=spec is guarded):
                if not c.remaining():
                    self.running = c.run(c.plan(*c.spec(2), [0, 1]))
                    self.sweep[3:] = [
                        f"removing stack-managed on fixture-ring-bravo id={self.running[1]['id'][:12]} spec "
                        f"{self.running[1]['labels']['io.pulsar.gb10.conf'][:12]} rank=1",
                        f"removing stack-managed id={self.running[0]['id'][:12]} spec "
                        f"{self.running[0]['labels']['io.pulsar.gb10.conf'][:12]} rank=0 on fixture-ring-dorado"]
                plan = c.plan(spec, manifest, [0, 1], topology_id='e' * 64)
                (left,) = c.run(plan, ranks=[1])
                result = c.pulsar('stop', '--all', '--yes')
                self.assertEqual(result.returncode, 1)
                self.assert_swept(result, refused=f"warning: refusing managed candidate id={left['id'][:12]} on "
                                                  f'fixture-ring-bravo: {refusal}')
                self.assertIn(f"container {left['id'][:12]} on {c.hostname(1)} is still there. Pulsar does not "
                              'remove containers of an earlier saved cluster, so nothing was removed and the record '
                              f"is kept. Next: remove that container with Docker on its node (docker rm -f "
                              f"{left['id'][:12]}), then rerun ./pulsar stop --all --yes", result.stderr)
                self.assertTrue(c.recorded(plan))
                self.assertEqual([item['id'] for item in c.remaining()], [left['id']])
                c.store.remove('services', plan['service_id'])
                c.containers = []
                c.write()

    def test_a_guarded_trial_of_this_cluster_off_the_default_order_is_still_refused(self):
        c = self.cluster
        guarded, manifest = c.spec(2, 1, catalog=False, guarded=True)
        trial = c.plan(guarded, manifest, [2, 3])
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"stop --all does not stop guarded trial {trial['guard_run_id'][:12]}", result.stderr)
        self.assertEqual(c.removed(), [])


class StopWithNodeBesideARecordOfAnEarlierCluster(Cluster):
    """stop SPEC --node for a spec whose only record is of an earlier saved
    cluster clears or reports that record, then runs the label stop with
    --node, which is pinned from 8c0c673 on these doubles."""

    def test_no_container_left(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        for positions in ([0, 1], [2, 3]):
            with self.subTest(positions=positions):
                plan = c.plan(spec, manifest, positions, topology_id='e' * 64)
                c.write()
                result = c.pulsar('stop', spec['spec_id'], '--node', c.hostname(1))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(lines(result), [
                    f"Spec {spec['spec_id'][:12]}'s service was recorded under an earlier saved cluster and has no "
                    'container on any node of this one; retired its stale service record.',
                    # As at 8c0c673.
                    f"no stack-managed service is running for spec {spec['spec_id']}; nothing to stop",
                    'Nothing was stopped. Model files, pins, archives and experiment evidence are unchanged.'])
                self.assertFalse(c.recorded(plan))

    def test_a_container_still_there(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        plan = c.plan(spec, manifest, [0, 1], topology_id='e' * 64)
        (left,) = c.run(plan, ranks=[1])
        result = c.pulsar('stop', spec['spec_id'], '--node', c.hostname(1))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"its container {left['id'][:12]} on {c.hostname(1)} is still there", result.stderr)
        # As at 8c0c673.
        self.assertIn(f"warning: refusing spec {spec['spec_id'][:12]} id={left['id'][:12]} on {c.hostname(1)}: "
                      'ownership not proven', lines(result))
        self.assertTrue(c.recorded(plan))
        self.assertEqual(c.removed(), [])


class MachinesNotChecked(Cluster):
    """A record of an earlier saved cluster that names a machine no longer in
    the saved cluster: Stop retires it when no node of this cluster has its
    container, and names the machine it could not check."""

    def outside_record(self, *, guarded):
        from scripts.container_runtime import service_identifier
        from scripts.service_state import save
        from support.service_cluster import replanned
        c = self.cluster
        spec, manifest = c.spec(2, catalog=not guarded, guarded=guarded)
        plan = c.plan(spec, manifest, [2, 3], topology_id='e' * 64, record=False)
        ranks = [dict(plan['ranks'][0]), dict(plan['ranks'][1], node_id='fixture-ring-node-9',
                                                 hostname='fixture-ring-gone', ssh_host='ring-gone')]
        plan = replanned(plan, ranks=ranks, service_id=service_identifier(
            spec['spec_id'], 'e' * 64, [rank['node_id'] for rank in ranks]))
        save(c.store, plan)
        return spec, plan

    def test_an_ordinary_and_a_guarded_record(self):
        c = self.cluster
        for guarded in (False, True):
            with self.subTest(guarded=guarded):
                spec, plan = self.outside_record(guarded=guarded)
                result = c.pulsar('stop', spec['spec_id'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('retired its stale service record', result.stdout)
                self.assertIn('warning: the record also names fixture-ring-gone, which is not in the saved cluster, '
                              'so Pulsar did not check it; a container of this service left there is not removed by '
                              'Pulsar. Next: remove it with Docker on that machine', ' '.join(result.stderr.split()))
                self.assertNotIn('Stopped spec', result.stdout)
                self.assertNotIn('Stopped guarded', result.stdout)
                self.assertFalse(c.recorded(plan))


class StopAllSpeaksForWhatItChecked(Cluster):
    """stop --all's closing line says clean only for what it looked at."""
    outside_record = MachinesNotChecked.outside_record

    def test_after_a_machine_was_not_checked(self):
        c = self.cluster
        self.outside_record(guarded=False)
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('so Pulsar did not check it', ' '.join(result.stderr.split()))
        self.assertEqual(result.stdout.splitlines()[-1], 'clean — no stack-managed containers remain on the nodes of '
                                                         'the saved cluster; the machines named above were not checked')

    def test_otherwise_unchanged(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        c.plan(spec, manifest, [2, 3], topology_id='e' * 64)
        c.write()
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('did not check', result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], 'clean — no stack-managed containers remain')


class OneNodeRecordsOfAnEarlierCluster(Cluster):
    """A one-node record of an earlier saved cluster is cleared under the same
    rule as a multi-node one: Stop retires it once every node of this cluster
    was observed and none holds its container. Its spec need not be in the
    catalog. A node that was not observed is not absence: the record stays."""

    def record(self, position=2, *, catalog=False):
        c = self.cluster
        spec, manifest = c.spec(1, 3, catalog=catalog)
        return spec, c.plan(spec, manifest, [position], topology_id='e' * 64)

    def test_no_container_left_retires_it_with_stop_spec_and_with_stop_all(self):
        c = self.cluster
        for command in (lambda spec: ('stop', spec['spec_id']), lambda spec: ('stop', '--all', '--yes')):
            spec, plan = self.record()
            c.write()
            with self.subTest(command=command(spec)[1]):
                result = c.pulsar(*command(spec))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Spec {spec['spec_id'][:12]}'s service was recorded under an earlier saved cluster and "
                              'has no container on any node of this one; retired its stale service record.',
                              ' '.join(result.stdout.split()))
                self.assertFalse(c.recorded(plan))
                self.assertEqual(c.removed(), [])

    def test_a_container_still_there_is_kept_with_the_record(self):
        c = self.cluster
        spec, plan = self.record()
        (left,) = c.run(plan, state='exited')
        for args in (('stop', spec['spec_id']), ('stop', '--all', '--yes')):
            with self.subTest(args=args):
                result = c.pulsar(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"its container {left['id'][:12]} on {c.hostname(2)} is still there. Pulsar does not "
                              'remove containers of an earlier saved cluster, so nothing was removed and the record '
                              f"is kept. Next: remove that container with Docker on its node (docker rm -f "
                              f"{left['id'][:12]})", ' '.join(result.stderr.split()))
                self.assertTrue(c.recorded(plan))
                self.assertEqual(c.removed(), [])
        # Once it is removed with Docker, Stop retires the record.
        c.containers = []
        c.write()
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(c.recorded(plan))

    def test_a_node_that_could_not_be_checked_keeps_the_record(self):
        c = self.cluster
        spec, plan = self.record()
        c.write()
        # Its node is in the saved cluster but does not answer.
        for args in (('stop', spec['spec_id']), ('stop', '--all', '--yes')):
            with self.subTest(args=args):
                result = c.pulsar(*args, SERVICE_UNREACHABLE=c.node_id(2))
                self.assertEqual(result.returncode, 1)
                self.assertIn(c.hostname(2), result.stderr)
                self.assertTrue(c.recorded(plan))
        # Its node is not in the saved cluster any more.
        from scripts.container_runtime import service_identifier
        from scripts.service_state import save
        from support.service_cluster import replanned
        c.store.remove('services', plan['service_id'])
        ranks = [dict(plan['ranks'][0], node_id='fixture-ring-node-9', hostname='fixture-ring-gone',
                      ssh_host='ring-gone')]
        gone = replanned(plan, ranks=ranks, service_id=service_identifier(spec['spec_id'], 'e' * 64,
                                                                            ['fixture-ring-node-9']))
        save(c.store, gone)
        for args in (('stop', spec['spec_id']), ('stop', '--all', '--yes')):
            with self.subTest(args=args, outside=True):
                result = c.pulsar(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"warning: spec {spec['spec_id'][:12]}'s service was recorded under an earlier saved "
                              'cluster on fixture-ring-gone, which is not in the saved cluster, so Pulsar could not '
                              'check that machine and kept the record.', ' '.join(result.stderr.split()))
                self.assertNotIn('retired', result.stdout)
                self.assertTrue(c.recorded(gone))
        self.assertEqual(result.stdout.splitlines()[-1], 'clean — no stack-managed containers remain on the nodes of '
                                                         'the saved cluster; the machines named above were not checked')


class LeftoversOfAnEarlierCluster(Cluster):
    """A container of an earlier saved cluster that no record names: stop
    --all and inventory say it is left from an earlier saved cluster, that
    Pulsar does not remove it, and the docker rm -f that does."""

    def leftover(self):
        c = self.cluster
        spec, manifest = c.spec(1, 4, catalog=False)
        plan = c.plan(spec, manifest, [2], topology_id='e' * 64, record=False)
        (left,) = c.run(plan, state='exited')
        return left

    def test_stop_all(self):
        c = self.cluster
        left = self.leftover()
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"warning: container {left['id'][:12]} on {c.hostname(2)} is left from an earlier saved "
                      'cluster. Pulsar does not remove containers of an earlier saved cluster, so it is not removed. '
                      f"Next: remove that container with Docker on its node (docker rm -f {left['id'][:12]})",
                      ' '.join(result.stderr.split()))
        self.assertIn('warning: the containers left from an earlier saved cluster named above were not removed',
                      lines(result))
        self.assertNotIn('unknown spec or rank placement', result.stderr)
        self.assertEqual(c.removed(), [])

    def test_inventory(self):
        c = self.cluster
        left = self.leftover()
        result = c.pulsar('inventory')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = ' '.join(result.stdout.split())
        self.assertIn('reason left from an earlier saved cluster. Pulsar does not remove containers of an earlier '
                      'saved cluster, so it is not removed. Next: remove that container with Docker on its node '
                      f"(docker rm -f {left['id'][:12]})", text)
        # The JSON keeps its reasons as they were.
        (service,) = json.loads(c.pulsar('inventory', '--json').stdout)['services']
        self.assertNotIn('earlier saved cluster', json.dumps(service))

    def test_a_container_of_this_cluster_is_not_called_a_leftover(self):
        c = self.cluster
        spec, manifest = c.spec(1, 5, catalog=False)
        c.run(c.plan(spec, manifest, [2], record=False), state='exited', labels={'io.pulsar.gb10.node-id': 'other'})
        result = c.pulsar('stop', '--all', '--yes')
        self.assertIn('unknown spec or rank placement', result.stderr)
        self.assertNotIn('earlier saved cluster', result.stdout + result.stderr)
        self.assertNotIn('earlier saved cluster', c.pulsar('inventory').stdout)


class WhatRetiringDid(Cluster):
    """Stop says what retiring a record did: retired it, found it already
    retired, or found the service's record naming another plan and kept it."""

    def stop_earlier(self, plan):
        c = self.cluster
        return c.bash(f"load_cluster_topology\nstop_earlier_cluster_record {plan['selected_spec_id']} "
                      f"{plan['plan_id']} {plan['container_name']}")

    def test_the_three_outcomes(self):
        from support.service_cluster import replanned
        c = self.cluster
        spec, manifest = c.spec(2)
        plan = c.plan(spec, manifest, [2, 3], topology_id='e' * 64)
        prefix = f"Spec {spec['spec_id'][:12]}'s service was recorded under an earlier saved cluster and has no " \
                 'container on any node of this one; '
        # Another plan of the same service is recorded now.
        newer = replanned(plan, port=8001)
        c.store.put('service-plans', newer['plan_id'], newer)
        c.store.put('services', plan['service_id'], dict(c.store.get('services', plan['service_id']),
                                                         plan_id=newer['plan_id']))
        result = self.stop_earlier(plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(prefix + 'its service record now names another launch plan, so it was kept.', result.stdout)
        self.assertTrue(c.recorded(newer))
        # The service's record names this plan again: it is retired.
        c.store.put('services', plan['service_id'], dict(c.store.get('services', plan['service_id']),
                                                         plan_id=plan['plan_id']))
        result = self.stop_earlier(plan)
        self.assertIn(prefix + 'retired its stale service record.', result.stdout)
        # Nothing left to retire.
        result = self.stop_earlier(plan)
        self.assertIn(prefix + 'its stale service record was already retired.', result.stdout)


if __name__ == '__main__':
    unittest.main()
