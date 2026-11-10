"""Shared-copy ownership, compatibility and deletion with real temporary files."""
import hashlib
import json
import multiprocessing
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from release_spec import build_snapshot_manifest
from model_library import node
from model_library.integrity import StorageError, verify_tree
from model_library.local import begin_staging, copy_files, finish_staging, home_record, payload, prepared_record
from model_library.planning import preparation_candidates, purge_plan
from model_library.state import Store, reconcile_views, shared_view, view_key, view_record_key


def competing_operation(request, ready, done, result):
    ready.set()
    try:
        with patch.object(node, 'require_serving_filesystem'):
            result.put(node.run(request))
    except Exception as exc:
        result.put(str(exc))
    finally:
        done.set()


class PreparedContent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view_root = self.root/'views'
        source = self.root/'source'; source.mkdir()
        (source/'weights.bin').write_bytes(b'synthetic shared weights')
        self.manifest = build_snapshot_manifest(model_id='fixture/model', snapshot_revision='a'*40,
            files=[{'path':'weights.bin','size':24,'sha256':hashlib.sha256((source/'weights.bin').read_bytes()).hexdigest()}])
        stage = begin_staging(self.view_root)
        copy_files(source, payload(stage,self.manifest), self.manifest)
        hub = self.view_root/('b'*64)
        stamp = finish_staging(stage,hub,self.manifest)
        self.view = prepared_record(home_record(self.manifest,'fixture-worker',hub,stamp),
            spec_id='b'*64,topology_id='d'*64,rank=1)
        self.store = Store(self.view_root/'.pulsar-node')
        self.store.put('views',view_record_key(self.view),self.view)
        self.patch = patch.object(node,'require_serving_filesystem'); self.patch.start(); self.addCleanup(self.patch.stop)

    def request(self, operation, **values):
        return dict(operation=operation,manifest=self.manifest,view_root=str(self.view_root),
                    home_root=str(self.root/'homes'),**values)

    def bind_request(self, source=None, **values):
        return self.request('bind-view',source_view=source or self.view,spec_id=values.get('spec_id','c'*64),
                            node_id='fixture-worker',rank=0,topology_id='e'*64,view_schema=values.get('view_schema',1))

    def bind(self, **values):
        return node.run(self.bind_request(**values))['view']

    def release(self, view, **values):
        result=node.run(self.request('remove-view',view=view,all_views=values.get('all_views',self.store.views())))
        node.run(self.request('forget-view',view=view))
        return result

    def test_binding_preserves_bytes_old_identity_and_pin(self):
        self.view['pinned'] = True
        self.store.put('views',view_record_key(self.view),self.view)
        inode = Path(self.view['path']).stat().st_ino
        bound = self.bind()
        old = self.store.get('views',view_record_key(self.view))
        self.assertEqual(bound['path'],self.view['path'])
        self.assertEqual(Path(bound['path']).stat().st_ino,inode)
        self.assertTrue(old['pinned']); self.assertFalse(bound['pinned'])
        self.assertEqual((old['rank'],old['topology_id']),(1,'d'*64))
        self.assertEqual((bound['rank'],bound['topology_id']),(0,'e'*64))
        self.assertEqual(old['schema_version'],3)
        self.assertEqual(bound['verification']['method'],'metadata')
        self.assertEqual(self.bind(),bound)
        self.assertFalse(self.release(bound)['removed'])
        with self.assertRaisesRegex(StorageError,'pinned'): self.release(old)

    def test_last_binding_releases_files_and_retry_is_safe(self):
        bound = self.bind()
        self.assertFalse(self.release(self.view)['removed'])
        verify_tree(bound['path'],self.manifest)
        self.assertTrue(self.release(bound)['removed'])
        self.assertFalse(Path(bound['hub_path']).exists())
        self.assertFalse(self.release(bound)['removed'])
        self.assertEqual(self.store.views(),[])

    def test_stale_controller_reference_preserves_files(self):
        bound = self.bind()
        records = self.store.views()
        self.release(self.view)
        self.assertFalse(self.release(bound,all_views=records)['removed'])
        self.assertTrue(Path(bound['path']).is_dir())
        with self.assertRaisesRegex(StorageError,'ownership is missing'):
            self.release(bound)

    def test_partial_upgrade_reconciles_without_key_change(self):
        for version in (1,2):
            with self.subTest(version=version):
                old={**self.view,'schema_version':version}
                shared={**shared_view(old),'pinned':True}
                self.assertEqual(view_record_key(old),view_record_key(shared))
                for rows in ([old,shared],[shared,old]):
                    merged=reconcile_views(rows)
                    self.assertEqual(len(merged),1)
                    self.assertEqual(merged[0]['schema_version'],3)
                    self.assertTrue(merged[0]['pinned'])
        bad={**self.view,'path':str(self.root/'elsewhere')}
        with self.assertRaisesRegex(StorageError,'disagree'): reconcile_views([self.view,bad])

    def test_existing_shared_source_can_bind_other_record_layout(self):
        bound = self.bind(view_schema=2)
        third = self.bind(source=bound,spec_id='f'*64)
        self.assertEqual(third['binding_schema'],1)
        self.assertEqual(bound['binding_schema'],2)
        self.assertEqual(len(self.store.views()),3)

    def test_changed_source_or_target_ownership_cannot_be_overwritten(self):
        bound = self.bind()
        for source in ({**self.view,'node_id':'another-node'}, {**self.view,'rank':2}):
            with self.subTest(source=source),self.assertRaises(StorageError): self.bind(source=source)
        changed={**bound,'topology_id':'f'*64}
        self.store.put('views',view_record_key(changed),changed)
        with self.assertRaisesRegex(StorageError,'different prepared copy'): self.bind()

    def test_corrupt_source_does_not_publish_binding(self):
        (Path(self.view['path'])/'weights.bin').write_bytes(b'x'*24)
        with self.assertRaises(StorageError): self.bind()
        self.assertEqual(len(self.store.views()),1)

    def test_pin_updates_cannot_downgrade_shared_ownership(self):
        self.bind()
        node.run(self.request('pin-view',view={**self.view,'pinned':True}))
        old=self.store.get('views',view_record_key(self.view))
        self.assertEqual(old['schema_version'],3)
        self.assertTrue(old['pinned'])

    def test_bind_and_final_release_are_serialized_on_node(self):
        context=multiprocessing.get_context('spawn')
        ready,done,result=context.Event(),context.Event(),context.Queue()
        worker=context.Process(target=competing_operation,args=(self.bind_request(),ready,done,result))
        # Hold the same lock as deletion while the second controller tries to
        # bind. Deletion finishes first; binding must then reject missing bytes.
        with self.store.lock():
            worker.start(); self.assertTrue(ready.wait(5))
            self.assertFalse(done.wait(.1))
            node.run_node(self.request('remove-view',view=self.view,all_views=[self.view]),
                          self.store,self.root/'homes',self.view_root)
        worker.join(5)
        if worker.is_alive(): worker.kill(); worker.join(); self.fail('node operation did not finish')
        self.assertIn('cannot open managed directory',result.get(timeout=2))
        self.assertFalse(Path(self.view['path']).exists())
        node.run(self.request('forget-view',view=self.view))
        self.assertEqual(self.store.views(),[])

    def test_purge_plan_retains_other_recipe_and_container_protections(self):
        bound=self.bind()
        observations=[{'node_id':'fixture-worker','observable':True,'containers':[]}]
        plan=purge_plan(views=[self.view],all_views=[self.view,bound],node_ids=['fixture-worker'],observations=observations)
        self.assertTrue(plan['eligible']); self.assertEqual(plan['actions'][0]['action'],'release-binding')
        observations[0]['containers']=[{'mounts':[self.view['hub_path']],'running':False}]
        self.assertFalse(purge_plan(views=[self.view],all_views=[self.view,bound],node_ids=['fixture-worker'],observations=observations)['eligible'])

    def test_missing_current_binding_candidates_are_deterministic(self):
        bound=self.bind()
        home={**self.view,'node_id':'fixture-home'}
        candidates=preparation_candidates(spec={'spec_id':'f'*64},home=home,node_id='fixture-worker',views=[bound,self.view])
        self.assertEqual(candidates,[self.view])
        candidates=preparation_candidates(spec={'spec_id':bound['spec_id']},home=home,node_id='fixture-worker',views=[self.view,bound])
        self.assertEqual(candidates,[bound])

    def test_owned_pending_transfer_is_resumed_before_sharing(self):
        home={**self.view,'node_id':'fixture-home'}
        pending={**self.view,'spec_id':'f'*64}
        candidates=preparation_candidates(spec={'spec_id':'f'*64},home=home,node_id='fixture-worker',
                                         views=[self.view],transactions=[pending])
        self.assertEqual(candidates,[])

    def test_failed_binding_publication_keeps_old_copy_and_retry_recovers(self):
        original=Store.put
        def fail_target(store,namespace,key,value,**kwargs):
            if namespace=='views' and value.get('spec_id')=='c'*64:
                raise StorageError('fixture publication interruption')
            return original(store,namespace,key,value,**kwargs)
        with patch.object(Store,'put',new=fail_target),self.assertRaisesRegex(StorageError,'interruption'):
            self.bind()
        old=self.store.views()
        self.assertEqual(len(old),1)
        self.assertEqual(old[0]['schema_version'],3)
        verify_tree(self.view['path'],self.manifest)
        self.bind()
        self.assertEqual(len(self.store.views()),2)

    def test_pending_operation_prevents_last_copy_deletion(self):
        pending={**self.view,'stage':str(self.view_root/'.pending-owned'),
                 'destination':self.view['hub_path']}
        self.store.put('transactions',view_record_key(pending),pending)
        with self.assertRaisesRegex(StorageError,'incomplete preparation'): self.release(self.view)
        self.assertTrue(Path(self.view['path']).exists())

    def test_different_manifest_cannot_bind_existing_copy(self):
        request=self.bind_request()
        request['manifest']=build_snapshot_manifest(model_id=self.manifest['model_id'],
            snapshot_revision='f'*40,files=self.manifest['files'])
        with self.assertRaisesRegex(StorageError,'exact working-copy manifest'): node.run(request)
        self.assertEqual(len(self.store.views()),1)

    def test_lost_release_reply_retains_node_proof_until_retry(self):
        bound=self.bind()
        result=node.run(self.request('remove-view',view=self.view,all_views=self.store.views()))
        self.assertFalse(result['removed'])
        self.assertEqual(len(self.store.views()),2)
        self.assertFalse(self.release(bound)['removed'])
        self.assertTrue(self.release(self.view)['removed'])
        self.assertEqual(self.store.views(),[])

    def test_large_shared_inventory_does_not_use_exec_arguments(self):
        value=[{'node_id':'fixture-worker','verification':'x'*4096} for _ in range(64)]
        data=self.root/'records.json';data.write_text(json.dumps(value))
        helper=Path(__file__).resolve().parents[1]/'scripts/model-library-common.sh'
        result=subprocess.run(['bash','-c',
            'source "$1"; data=$(cat "$2"); model_json views: "$data" label "$3"',
            'fixture',str(helper),str(data),'literal\n$()\\value'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'views':value,'label':'literal\n$()\\value'})



class UnrecordedDestination(unittest.TestCase):
    """A folder at a rank's prepared copy path that no node record names (no adoption, no deletion)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.view_root = self.root/'views'; self.view_root.mkdir()
        self.manifest = build_snapshot_manifest(model_id='fixture/model', snapshot_revision='a'*40,
            files=[{'path':'weights.bin','size':4,'sha256':hashlib.sha256(b'data').hexdigest()}])
        self.patch = patch.object(node,'require_serving_filesystem'); self.patch.start(); self.addCleanup(self.patch.stop)

    def request(self, operation, view_schema=1, **values):
        return dict(operation=operation,manifest=self.manifest,view_root=str(self.view_root),
                    home_root=str(self.root/'homes'),view_schema=view_schema,spec_id='b'*64,
                    node_id='fixture-worker',rank=1,topology_id='d'*64,**values)

    def leftover(self, view_schema=1):
        """The folder a lost record left at the destination begin-view computes."""
        dest = self.view_root/('b'*64 if view_schema == 1 else view_key('b'*64,'fixture-worker',self.manifest['manifest_id']))
        payload(dest,self.manifest).mkdir(parents=True)
        (payload(dest,self.manifest)/'weights.bin').write_bytes(b'data')
        return dest

    def test_missing_destination_is_reported_without_creating_node_state(self):
        result = node.run(self.request('view-destination'))
        self.assertEqual(result, {'path':str(self.view_root/('b'*64)),'state':'missing','recorded':False})
        self.assertFalse((self.view_root/'.pulsar-node').exists())

    def test_unrecorded_folder_is_present_for_both_destination_names(self):
        for schema in (1, 2):
            with self.subTest(view_schema=schema):
                dest = self.leftover(schema)
                result = node.run(self.request('view-destination', view_schema=schema))
                self.assertEqual(result, {'path':str(dest),'state':'present','recorded':False})
                self.assertFalse((self.view_root/'.pulsar-node').exists())

    def test_a_file_or_symlink_at_the_destination_is_present_not_an_error(self):
        dest = self.view_root/('b'*64)
        dest.symlink_to(self.root)
        self.assertEqual(node.run(self.request('view-destination'))['state'], 'present')
        dest.unlink(); dest.write_text('not a folder')
        self.assertEqual(node.run(self.request('view-destination'))['state'], 'present')

    def test_a_node_record_naming_the_folder_counts_as_recorded(self):
        dest = self.leftover()
        store = Store(self.view_root/'.pulsar-node')
        record = prepared_record(home_record(self.manifest,'fixture-worker',dest,verify_tree(payload(dest,self.manifest),self.manifest,full=True)),
            spec_id='c'*64,topology_id='d'*64,rank=1)
        store.put('views',view_record_key(record),record)
        self.assertTrue(node.run(self.request('view-destination'))['recorded'])

    def test_begin_view_refuses_with_advice_that_does_not_name_purge(self):
        dest = self.leftover()
        with self.assertRaises(StorageError) as raised:
            node.run(self.request('begin-view'))
        message = str(raised.exception)
        self.assertEqual(message, f'a folder already exists where Prepare would place the prepared copy: {dest}. '
                         + node.UNRECORDED_DESTINATION_FIX)
        self.assertIn('--adopt-unrecorded', message)
        self.assertNotIn('purge', message.lower())
        self.assertEqual(sorted(p.name for p in self.view_root.iterdir() if p.name != '.pulsar-node'), ['b'*64])
        self.assertEqual((payload(dest,self.manifest)/'weights.bin').read_bytes(), b'data')
        self.assertEqual(Store(self.view_root/'.pulsar-node').records('transactions'), [])

    # D7: Prepare's --adopt-unrecorded takes over such a folder only after every file verifies.
    def lost_copy(self, view_schema=1):
        """A complete prepared copy whose records were lost: manifest.json and its snapshot."""
        dest = self.leftover(view_schema)
        (dest/'manifest.json').write_text(json.dumps(self.manifest))
        return dest

    def folder_state(self, dest):
        return {str(p): (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in [dest, *dest.rglob('*')]}

    def test_asked_whether_a_folder_could_be_taken_over_the_node_answers_without_hashing(self):
        dest = self.lost_copy()
        # Without adopt the answer is unchanged.
        self.assertEqual(node.run(self.request('view-destination')),
                         {'path': str(dest), 'state': 'present', 'recorded': False})
        with patch.object(node, 'verify_tree', side_effect=AssertionError('hashed')):
            self.assertEqual(node.run(self.request('view-destination', adopt=True)),
                             {'path': str(dest), 'state': 'present', 'recorded': False, 'adoptable': True})
        (dest/'notes.txt').write_text('mine')
        answer = node.run(self.request('view-destination', adopt=True))
        self.assertEqual((answer['adoptable'], answer['mismatch']),
                         (False, 'it holds other files than one prepared copy (manifest.json and snapshots)'))
        self.assertFalse((self.view_root/'.pulsar-node').exists())

    def test_adopt_view_hashes_every_file_then_records_what_publish_view_would(self):
        for schema in (1, 2):
            with self.subTest(view_schema=schema):
                dest = self.lost_copy(schema)
                before = self.folder_state(dest)
                with patch.object(node, 'verify_tree', wraps=verify_tree) as hashed:
                    result = node.run(self.request('adopt-view', view_schema=schema))
                # Every file hashed, no earlier stamp reused.
                self.assertEqual([(call.kwargs.get('full'), call.kwargs.get('stamp')) for call in hashed.call_args_list],
                                 [(True, None)])
                record = result['view']
                self.assertEqual(result['home']['verification']['method'], 'sha256')
                store = Store(self.view_root/'.pulsar-node')
                self.assertEqual(store.views(), [record])
                self.assertEqual((record['hub_path'], record['path'], record['rank'], record['topology_id'],
                                  record['pinned'], record['is_home_view'], record['schema_version']),
                                 (str(dest), str(payload(dest, self.manifest)), 1, 'd'*64, False, False, schema))
                self.assertEqual(self.folder_state(dest), before)
                # Now recorded: a second take-over is refused, and nothing changes.
                with self.assertRaisesRegex(StorageError, 'record on this node now names'):
                    node.run(self.request('adopt-view', view_schema=schema))
                store.remove('views', view_record_key(record))
                import shutil
                shutil.rmtree(dest)

    def test_a_folder_that_does_not_verify_is_never_recorded_or_changed(self):
        dest = self.lost_copy()
        (payload(dest, self.manifest)/'weights.bin').write_bytes(b'DATA')  # Same size, other bytes.
        before = self.folder_state(dest)
        # Sizes match, so the node's quick answer allows it; the hash decides.
        self.assertTrue(node.run(self.request('view-destination', adopt=True))['adoptable'])
        with self.assertRaises(StorageError) as raised:
            node.run(self.request('adopt-view'))
        message = str(raised.exception)
        self.assertTrue(message.startswith(f'a folder already exists where Prepare would place the prepared copy: '
                                           f'{dest}. It was not taken over: snapshot SHA-256 differs: weights.bin. '),
                        message)
        self.assertTrue(message.endswith(node.UNRECORDED_MISMATCH_FIX), message)
        self.assertEqual(Store(self.view_root/'.pulsar-node').views(), [])
        self.assertEqual(self.folder_state(dest), before)
        for change, reason in ((lambda: (dest/'extra').mkdir(), 'it holds other files than one prepared copy'),
                               (lambda: (dest/'manifest.json').write_text('{}'), 'its manifest describes other files'),
                               (lambda: (dest/'manifest.json').write_text('{'), 'could not be read as a prepared copy')):
            with self.subTest(reason):
                dest = self.lost_copy() if not dest.exists() else dest
                change()
                with self.assertRaisesRegex(StorageError, reason):
                    node.run(self.request('adopt-view'))
                self.assertEqual(Store(self.view_root/'.pulsar-node').views(), [])
                import shutil
                shutil.rmtree(dest)


if __name__=='__main__': unittest.main()
