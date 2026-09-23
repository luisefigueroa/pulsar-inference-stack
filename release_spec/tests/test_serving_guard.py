"""Guard policy identity and schema admission without a serving implementation."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from release_spec import serving
from release_spec.serving_guard import validate

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/contracts'
GIB = 1024**3


def policy(version=2):
    value = {'schema_version': version, 'program_sha256': 'a' * 64,
             'entrypoint': ['python3', '/opt/example/entrypoint.py'],
             'min_host_available_bytes': 24 * GIB,
             'startup_timeout_seconds': 7200, 'timeout_seconds': 18000}
    if version == 2:
        value['max_host_swap_growth_bytes'] = 256 * 1024**2
    return value


def guarded(spec, version=2):
    return serving.apply_overrides(spec, {'container': {
        'memory_limit_bytes': 96 * GIB, 'network_mode': 'host',
        'restart_policy': 'no', 'restart_max_retries': 0,
        'healthcheck': None, 'guard': policy(version),
    }})


class ServingGuardSchema(unittest.TestCase):
    def setUp(self):
        self.original = json.loads((FIXTURES / 'spec.json').read_text())
        self.spec = guarded(self.original)

    def test_absent_guard_preserves_existing_golden_identity(self):
        draft = json.loads((FIXTURES / 'draft.json').read_text())
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        self.assertEqual(serving.freeze(draft, manifest), self.original)
        self.assertNotIn('guard', serving.verify_spec(self.original)['recipe']['container'])

    def test_both_versions_round_trip_without_mutating_input(self):
        for version in (1, 2):
            with self.subTest(version=version):
                spec = guarded(self.original, version)
                before = copy.deepcopy(spec)
                self.assertEqual(serving.verify_spec(spec), before)
                self.assertEqual(spec, before)
                self.assertNotEqual(spec['spec_id'], self.original['spec_id'])

    def test_guard_changes_bind_identity_and_tampering_is_rejected(self):
        for key, value in [('program_sha256', 'b' * 64), ('entrypoint', ['engine']),
                           ('min_host_available_bytes', 25 * GIB),
                           ('startup_timeout_seconds', 7000), ('timeout_seconds', 17000),
                           ('max_host_swap_growth_bytes', 0)]:
            with self.subTest(key=key):
                changed = serving.apply_overrides(self.spec, {'container': {'guard': {key: value}}})
                self.assertNotEqual(changed['spec_id'], self.spec['spec_id'])
                tampered = copy.deepcopy(self.spec)
                tampered['recipe']['container']['guard'][key] = value
                with self.assertRaises(ValueError):
                    serving.verify_spec(tampered)

    def test_schema_three_preserves_required_snapshot_binding(self):
        from release_spec.normalize import snapshot_manifest_id
        draft = json.loads((FIXTURES / 'draft.json').read_text())
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        second = copy.deepcopy(manifest)
        second['snapshot_revision'] = 'e' * 40
        second['manifest_id'] = snapshot_manifest_id(second)
        draft['schema_version'] = 2
        draft['recipe']['container'] = copy.deepcopy(self.spec['recipe']['container'])
        draft['recipe']['required_snapshots'] = {'draft': {
            'model_id': second['model_id'], 'model_commit': second['snapshot_revision']}}
        draft['recipe']['engine_args'] += ['--speculative_config.model', 'pulsar-snapshot:draft']
        spec = serving.freeze(draft, {'target': manifest, 'draft': second})
        self.assertEqual(spec['schema_version'], 3)
        self.assertEqual(serving.verify_spec(spec), spec)
        self.assertEqual(spec['recipe']['required_snapshots']['draft']['snapshot_manifest'], second)

    def test_closed_shape_and_versions(self):
        candidates = [None, [], {}, {**policy(), 'unknown': 1},
                      {**policy(), 'schema_version': True}, {**policy(), 'schema_version': 3},
                      {**policy(), 'schema_version': 1}]
        missing = policy(); missing.pop('max_host_swap_growth_bytes'); candidates.append(missing)
        for value in candidates:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value, self.spec['recipe']['container'])

    def test_limits_reject_booleans_floats_and_out_of_range_values(self):
        ranges = {'min_host_available_bytes': (8 * GIB, 128 * GIB),
                  'startup_timeout_seconds': (10, 14400),
                  'timeout_seconds': (10, 21600),
                  'max_host_swap_growth_bytes': (0, 256 * 1024**2)}
        for key, (low, high) in ranges.items():
            for value in (True, 1.5, low - 1, high + 1):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate({**policy(), key: value}, self.spec['recipe']['container'])
        for value in (0, 256 * 1024**2):
            self.assertEqual(validate({**policy(), 'max_host_swap_growth_bytes': value},
                                      self.spec['recipe']['container'])['max_host_swap_growth_bytes'], value)
        with self.assertRaisesRegex(ValueError, 'startup limit exceeds'):
            validate({**policy(), 'timeout_seconds': 6000}, self.spec['recipe']['container'])

    def test_program_and_entrypoint_validation(self):
        for value in ('', 'g' * 64, 'a' * 63, 0):
            with self.subTest(hash=value), self.assertRaises(ValueError):
                validate({**policy(), 'program_sha256': value}, self.spec['recipe']['container'])
        for value in ([], 'engine', [''], [1], ['bad\nentrypoint'], ['a' * 1025], ['a'] * 17):
            with self.subTest(entrypoint=value), self.assertRaises(ValueError):
                validate({**policy(), 'entrypoint': value}, self.spec['recipe']['container'])

    def test_container_requirements(self):
        for changes in ({'memory_limit_bytes': 0}, {'memory_limit_bytes': 113 * GIB},
                        {'network_mode': 'bridge'}, {'restart_policy': 'always'},
                        {'healthcheck': {'path': '/health'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate(policy(), {**self.spec['recipe']['container'], **changes})

    def test_entrypoint_rejects_all_unicode_control_characters(self):
        controls = [*range(0x20), *range(0x7f, 0xa0)]
        for version in (1, 2):
            with self.subTest(version=version):
                accepted = []
                for point in controls:
                    value = {**policy(version), 'entrypoint': ['engine' + chr(point)]}
                    try:
                        validate(value, self.spec['recipe']['container'])
                    except ValueError:
                        continue
                    accepted.append(f'U+{point:04X}')
                self.assertEqual(accepted, [])

    def test_entrypoint_preserves_non_control_unicode(self):
        entrypoint = ['python3', '/opt/模型/入口.py', 'résumé', '😀']
        changed = serving.apply_overrides(self.spec, {'container': {'guard': {'entrypoint': entrypoint}}})
        self.assertEqual(serving.verify_spec(changed)['recipe']['container']['guard']['entrypoint'], entrypoint)

    def test_catalog_admits_guarded_spec_without_execution_support(self):
        with tempfile.TemporaryDirectory() as temp:
            releases = Path(temp) / 'releases'; releases.mkdir()
            path = releases / (self.spec['spec_id'] + '.json')
            path.write_text(json.dumps(self.spec))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/check-catalog.py'),
                                     '--repo-root', temp, '--json'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['verified'])
            compatibility = subprocess.run([sys.executable, str(ROOT / 'scripts/check-launch-compatibility.py'),
                                            '--spec', str(path), '--json'], capture_output=True, text=True)
            self.assertNotEqual(compatibility.returncode, 0)
            self.assertIn('guard execution is not supported', compatibility.stderr)


if __name__ == '__main__':
    unittest.main()
