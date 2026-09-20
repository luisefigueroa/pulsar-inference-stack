"""Focused synthetic proof of the supervised development journal route."""
import base64
import copy
import hashlib
import json
import os
import signal
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_diagnostic_lifecycle import Integrated
from tests.support import diagnostic_fixture as fixture
from scripts.diagnostic_run import validate_image_document
from scripts.diagnostic_runtime import environment, docker_create_argv
from release_spec.diagnostic import verify_definition


def image_metadata():
    config = {'os': 'linux', 'architecture': 'arm64',
              'config': {'Env': ['PATH=/usr/local/bin:/usr/bin:/bin', 'LD_LIBRARY_PATH=/opt/image/lib'],
                         'User': '', 'Volumes': None},
              'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + '3' * 64, 'sha256:' + '4' * 64]}}
    config_bytes = json.dumps(config).encode()
    config_digest = 'sha256:' + hashlib.sha256(config_bytes).hexdigest()
    manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'config': {'mediaType': 'application/vnd.oci.image.config.v1+json',
                           'digest': config_digest, 'size': len(config_bytes)}}
    manifest_bytes = json.dumps(manifest).encode()
    digest = 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()
    evidence = {'manifest_base64': base64.b64encode(manifest_bytes).decode(),
                'config_base64': base64.b64encode(config_bytes).decode()}
    image = {'Id': digest, 'Descriptor': {'mediaType': manifest['mediaType'], 'digest': digest, 'size': len(manifest_bytes)},
             'Os': 'linux', 'Architecture': 'arm64', 'RootFS': {'Type': 'layers', 'Layers': config['rootfs']['diff_ids']},
             'Config': config['config']}
    return evidence, image, config_digest


class JournalTrial(Integrated):
    def setUp(self):
        super().setUp()
        evidence, image, config_digest = image_metadata()
        self.definition.update(schema_version=2, image_reference=image['Id'], image_id=image['Id'],
                               image_config_digest=config_digest, image_evidence=evidence,
                               environment={'HOME': '/tmp', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.definition['observer']['backend'] = 'journal'
        self.configure(image_document=image)
        (self.root / 'journal-records.jsonl').write_text(json.dumps(fixture.journal_record(0, 'synthetic anchor')) + '\n')
        binary = self.root / 'topo/bin/journalctl'
        binary.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ROOT)!r})\nfrom tests.support.diagnostic_fixture import journal_main\nraise SystemExit(journal_main())\n')
        inputs = self.root / 'inputs'
        (inputs / 'provenance.py').write_text('import sys,time\ntime.sleep(0.8)\nprint("provenance verified")\nsys.exit(int(sys.argv[1]))\n')
        (inputs / 'step.py').write_text('import json,pathlib,sys,time\ntime.sleep(0.8)\npathlib.Path(sys.argv[1]).write_text(json.dumps({"cases":8,"synthetic":True,"relative_l2":0.005}))\nprint("fixture complete")\n')
        self.definition['inputs'] = [{'name': p.name, 'bytes': p.stat().st_size, 'mode': p.stat().st_mode & 0o777,
                                      'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(inputs.iterdir())]
        self.definition['steps'] = [
            {'argv': [sys.executable, '-I', '-B', '/pulsar-check/provenance.py', '0'], 'timeout_seconds': 5, 'capture_file': None},
            {'argv': [sys.executable, '-I', '-B', '/pulsar-check/step.py', '/tmp/result.json'], 'timeout_seconds': 5, 'capture_file': '/tmp/result.json'}]
        self.save_definition()

    def save_definition(self):
        (self.root / 'definition.json').write_text(json.dumps(self.definition))

    def assert_journal_closed(self, result):
        self.assertTrue(result['observer_closure']['waited'], result)
        self.assertFalse(result['observer_closure']['forced'], result)
        self.assertTrue(result['observer_closure']['clients_closed'], result)
        closure = json.loads((self.attempt / 'journal-final-ready.json').read_text())
        self.assertTrue(closure['identity_verified'], closure)
        self.assertTrue(closure['waited'], closure)
        self.assertFalse(closure['forced'], closure)
        self.assertEqual(closure['exit_code'], 0, closure)
        calls = [json.loads(line) for line in (self.root / 'journal-calls.jsonl').read_text().splitlines()]
        follow = next(row for row in calls if '--follow' in row['argv'])
        create = next(row for row in (json.loads(line) for line in (self.root / 'docker-calls.jsonl').read_text().splitlines()) if row['argv'][0] == 'create')
        self.assertLess(follow['ns'], create['ns'])
        self.assertGreater(closure['query_begin_ns'], result['cleanup']['completed_monotonic_ns'])

    def test_healthy_journal_and_tmpfs_capture(self):
        result = self.run_attempt()
        self.assertEqual(result['outcome'], 'succeeded', result)
        self.assert_journal_closed(result)
        lines = (self.attempt / 'logs.stdout').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(b'provenance verified', base64.b64decode(lines[0].split(' ')[4]))
        detail = base64.b64decode(lines[1].split(' ')[4]).decode().split('PULSAR_OUTPUT_JSON\n')[1]
        self.assertEqual(json.loads(detail), {'cases': 8, 'synthetic': True, 'relative_l2': 0.005})
        self.assertFalse(result['observation']['journal_delivery_lossless'])
        self.assertTrue(result['observation']['journal_query_complete'])

    def test_cleanup_queued_driver_error_retains_failure(self):
        self.configure(journal_queue_until_final=True, journal_on_remove=['synthetic queued'] * 3 + ['NVRM: Xid (PCI:0000:00:00): 13'])
        result = self.run_attempt()
        self.assertEqual(result['outcome'], 'failed_clean', result)
        self.assert_journal_closed(result)
        self.assertEqual(result['observation']['kernel_records'], 4)
        self.assertEqual(result['observation']['kernel_faults'], 1)
        self.assertIn('Xid', result['observation']['first_safety_failure'])
        self.assertTrue(result['cleanup']['absent'])
        self.assertTrue(result['observation']['journal_query_complete'])
        events = [json.loads(line) for line in (self.attempt / 'observer.jsonl').read_text().splitlines()]
        fault = next(row for row in events if row.get('record', {}).get('message', '').startswith('NVRM'))
        self.assertEqual(fault['phase'], 'draining')

    def test_provenance_failure_bars_fixture(self):
        self.definition['steps'][0]['argv'][-1] = '9'
        self.save_definition()
        result = self.run_attempt()
        self.assertEqual(result['outcome'], 'failed_clean', result)
        self.assert_journal_closed(result)
        self.assertEqual([step['exit_code'] for step in result['workload']['steps']], [9])
        self.assertFalse((self.root / 'scratch/result.json').exists())

    def test_signal_closes_owned_journal_and_container(self):
        child = self.launch()
        self.wait_for(lambda: (self.attempt / 'observer-status.json').exists() and
                      json.loads((self.attempt / 'observer-status.json').read_text()).get('cgroup_samples', 0) > 0, child)
        os.kill(child.pid, signal.SIGTERM)
        result = self.finish(child)
        self.assertNotEqual(result['outcome'], 'succeeded', result)
        self.assertEqual(sum(call[0] == 'start' for call in self.calls()), 1)
        self.assertEqual(sum(call[0] == 'stop' for call in self.calls()), 1)
        self.assertTrue(result['cleanup']['absent'], result)
        self.assert_journal_closed(result)


class Admission(unittest.TestCase):
    def test_manifest_environment_and_mismatch(self):
        evidence, image, config_digest = image_metadata()
        from tests.test_diagnostic_container import definition
        doc = definition([{'name': 'synthetic.py', 'bytes': 1, 'mode': 420, 'sha256': '0' * 64}])
        doc.update(schema_version=2, image_reference=image['Id'], image_id=image['Id'], image_config_digest=config_digest,
                   image_evidence=evidence, environment={'HOME': '/tmp'})
        for step in doc['steps']: step['capture_file'] = None
        verify_definition(doc)
        plan = {'definition': doc}
        self.assertTrue(validate_image_document(image, plan)['ok'])
        self.assertNotIn('ConfigDigest', image)
        self.assertEqual(environment(plan)['LD_LIBRARY_PATH'], '/opt/image/lib')
        self.assertEqual(environment(plan)['PATH'], '/usr/local/bin:/usr/bin:/bin')
        wrong = copy.deepcopy(image)
        wrong['RootFS']['Layers'].reverse()
        with self.assertRaisesRegex(ValueError, 'ordered rootfs'):
            validate_image_document(wrong, plan)


def load_tests(loader, tests, pattern):
    # Reuse the lifecycle fixture without replaying its unchanged test matrix.
    return unittest.TestSuite([loader.loadTestsFromTestCase(Admission), *[
        JournalTrial(name) for name in JournalTrial.__dict__ if name.startswith('test_')]])


if __name__ == '__main__':
    unittest.main()
