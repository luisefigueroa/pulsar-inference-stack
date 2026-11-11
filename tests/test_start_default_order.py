"""Pins: an ordinary Start of two- and three-node recipes in the default node
order, through the real up.sh, launcher and their helpers, on a saved
four-node ring (three nodes: a full mesh) with Docker, SSH and curl doubles
(tests/support/service_cluster.py). Image, memory, model-file and
post-health checks are doubles that pass; these pins record where Start acts
and in which order, so that Start on named nodes leaves the default order
unchanged.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.service_cluster import ServiceCluster  # noqa: E402

PROBES = ['node-0 docker inspect by name', 'node-1 docker inspect by name over ssh']
CHECKED = PROBES + ['port 8000 on local', 'port 29500 on local']
# The launcher probes the remote ranks first, then this node, and rechecks the ports.
LAUNCHER_PROBES = ['node-1 docker inspect by name over ssh', 'node-0 docker inspect by name',
                   'port 8000 on local', 'port 29500 on local']
LAUNCH = ['node-1 docker run rank 1 over ssh', 'node-0 docker run rank 0']
SERVED = ['curl http://127.0.0.1:8000/health', 'curl http://127.0.0.1:8000/v1/completions']


class Scenario(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def trace(self):
        """Where Start acted, in order (Docker's read-only `info` left out)."""
        return [line for line in self.cluster.trace() if 'docker info' not in line]

    def lines(self, result, spec):
        text = (result.stdout + result.stderr).replace(spec['spec_id'], '<spec>')
        return [' '.join(line.split()) for line in text.splitlines()]


class DefaultOrder(Scenario):
    def test_two_nodes(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace(), CHECKED + LAUNCHER_PROBES + LAUNCH + SERVED)
        lines = self.lines(result, spec)
        for line in ('starting rank 1 on ring-bravo', 'starting rank 0 locally',
                     'waiting for http://127.0.0.1:8000/health (cold load can take ~10 min; up to 2 health checks, '
                     '0 s apart)', 'url: http://127.0.0.1:8000/v1', 'status: ./pulsar status <spec>',
                     'stop: ./pulsar stop <spec>'):
            self.assertIn(line, lines)
        (record,) = c.store.records('services')
        plan = c.store.get('service-plans', record['plan_id'])
        self.assertEqual([rank['node_id'] for rank in plan['ranks']], [c.node_id(0), c.node_id(1)])

    def test_three_nodes_on_a_mesh(self):
        c = self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())), mesh=3)
        spec, _ = c.spec(3)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace(), [
            'node-0 docker inspect by name', 'node-1 docker inspect by name over ssh',
            'node-2 docker inspect by name over ssh', 'port 8000 on local', 'port 29500 on local',
            'node-1 docker inspect by name over ssh', 'node-2 docker inspect by name over ssh',
            'node-0 docker inspect by name', 'port 8000 on local', 'port 29500 on local',
            'node-1 docker run rank 1 over ssh', 'node-2 docker run rank 2 over ssh', 'node-0 docker run rank 0',
            *SERVED])

    def test_an_existing_service_is_not_replaced(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        c.run(c.plan(spec, manifest, [0, 1]))
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace(), PROBES)
        self.assertIn('BLOCKED service_exists: a service for this spec already exists (container vllm-cluster-<spec> '
                      'on fixture-ring-dorado, fixture-ring-bravo). Next: ./pulsar status e51939b8d97d',
                      self.lines(result, spec))

    def test_replace(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        c.run(c.plan(spec, manifest, [0, 1]))
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight', '--replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace(), PROBES + [
            'node-1 docker inspect by name over ssh', 'node-0 docker inspect by name',
            # remove_stack_owned_cluster: probe, revalidate by ID, remove high to low, confirm each.
            'node-1 docker inspect by name over ssh', 'node-0 docker inspect by name',
            'node-1 docker inspect by id over ssh', 'node-0 docker inspect by id',
            'node-1 docker rm by id over ssh', 'node-1 docker inspect by id over ssh',
            'node-0 docker rm by id', 'node-0 docker inspect by id',
            'port 8000 on local', 'port 29500 on local'] + LAUNCH + SERVED)

    def test_a_failed_launch_removes_what_it_started_and_keeps_the_record(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_RUN_FAILS=c.node_id(0))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace(), CHECKED + LAUNCHER_PROBES + [
            'node-1 docker run rank 1 over ssh', 'node-0 docker run fails', 'node-1 docker rm by id over ssh',
            'node-0 docker state', 'node-1 docker state over ssh'])
        lines = self.lines(result, spec)
        self.assertIn('FAILED container_start_failed: fixture-ring-dorado (rank 0): the service container could not '
                      'be started. Next: ./pulsar doctor', lines)
        # The plan and record are saved just before the first docker run; a
        # failed launch removes its containers and keeps the record.
        self.assertEqual((len(c.store.records('services')), len(c.remaining())), (1, 0))
        # A retry on the same nodes starts.
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual((result.returncode, len(c.store.records('services')), len(c.remaining())), (0, 1, 2))

    def test_a_health_timeout_removes_both_ranks_and_keeps_the_record(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_HEALTHY='none')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace(), CHECKED + LAUNCHER_PROBES + LAUNCH + [
            'curl http://127.0.0.1:8000/health', 'node-0 docker state', 'node-1 docker state over ssh',
            'curl http://127.0.0.1:8000/health', 'node-0 docker state', 'node-1 docker state over ssh',
            'node-0 docker logs', 'node-1 docker logs over ssh',
            'node-0 docker rm by id', 'node-1 docker rm by id over ssh',
            'node-0 docker state', 'node-1 docker state over ssh'])
        self.assertIn("FAILED health_timeout: fixture-ring-dorado (rank 0): the service did not become healthy in time. "
                      "Note: The cluster's containers were removed; the logs are shown above.",
                      self.lines(result, spec))
        self.assertEqual((len(c.store.records('services')), len(c.remaining())), (1, 0))


if __name__ == '__main__':
    unittest.main()
