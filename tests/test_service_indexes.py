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
                    result=subprocess.run([str(ROOT/'pulsar'),'stop',selector,'--json'],
                        env={**env,'FIXTURE_STOP_RC':str(rc),'FIXTURE_NODE_COUNT':str(nodes)},
                        text=True,capture_output=True,timeout=10)
                    with self.subTest(nodes=nodes,selector=selector,rc=rc):
                        self.assertEqual(result.returncode==0,rc==0,result.stderr+result.stdout)
                        self.assertEqual(self.store.get('services',plan['service_id']) is None,rc==0)
                        self.assertIsNotNone(self.store.get('service-plans',plan['plan_id']))
                        if rc==0:
                            stopped=None if selector=='--all' else True
                            self.assertEqual(json.loads(result.stdout)['result']['stopped'],stopped)
        result=subprocess.run([str(ROOT/'pulsar'),'stop',self.plan['selected_spec_id'],'--json'],
            env={**env,'FIXTURE_STOP_RC':'0','FIXTURE_NODE_COUNT':'1','FIXTURE_NOTHING_FOUND':'1'},
            text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],
                         {'completed':True,'spec_id':self.plan['selected_spec_id'],'stopped':False})

    def test_ordinary_stop_refuses_nondefault_recorded_pair_before_mutation(self):
        spec, facts, prepared, _, _, _ = fixture(2)
        for slot, physical in enumerate((2, 1)):
            facts['ranks'][slot].update(node_id=f'node-{physical}', hostname=f'rank-{physical}', ssh_host=f'rank-{physical}')
            prepared['ranks'][slot]['node_id'] = f'node-{physical}'
        plan = build_plan(spec, spec['spec_id'], facts, prepared)
        save(self.store, plan)
        marker = self.root/'stopped'
        envfile = self.root/'nondefault-env.sh'
        envfile.write_text(f". '{ROOT}/scripts/lib.sh'\n"+f'''
load_cluster_topology() {{ CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_ID={'c'*64}; CLUSTER_NODE_IDS=(node-0 node-1 node-2); }}
stop_named_service_by_labels() {{ touch '{marker}'; }}
''')
        result = subprocess.run([str(ROOT/'pulsar'), 'stop', spec['spec_id'], '--json'],
            env={**os.environ, 'BASH_ENV':str(envfile), 'PULSAR_MODEL_LIBRARY_DIR':str(self.store.root)},
            text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('guarded stop', result.stderr)
        self.assertFalse(marker.exists())
        self.assertIsNotNone(self.store.get('services', plan['service_id']))


if __name__=='__main__': unittest.main()
