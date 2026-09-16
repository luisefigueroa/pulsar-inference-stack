"""Prepared-set coverage, deduplication and assembly without node execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from model_library.inspection import plan, assemble, write_jobs
from model_library.integrity import StorageError
from model_library.local import home_record, prepared_record
from model_library.preparation_verification import cache_path
from model_library.state import Store, view_record_key
from release_spec import serving
from tests.test_container_runtime import fixture


class Inspection(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'state')
        self.spec=fixture(3,speculative=True)[0]
        self.nodes=['node-0','node-1','node-2'];self.topology='c'*64
        self.seed()

    def seed(self):
        for index,model in enumerate(serving.required_snapshots(self.spec).values()):
            manifest=model['snapshot_manifest'];home_rank=2-index
            for rank in range(3):
                home=home_record(manifest,self.nodes[rank],self.root/f'{manifest["manifest_id"]}-{rank}',
                    {'snapshot_manifest_id':manifest['manifest_id'],'method':'sha256'})
                if rank==home_rank: self.store.put('homes',manifest['manifest_id'],home)
                row=prepared_record(home,spec_id=self.spec['spec_id'],topology_id=self.topology,
                    rank=rank,is_home_view=rank==home_rank,schema_version=2,pinned=rank==1)
                self.store.put('views',view_record_key(row),row)

    def plan(self, **kwargs):
        return plan(self.store,self.spec,self.nodes,self.topology,**kwargs)

    def test_all_named_copies_and_remote_homes_are_in_one_ordered_batch(self):
        value=self.plan(full=True)
        self.assertEqual(list(value['members']),['target','draft'])
        self.assertEqual([j['node_slot'] for j in value['jobs']],[0,1,2,0,1,2])
        self.assertEqual([value['members'][name]['home_job'] for name in ('target','draft')],[2,4])
        self.assertTrue(all(j['full'] for j in value['jobs']))

    def test_same_named_content_is_verified_once_per_physical_copy(self):
        recipe=copy.deepcopy(self.spec['recipe'])
        target=recipe['model'].pop('snapshot_manifest')
        recipe['required_snapshots']['draft']=copy.deepcopy(recipe['model'])
        self.spec=serving.freeze({'schema_version':2,'kind':'pulsar-recipe-draft',
            'source':self.spec['source'],'recipe':recipe},{'target':target,'draft':target})
        self.seed()
        value=self.plan(full=True)
        self.assertEqual(len(value['jobs']),3)
        self.assertEqual([r['job'] for r in value['members']['target']['ranks']],
                         [r['job'] for r in value['members']['draft']['ranks']])

    def test_preparation_cache_changes_proof_only_and_keeps_metadata_validation(self):
        value=self.plan();record=value['jobs'][0]['record'];cache=self.root/'cache';cache.mkdir()
        cached={'verification':{**record['verification'],'method':'metadata'},'verified_at':'fixture-new-proof'}
        cache_path(str(cache),record).write_text(json.dumps(cached))
        value=self.plan(full=True,cache=str(cache))
        self.assertFalse(value['jobs'][0]['full'])
        self.assertTrue(all(j['full'] for j in value['jobs'][1:]))
        self.assertEqual(value['jobs'][0]['record'],record)
        self.assertEqual(value['jobs'][0]['candidate'],{**record,**cached})

    def test_missing_member_prevents_work_and_cannot_make_other_members_ready(self):
        manifest=self.spec['recipe']['required_snapshots']['draft']['snapshot_manifest']['manifest_id']
        self.store.remove('homes',manifest)
        value=self.plan(full=True)
        self.assertEqual(write_jobs(value,self.root),1)
        self.assertFalse((self.root/'prepared.json').exists())
        members=json.loads((self.root/'members.json').read_text())
        self.assertEqual(members['draft']['rc'],1)
        self.assertEqual(members['target']['rc'],255)
        self.assertFalse(list((self.root/'jobs').iterdir()))

    def test_wrong_placement_or_home_binding_is_not_verified(self):
        for mutation in ('topology','home','missing'):
            with self.subTest(mutation=mutation):
                rows=self.store.views(spec_id=self.spec['spec_id']);row=next(r for r in rows if r['is_home_view'])
                previous=copy.deepcopy(row);key=view_record_key(row)
                if mutation=='topology': row['topology_id']='d'*64
                elif mutation=='home': row['path'] += '/another'
                if mutation=='missing': self.store.remove('views',key)
                else: self.store.put('views',key,row)
                value=self.plan()
                self.assertTrue(any(m['rc'] for m in value['members'].values()))
                self.store.put('views',key,previous)
        with self.assertRaises(StorageError): plan(self.store,self.spec,self.nodes[:2],self.topology)
        with self.assertRaises(StorageError): plan(self.store,self.spec,[self.nodes[0]]*3,self.topology)

    def test_result_order_and_record_bindings_do_not_depend_on_completion_order(self):
        value=self.plan();(self.root/'jobs').mkdir()
        for job in value['jobs']:
            checked={**job['record'],'verified_at':f'completed-{job["index"]}'}
            (self.root/'jobs'/f'{job["index"]}.verified.json').write_text(json.dumps(checked))
        # Controller JSON sorts object keys. Assembly still emits target first.
        value=json.loads(json.dumps(value,sort_keys=True))
        statuses=[{'index':j['index'],'returncode':0} for j in reversed(value['jobs'])]
        self.assertEqual(assemble(value,self.root,{'results':statuses}),0)
        result=json.loads((self.root/'prepared.json').read_text())
        self.assertEqual(list(result['snapshots']),['target','draft'])
        for member in result['snapshots'].values():
            self.assertEqual([r['rank'] for r in member['ranks']],[0,1,2])
            self.assertEqual([r['pinned'] for r in member['ranks']],[False,True,False])
            self.assertEqual(member['home']['verified_at'],next(r['verified_at'] for r in member['ranks'] if r['is_home_view']))
        (self.root/'prepared.json').unlink()
        statuses[-1].update(returncode=2,error='SHA-256 mismatch')
        self.assertEqual(assemble(value,self.root,{'results':statuses}),2)
        self.assertFalse((self.root/'prepared.json').exists())
        self.assertIn('SHA-256 mismatch',json.loads((self.root/'members.json').read_text())['target']['error'])


if __name__=='__main__': unittest.main()
