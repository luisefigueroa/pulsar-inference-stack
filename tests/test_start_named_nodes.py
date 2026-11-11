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


if __name__ == '__main__':
    unittest.main()
