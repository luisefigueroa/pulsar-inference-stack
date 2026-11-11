"""An ordinary Start of a multi-node recipe on nodes the operator names, on a
saved four-node ring (and a full mesh), through the real up.sh, launcher and
preflight with Docker, SSH and curl doubles (tests/support/service_cluster.py).
Nothing here contacts a node."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from scripts.container_runtime import service_identifier  # noqa: E402
from support.service_cluster import ServiceCluster  # noqa: E402


class Cluster(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def trace(self):
        return [line for line in self.cluster.trace() if 'docker info' not in line]

    def launcher(self, spec, *args, **env):
        c = self.cluster
        stack = c.install()
        environment = {**c.env, 'SERVICE_STACK': str(stack), 'BASH_ENV': str(c.root / 'start-env.sh'),
                       'VLLM_IMAGE_MAINLINE': 'example/image', 'VLLM_EXTRA_ARGS': '', 'EXTRA_ENV': '',
                       'WAIT_ATTEMPTS': '2', 'WAIT_SECONDS': '0', **env}
        return subprocess.run(['bash', str(stack / 'cluster/start-cluster.sh'), spec['spec_id'], *args],
                              env=environment, text=True, capture_output=True, timeout=120)


class OneMap(Cluster):
    """The launcher resolves the placement Start resolved and builds the same plan."""

    def test_the_launcher_builds_its_plan_on_the_named_nodes(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result_file = c.root / 'launch-result.json'
        result = self.launcher(spec, '--skip-preflight', '--dry-run', '--placement-nodes',
                               f'{c.hostname(2)},{c.hostname(3)}', PULSAR_LAUNCH_RESULT_FILE=str(result_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result_file.read_text())['service_id'],
                         service_identifier(spec['spec_id'], c.topology_id, [c.node_id(2), c.node_id(3)]))

    def test_a_plan_that_differs_from_the_one_start_checked_starts_nothing(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_PREPARED_DRIFT='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('error: the launch plan differs from the one start checked', result.stderr)
        self.assertIn('Nothing was started.', result.stderr)
        self.assertFalse([line for line in self.trace() if 'docker run' in line or 'docker rm' in line])
        self.assertEqual(c.store.records('services'), [])


class FirstNodeAddress(Cluster):
    """Health comes from the first node's own address."""

    def launch(self, positions, **env):
        c = self.cluster
        spec, _ = c.spec(2)
        names = ','.join(c.hostname(position) for position in positions)
        return self.launcher(spec, '--skip-preflight', '--placement-nodes', names, **env)

    def test_a_service_on_this_machines_loopback_port_does_not_answer_for_it(self):
        # The curl double answers /health on 127.0.0.1 only: another service here.
        result = self.launch([2, 3], SERVICE_HEALTHY='127.0.0.1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('waiting for http://192.0.2.32:8000/health', result.stdout)
        self.assertNotIn('healthy ·', result.stdout)
        self.assertEqual({url for url in self.cluster.calls()['curl']}, {'http://192.0.2.32:8000/health'})
        self.assertEqual(self.cluster.remaining(), [])

    def test_the_first_nodes_address_answers(self):
        result = self.launch([2, 3], SERVICE_HEALTHY='192.0.2.32')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('healthy · first-health=', result.stdout)

    def test_the_loopback_address_only_when_the_first_node_is_this_machine(self):
        result = self.launch([0, 1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cluster.calls()['curl'], ['http://127.0.0.1:8000/health'])


class Preflight(Cluster):
    """Preflight checks the chosen nodes only, and its rows name role ranks."""

    def test_a_pair_on_saved_ranks_2_and_3(self):
        c = self.cluster
        spec, _ = c.spec(2)
        stack = c.install()
        environment = {**c.env, 'SERVICE_STACK': str(stack), 'BASH_ENV': str(c.root / 'start-env.sh'),
                       'VLLM_IMAGE_MAINLINE': 'example/image', 'VLLM_EXTRA_ARGS': '', 'EXTRA_ENV': ''}
        result = subprocess.run(['bash', str(stack / 'cluster/preflight.sh'), spec['spec_id'], '--placement-nodes',
                                 f'{c.hostname(3)},{c.hostname(2)}'], env=environment, text=True,
                                capture_output=True, timeout=120)
        nodes = {line.split()[0] for line in self.trace() if line.startswith('node-')}
        self.assertEqual(nodes, {'node-2', 'node-3'}, self.trace())
        self.assertIn('ok   rank 0 SSH ring-alpha · fixture-ring-alpha', result.stdout)
        self.assertIn('ok   rank 1 SSH ring-echo · fixture-ring-echo', result.stdout)
        self.assertIn('ok   rank 0: no stale vllm-cluster containers', result.stdout)
        self.assertIn('ranks 0/1:', result.stdout)
        self.assertIn(f"check-weights.sh {spec['spec_id']} --placement-nodes "
                      f'{c.hostname(3)},{c.hostname(2)}', c.calls()['checks'])
        for word in ('rank 2', 'rank 3', 'dorado', 'bravo'):
            self.assertNotIn(word, result.stdout)


class OneServicePerSpec(Cluster):
    """Start reads the saved records: a spec runs as one service."""

    def recorded_on(self, positions, **options):
        c = self.cluster
        spec, manifest = c.spec(2)
        return spec, c.plan(spec, manifest, positions, **options)

    def test_a_record_on_other_nodes_refuses_a_default_order_start(self):
        c = self.cluster
        spec, _ = self.recorded_on([2, 3])
        for flags in ((), ('--replace',)):
            with self.subTest(flags=flags):
                c.forget_calls()
                result = c.start(spec['spec_id'], '--skip-preflight', *flags)
                self.assertEqual(result.returncode, 1)
                self.assertIn('BLOCKED service_recorded_elsewhere: a service of this spec is recorded on other nodes '
                              '(on fixture-ring-echo, fixture-ring-alpha). Next: ./pulsar stop '
                              f"{spec['spec_id'][:12]}", ' '.join(result.stdout.split()))
                self.assertFalse([line for line in self.trace() if 'docker' in line])

    def test_a_record_of_an_earlier_saved_cluster_is_another_service(self):
        c = self.cluster
        spec, plan = self.recorded_on([0, 1], topology_id='e' * 64)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 1)
        self.assertIn('service_recorded_elsewhere', result.stdout)
        # ./pulsar stop clears it when none of its containers is left, and Start then runs.
        self.assertEqual(c.pulsar('stop', spec['spec_id']).returncode, 0)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_replace_off_the_default_order_is_refused_before_anything_is_contacted(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', '--replace', '--placement-nodes',
                         f'{c.hostname(2)},{c.hostname(3)}')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--replace replaces a service on the default node order only. To start this spec on '
                      f'{c.hostname(2)},{c.hostname(3)}, stop its service first (./pulsar stop {spec["spec_id"][:12]}), '
                      'then start it without --replace', result.stderr)
        self.assertEqual(self.trace(), [])

    def test_the_launcher_refuses_replace_off_the_default_order_too(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = self.launcher(spec, '--skip-preflight', '--replace', '--placement-nodes',
                               f'{c.hostname(2)},{c.hostname(3)}')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--replace replaces a service on the default node order only', result.stderr)
        self.assertFalse([line for line in self.trace() if 'docker' in line])


KEY = 'fixture-secret-key-5f1e'


class ApiKey(Cluster):
    """The key goes where it goes for a one-node start on a remote node: the
    docker run sent over SSH to the first node, and that container's
    arguments; nowhere else."""

    def test_the_key_reaches_only_the_first_node(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = self.launcher(spec, '--skip-preflight', '--placement-nodes', f'{c.hostname(2)},{c.hostname(3)}',
                               VLLM_API_KEY=KEY, SERVICE_HEALTHY='192.0.2.32')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        calls = c.calls()
        to_nodes = [(node, command) for node, command in calls['ssh'] if KEY in command]
        self.assertEqual([node for node, _ in to_nodes], [c.node_id(2)])
        self.assertTrue(to_nodes[0][1].startswith('docker run '))
        runs = {node: args for node, args in calls['runs']}
        self.assertIn(KEY, runs[c.node_id(2)])
        self.assertNotIn(KEY, ' '.join(runs[c.node_id(3)]))
        self.assertFalse([args for node, args in calls['docker'] if node != c.node_id(2) and KEY in ' '.join(args)])
        for path in (c.root / 'model-library').rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(errors='replace'), path)
        for name in ('trace.log', 'checks.log'):
            self.assertNotIn(KEY, (c.root / name).read_text())

    def test_a_dry_run_prints_it_redacted(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = self.launcher(spec, '--skip-preflight', '--dry-run', '--placement-nodes',
                               f'{c.hostname(2)},{c.hostname(3)}', VLLM_API_KEY=KEY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertIn('--api-key \\<redacted\\>', result.stdout)
        self.assertIn('rank 0 · ssh ring-echo · fixture-ring-echo', result.stdout)


class LauncherOnTheMap(Cluster):
    """The launcher runs each role rank on its node: local Docker for this
    machine, SSH otherwise, the first node included; the other ranks first."""

    def launch(self, positions, **env):
        c = self.cluster
        spec, _ = c.spec(2)
        names = ','.join(c.hostname(position) for position in positions)
        return self.launcher(spec, '--skip-preflight', '--placement-nodes', names, **env)

    def test_a_failed_health_wait_removes_what_it_started_on_the_pair_only(self):
        result = self.launch([2, 3], SERVICE_HEALTHY='none')
        self.assertEqual(result.returncode, 1)
        trace = [line for line in self.trace() if not line.startswith(('curl', 'port'))]
        self.assertEqual(trace, [
            'node-3 docker inspect by name over ssh', 'node-2 docker inspect by name over ssh',
            'node-3 docker run rank 1 over ssh', 'node-2 docker run rank 0 over ssh',
            'node-2 docker state over ssh', 'node-3 docker state over ssh',
            'node-2 docker state over ssh', 'node-3 docker state over ssh',
            'node-2 docker logs over ssh', 'node-3 docker logs over ssh',
            'node-2 docker rm by id over ssh', 'node-3 docker rm by id over ssh',
            'node-2 docker state over ssh', 'node-3 docker state over ssh'])
        self.assertIn('port 8000 on ring-echo', self.trace())
        self.assertIn('starting rank 1 on ring-alpha', result.stdout)
        self.assertIn('starting rank 0 on ring-echo', result.stdout)
        self.assertEqual(self.cluster.remaining(), [])

    def test_a_failed_first_node_removes_the_other_rank_on_its_node(self):
        c = self.cluster
        result = self.launch([2, 3], SERVICE_RUN_FAILS=c.node_id(2))
        self.assertEqual(result.returncode, 1)
        trace = [line for line in self.trace() if not line.startswith(('curl', 'port'))]
        self.assertEqual(trace[2:5], ['node-3 docker run rank 1 over ssh', 'node-2 docker run fails over ssh',
                                      'node-3 docker rm by id over ssh'])
        self.assertIn('fixture-ring-echo (rank 0): the service container could not be started', result.stdout)
        self.assertEqual(c.remaining(), [])

    def test_this_machine_as_the_second_node_runs_on_local_docker(self):
        result = self.launch([3, 0], SERVICE_HEALTHY='none')
        trace = [line for line in self.trace() if 'docker run' in line]
        self.assertEqual(trace, ['node-0 docker run rank 1', 'node-3 docker run rank 0 over ssh'])
        self.assertIn('starting rank 1 locally', result.stdout)
        self.assertIn('starting rank 0 on ring-alpha', result.stdout)


class Decision(Cluster):
    """scripts/start_placement.py: the one decision, and what it accepts."""

    def refusal(self, positions, *, guarded=False, topology=None):
        from scripts.start_placement import ordinary_start_refusal
        c = self.cluster
        return ordinary_start_refusal(topology or c.topology, [c.node_id(p) for p in positions], guarded=guarded)

    def test_a_neighbouring_pair_of_a_ring_in_either_order(self):
        for positions in ((2, 3), (3, 2), (0, 3), (3, 0), (1, 2)):
            with self.subTest(positions=positions):
                self.assertIsNone(self.refusal(positions))

    def test_what_stays_refused(self):
        self.assertEqual(self.refusal((0, 1, 2)), 'on a ring Start runs only a two-node recipe on named nodes; this '
                         'recipe needs 3 nodes and runs on the ring\'s nodes in saved order; omit --placement-nodes')
        self.assertEqual(self.refusal((0, 2)), 'fixture-ring-dorado and fixture-ring-echo are not neighbours on the ring')
        self.assertEqual(self.refusal((2, 3), guarded=True), 'a guarded recipe runs only as a guarded trial')
        mesh = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())), mesh=3)
        from scripts.start_placement import ordinary_start_refusal
        self.assertEqual(ordinary_start_refusal(mesh.topology, [mesh.node_id(2), mesh.node_id(1)], guarded=False),
                         'on a full mesh Start runs a 2-node recipe on the first 2 nodes in saved order; '
                         'omit --placement-nodes')


class StartOnNamedNodes(Cluster):
    """An ordinary Start on a neighbouring pair, end to end under doubles."""

    def start(self, positions, *args, **env):
        c = self.cluster
        names = ','.join(c.hostname(position) for position in positions)
        first = c.nodes[positions[0]]['control']['ip'] if positions[0] else '127.0.0.1'
        env.setdefault('SERVICE_HEALTHY', first)
        return c.start(self.spec['spec_id'], '--skip-preflight', '--placement-nodes', names, *args, **env)

    def setUp(self):
        super().setUp()
        self.spec, _ = self.cluster.spec(2)

    def test_a_pair_that_does_not_include_this_machine_in_both_orders(self):
        c = self.cluster
        for positions in ((2, 3), (3, 2)):
            with self.subTest(positions=positions):
                c.forget_calls()
                result = self.start(positions)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                first = c.nodes[positions[0]]
                url = f"http://{first['control']['ip']}:8000"
                self.assertIn(f'url:      {url}/v1', result.stdout)
                self.assertEqual(c.calls()['curl'], [f'{url}/health', f'{url}/v1/completions'])
                pair = {f'node-{position}' for position in positions}
                self.assertEqual({line.split()[0] for line in self.trace() if line.startswith('node-')}, pair)
                self.assertEqual({node.rsplit('-', 1)[-1] for node, _ in c.calls()['ssh']},
                                 {str(position) for position in positions})
                runs = [line for line in self.trace() if 'docker run' in line]
                self.assertEqual(runs, [f'node-{positions[1]} docker run rank 1 over ssh',
                                        f'node-{positions[0]} docker run rank 0 over ssh'])
                (record,) = c.store.records('services')
                plan = c.store.get('service-plans', record['plan_id'])
                self.assertEqual([rank['node_id'] for rank in plan['ranks']], [c.node_id(p) for p in positions])
                self.assertEqual(c.pulsar('stop', self.spec['spec_id']).returncode, 0)

    def test_this_machine_with_its_other_neighbour(self):
        c = self.cluster
        result = self.start((0, 3))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([line for line in self.trace() if 'docker run' in line],
                         ['node-3 docker run rank 1 over ssh', 'node-0 docker run rank 0'])
        self.assertIn('url:      http://127.0.0.1:8000/v1', result.stdout)

    def test_a_node_outside_the_pair_that_is_down_never_blocks_it(self):
        c = self.cluster
        result = self.start((2, 3), SERVICE_UNREACHABLE=f'{c.node_id(1)}', SERVICE_DOCKER_DOWN=c.node_id(0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_start_then_inventory_then_stop(self):
        c = self.cluster
        result = self.start((2, 3))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        inventory = c.pulsar('inventory', '--json')
        self.assertEqual(inventory.returncode, 0, inventory.stderr)
        (service,) = json.loads(inventory.stdout)['services']
        self.assertEqual((service['state'], service['ownership'], service['safe_to_stop']), ('running', 'managed', True))
        self.assertEqual([rank['node'] for rank in service['ranks']], ['rank-2', 'rank-3'])
        stopped = c.pulsar('stop', self.spec['spec_id'])
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn(f"Stopped spec {self.spec['spec_id'][:12]} on fixture-ring-echo (rank 0), fixture-ring-alpha "
                      '(rank 1)', stopped.stdout)
        self.assertEqual((c.remaining(), c.store.records('services')), ([], []))
        # stop --all removes it too.
        self.assertEqual(self.start((2, 3)).returncode, 0)
        self.assertEqual(c.pulsar('stop', '--all', '--yes').returncode, 0)
        self.assertEqual((c.remaining(), c.store.records('services')), ([], []))

    def test_start_stays_non_replacing(self):
        c = self.cluster
        self.assertEqual(self.start((2, 3)).returncode, 0)
        c.forget_calls()
        result = self.start((2, 3))
        self.assertEqual(result.returncode, 1)
        self.assertIn('BLOCKED service_exists', result.stdout)
        self.assertFalse([line for line in self.trace() if 'docker run' in line or 'docker rm' in line])

    def test_refused_placements_contact_nothing(self):
        c = self.cluster
        three, _ = c.spec(3)
        for spec, names, words in (
                (self.spec, f'{c.hostname(0)},{c.hostname(2)}', 'confirmed serving placement is unavailable'),
                (three, f'{c.hostname(1)},{c.hostname(2)},{c.hostname(3)}', 'confirmed serving placement is unavailable')):
            with self.subTest(names=names):
                c.forget_calls()
                result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(words, result.stderr)
                self.assertEqual(self.trace(), [])


class Suggestions(Cluster):
    """No suggestion gives --placement-nodes to stop or status; a suggested
    Start carries it."""

    def test_the_catalog_of_blockers(self):
        from scripts.start_blockers import blocker
        spec = 'ab' * 32
        placement = '--placement-nodes fixture-ring-echo,fixture-ring-alpha'
        record = blocker('service_exists', spec=spec, placement=placement)
        self.assertEqual(record['fix'], './pulsar status abababababab')
        record = blocker('start_interrupted', spec=spec, placement=placement)
        self.assertIn('To remove it: ./pulsar stop abababababab', record['note'])
        self.assertNotIn('--placement-nodes', record['fix'] + record['note'])
        record = blocker('container_exited', spec=spec, placement=placement, unconfirmed='fixture-ring-echo (rank 0)')
        self.assertEqual(record['fix'], './pulsar stop abababababab')
        self.assertEqual(blocker('image_missing', spec=spec, placement=placement)['fix'],
                         f'./pulsar start abababababab {placement} --pull-image')
        # One-node services keep their --node.
        self.assertEqual(blocker('service_exists', spec=spec, placement='--node spark-2')['fix'],
                         './pulsar status abababababab --node spark-2')

    def test_ready_and_the_launchers_records(self):
        c = self.cluster
        spec, _ = c.spec(2)
        names = f'{c.hostname(2)},{c.hostname(3)}'
        result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names, SERVICE_HEALTHY='192.0.2.32')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [' '.join(line.split()) for line in result.stdout.splitlines()]
        self.assertIn(f"status: ./pulsar status {spec['spec_id']}", lines)
        self.assertIn(f"stop: ./pulsar stop {spec['spec_id']}", lines)
        c.pulsar('stop', spec['spec_id'])
        result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names, SERVICE_HEALTHY='none',
                         PULSAR_START_BLOCKERS_FILE=str(c.root / 'records.jsonl'))
        from scripts import start_blockers
        (record,) = start_blockers.read(c.root / 'records.jsonl')
        self.assertEqual((record['blocker'], record['node']), ('health_timeout', 'fixture-ring-echo'))
        self.assertNotIn('--placement-nodes', (record['fix'] or '') + (record['note'] or ''))


class Readers(Cluster):
    """The Connect card and Restart for a pair Start ran on saved ranks 2 and 3."""

    def setUp(self):
        super().setUp()
        c = self.cluster
        self.spec, _ = c.spec(2)
        self.card = c.root / 'connect.json'
        result = c.start(self.spec['spec_id'], '--skip-preflight', '--placement-nodes',
                         f'{c.hostname(2)},{c.hostname(3)}', SERVICE_HEALTHY='192.0.2.32',
                         PULSAR_SERVICE_RESULT_FILE=str(self.card))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_connect_card_names_the_first_nodes_address(self):
        self.assertEqual(json.loads(self.card.read_text())['api_url'], 'http://192.0.2.32:8000')

    def test_restart_runs_start_on_the_same_nodes_in_the_same_order(self):
        from model_library.catalog import entries as catalog_entries
        from model_library.node_names import NodeNames
        from scripts import serve_plan
        from scripts import topology_manifest as tm
        c = self.cluster
        pages = c.pages(self.spec['spec_id'])
        self.assertIn('restart', [action for action, _ in pages['choices']])
        _, _, _, node = pages['page'].command(self.spec['spec_id'], 'restart')
        names = NodeNames({n['node_id']: n['hostname'] for n in c.nodes}, [n['node_id'] for n in c.nodes],
                          tm.RING_CLASS)
        entries = catalog_entries(c.root, c.store)
        row = next(entry for entry in entries if entry['spec_id'] == self.spec['spec_id'])
        inventory = json.loads(c.pulsar('inventory', '--json').stdout)
        plan = serve_plan.compose(row, entries, names=names, node=node, inventory=inventory, restart=True)
        self.assertEqual(plan['blocked'], [])
        self.assertEqual([step['command'] for step in plan['steps']], [
            ['./pulsar', 'stop', self.spec['spec_id']],
            ['./pulsar', 'start', self.spec['spec_id'], '--placement-nodes', 'fixture-ring-echo,fixture-ring-alpha']])


class RecordsWithoutContainers(Cluster):
    """After a Start that failed once its plan was saved (the plan and record
    are saved just before the first docker run, and a failed launch keeps
    them, as the default-order pins show)."""

    def test_a_retry_on_the_same_nodes_works(self):
        c = self.cluster
        spec, _ = c.spec(2)
        names = f'{c.hostname(2)},{c.hostname(3)}'
        result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names,
                         SERVICE_RUN_FAILS=c.node_id(2))
        self.assertEqual(result.returncode, 1)
        self.assertEqual((len(c.store.records('services')), c.remaining()), (1, []))
        result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names, SERVICE_HEALTHY='192.0.2.32')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_other_nodes_are_refused_until_stop_clears_the_record(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', f'{c.hostname(2)},{c.hostname(3)}',
                         SERVICE_RUN_FAILS=c.node_id(2))
        self.assertEqual(result.returncode, 1)
        for positions in ((0, 1), (3, 2)):
            with self.subTest(positions=positions):
                names = ','.join(c.hostname(p) for p in positions)
                result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names)
                self.assertEqual(result.returncode, 1)
                self.assertIn('BLOCKED service_recorded_elsewhere: a service of this spec is recorded on other nodes '
                              f"(on fixture-ring-echo, fixture-ring-alpha). Next: ./pulsar stop {spec['spec_id'][:12]}",
                              ' '.join(result.stdout.split()))
        # The one command that clears it: a record with no container left is retired.
        stopped = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn('had no container left', stopped.stdout)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
