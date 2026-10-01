"""Synthetic selected-node warm proof and public guarded admission checks."""
import copy
import json
from pathlib import Path
import re
import subprocess
import unittest

from scripts import start_blockers
from tests import test_diagnostics as diagnostics
from tests.test_container_runtime import fixture
from tests.test_serving_guard import guarded_fixture

ROOT = Path(__file__).resolve().parents[1]


class GuardPlacementAdmission(unittest.TestCase):
    def setUp(self):
        self.base = diagnostics.Diagnostics()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.spec, _, self.prepared, _, _, _ = guarded_fixture(nodes=2)
        self.base.path.write_text(json.dumps(self.spec))
        # Restore the real warm-proof helper overridden by Diagnostics. All
        # transport and memory observations below remain synthetic.
        source = (ROOT / 'scripts/lib.sh').read_text()
        proof = re.search(r'^profile_service_is_proven_running\(\) \{\n.*?^\}\n',
                          source, re.M | re.S).group(0)
        with (self.base.root / 'env.sh').open('a') as stream:
            stream.write('\n' + proof + r'''
load_cluster_topology() {
 CLUSTER_TOPOLOGY_LOADED=1; CLUSTER_TOPOLOGY_ID=$(printf 'c%.0s' {1..64})
 CLUSTER_TOPOLOGY_COUNT=4; CLUSTER_TOPOLOGY_SSH_TRUSTED=1
 CLUSTER_NODE_IDS=(node-0 node-1 node-2 node-3)
 CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2 rank-3)
 CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2 alias-3)
 CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3 192.0.2.4)
 CLUSTER_NODE_CONTROL_IFS=(eth0 eth0 eth0 eth0)
 CLUSTER_PROFILE_HCAS=(mlx5_0 mlx5_0 mlx5_0 mlx5_0)
}
estimate_weights_ram_gib() { echo 80; }
estimate_kv_gib() { echo 1; }
mem_available_gib_local() { echo 16; }
mem_available_gib_remote() { echo 16; }
container_running_exact() { fixture_container running 0 "$1"; }
container_running_exact_remote() { fixture_container running "${1##*-}" "$2"; }
container_ownership_inspect_local() { fixture_container inspect 0 "$1"; }
container_ownership_inspect_remote() { fixture_container inspect "${1##*-}" "$2"; }
fixture_container() { python3 "$DIAG_ROOT/container.py" "$@"; }
library_hot_info_for_profile() { cat "$DIAG_ROOT/prepared.json"; }
port_free() { return 0; }
''')
        (self.base.root / 'container.py').write_text('''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['DIAG_ROOT'])
action, index, name = sys.argv[1:]
with (root / 'container-probes').open('a') as stream:
    stream.write(action + ' ' + index + '\\n')
meta = json.loads((root / 'containers.json').read_text()).get(index)
if meta is None or meta['name'].lstrip('/') != name:
    raise SystemExit(3 if action == 'inspect' else 1)
if action == 'running':
    raise SystemExit(0 if meta['running'] else 1)
print(json.dumps(meta))
''')
        for rank, physical in enumerate((2, 3)):
            self.prepared['ranks'][rank]['node_id'] = f'node-{physical}'
        (self.base.root / 'prepared.json').write_text(json.dumps(self.prepared))
        self.containers = {}
        self.write_containers()

    def write_containers(self):
        (self.base.root / 'containers.json').write_text(json.dumps(self.containers))

    def service(self, placement, *, single=False):
        for rank, physical in enumerate(placement):
            self.containers[str(physical)] = {
                'id': f'container-{physical}',
                'name': ('/vllm-' if single else '/vllm-cluster-') + self.spec['spec_id'],
                'running': True,
                'labels': {
                    'io.pulsar.gb10.managed': 'true',
                    'io.pulsar.gb10.conf': self.spec['spec_id'],
                    'io.pulsar.gb10.rank': 'single' if single else str(rank),
                    'io.pulsar.gb10.node-id': f'node-{physical}',
                    'io.pulsar.gb10.topology': 'c' * 64,
                },
            }
        self.write_containers()

    def memory(self, placement='node-2,node-3', *extra):
        args = [self.spec['spec_id'], '--json']
        if placement:
            args += ['--placement-nodes', placement]
        result = self.base.run_tool('check-memory.sh', [*args, *extra])
        self.assertIn(result.returncode, (0, 1, 2), result.stderr)
        return result, json.loads(result.stdout)

    def test_identical_default_service_does_not_exempt_idle_selected_nodes(self):
        self.service((0, 1))
        result, report = self.memory()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report['mode'], 'cold-start')
        self.assertFalse(report['already_loaded'])
        self.assertIn('cannot fit', report['reason'])
        self.assertEqual((self.base.root / 'container-probes').read_text(), 'running 2\n')

    def test_public_dry_run_blocks_insufficient_memory_on_idle_selected_nodes(self):
        self.service((0, 1))
        blockers = self.base.root / 'blockers.jsonl'
        result = subprocess.run([
            str(ROOT / 'pulsar'), 'start', self.spec['spec_id'],
            '--spec-file', str(self.base.path), '--placement-nodes', 'node-2,node-3', '--dry-run',
        ], env={**self.base.env, 'PULSAR_START_BLOCKERS_FILE': str(blockers),
                'PULSAR_MODEL_LIBRARY_DIR': str(self.base.root / 'library')},
            cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('memory_insufficient', [row['blocker'] for row in start_blockers.read(blockers)])
        self.assertIn('cannot fit', result.stdout)
        self.assertNotIn('already loaded', result.stdout)
        self.assertFalse((self.base.root / 'mutations').exists())

    def test_complete_selected_service_is_warm_including_remote_head_local_worker(self):
        for placement in ((2, 3), (2, 0), (3, 2), (0, 1)):
            with self.subTest(placement=placement):
                self.containers = {}
                self.service(placement)
                result, report = self.memory(','.join(f'node-{index}' for index in placement))
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertTrue(report['already_loaded'])
                self.assertEqual(report['mode'], 'already-loaded')

    def test_default_placement_and_forced_cold_admission_are_preserved(self):
        self.service((0, 1))
        result, report = self.memory('')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(report['already_loaded'])
        result, report = self.memory('', '--cold-start')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report['mode'], 'cold-start')

    def test_partial_stopped_or_mismatched_selected_rank_is_not_warm(self):
        self.service((2, 3))
        original = copy.deepcopy(self.containers)
        cases = (
            ('absent', None), ('stopped', None),
            ('io.pulsar.gb10.conf', 'f' * 64),
            ('io.pulsar.gb10.rank', '0'),
            ('io.pulsar.gb10.node-id', 'node-1'),
            ('io.pulsar.gb10.topology', 'd' * 64),
            ('io.pulsar.gb10.managed', 'false'),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self.containers = copy.deepcopy(original)
                if field == 'absent':
                    del self.containers['3']
                elif field == 'stopped':
                    self.containers['3']['running'] = False
                else:
                    self.containers['3']['labels'][field] = value
                self.write_containers()
                result, report = self.memory()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertFalse(report['already_loaded'])
                self.assertEqual(report['mode'], 'cold-start')

    def test_single_node_placement_still_uses_its_exact_node(self):
        self.spec, *_ = fixture(nodes=1)
        self.base.path.write_text(json.dumps(self.spec))
        for physical in (0, 2):
            with self.subTest(physical=physical):
                self.containers = {}
                self.service((physical,), single=True)
                result, report = self.memory('', '--node', f'node-{physical}')
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue(report['already_loaded'])
                self.assertEqual(report['placement']['node_id'], f'node-{physical}')


if __name__ == '__main__':
    unittest.main()
