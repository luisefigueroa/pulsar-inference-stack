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


if __name__ == '__main__':
    unittest.main()
