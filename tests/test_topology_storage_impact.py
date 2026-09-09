"""Topology changes report affected managed records without mutating them."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'tests')]

from model_library.state import Store, view_key
from support.topology_fixture import Fixture
import topology_storage_impact as impact


class TopologyStorageImpact(unittest.TestCase):
    def test_changed_topology_reports_views_and_removed_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'topology').mkdir();fixture=Fixture(root/'topology')
            store=Store(root/'state')
            manifest_id='a'*64
            home={'schema_version':1,'kind':'pulsar-home',
                  'snapshot_manifest_id':manifest_id,'node_id':'fixture-node-1',
                  'hub_path':str(root/'home'),'path':str(root/'home/snapshot'),
                  'verification':{'snapshot_manifest_id':manifest_id},
                  'verified_at':'2026-09-07T00:00:00Z'}
            store.put('homes',manifest_id,home)
            view={**home,'kind':'pulsar-prepared-view','spec_id':'b'*64,
                  'topology_id':fixture.topology['topology_id'],'rank':1,
                  'pinned':True,'is_home_view':False}
            store.put('views',view_key(view['spec_id'],view['node_id']),view)
            proposed=copy.deepcopy(fixture.topology)
            proposed['topology_id']='c'*64
            proposed['nodes']=proposed['nodes'][:1]
            result=impact.assess(fixture.topology,proposed,store)
            self.assertEqual(result['homes_on_removed_nodes'],['fixture-node-1'])
            self.assertEqual(result['prepared_views_requiring_reconciliation'],1)
            self.assertEqual(result['pinned_views_requiring_reconciliation'],1)
            self.assertEqual(Store(root/'state').home(manifest_id),home)


if __name__ == '__main__':
    unittest.main()
