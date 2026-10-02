"""Synthetic public reconciliation: immutable history and no container mutations."""
import copy
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from model_library.state import Store
from model_library.verification_process import process_identity
from release_spec.normalize import canonical_json_digest
from scripts import service_state
from serving_guard import reconciliation
from tests.test_serving_guard import guarded_fixture

ROOT = Path(__file__).resolve().parents[1]


class GuardReconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installation = self.root / 'stack'
        self.installation.mkdir()
        for package in ('model_library', 'release_spec', 'serving_guard', 'scripts'):
            shutil.copytree(ROOT / package, self.installation / package,
                            ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT / 'pulsar', self.installation / 'pulsar')
        self.output = self.root / 'completed'
        self.output.mkdir()
        self.store = Store(self.root / 'state')
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                    'PULSAR_MODEL_LIBRARY_DIR': str(self.store.root),
                    'PULSAR_TEST_ROOT': str(self.root)}
        for key in ('BASH_ENV', 'PULSAR_VERIFICATION_OWNER', 'PULSAR_VERIFICATION_REPORT',
                    'PULSAR_VERIFICATION_REPORT_FD', 'PULSAR_MODEL_LIBRARY_LOCK_FD'):
            self.env.pop(key, None)
        self.docker = self.root / 'docker-double'
        self.docker.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['PULSAR_TEST_ROOT'])
node=os.environ.get('PULSAR_TEST_NODE','0')
with (root/'probes').open('a') as out:out.write(json.dumps([node,*sys.argv[1:]])+'\\n')
if sys.argv[1:6]!=['container','ls','-aq','--no-trunc','--filter']:raise SystemExit(97)
if node==os.environ.get('UNKNOWN_NODE'):raise SystemExit(2)
if node==os.environ.get('OCCUPIED_NODE'):print('f'*64)
''')
        self.docker.chmod(0o700)
        self.env['PULSAR_DOCKER'] = str(self.docker)
        (self.installation / 'scripts' / 'lib.sh').write_text('''
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { echo "$*" >&2; exit 2; }
acquire_model_library_lifecycle_lock() {
 mkdir -p "$PULSAR_MODEL_LIBRARY_DIR"
 exec {test_lock}>"$PULSAR_MODEL_LIBRARY_DIR/lifecycle.lock"
 flock -x "$test_lock"
}
reload_cluster_topology() {
 CLUSTER_TOPOLOGY_COUNT=3
 CLUSTER_TOPOLOGY_ID=${PULSAR_TEST_TOPOLOGY_ID:-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc}
 if [ "${CHANGED_AFTER_PROBES:-0}" = 1 ] && [ -f "$PULSAR_TEST_ROOT/probes" ]; then
  CLUSTER_TOPOLOGY_ID=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
 fi
 CLUSTER_NODE_IDS=(node-0 node-1 node-2)
 CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2)
 CLUSTER_NODE_SSH_HOSTS=(local rank-1 rank-2)
 CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3)
 CLUSTER_NODE_CONTROL_IFS=(eth0 eth0 eth0)
 [ "${CHANGED_MAPPING:-0}" != 1 ] || CLUSTER_NODE_CONTROL_IPS[2]=192.0.2.99
}
require_topology_ssh_trust() { [ "${UNKNOWN_TRUST:-0}" != 1 ]; }
load_cluster_topology() { reload_cluster_topology; }
shell_join_q() { printf '%q ' "$@"; }
ssh_node() {
 local test_node="$1"; shift
 [ "$test_node" != "${UNKNOWN_SSH_NODE:-}" ] || return 2
 local test_command="${1/#docker/$PULSAR_DOCKER}"
 PULSAR_TEST_NODE="$test_node" bash -c "$test_command"
}
''')
        self.make_session()

    def put(self, name, value):
        path = self.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def make_session(self, placement=(0, 1), *, status='stopped'):
        spec, facts, prepared, _, *_ = guarded_fixture(nodes=len(placement))
        for rank, physical in enumerate(placement):
            facts['ranks'][rank].update(node_id=f'node-{physical}', hostname=f'rank-{physical}',
                ssh_host='local' if physical == 0 else f'rank-{physical}',
                control_ip=f'192.0.2.{physical+1}')
            prepared['ranks'][rank]['node_id'] = f'node-{physical}'
        from scripts.container_runtime import build_plan
        original = build_plan(spec, spec['spec_id'], facts, prepared)
        plan = {**original, 'lifecycle_action': 'start'}
        plan['plan_id'] = canonical_json_digest({k: v for k, v in plan.items() if k != 'plan_id'})
        self.plan = plan
        self.put('spec.json', spec)
        self.put('plan.json', original)
        self.put('active-plan.json', plan)
        self.put('context.json', {'plan': plan, 'ranks': plan['ranks'], 'guard_files': {}})
        self.put('controller.json', {'run_id': plan['guard_run_id'], 'owner': [99999999, '1']})
        self.put('result.json', {'schema_version': 1, 'kind': 'pulsar-guarded-serving-result',
            'run_id': plan['guard_run_id'], 'service_id': plan['service_id'], 'spec_id': plan['spec_id'],
            'status': status, 'qualification': False,
            'phases': {'cleanup': {'complete': True, 'ranks': len(placement)}}})
        self.put('cleanup/batch.json', {'outcome': 'complete', 'returncode': 0,
            'results': [{'index': rank, 'returncode': 0} for rank in range(len(placement))]})
        for rank in range(len(placement)):
            self.put(f'cleanup/jobs/{rank}.out', {'rank': rank, 'run_id': plan['guard_run_id'],
                'spec_id': plan['spec_id'], 'cleanup_verified': True})
        service_state.save(self.store, plan)

    def run_cli(self, *, json_output=True, **extra):
        return subprocess.run([str(self.installation / 'pulsar'), 'guarded', 'reconcile',
            '--output-dir', str(self.output), '--run-id', extra.pop('run_id', self.plan['guard_run_id']),
            *(['--json'] if json_output else [])], env={**self.env, **extra}, cwd=self.root, text=True, capture_output=True, timeout=20)

    def assert_preserved(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertEqual(self.store.get('services', self.plan['service_id'])['plan_id'], self.plan['plan_id'])
        self.assertFalse(list((self.output / 'reconciliations').glob('*.json')))

    def direct_reconcile(self):
        observed = self.root / 'observed'
        observed.mkdir(exist_ok=True)
        (observed / 'plan.json').write_text(json.dumps(self.plan))
        for rank in range(len(self.plan['ranks'])):
            for selector in ('name', 'run', 'plan', 'spec'):
                (observed / f'{rank}-{selector}.out').write_text('')
        members = [field for index in range(3) for field in
                   (f'node-{index}', f'rank-{index}', 'local' if index == 0 else f'rank-{index}',
                    f'192.0.2.{index + 1}', 'eth0')]
        with patch.object(reconciliation, 'Store', return_value=self.store):
            return reconciliation.reconcile(self.output, self.plan['guard_run_id'],
                self.store.root, observed, self.plan['topology_id'], members)

    def receipts(self):
        return sorted((self.output / 'reconciliations').glob('*.json'))

    def test_receipt_write_failure_before_retirement_preserves_locator(self):
        for code in (errno.EACCES, errno.ENOSPC):
            with self.subTest(errno=code), patch.object(reconciliation, 'atomic_json',
                    side_effect=OSError(code, 'synthetic receipt write failure')):
                with self.assertRaises((OSError, ValueError)):
                    self.direct_reconcile()
                self.assertEqual(self.store.get('services', self.plan['service_id'])['plan_id'],
                                 self.plan['plan_id'])
                self.assertFalse(self.receipts())
        result = self.direct_reconcile()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['service_locator_retired'])

    def test_pending_receipt_publication_error_never_removes_locator(self):
        write = reconciliation.atomic_json
        def fail_after_write(*args, **kwargs):
            write(*args, **kwargs)
            raise OSError(errno.EIO, 'synthetic receipt directory fsync failure')
        with patch.object(reconciliation, 'atomic_json', side_effect=fail_after_write):
            with self.assertRaisesRegex(ValueError, 'locator was not changed.*receipt publication failed'):
                self.direct_reconcile()
        self.assertIsNotNone(self.store.get('services', self.plan['service_id']))
        receipt = json.loads(self.receipts()[0].read_text())
        self.assertEqual(receipt['status'], 'retirement-pending')
        self.assertIsNone(receipt['service_locator_retired'])

    def test_locator_removal_fault_keeps_truthful_pending_receipt(self):
        remove = self.store.remove
        for after_unlink in (False, True):
            with self.subTest(after_unlink=after_unlink):
                service_state.save(self.store, self.plan)
                before = set(self.receipts())
                def fail_remove(namespace, key):
                    if after_unlink:
                        remove(namespace, key)
                    raise OSError(errno.EIO, 'synthetic locator removal failure')
                with patch.object(self.store, 'remove', side_effect=fail_remove):
                    with self.assertRaisesRegex(ValueError, 'outcome is unknown.*pending receipt') as error:
                        self.direct_reconcile()
                path, = set(self.receipts()) - before
                self.assertIn(str(path), str(error.exception))
                original = path.read_bytes()
                receipt = json.loads(original)
                self.assertEqual(receipt['status'], 'retirement-pending')
                self.assertIsNone(receipt['service_locator_retired'])
                self.assertEqual(self.store.get('services', self.plan['service_id']) is None,
                                 after_unlink)
                retry = self.direct_reconcile()
                self.assertEqual(retry['status'], 'complete')
                self.assertEqual(retry['service_locator_retired'], not after_unlink)
                self.assertEqual(path.read_bytes(), original)

    def test_final_receipt_write_failure_keeps_pending_and_retry_preserves_it(self):
        write = reconciliation.atomic_json
        calls = 0
        def fail_final(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.ENOSPC, 'synthetic completion receipt write failure')
            write(*args, **kwargs)
        with patch.object(reconciliation, 'atomic_json', side_effect=fail_final):
            with self.assertRaisesRegex(ValueError, 'completion receipt publication failed') as error:
                self.direct_reconcile()
        self.assertIsNone(self.store.get('services', self.plan['service_id']))
        path, = self.receipts()
        self.assertIn(str(path), str(error.exception))
        original = path.read_bytes()
        receipt = json.loads(original)
        self.assertEqual(receipt['status'], 'retirement-pending')
        self.assertIsNone(receipt['service_locator_retired'])
        retry = self.direct_reconcile()
        self.assertFalse(retry['service_locator_retired'])
        self.assertEqual(retry['status'], 'complete')
        self.assertNotEqual(retry['receipt_file'], str(path))
        self.assertEqual(path.read_bytes(), original)

    def test_final_receipt_fsync_error_keeps_truthful_published_outcome(self):
        write = reconciliation.atomic_json
        calls = 0
        def fail_final_after_publish(*args, **kwargs):
            nonlocal calls
            calls += 1
            write(*args, **kwargs)
            if calls == 2:
                raise OSError(errno.EIO, 'synthetic completion receipt fsync failure')
        with patch.object(reconciliation, 'atomic_json', side_effect=fail_final_after_publish):
            with self.assertRaisesRegex(ValueError, 'completion receipt publication failed'):
                self.direct_reconcile()
        self.assertIsNone(self.store.get('services', self.plan['service_id']))
        receipt = json.loads(self.receipts()[0].read_text())
        self.assertEqual(receipt['status'], 'complete')
        self.assertTrue(receipt['service_locator_retired'])

    def test_replacement_refusal_does_not_publish_retirement_intent(self):
        replacement = copy.deepcopy(self.plan)
        replacement['guard_run_id'] = 'e' * 64
        replacement['plan_id'] = canonical_json_digest({k: v for k, v in replacement.items() if k != 'plan_id'})
        service_state.save(self.store, replacement)
        with self.assertRaisesRegex(ValueError, 'different launch plan'):
            self.direct_reconcile()
        self.assertFalse(self.receipts())
        self.assertEqual(self.store.get('services', replacement['service_id'])['plan_id'],
                         replacement['plan_id'])

    def test_absent_default_nondefault_and_failed_session_cleanup_retire_exact_locator(self):
        for placement, status in (((0, 1), 'stopped'), ((2, 1), 'failed'), ((0,), 'stopped')):
            with self.subTest(placement=placement, status=status):
                self.make_session(placement, status=status)
                frozen = {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
                self.store.put('services', 'a' * 64, {'plan_id': 'b' * 64})
                before = len((self.root / 'probes').read_text().splitlines()) if (self.root / 'probes').exists() else 0
                result = self.run_cli()
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                receipt = json.loads(result.stdout)['result']
                self.assertTrue(receipt['service_locator_retired'])
                self.assertTrue(Path(receipt['receipt_file']).is_file())
                probes = [json.loads(line) for line in (self.root / 'probes').read_text().splitlines()[before:]]
                self.assertEqual(len(probes), 4 * len(placement))
                self.assertEqual({row[0] for row in probes}, {str(index) for index in placement})
                self.assertIsNone(self.store.get('services', self.plan['service_id']))
                self.assertEqual(self.store.get('service-plans', self.plan['plan_id']), self.plan)
                self.assertEqual(self.store.get('services', 'a' * 64), {'plan_id': 'b' * 64})
                for relative, content in frozen.items():
                    self.assertEqual((self.output / relative).read_bytes(), content)
                second = self.run_cli()
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertFalse(json.loads(second.stdout)['result']['service_locator_retired'])
                self.assertNotEqual(json.loads(second.stdout)['result']['receipt_file'], receipt['receipt_file'])
        rows = [json.loads(line) for line in (self.root / 'probes').read_text().splitlines()]
        self.assertEqual({row[1] for row in rows}, {'container'})
        self.assertEqual({row[2] for row in rows}, {'ls'})
        self.assertEqual({row[0] for row in rows}, {'0', '1', '2'})

    def test_live_or_unrelated_and_unknown_rank_refuse(self):
        for extra in ({'OCCUPIED_NODE': '0'}, {'OCCUPIED_NODE': '1'},
                      {'UNKNOWN_NODE': '0'}, {'UNKNOWN_NODE': '1'},
                      {'UNKNOWN_SSH_NODE': '1'}, {'UNKNOWN_TRUST': '1'}):
            with self.subTest(extra=extra):
                self.assert_preserved(self.run_cli(**extra))

    def test_cleanup_proof_never_requires_or_reads_execute_programs(self):
        (self.output / 'execute').symlink_to(self.root / 'unavailable-execute', target_is_directory=True)
        (self.output / 'code').symlink_to(self.root / 'unavailable-code', target_is_directory=True)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.output / 'execute').is_symlink())
        self.assertTrue((self.output / 'code').is_symlink())

    def test_changed_topology_or_mapping_refuse_before_probes(self):
        for extra in ({'PULSAR_TEST_TOPOLOGY_ID': 'd' * 64}, {'CHANGED_MAPPING': '1'}):
            self.make_session((2, 1))
            with self.subTest(extra=extra):
                self.assert_preserved(self.run_cli(**extra))
                self.assertFalse((self.root / 'probes').exists())

    def test_topology_change_during_probes_preserves_locator(self):
        self.assert_preserved(self.run_cli(CHANGED_AFTER_PROBES='1'))
        self.assertTrue((self.root / 'probes').exists())

    def test_human_output_and_usage_envelope(self):
        result = self.run_cli(json_output=False, COLUMNS='50')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('Retired service locator', result.stdout)
        result = subprocess.run([str(self.installation / 'pulsar'), 'guarded', 'reconcile',
            '--output-dir', '--json'], env=self.env, cwd=self.root, text=True, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'usage_error')

    def test_newer_replacement_locator_is_preserved(self):
        replacement = copy.deepcopy(self.plan)
        replacement['guard_run_id'] = 'e' * 64
        replacement['plan_id'] = canonical_json_digest({k: v for k, v in replacement.items() if k != 'plan_id'})
        service_state.save(self.store, replacement)
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('different launch plan', result.stderr)
        self.assertEqual(self.store.get('services', replacement['service_id'])['plan_id'], replacement['plan_id'])
        self.assertEqual(self.store.get('service-plans', replacement['plan_id']), replacement)

    def test_ordinary_stop_still_refuses_live_nondefault_guarded_placement(self):
        self.make_session((2, 1))
        result = subprocess.run([str(self.installation / 'pulsar'), 'stop', self.plan['spec_id'],
            '--spec-file', str(self.output / 'spec.json'), '--json'], env={**self.env, 'OCCUPIED_NODE': '2'},
            cwd=self.root, text=True, capture_output=True, timeout=20)
        self.assert_preserved(result)
        self.assertIn('nondefault guarded placement', result.stderr)
        self.assertFalse((self.root / 'probes').exists())

    def test_invalid_historical_records_refuse_before_any_probe_or_locator_mutation(self):
        cases = [('result.json', lambda r: r.update(run_id='d' * 64)),
                 ('result.json', lambda r: r.update(service_id='d' * 64)),
                 ('result.json', lambda r: r['phases']['cleanup'].update(complete=False)),
                 ('cleanup/batch.json', lambda r: r['results'][1].update(returncode=1)),
                 ('cleanup/jobs/1.out', lambda r: r.update(cleanup_verified=False)),
                 ('cleanup/jobs/1.out', lambda r: r.update(run_id='d' * 64)),
                 ('controller.json', lambda r: r.update(run_id='d' * 64)),
                 ('controller.json', lambda r: r.update(owner=process_identity(os.getpid()))),
                 ('context.json', lambda r: r.update(ranks=[])),
                 ('active-plan.json', lambda r: r.update(plan_id='d' * 64))]
        for name, change in cases:
            with self.subTest(name=name):
                self.make_session()
                value = json.loads((self.output / name).read_text())
                change(value)
                self.put(name, value)
                self.assert_preserved(self.run_cli())
                self.assertFalse((self.root / 'probes').exists())
        self.make_session()
        self.assert_preserved(self.run_cli(run_id='short'))

    def test_bad_locator_fields_and_stored_plan_preserve_state(self):
        row = self.store.get('services', self.plan['service_id'])
        self.store.put('services', self.plan['service_id'], {**row, 'selected_spec_id': 'd' * 64})
        self.assert_preserved(self.run_cli())
        self.store.put('services', self.plan['service_id'], row)
        saved = {**self.plan, 'port': 99}
        self.store.put('service-plans', self.plan['plan_id'], saved)
        self.assert_preserved(self.run_cli())
        self.assertEqual(self.store.get('service-plans', self.plan['plan_id']), saved)

    def test_symlink_file_and_parent_refuse(self):
        path = self.output / 'result.json'
        target = self.root / 'linked-result'
        path.rename(target)
        path.symlink_to(target)
        self.assert_preserved(self.run_cli())
        path.unlink()
        target.rename(path)
        alias = self.root / 'alias'
        alias.symlink_to(self.output, target_is_directory=True)
        self.output = alias
        self.assert_preserved(self.run_cli())
        self.assertFalse((self.root / 'probes').exists())


if __name__ == '__main__':
    unittest.main()
