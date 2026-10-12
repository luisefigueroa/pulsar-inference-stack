"""Ordinary stop recovers a guarded trial whose controller has exited (synthetic Docker only)."""
import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest

import tests.test_guard_reconciliation as reconciliation_tests

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'io.pulsar.gb10.'

DOCKER = '''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['PULSAR_TEST_ROOT'])
node=os.environ.get('PULSAR_TEST_NODE','0')
state=root/'containers.json'
containers=json.loads(state.read_text())
with (root/'probes').open('a') as out:out.write(json.dumps([node,*sys.argv[1:]])+'\\n')
if node==os.environ.get('UNKNOWN_NODE'):raise SystemExit(2)
args=sys.argv[1:]
if args[:4]==['container','ls','-aq','--no-trunc']:
    filters=[args[i+1] for i in range(4,len(args),2)]
    def match(c):
        for f in filters:
            key,_,value=f.partition('=')
            if key=='name' and '^/'+c['name']+'$'!=value:return False
            if key=='label' and c['labels'].get(value.partition('=')[0])!=value.partition('=')[2]:return False
        return True
    for c in containers:
        if c['node']==node and match(c):print(c['id'])
elif args[:2]==['rm','-f']:
    with (root/'mutations').open('a') as out:out.write(json.dumps([node,*args])+'\\n')
    state.write_text(json.dumps([c for c in containers if not (c['node']==node and c['id']==args[2])]))
else:raise SystemExit(97)
'''


class GuardedStopRecovery(unittest.TestCase):
    # Reuse the reconciliation fixture's installation and session builders without
    # inheriting its test cases.
    put = reconciliation_tests.GuardReconciliation.put
    make_session = reconciliation_tests.GuardReconciliation.make_session

    def setUp(self):
        reconciliation_tests.GuardReconciliation.setUp(self)
        # Ordinary stop needs the real library; only topology, SSH and Docker are doubles.
        shutil.copy2(ROOT / 'scripts/lib.sh', self.installation / 'scripts/lib.sh')
        for directory in ('cluster', 'platforms'):
            shutil.copytree(ROOT / directory, self.installation / directory,
                            ignore=shutil.ignore_patterns('__pycache__'))
        self.docker.write_text(DOCKER)
        overrides = self.root / 'overrides.sh'
        overrides.write_text(f". '{self.installation}/scripts/lib.sh'\n" + '''
load_cluster_topology() {
 CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_ID=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
 CLUSTER_NODE_IDS=(node-0 node-1 node-2); CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2)
 CLUSTER_NODE_SSH_HOSTS=(local rank-1 rank-2); CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3)
 CLUSTER_NODE_CONTROL_IFS=(eth0 eth0 eth0)
}
reload_cluster_topology() { load_cluster_topology; }
require_topology_ssh_trust() { [ "${UNKNOWN_TRUST:-0}" != 1 ]; }
ssh_node() { local node="$1"; shift; PULSAR_TEST_NODE="$node" bash -c "${1/#docker/$PULSAR_DOCKER}"; }
stop_named_service_by_labels() { STOP_NAMED_NOTHING_FOUND=1; }
''')
        self.env['BASH_ENV'] = str(overrides)
        # The trial ran on node-2 (rank 0) and node-1 (rank 1); its controller has exited.
        self.make_session((2, 1), status='failed')
        result = json.loads((self.output / 'result.json').read_text())
        result['phases']['cleanup'] = {'complete': False, 'error': 'incomplete guarded rank phase: cleanup'}
        self.put('result.json', result)
        self.result_before = (self.output / 'result.json').read_text()
        owned = lambda node, rank: {'node': node, 'id': f'{node}{rank}' * 32, 'name': self.plan['container_name'],
            'labels': {PREFIX + 'guard-run': self.plan['guard_run_id'], PREFIX + 'launch-plan': self.plan['plan_id'],
                       PREFIX + 'spec-id': self.plan['spec_id'], PREFIX + 'rank': str(rank)}}
        unrelated = {**owned('2', 0), 'id': 'e' * 64,
                     'labels': {**owned('2', 0)['labels'], PREFIX + 'guard-run': 'd' * 64}}
        (self.root / 'containers.json').write_text(json.dumps([owned('2', 0), owned('1', 1), unrelated]))

    def stop(self, *args, **extra):
        return subprocess.run([str(self.installation / 'pulsar'), 'stop', *args], env={**self.env, **extra},
                              cwd=self.root, text=True, capture_output=True, timeout=60)

    def remaining(self):
        return sorted(c['id'][:2] for c in json.loads((self.root / 'containers.json').read_text()))

    def assert_untouched(self):
        self.assertEqual(self.remaining(), ['11', '20', 'ee'])
        self.assertFalse((self.root / 'mutations').exists())
        self.assertEqual(self.store.get('services', self.plan['service_id'])['plan_id'], self.plan['plan_id'])

    def test_an_exited_trial_is_stopped_on_its_recorded_nodes(self):
        result = self.stop(self.plan['selected_spec_id'])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(f"Stopped guarded trial {self.plan['guard_run_id'][:12]}", result.stdout)
        self.assertIn('rank-2 (rank 0), rank-1 (rank 1)', result.stdout)
        # Only the trial's own containers; one with another run's label stays.
        self.assertEqual(self.remaining(), ['ee'])
        self.assertIsNone(self.store.get('services', self.plan['service_id']))
        self.assertEqual(self.store.get('service-plans', self.plan['plan_id']), self.plan)
        # The trial's own record of its failed cleanup is kept as it was.
        self.assertEqual((self.output / 'result.json').read_text(), self.result_before)

    def test_json_reports_the_trial_as_stopped(self):
        result = self.stop(self.plan['selected_spec_id'], '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['result'],
                         {'completed': True, 'spec_id': self.plan['selected_spec_id'], 'stopped': True})

    def test_an_unobservable_recorded_node_removes_nothing(self):
        result = self.stop(self.plan['selected_spec_id'], UNKNOWN_NODE='1')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('error: stop did not run: rank-1 cannot be observed', result.stderr)
        self.assert_untouched()

    def test_a_running_controller_is_left_to_guarded_stop(self):
        lock = Path(self.env['PULSAR_MODEL_LIBRARY_DIR']) / 'lifecycle.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        # A running guarded run holds the lifecycle lock shared for its whole life.
        holder = subprocess.Popen(['flock', '-s', str(lock), 'sleep', '30'])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        deadline = time.monotonic() + 5
        while subprocess.run(['flock', '-n', '-x', str(lock), 'true']).returncode == 0 and time.monotonic() < deadline:
            time.sleep(.05)
        result = self.stop(self.plan['selected_spec_id'])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"runs as guarded trial {self.plan['guard_run_id'][:12]} on rank-2, rank-1", result.stderr)
        self.assertIn(f"Next: ./pulsar guarded stop --output-dir <its output directory> --run-id "
                      f"{self.plan['guard_run_id']}", result.stderr)
        self.assertFalse((self.root / 'probes').exists())
        self.assert_untouched()

    def test_node_selection_cannot_split_a_trial(self):
        result = self.stop(self.plan['selected_spec_id'], '--node', 'rank-1')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('a trial stops on all its nodes together, so --node cannot select one', result.stderr)
        self.assertFalse((self.root / 'probes').exists())
        self.assert_untouched()

    def test_stop_all_does_not_stop_a_guarded_trial(self):
        result = self.stop('--all', '--yes')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"stop --all does not stop guarded trial {self.plan['guard_run_id'][:12]}", result.stderr)
        self.assert_untouched()


if __name__ == '__main__':
    unittest.main()
