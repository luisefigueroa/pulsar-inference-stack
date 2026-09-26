"""Stops retire only active locators; immutable plan history remains readable."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from model_library.state import Store
from scripts.container_runtime import build_plan
from scripts.service_state import save, locate, retire
from tests.test_container_runtime import fixture

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


if __name__=='__main__': unittest.main()
