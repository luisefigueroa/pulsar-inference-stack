"""Prepared copies hold one role per node per spec: a pair prepared on saved
ranks 2 and 3 in one order, then started in the other. Synthetic model
library fixture (tests/support/model_library_fixture.py); nothing contacts a
node."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tests/support')]
from model_library.state import Store  # noqa: E402
from model_library_fixture import Fixture as LibraryFixture  # noqa: E402


class PreparedOrder(unittest.TestCase):
    def setUp(self):
        self.f = LibraryFixture(Path(self.enterContext(tempfile.TemporaryDirectory())), 4)
        self.assertEqual(self.f.acquire(0).returncode, 0)
        self.f.candidate(2)
        result = self.f.run('prepare', '--placement-nodes', 'node-2,node-3', '--yes', spec=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_other_order_needs_prepare_again(self):
        f = self.f
        result = f.run('check', '--placement-nodes', 'node-3,node-2', spec=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('required prepared copy is missing or placement changed', result.stdout)
        # Prepare in the new order rebinds each node's copy to its new rank; nothing is copied.
        plan = json.loads(f.run('prepare', '--placement-nodes', 'node-3,node-2', '--plan', spec=True).stdout)
        self.assertEqual([(a['node_id'], a['action'], a['rank']) for a in plan['actions']],
                         [('node-3', 'rebind', 0), ('node-2', 'rebind', 1)])
        self.assertEqual(f.run('prepare', '--placement-nodes', 'node-3,node-2', '--yes', spec=True).returncode, 0)
        self.assertEqual(f.run('check', '--placement-nodes', 'node-3,node-2', spec=True).returncode, 0)

    def test_prepare_again_is_blocked_while_a_container_mounts_the_copy(self):
        f = self.f
        view = next(v for v in Store(f.state).views(spec_id=f.spec['spec_id']) if v['node_id'] == 'node-2')
        node = next(n for n in f.cfg['nodes'] if n['node_id'] == 'node-2')
        node['containers'] = [{'Id': 'serving-pair', 'State': {'Running': True}, 'Config': {'Labels': {}},
                               'Mounts': [{'Source': view['path']}]}]
        f.save()
        result = f.run('prepare', '--placement-nodes', 'node-3,node-2', '--yes', spec=True)
        self.assertNotEqual(result.returncode, 0)
        plan = json.loads(result.stdout)
        self.assertFalse(plan['eligible'])
        (detail,) = plan['blocker_details']
        self.assertEqual((detail['code'], detail['container']['id'], detail['fix']),
                         ('container-reference', 'serving-pair', 'remove the container or its mount; Pulsar never '
                          'removes it'))
        self.assertEqual(plan['blockers'], ['node-2: existing prepared copy is pinned or referenced'])


class StartInTheOtherOrder(unittest.TestCase):
    """Start's own refusal says what to do: prepare again in this order, and
    stop the service first if it still runs on the copies."""

    def test_the_blocker_names_prepare_in_this_order_and_stop(self):
        from support.service_cluster import ServiceCluster
        c = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))
        spec, _ = c.spec(2)
        names = f'{c.hostname(3)},{c.hostname(2)}'
        result = c.start(spec['spec_id'], '--skip-preflight', '--placement-nodes', names, SERVICE_WEIGHTS_RC='1')
        self.assertEqual(result.returncode, 1)
        text = ' '.join(result.stdout.split())
        self.assertIn('BLOCKED model_files_not_ready: model files are not prepared on every serving rank (see the '
                      f"model-files check above). Next: ./pulsar model prepare {spec['spec_id'][:12]} "
                      f'--placement-nodes {names} --yes', text)
        self.assertIn("Each node's prepared copy serves one rank. Copies prepared for these nodes in another order "
                      'are prepared again for this order by the command above; if a service of this spec still runs '
                      f"on them, stop it first: ./pulsar stop {spec['spec_id'][:12]}", text)


if __name__ == '__main__':
    unittest.main()
