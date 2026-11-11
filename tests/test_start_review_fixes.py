"""Start's launch, plan check and one service per spec, under the doubles of
tests/support/service_cluster.py: the launcher builds every rank's command
before the first container starts, the plan check fails closed, and the saved
records allow one service per spec even for two Starts at once. Nothing here
contacts a node."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.service_cluster import ServiceCluster  # noqa: E402


class Cluster(unittest.TestCase):
    maxDiff = None
    MESH = 4

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())), mesh=self.MESH)

    def trace(self):
        return [line for line in self.cluster.trace() if 'docker info' not in line]


class EveryCommandBeforeTheFirstContainer(Cluster):
    """A rank whose command cannot be built starts nothing, even the last one."""

    def builds(self, spec, args, **env):
        c = self.cluster
        c.forget_calls()
        return c.start(spec['spec_id'], '--skip-preflight', *args, SERVICE_ARGV_RANK='2', **env)

    def test_a_failed_build_of_the_last_ranks_command_starts_no_container(self):
        c = self.cluster
        spec, _ = c.spec(3)
        for args, positions in (((), [0, 1, 2]),
                                (('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3))), [2, 0, 3])):
            with self.subTest(positions=positions):
                # How many times a Start builds rank 2's command; the last of those fails.
                result = self.builds(spec, args, SERVICE_HEALTHY='none')
                self.assertEqual(result.returncode, 1)
                self.assertEqual(c.remaining(), [])
                count = len((c.root / 'argv-builds').read_text().splitlines())
                for record in c.store.records('services'):
                    c.store.remove('services', record['service_id'])
                result = self.builds(spec, args, SERVICE_ARGV_FAILS_AT=str(count))
                self.assertEqual(result.returncode, 1)
                self.assertIn('fixture: the launch command of rank 2 could not be read', result.stderr)
                self.assertIn('argv of rank 2 fails', self.trace())
                self.assertFalse([line for line in self.trace() if 'docker run' in line], self.trace())
                self.assertEqual(c.remaining(), [])

    def test_an_unexpected_exit_after_the_containers_started_removes_them(self):
        # A step between the launch and the health wait fails: every container
        # this launch started is removed, on the default order and on named nodes.
        c = self.cluster
        spec, _ = c.spec(3)
        for args in ((), ('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3)))):
            with self.subTest(args=args):
                c.forget_calls()
                for record in c.store.records('services'):
                    c.store.remove('services', record['service_id'])
                result = c.start(spec['spec_id'], '--skip-preflight', *args, SERVICE_FAILS_LOADING='1')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('the launch stopped unexpectedly', result.stderr, (result.stdout, self.trace()))
                trace = self.trace()
                self.assertEqual(len([line for line in trace if 'docker run' in line]), 3, trace)
                self.assertEqual(len([line for line in trace if 'docker rm by id' in line]), 3, trace)
                self.assertEqual(c.remaining(), [])


class ThePlanCheckFailsClosed(Cluster):
    """Start hands the launcher the ID of the plan it checked; without it, nothing launches."""

    def test_a_plan_without_its_id_launches_nothing(self):
        c = self.cluster
        spec, _ = c.spec(3)
        for args in ((), ('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3)))):
            with self.subTest(args=args):
                c.forget_calls()
                result = c.start(spec['spec_id'], '--skip-preflight', *args, SERVICE_PLAN_WITHOUT_ID='1')
                self.assertEqual(result.returncode, 1)
                self.assertIn('error: the launch plan start checked cannot be read, so the launcher could not '
                              'confirm it builds the same one; nothing was launched', result.stderr)
                # The launcher never ran: it prints its spec line first.
                self.assertNotIn('exact spec:', result.stdout)
                self.assertFalse([line for line in self.trace() if 'docker run' in line])
                self.assertEqual(c.calls()['runs'], [])

    def test_a_plan_the_launcher_builds_differently_still_starts_nothing(self):
        # With the ID handed over, the launcher's own check refuses a different plan.
        c = self.cluster
        spec, _ = c.spec(3)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_PREPARED_DRIFT='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('error: the launch plan differs from the one start checked', result.stderr)
        self.assertEqual(c.calls()['runs'], [])


class OneServicePerSpecWhereTheRecordIsSaved(Cluster):
    """service_state.save refuses an ordinary multi-node record while another
    service of the same spec is recorded, under the lock every save takes."""

    def test_two_saves_for_different_nodes(self):
        from scripts.service_state import RecordedElsewhere, save
        c = self.cluster
        spec, manifest = c.spec(3)
        first = c.plan(spec, manifest, [1, 2, 3], record=False)
        second = c.plan(spec, manifest, [2, 0, 3], record=False)
        save(c.store, first)
        with self.assertRaises(RecordedElsewhere) as refused:
            save(c.store, second)
        self.assertEqual(refused.exception.hosts, [[c.hostname(1), c.hostname(2), c.hostname(3)]])
        self.assertTrue(c.recorded(first))
        self.assertIsNone(c.store.get('services', second['service_id']))

    def test_a_re_save_of_the_same_service_after_a_failed_start(self):
        from scripts.service_state import save
        from support.service_cluster import replanned
        c = self.cluster
        spec, manifest = c.spec(3)
        plan = c.plan(spec, manifest, [2, 0, 3], record=False)
        save(c.store, plan)
        again = replanned(plan, port=8001)
        save(c.store, again)
        self.assertTrue(c.recorded(again))

    def test_guarded_and_one_node_records_keep_their_own_rules(self):
        from scripts.service_state import save
        c = self.cluster
        guarded, guarded_manifest = c.spec(2, catalog=False, guarded=True)
        for positions in ([0, 1], [2, 3]):
            save(c.store, c.plan(guarded, guarded_manifest, positions, record=False))
        one, one_manifest = c.spec(1)
        for positions in ([1], [2]):
            save(c.store, c.plan(one, one_manifest, positions, record=False))
        self.assertEqual(len(c.store.records('services')), 4)

    def test_the_launcher_refuses_before_any_docker_run(self):
        # Another Start of the spec saved its record after this Start's own check:
        # the launcher, called as up.sh calls it, refuses when it saves its record.
        import json
        import subprocess
        c = self.cluster
        spec, manifest = c.spec(3)
        c.plan(spec, manifest, [1, 2, 3])
        stack = c.install()
        blockers = c.root / 'blockers.jsonl'
        environment = {**c.env, 'SERVICE_STACK': str(stack), 'BASH_ENV': str(c.root / 'start-env.sh'),
                       'VLLM_IMAGE_MAINLINE': 'example/image', 'VLLM_EXTRA_ARGS': '', 'EXTRA_ENV': '',
                       'WAIT_ATTEMPTS': '2', 'WAIT_SECONDS': '0', 'PULSAR_START_BLOCKERS_FILE': str(blockers)}
        result = subprocess.run(['bash', str(stack / 'cluster/start-cluster.sh'), spec['spec_id'], '--skip-preflight',
                                 '--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3))],
                                env=environment, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 1)
        self.assertIn('a service of spec', result.stderr)
        self.assertIn('is recorded on other nodes; a spec runs as one service, so nothing was started', result.stderr)
        records = [json.loads(line) for line in blockers.read_text().splitlines()]
        self.assertEqual([record['blocker'] for record in records], ['service_recorded_elsewhere'], records)
        self.assertIn(f'on {c.hostname(1)}, {c.hostname(2)}, {c.hostname(3)}', json.dumps(records[0]))
        self.assertFalse([line for line in self.trace() if 'docker run' in line])
        self.assertEqual(len(c.store.records('services')), 1)


class SmallOnes(Cluster):
    def test_unreadable_records_say_why(self):
        c = self.cluster
        spec, _ = c.spec(3)
        c.store.put('services', 'f' * 64, {'schema_version': 1, 'service_id': 'f' * 64,
                                           'selected_spec_id': spec['spec_id'], 'plan_id': 'a' * 64})
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 1)
        text = ' '.join(result.stdout.split())
        self.assertIn('BLOCKED service_recorded_elsewhere: a service of this spec is recorded on other nodes (the '
                      'saved service records cannot be read: ', text)
        self.assertFalse([line for line in self.trace() if 'docker' in line])

    def test_the_ports_are_checked_on_the_first_node(self):
        c = self.cluster
        spec, _ = c.spec(3)
        for args, where in (((), 'local'),
                            (('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3))), 'alias-2')):
            with self.subTest(where=where):
                c.forget_calls()
                for record in c.store.records('services'):
                    c.store.remove('services', record['service_id'])
                c.containers = []
                c.write()
                result = c.start(spec['spec_id'], '--skip-preflight', *args,
                                 SERVICE_HEALTHY='127.0.0.1' if where == 'local' else '192.0.2.12')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                ports = [line for line in self.trace() if line.startswith('port ')]
                self.assertEqual(ports, [f'port 8000 on {where}', f'port 29500 on {where}'] * 2)


if __name__ == '__main__':
    unittest.main()
