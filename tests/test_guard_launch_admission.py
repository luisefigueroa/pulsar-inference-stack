"""Actual launcher entrypoints refuse unsupported guards before side effects."""
import json
import copy
import subprocess
import unittest

from release_spec.tests.test_serving_guard import guarded, policy, GIB
from tests.test_container_runtime import fixture
from tests import test_diagnostics as diagnostics

ROOT = diagnostics.ROOT


class GuardLaunchAdmission(unittest.TestCase):
    def setUp(self):
        self.base = diagnostics.Diagnostics()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def test_guard_does_not_block_prelaunch_resource_inspection(self):
        spec = guarded(self.base.spec)
        self.base.path.write_text(json.dumps(spec))
        with open(self.base.env['BASH_ENV'], 'a') as stream:
            stream.write('\nload_cluster_topology() { echo READ_ONLY_TOPOLOGY >&2; exit 67; }\n')
        result = subprocess.run(['bash', str(ROOT / 'scripts/resources.sh'),
                                 '--spec-file', str(self.base.path), '--jsonl'],
                                env=self.base.env, cwd=ROOT, capture_output=True, text=True)
        # Stop at the topology double before any real transport or sampling.
        self.assertEqual(result.returncode, 67, result.stderr)
        self.assertIn('READ_ONLY_TOPOLOGY', result.stderr)
        self.assertNotIn('guard execution is not supported', result.stderr)
        self.assertFalse((self.base.root / 'mutations').exists())

    def test_guard_does_not_block_recorded_observation_admission(self):
        from model_library.state import Store
        from scripts import container_runtime, service_state
        spec = guarded(self.base.spec)
        prepared = copy.deepcopy(self.base.prepared)
        prepared['spec_id'] = spec['spec_id']
        for rank in prepared['ranks']:
            rank['spec_id'] = spec['spec_id']
        plan = container_runtime.build_plan(spec, spec['spec_id'], self.base.facts, prepared)
        service_state.save(Store(self.base.root / 'library'), plan)
        with open(self.base.env['BASH_ENV'], 'a') as stream:
            stream.write('\nrequire_profile_topology() { echo READ_ONLY_TOPOLOGY >&2; exit 67; }\n')
        env = {**self.base.env, 'PULSAR_MODEL_LIBRARY_DIR': str(self.base.root / 'library')}
        result = subprocess.run(['bash', str(ROOT / 'scripts/observe-serving.sh'),
                                 '--service-id', plan['service_id'], '--json'],
                                env=env, cwd=ROOT, capture_output=True, text=True)
        # Only admission is exercised; this is not a guarded execution fixture.
        self.assertEqual(result.returncode, 67, result.stderr)
        self.assertIn('READ_ONLY_TOPOLOGY', result.stderr)
        self.assertNotIn('guard execution is not supported', result.stderr)
        self.assertFalse((self.base.root / 'mutations').exists())

    def test_every_launcher_rejects_guard_before_pulls_or_replacement(self):
        for script, nodes, flags in [('scripts/up.sh', 3, ['--yes', '--pull-image', '--replace']),
                                     ('serve.sh', 1, ['--replace']),
                                     ('cluster/start-cluster.sh', 3, ['--replace'])]:
            with self.subTest(script=script):
                spec = guarded(fixture(nodes)[0])
                self.base.path.write_text(json.dumps(spec))
                result = subprocess.run(['bash', str(ROOT / script), spec['spec_id'], *flags],
                                        env=self.base.env, cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('guard execution is not supported', result.stderr)
                self.assertFalse((self.base.root / 'mutations').exists())

    def test_guard_added_by_override_is_also_rejected(self):
        spec = fixture(3)[0]
        self.base.path.write_text(json.dumps(spec))
        override = self.base.root / 'override.json'
        override.write_text(json.dumps({'container': {'guard': policy(),
            'memory_limit_bytes': 96 * GIB, 'network_mode': 'host',
            'restart_policy': 'no', 'restart_max_retries': 0, 'healthcheck': None}}))
        result = subprocess.run(['bash', str(ROOT / 'scripts/up.sh'), spec['spec_id'],
                                 '--override-file', str(override), '--yes', '--replace', '--pull-image'],
                                env=self.base.env, cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('guard execution is not supported', result.stderr)
        self.assertFalse((self.base.root / 'mutations').exists())


if __name__ == '__main__':
    unittest.main()
