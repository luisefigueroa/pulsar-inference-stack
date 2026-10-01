"""Actual launcher entrypoints refuse unsupported guards before side effects."""
import contextlib
import io
import json
import copy
import os
import subprocess
import unittest
from unittest.mock import patch

from release_spec.tests.test_serving_guard import guarded, policy, GIB
from scripts import public_cli, start_blockers
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
        from serving_guard.program import digest, program
        from release_spec import serving
        spec = serving.apply_overrides(spec, {'container': {'guard': {
            **spec['recipe']['container']['guard'], 'program_sha256': digest(program())}}})
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

    def launch(self, script, spec_id, *flags):
        blockers = self.base.root / 'blockers.jsonl'
        blockers.unlink(missing_ok=True)
        result = subprocess.run(['bash', str(ROOT / script), spec_id, *flags],
                                env={**self.base.env, 'PULSAR_START_BLOCKERS_FILE': str(blockers)},
                                cwd=ROOT, capture_output=True, text=True)
        return result, start_blockers.read(blockers)

    def test_every_launcher_rejects_guard_before_pulls_or_replacement(self):
        for script, nodes, flags in [('scripts/up.sh', 3, ['--yes', '--pull-image', '--replace']),
                                     ('serve.sh', 1, ['--replace']),
                                     ('cluster/start-cluster.sh', 3, ['--replace'])]:
            with self.subTest(script=script):
                spec = guarded(fixture(nodes)[0])
                self.base.path.write_text(json.dumps(spec))
                result, recorded = self.launch(script, spec['spec_id'], *flags)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('guard execution is not supported', result.stderr)
                self.assertIn(spec['spec_id'][:12], result.stderr)
                self.assertEqual(result.stdout.count('BLOCKED '), 1, result.stdout)
                self.assertIn('BLOCKED guard_unsupported: this spec requires serving-guard enforcement '
                              '(recipe.container.guard), which ordinary start cannot enforce. Note: Use an explicitly '
                              'scoped pulsar guarded run; see docs/SERVING_GUARD_SCHEMA.md.', result.stdout)
                self.assertEqual(recorded, [start_blockers.blocker('guard_unsupported')])
                self.assertFalse((self.base.root / 'mutations').exists())

    def test_start_json_returns_the_guard_blocker_in_details(self):
        spec = guarded(fixture(3)[0])
        self.base.path.write_text(json.dumps(spec))
        output = io.StringIO()
        with patch.dict(os.environ, self.base.env, clear=True), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            status = public_cli.main(['start', spec['spec_id'], '--yes', '--json'])
        error = json.loads(output.getvalue())['error']
        self.assertEqual(status, 3)
        self.assertEqual(error['code'], 'prerequisite_failed')
        self.assertEqual(error['details'], [start_blockers.blocker('guard_unsupported')])
        self.assertIn('guard execution is not supported', error['message'])
        self.assertFalse((self.base.root / 'mutations').exists())

    def test_unguarded_spec_passes_launch_admission(self):
        spec = fixture(3)[0]
        self.base.path.write_text(json.dumps(spec))
        # The topology check is the first one after admission; start silences
        # its output, so the double leaves a marker instead.
        marker = self.base.root / 'past-admission'
        with open(self.base.env['BASH_ENV'], 'a') as stream:
            stream.write(f'\nrequire_cluster_nodes() {{ touch {str(marker)!r}; exit 67; }}\n')
        result, recorded = self.launch('scripts/up.sh', spec['spec_id'], '--yes')
        self.assertEqual(result.returncode, 67, result.stderr)
        self.assertTrue(marker.exists(), result.stderr)
        self.assertEqual(recorded, [])

    def test_guarded_dry_run_reaches_readonly_prerequisites(self):
        spec = guarded(fixture(3)[0])
        self.base.path.write_text(json.dumps(spec))
        marker = self.base.root/'guarded-planning'
        with open(self.base.env['BASH_ENV'], 'a') as stream:
            stream.write(f'\nrequire_cluster_nodes() {{ touch {str(marker)!r}; exit 67; }}\n')
        result, recorded = self.launch('scripts/up.sh', spec['spec_id'], '--dry-run')
        self.assertEqual(result.returncode, 67, result.stderr)
        self.assertTrue(marker.exists(), result.stderr)
        self.assertEqual(recorded, [])
        self.assertFalse((self.base.root/'mutations').exists())

    def test_guard_added_by_override_is_also_rejected(self):
        spec = fixture(3)[0]
        self.base.path.write_text(json.dumps(spec))
        override = self.base.root / 'override.json'
        override.write_text(json.dumps({'container': {'guard': policy(),
            'memory_limit_bytes': 96 * GIB, 'network_mode': 'host',
            'restart_policy': 'no', 'restart_max_retries': 0, 'healthcheck': None}}))
        result, recorded = self.launch('scripts/up.sh', spec['spec_id'], '--override-file', str(override),
                                       '--yes', '--replace', '--pull-image')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('guard execution is not supported', result.stderr)
        self.assertEqual([row['blocker'] for row in recorded], ['guard_unsupported'])
        self.assertIn('(added by --override-file)', recorded[0]['message'])
        self.assertFalse((self.base.root / 'mutations').exists())


if __name__ == '__main__':
    unittest.main()
