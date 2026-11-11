"""An ordinary Start of a multi-node recipe on any nodes the operator names
that are saved members cabled as the recipe needs: pairs and three nodes of a
full mesh, the first node remote and this machine a headless rank or not in
the placement at all, and a recipe that passes data around a loop on the
whole ring in a rotated order. Through the real up.sh, preflight and launcher
with Docker, SSH and curl doubles (tests/support/service_cluster.py); nothing
here contacts a node."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from scripts.container_runtime import service_identifier  # noqa: E402
from support.service_cluster import ServiceCluster  # noqa: E402
from tests.test_serve_pair_question import RingHarness, recipe, ring_inventory  # noqa: E402
from tests.test_serve_plan import SPEC as MENU_SPEC  # noqa: E402


class Cluster(unittest.TestCase):
    maxDiff = None
    MESH = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())), mesh=self.MESH)

    def trace(self):
        return [line for line in self.cluster.trace() if 'docker info' not in line]

    def address(self, position):
        return self.cluster.nodes[position]['control']['ip']

    def start(self, spec, positions, *args, **env):
        c = self.cluster
        names = ','.join(c.hostname(position) for position in positions)
        env.setdefault('SERVICE_HEALTHY', self.address(positions[0]) if positions[0] else '127.0.0.1')
        return c.start(spec['spec_id'], '--placement-nodes', names, *args, **env)

    def assert_started_on(self, result, spec, positions):
        """It ran each role rank on its node, checked health on the first node's own
        address, reported that address and recorded the service on those nodes."""
        c = self.cluster
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS  preflight cluster OK', result.stdout)
        runs = [line for line in self.trace() if 'docker run' in line]
        self.assertEqual(sorted(runs), sorted(
            f"node-{position} docker run rank {rank}{'' if position == 0 else ' over ssh'}"
            for rank, position in enumerate(positions)))
        # The other ranks first, the first node last.
        self.assertTrue(runs[-1].startswith(f'node-{positions[0]} docker run rank 0'), runs)
        first = '127.0.0.1' if positions[0] == 0 else self.address(positions[0])
        self.assertEqual(set(c.calls()['curl']), {f'http://{first}:8000/health', f'http://{first}:8000/v1/completions'})
        self.assertIn(f'url:      http://{first}:8000/v1', result.stdout)
        # Preflight and the launcher reached the placement's nodes only.
        self.assertEqual({line.split()[0] for line in self.trace() if line.startswith('node-')},
                         {f'node-{position}' for position in positions})
        record = c.store.get('services', service_identifier(spec['spec_id'], c.topology_id,
                                                              [c.node_id(position) for position in positions]))
        self.assertIsNotNone(record)


class FullMeshOfThree(Cluster):
    MESH = 3

    def test_a_pair_without_this_machine(self):
        spec, _ = self.cluster.spec(2)
        self.assert_started_on(self.start(spec, [1, 2]), spec, [1, 2])

    def test_saved_ranks_2_then_1(self):
        # tests/test_diagnostics.py's pair, which ordinary Start once refused.
        spec, _ = self.cluster.spec(2)
        self.assert_started_on(self.start(spec, [2, 1]), spec, [2, 1])

    def test_the_default_pair_in_reverse_makes_this_machine_a_headless_rank(self):
        spec, _ = self.cluster.spec(2)
        result = self.start(spec, [1, 0])
        self.assert_started_on(result, spec, [1, 0])
        self.assertIn('starting rank 1 locally', result.stdout)
        self.assertIn('starting rank 0 on alias-1', result.stdout)


class FullMeshOfFour(Cluster):
    MESH = 4

    def test_a_pair_without_this_machine(self):
        spec, _ = self.cluster.spec(2)
        self.assert_started_on(self.start(spec, [3, 2]), spec, [3, 2])

    def test_three_nodes_with_the_first_remote_and_this_machine_another_rank(self):
        spec, _ = self.cluster.spec(3)
        result = self.start(spec, [2, 0, 3])
        self.assert_started_on(result, spec, [2, 0, 3])
        self.assertIn('starting rank 1 locally', result.stdout)

    def test_three_nodes_without_this_machine(self):
        spec, _ = self.cluster.spec(3)
        self.assert_started_on(self.start(spec, [1, 2, 3]), spec, [1, 2, 3])


class WholeRingRotated(Cluster):
    def test_a_ring_pattern_recipe_on_the_whole_ring_from_another_machine(self):
        spec, _ = self.cluster.spec(4, ring=True)
        self.assert_started_on(self.start(spec, [1, 2, 3, 0]), spec, [1, 2, 3, 0])


KEY = 'fixture-secret-key-7c2a'


class WhatHoldsForThem(Cluster):
    """Part of every Start on named nodes, shown for three ranks of a four-node
    full mesh with the first node remote and this machine rank 1."""
    MESH = 4
    PLACE = [2, 0, 3]

    def setUp(self):
        super().setUp()
        self.spec, self.manifest = self.cluster.spec(3)

    def test_one_service_per_spec(self):
        c = self.cluster
        c.plan(self.spec, self.manifest, [1, 2, 3])
        for args in ((), ('--placement-nodes', ','.join(c.hostname(p) for p in self.PLACE))):
            with self.subTest(args=args):
                c.forget_calls()
                result = c.start(self.spec['spec_id'], *args)
                self.assertEqual(result.returncode, 1)
                self.assertIn('BLOCKED service_recorded_elsewhere: a service of this spec is recorded on other '
                              f'nodes (on {c.hostname(1)}, {c.hostname(2)}, {c.hostname(3)})',
                              ' '.join(result.stdout.split()))
                self.assertFalse([line for line in self.trace() if 'docker' in line])

    def test_replace_on_the_default_order_only(self):
        result = self.start(self.spec, self.PLACE, '--replace')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--replace replaces a service on the default node order only', result.stderr)
        self.assertEqual(self.trace(), [])

    def test_clean_up_only_on_the_placements_nodes(self):
        c = self.cluster
        result = self.start(self.spec, self.PLACE, '--skip-preflight', SERVICE_HEALTHY='none')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(c.remaining(), [])
        self.assertEqual({node for node, _ in c.removed()}, {c.node_id(p) for p in self.PLACE})
        self.assertEqual({line.split()[0] for line in self.trace() if line.startswith('node-')},
                         {f'node-{p}' for p in self.PLACE})

    def test_the_api_key_only_in_the_first_nodes_command(self):
        c = self.cluster
        result = self.start(self.spec, self.PLACE, '--skip-preflight', VLLM_API_KEY=KEY)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        calls = c.calls()
        self.assertEqual([node for node, command in calls['ssh'] if KEY in command], [c.node_id(2)])
        runs = {node: ' '.join(args) for node, args in calls['runs']}
        self.assertIn(KEY, runs[c.node_id(2)])
        self.assertNotIn(KEY, runs[c.node_id(0)] + runs[c.node_id(3)])
        for path in (c.root / 'model-library').rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(errors='replace'), path)

    def test_inventory_the_pages_restart_stop_and_stop_all(self):
        c = self.cluster
        self.assert_started_on(self.start(self.spec, self.PLACE), self.spec, self.PLACE)
        inventory = json.loads(c.pulsar('inventory', '--json').stdout)
        (service,) = inventory['services']
        self.assertEqual((service['state'], service['ownership'], service['safe_to_stop']), ('running', 'managed', True))
        pages = c.pages(self.spec['spec_id'])
        self.assertIn(f"running on {c.hostname(2)}, {c.hostname(0)}, {c.hostname(3)}",
                      ' '.join(pages['screen'].split()))
        # The Connect card's address is the first node's own.
        self.assertIn(f"API       http://{self.address(2)}:8000/v1 · seen", pages['screen'])
        actions = [action for action, _ in pages['choices']]
        self.assertIn('restart', actions)
        self.assertIn('stop', actions)
        self.assertEqual(pages['page'].command(self.spec['spec_id'], 'restart'),
                         ['--restart', self.spec['spec_id'], '--node',
                          ','.join(c.node_id(p) for p in self.PLACE)])
        stopped = c.pulsar('stop', self.spec['spec_id'])
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn(f"Stopped spec {self.spec['spec_id'][:12]} on {c.hostname(2)} (rank 0), {c.hostname(0)} "
                      f"(rank 1), {c.hostname(3)} (rank 2)", stopped.stdout)
        self.assertEqual((c.remaining(), c.store.records('services')), ([], []))
        self.assertEqual(self.start(self.spec, self.PLACE).returncode, 0)
        self.assertEqual(c.pulsar('stop', '--all', '--yes').returncode, 0)
        self.assertEqual((c.remaining(), c.store.records('services')), ([], []))


class MeshRestartThroughTheMenu(RingHarness):
    """Restart from Running services…, through the real serve-menu.sh, of three
    ranks Start ran on named nodes of a saved four-node full mesh: the same
    nodes, in the same order. The serve menu's doubles stand in for ./pulsar."""
    MENUS = ('services-menu.sh', 'serve-menu.sh', 'cluster-review.sh', 'connect_card.py')
    MENU = 'services-menu.sh'

    def setUp(self):
        super().setUp()
        from scripts import topology_manifest as tm
        from support.topology_fixture import node_probe
        from tests.test_services_menu import service
        files = []
        for rank in range(4):
            probe = dict(node_probe(rank), rdma=[
                dict(hca='roce0', netdev='fabric0', cidrs=[f'198.51.100.{10 + rank}/24']),
                dict(hca='roce1', netdev='fabric1', cidrs=[f'203.0.113.{10 + rank}/24'])])
            path = self.root / f'mesh-probe-{rank}.json'
            path.write_text(json.dumps(probe))
            files.append(str(path))
        self.use_topology(tm.mark_verified(tm.assemble(files, None))['topology'])
        self.entries(recipe(3, home={'node_id': 'fixture-node-0', 'path': '/fixture/home',
                                     'verified_at': '2026-09-05T00:00:00Z'}))
        nodes = {f'k{index}': {'node_id': f'fixture-node-{index}', 'hostname': f'fixture-rank-{index}',
                               'confirmed': True, 'probe_status': 'ok', 'control_ip': f'192.0.2.{10 + index}',
                               'local': index == 0} for index in range(4)}
        self.inventory(dict(ring_inventory(), nodes=nodes, services=[service(MENU_SPEC, keys=['k2', 'k0', 'k3'])]))
        (self.fixtures / 'stop.inventory').write_text(json.dumps(dict(ring_inventory(), nodes=nodes)))

    def test_restart_starts_it_again_on_the_same_nodes_in_the_same_order(self):
        result = self.run_menu(['Restart…', 'Go ahead', 'Back', 'Back'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ['Restart a service', 'Cancel', 'Go ahead', 'Details'])
        placement = '--placement-nodes fixture-rank-2,fixture-rank-0,fixture-rank-3'
        self.assertEqual(self.ran(), [f'stop {MENU_SPEC}', f'start {MENU_SPEC} {placement}'])
        self.assertIn('Restart org/model on fixture-rank-2, fixture-rank-0 and fixture-rank-3',
                      ' '.join(result.stdout.split()))


class StillRefused(Cluster):
    """Refused with the launch check's own words, or the decision's, and nothing contacted."""

    def refused(self, spec, positions, *words, names=None):
        c = self.cluster
        c.forget_calls()
        result = (c.start(spec['spec_id'], '--placement-nodes', names) if names
                  else self.start(spec, positions))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        for text in words:
            self.assertIn(text, ' '.join((result.stdout + result.stderr).split()))
        self.assertEqual(self.trace(), [])
        self.assertEqual(c.store.records('services'), [])
        return result

    def test_on_the_ring(self):
        c = self.cluster
        two, _ = c.spec(2)
        three, _ = c.spec(3, 1)
        loop, _ = c.spec(4, 2, ring=True)
        self.refused(two, [0, 2], f'{c.hostname(0)} and {c.hostname(2)} are not cabled to each other: the cluster '
                     'is a ring, and this recipe needs every one of its nodes cabled to the others')
        self.refused(three, [0, 1, 2], f'{c.hostname(0)} and {c.hostname(2)} are not cabled to each other')
        self.refused(loop, [0, 2, 1, 3], f'{c.hostname(0)} and {c.hostname(2)} are not cabled to each other: this '
                     'recipe passes data around a closed loop of its nodes, each cabled to the next and the last to '
                     'the first')

    def test_a_guarded_spec_and_a_node_outside_the_saved_cluster(self):
        c = self.cluster
        guarded, _ = c.spec(2, 1, guarded=True)
        two, _ = c.spec(2)
        self.refused(guarded, [2, 3], 'guard execution is not supported by ordinary start')
        self.refused(two, None, "placement node 'fixture-elsewhere' does not select exactly one confirmed node",
                     names=f'{c.hostname(2)},fixture-elsewhere')


if __name__ == '__main__':
    unittest.main()
