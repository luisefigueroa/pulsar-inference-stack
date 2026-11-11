"""An ordinary Start of a multi-node recipe on any nodes the operator names
that are saved members cabled as the recipe needs: pairs and three nodes of a
full mesh, the first node remote and this machine a headless rank or not in
the placement at all, and a recipe that passes data around a loop on the
whole ring in a rotated order. Through the real up.sh, preflight and launcher
with Docker, SSH and curl doubles (tests/support/service_cluster.py); nothing
here contacts a node."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from scripts.container_runtime import service_identifier  # noqa: E402
from support.service_cluster import ServiceCluster  # noqa: E402


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
