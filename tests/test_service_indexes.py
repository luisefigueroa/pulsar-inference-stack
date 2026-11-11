"""Stops retire only active locators; immutable plan history remains readable."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from model_library.state import Store
from scripts.container_runtime import build_plan
from scripts.service_state import save, locate, retire, retire_plan
from tests.test_container_runtime import fixture
from release_spec.normalize import canonical_json_digest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


class ServiceIndexes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'state')
        self.spec,self.facts,self.prepared,self.plan,_,_=fixture()
        save(self.store,self.plan)

    def another_topology(self):
        facts=copy.deepcopy(self.facts);facts['topology_id']='e'*64
        prepared=copy.deepcopy(self.prepared);prepared['topology_id']='e'*64
        return build_plan(self.spec,self.spec['spec_id'],facts,prepared)

    def test_stop_then_new_topology_has_one_active_selector_and_keeps_history(self):
        self.assertEqual(retire(self.store,topology_id=self.plan['topology_id'],
            node_ids=['node-0'],selected_spec_id=self.spec['spec_id']),[self.plan['service_id']])
        next_plan=self.another_topology();save(self.store,next_plan)
        self.assertEqual(locate(self.store,selected_spec_id=self.spec['spec_id']),next_plan)
        self.assertEqual(self.store.get('service-plans',self.plan['plan_id']),self.plan)

    def test_retirement_is_scoped_to_topology_and_fully_stopped_nodes(self):
        next_plan=self.another_topology();save(self.store,next_plan)
        self.assertEqual(retire(self.store,topology_id=self.plan['topology_id'],node_ids=['node-1']),[])
        retire(self.store,topology_id=self.plan['topology_id'],node_ids=['node-0'])
        self.assertIsNotNone(self.store.get('services',next_plan['service_id']))
        _,_,_,multi,_,_=fixture(2);save(self.store,multi)
        self.assertEqual(retire(self.store,topology_id=multi['topology_id'],node_ids=['node-0']),[])
        self.assertIsNotNone(self.store.get('services',multi['service_id']))

    def replacement(self):
        plan = copy.deepcopy(self.plan)
        plan['lifecycle_action'] = 'replace'
        plan['plan_id'] = canonical_json_digest({k:v for k,v in plan.items() if k != 'plan_id'})
        return plan

    def test_exact_retirement_preserves_replacement_and_unrelated_plan(self):
        replacement = self.replacement()
        unrelated = self.another_topology()
        save(self.store, replacement)
        save(self.store, unrelated)
        self.assertFalse(retire_plan(self.store, self.plan))
        self.assertEqual(locate(self.store, service_id=self.plan['service_id']), replacement)
        self.assertEqual(locate(self.store, service_id=unrelated['service_id']), unrelated)
        self.assertTrue(retire_plan(self.store, replacement))
        self.assertFalse(retire_plan(self.store, replacement))
        self.assertEqual(self.store.get('service-plans', self.plan['plan_id']), self.plan)
        self.assertEqual(self.store.get('service-plans', replacement['plan_id']), replacement)

    def test_exact_retirement_and_save_serialize_without_lifecycle_upgrade(self):
        entered = threading.Event()
        release = threading.Event()
        saved = threading.Event()
        errors = []
        remove = self.store.remove
        def paused_remove(namespace, key):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release retirement')
            remove(namespace, key)
        def retire_old():
            try:
                retire_plan(self.store, self.plan)
            except BaseException as exc:
                errors.append(exc)
        def save_new():
            try:
                save(self.store, self.replacement())
                saved.set()
            except BaseException as exc:
                errors.append(exc)
        # The old default lifecycle lock must remain usable and shared. Both
        # record operations proceed without trying to upgrade it.
        with self.store.lock(exclusive=False), patch.object(self.store, 'remove', paused_remove):
            retire_thread = threading.Thread(target=retire_old)
            save_thread = threading.Thread(target=save_new)
            retire_thread.start()
            try:
                self.assertTrue(entered.wait(5))
                save_thread.start()
                self.assertFalse(saved.wait(.1))
            finally:
                release.set()
                retire_thread.join(5)
                if save_thread.ident is not None:
                    save_thread.join(5)
        self.assertFalse(retire_thread.is_alive())
        self.assertFalse(save_thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(saved.is_set())
        self.assertEqual(locate(self.store, service_id=self.plan['service_id']), self.replacement())

    def test_retirement_record_keeps_compare_remove_lock_through_both_writes(self):
        before = threading.Event()
        remove_allowed = threading.Event()
        after = threading.Event()
        finish_allowed = threading.Event()
        saved = threading.Event()
        errors = []

        @contextmanager
        def record(matching):
            self.assertTrue(matching)
            self.assertIsNotNone(self.store.get('services', self.plan['service_id']))
            before.set()
            if not remove_allowed.wait(5):
                raise AssertionError('test did not release intent write')
            yield
            self.assertIsNone(self.store.get('services', self.plan['service_id']))
            after.set()
            if not finish_allowed.wait(5):
                raise AssertionError('test did not release completion write')

        def retire_old():
            try:
                self.assertTrue(retire_plan(self.store, self.plan, retirement_record=record))
            except BaseException as exc:
                errors.append(exc)

        def save_new():
            try:
                save(self.store, self.replacement())
                saved.set()
            except BaseException as exc:
                errors.append(exc)

        retire_thread = threading.Thread(target=retire_old)
        save_thread = threading.Thread(target=save_new)
        retire_thread.start()
        try:
            self.assertTrue(before.wait(5))
            save_thread.start()
            self.assertFalse(saved.wait(.1))
            remove_allowed.set()
            self.assertTrue(after.wait(5))
            self.assertFalse(saved.wait(.1))
        finally:
            remove_allowed.set()
            finish_allowed.set()
            retire_thread.join(5)
            if save_thread.ident is not None:
                save_thread.join(5)
        self.assertFalse(retire_thread.is_alive())
        self.assertFalse(save_thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(saved.is_set())
        self.assertEqual(locate(self.store, service_id=self.plan['service_id']), self.replacement())

    def test_public_stop_retires_only_after_success(self):
        envfile=self.root/'environment.sh'
        envfile.write_text(f". '{ROOT}/scripts/lib.sh'\n"+'''
load_cluster_topology() {
 CLUSTER_TOPOLOGY_COUNT="$FIXTURE_NODE_COUNT"; CLUSTER_TOPOLOGY_ID="$FIXTURE_TOPOLOGY"
 CLUSTER_NODE_IDS=(node-0)
 [ "$FIXTURE_NODE_COUNT" = 1 ] || CLUSTER_NODE_IDS+=(node-1)
 CLUSTER_NODE_SSH_HOSTS=(local fixture-peer)
}
stop_named_service_by_labels() { STOP_NAMED_NOTHING_FOUND="${FIXTURE_NOTHING_FOUND:-0}"; return "$FIXTURE_STOP_RC"; }
remove_all_stack_managed_local() { return "$FIXTURE_STOP_RC"; }
remove_all_stack_managed_remote() { return "$FIXTURE_STOP_RC"; }
list_managed_container_ids_local() { return 0; }
list_managed_container_ids_remote() { return 0; }
''')
        env={**os.environ,'BASH_ENV':str(envfile),'PULSAR_MODEL_LIBRARY_DIR':str(self.store.root),
             'FIXTURE_TOPOLOGY':self.plan['topology_id'],'PYTHONDONTWRITEBYTECODE':'1'}
        for nodes in (1,2):
            plan=fixture(nodes)[3]
            for selector in (plan['selected_spec_id'],'--all'):
                save(self.store,plan)
                for rc in (1,0):
                    approval=['--yes'] if selector=='--all' else []
                    result=subprocess.run([str(ROOT/'pulsar'),'stop',selector,*approval,'--json'],
                        env={**env,'FIXTURE_STOP_RC':str(rc),'FIXTURE_NODE_COUNT':str(nodes)},
                        text=True,capture_output=True,timeout=10)
                    with self.subTest(nodes=nodes,selector=selector,rc=rc):
                        self.assertNotIn('without --yes',result.stderr)
                        self.assertEqual(result.returncode==0,rc==0,result.stderr+result.stdout)
                        self.assertEqual(self.store.get('services',plan['service_id']) is None,rc==0)
                        self.assertIsNotNone(self.store.get('service-plans',plan['plan_id']))
                        if rc==0:
                            stopped=None if selector=='--all' else True
                            self.assertEqual(json.loads(result.stdout)['result']['stopped'],stopped)
        # Until CLI contract 2, stop --all without --yes warns once and still runs.
        save(self.store,self.plan)
        result=subprocess.run([str(ROOT/'pulsar'),'stop','--all','--json'],
            env={**env,'FIXTURE_STOP_RC':'0','FIXTURE_NODE_COUNT':'2'},text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stderr.count('warning: pulsar stop --all without --yes is deprecated: it stops '
                                             'every Stack-managed service on all 2 confirmed nodes. '
                                             'CLI contract 2 requires --yes.'),1,result.stderr)
        self.assertIsNone(self.store.get('services',self.plan['service_id']))
        result=subprocess.run([str(ROOT/'pulsar'),'stop',self.plan['selected_spec_id'],'--json'],
            env={**env,'FIXTURE_STOP_RC':'0','FIXTURE_NODE_COUNT':'1','FIXTURE_NOTHING_FOUND':'1'},
            text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],
                         {'completed':True,'spec_id':self.plan['selected_spec_id'],'stopped':False})

    def test_ordinary_stop_stops_nondefault_recorded_pair_on_its_recorded_nodes(self):
        # This scenario was refused while ordinary stop could not prove which
        # containers of a pair recorded off the default order were its own. It
        # now stops the pair on the nodes its record names, retires only that
        # record, and still runs the label stop for anything else of the spec.
        spec, facts, prepared, _, _, _ = fixture(2)
        for slot, physical in enumerate((2, 1)):
            facts['ranks'][slot].update(node_id=f'node-{physical}', hostname=f'rank-{physical}', ssh_host=f'rank-{physical}')
            prepared['ranks'][slot]['node_id'] = f'node-{physical}'
        with patch.dict('os.environ', {'API_KEY': '', 'VLLM_API_KEY': ''}):
            plan = build_plan(spec, spec['spec_id'], facts, prepared)
        save(self.store, plan)
        from scripts.container_runtime import rank_spec
        containers = [{'node': str(physical), 'id': str(physical) * 64, 'name': plan['container_name'],
                       'labels': rank_spec(plan, rank)['labels']} for rank, physical in enumerate((2, 1))]
        (self.root/'containers.json').write_text(json.dumps(containers))
        docker = self.root/'docker-double'
        docker.write_text(DOCKER)
        docker.chmod(0o700)
        marker = self.root/'stopped'
        envfile = self.root/'nondefault-env.sh'
        envfile.write_text(f". '{ROOT}/scripts/lib.sh'\n"+f'''
load_cluster_topology() {{ CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_ID={'c'*64}; CLUSTER_NODE_IDS=(node-0 node-1 node-2);
 CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2); CLUSTER_NODE_SSH_HOSTS=(local rank-1 rank-2); }}
reload_cluster_topology() {{ load_cluster_topology; }}
require_topology_ssh_trust() {{ :; }}
list_managed_container_ids_on_node() {{ :; }}
ssh_node() {{ local node="$1"; shift; PULSAR_TEST_NODE="$node" bash -c "${{1/#docker/$PULSAR_DOCKER}}"; }}
container_ownership_inspect_on_node() {{ PULSAR_TEST_NODE="$1" "$PULSAR_DOCKER" inspect --format x "$2"; }}
stop_named_service_by_labels() {{ touch '{marker}'; STOP_NAMED_NOTHING_FOUND=1; }}
''')
        result = subprocess.run([str(ROOT/'pulsar'), 'stop', spec['spec_id'], '--json'],
            env={**os.environ, 'BASH_ENV':str(envfile), 'PULSAR_MODEL_LIBRARY_DIR':str(self.store.root),
                 'PULSAR_DOCKER': str(docker), 'PULSAR_TEST_ROOT': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'},
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],
                         {'completed': True, 'spec_id': spec['spec_id'], 'stopped': True})
        self.assertIn(f"Stopped spec {spec['spec_id'][:12]} on rank-2 (rank 0), rank-1 (rank 1): removed 2 "
                      'container(s) and retired its service record.', result.stderr)
        self.assertEqual(json.loads((self.root/'containers.json').read_text()), [])
        self.assertTrue(marker.exists())
        self.assertIsNone(self.store.get('services', plan['service_id']))
        # The unrelated default-placement record from setUp is untouched.
        self.assertIsNotNone(self.store.get('services', self.plan['service_id']))


# A docker double for one service's containers on numbered nodes: lists by
# name and label filters, inspects for ownership, removes by ID.
DOCKER = '''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['PULSAR_TEST_ROOT'])
node=os.environ.get('PULSAR_TEST_NODE','0')
state=root/'containers.json'
containers=[c for c in json.loads(state.read_text())]
here=[c for c in containers if c['node']==node]
args=sys.argv[1:]
if args[:4]==['container','ls','-aq','--no-trunc']:
    filters=[args[i+1] for i in range(4,len(args),2)]
    def match(c):
        for f in filters:
            key,_,value=f.partition('=')
            if key=='name' and '^/'+c['name']+'$'!=value:return False
            if key=='label' and c['labels'].get(value.partition('=')[0])!=value.partition('=')[2]:return False
        return True
    for c in here:
        if match(c):print(c['id'])
elif args[:2]==['inspect','--format']:
    for c in here:
        if c['id']==args[3]:print(json.dumps({'id':c['id'],'name':'/'+c['name'],'labels':c['labels']}));break
    else:raise SystemExit(1)
elif args==['info']:pass
elif args[:2]==['rm','-f']:
    state.write_text(json.dumps([c for c in containers if not (c['node']==node and c['id']==args[2])]))
else:raise SystemExit(97)
'''


if __name__=='__main__': unittest.main()
