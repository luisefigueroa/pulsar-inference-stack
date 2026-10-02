"""End-to-end storage CLI contracts with synthetic nodes and real filesystem state."""
import copy
import json
import hashlib
import base64
import os
import shutil
import fcntl
import signal
import subprocess
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/support")]
from model_library_fixture import Fixture, contained, node_request
from model_library.integrity import read_json, verify_tree
from model_library.state import Store, view_key
from model_library.local import copy_snapshot, payload
from release_spec import build_snapshot_manifest, pretty_json_bytes, spec_id_for, verify_spec
from release_spec.identity import argv_from_identity


class ModelLibraryCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, nodes=1):
        return Fixture(self.root, nodes)

    def success(self, result):
        self.assertEqual(result.returncode, 0, f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}')
        self.assertTrue(result.stdout.strip(), "successful storage operation returned no result")
        return json.loads(result.stdout)

    def failure(self, result, text=None):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        if text: self.assertIn(text, result.stderr + result.stdout)

    def acquire_candidate(self, fixture, *, nodes=1, home=0):
        result = self.success(fixture.acquire(home))
        fixture.candidate(nodes)
        return result

    def test_long_operations_report_phases_on_stderr_only(self):
        f=self.fixture(nodes=2)
        acquired=f.acquire(1)
        self.success(acquired)
        self.assertIn('[acquire 1/4] staging on ',acquired.stderr)
        self.assertIn('[acquire 4/4] publishing the home on ',acquired.stderr)
        f.candidate(2)
        preview=f.run('prepare','--plan',spec=True)
        self.success(preview)
        self.assertNotIn('[prepare ',preview.stderr)
        prepared=f.run('prepare','--yes',spec=True)
        self.success(prepared)
        self.assertIn('[prepare 1/3] rank 0 on ',prepared.stderr)
        self.assertIn('[prepare 3/3] verifying every rank before recording readiness',prepared.stderr)

    def test_cancelled_full_audit_retains_stamp_releases_lock_and_retries(self):
        from model_library.verification_process import process_identity
        f=self.fixture(nodes=2)
        self.acquire_candidate(f,nodes=2,home=1)
        self.success(f.run('prepare','--yes',spec=True))
        before={p:p.read_bytes() for p in f.state.rglob('*.json')}
        ready=f.root/'verifier-ready.json';resume=f.root/'verifier-resume'
        f.cfg['block_verification']={'ready':str(ready),'resume':str(resume)};f.save()
        command=['bash',str(f.repo/'scripts/model-library.sh'),'info',f.spec['spec_id'],
                 '--spec-file',str(f.spec_path),'--full','--json']
        process=subprocess.Popen(command,cwd=f.repo,env=f.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            deadline=time.monotonic()+15
            identity=None
            while identity is None:
                try: identity=json.loads(ready.read_text())
                except (OSError,ValueError): pass
                if identity is not None: break
                if process.poll() is not None:
                    out,err=process.communicate();self.fail(f'audit exited before hashing: {out!r} {err!r}')
                if time.monotonic()>deadline: self.fail('verification did not start')
                time.sleep(.02)
            process.terminate()  # Signal the caller PID, not its whole group.
            out,err=process.communicate(timeout=8)
            self.assertNotEqual(process.returncode,0)
            self.assertEqual(out,b'')
            self.assertNotEqual(process_identity(identity[0]),identity)
            self.assertEqual({p:p.read_bytes() for p in before},before)
            with (f.state/'lifecycle.lock').open('r+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=5)
            f.cfg.pop('block_verification');f.save()
        self.success(f.run('info','--full',spec=True))

    def wait_for_verifiers(self, root, process, ranks):
        from model_library.verification_process import process_identity
        deadline=time.monotonic()+15;identities={}
        while len(identities)!=len(ranks):
            for rank in ranks:
                try: identities[rank]=json.loads((root/f'ready-{rank}').read_text())
                except (OSError,ValueError): pass
            if process.poll() is not None:
                out,err=process.communicate();self.fail(f'audit ended before barrier: {out!r} {err!r}')
            if time.monotonic()>deadline: self.fail('all-node verification barrier was not reached')
            time.sleep(.02)
        self.assertTrue(all(process_identity(identity[0])==identity for identity in identities.values()))
        return identities

    def test_parallel_named_audit_matches_serial_and_hashes_every_copy_once(self):
        f=self.speculative_fixture(nodes=3,draft_home=2)
        self.success(f.run('prepare','--yes',spec=True))
        f.cfg['trace_verification']=True;f.save()
        serial=self.success(f.run('info','--full','--verification-jobs','1',spec=True))
        size=sum(model['snapshot_manifest']['total_bytes'] for model in
                 [f.spec['recipe']['model'],*f.spec['recipe']['required_snapshots'].values()])
        before=len(f.events('verification-read'))
        self.assertEqual(sum(row['bytes'] for row in f.events('verification-read')),size*3)
        old_records={p:p.read_bytes() for p in f.state.rglob('*.json')}
        f.cfg['block_verification']={'ready':str(f.root/'ready'),'resume':str(f.root/'resume'),'per_rank':True};f.save()
        command=['bash',str(f.repo/'scripts/model-library.sh'),'info',f.spec['spec_id'],
                 '--spec-file',str(f.spec_path),'--full','--json']
        process=subprocess.Popen(command,cwd=f.repo,env=f.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            self.wait_for_verifiers(f.root,process,range(3))
            self.assertEqual({p:p.read_bytes() for p in old_records},old_records)
            for rank in range(3): (f.root/f'resume-{rank}').touch()
            out,err=process.communicate(timeout=30)
            self.assertEqual(process.returncode,0,err)
            parallel=json.loads(out)
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=5)
            f.cfg.pop('block_verification');f.save()
        reads=f.events('verification-read')[before:]
        self.assertEqual({rank:sum(row['bytes'] for row in reads if row['rank']==rank) for rank in range(3)},
                         {rank:size for rank in range(3)})
        def stable(value):
            if isinstance(value,dict): return {k:stable(v) for k,v in value.items() if k!='verified_at'}
            if isinstance(value,list): return [stable(v) for v in value]
            return value
        self.assertEqual(stable(serial),stable(parallel))
        before=len(f.events('verification-read'))
        self.success(f.run('info',spec=True))
        self.assertEqual(len(f.events('verification-read')),before)
        result=self.success(f.run('check','--full','--verification-jobs','2',spec=True))
        self.assertEqual(result['observation']['local_state'],'ready')
        self.assertEqual(sum(row['bytes'] for row in f.events('verification-read')[before:]),size*3)
        self.assertEqual(list((f.root/'tmp').iterdir()),[])

    def test_corrupt_parallel_worker_cancels_other_nodes_without_masking_error(self):
        from model_library.verification_process import process_identity
        f=self.fixture(3);self.acquire_candidate(f,nodes=3,home=2)
        self.success(f.run('prepare','--yes',spec=True))
        view=next(v for v in Store(f.state).views(spec_id=f.spec['spec_id']) if v['rank']==1)
        path=Path(view['path'])/'weights.bin';data=path.read_bytes();path.write_bytes(b'x'*len(data))
        old_records={p:p.read_bytes() for p in f.state.rglob('*.json')}
        f.cfg['block_verification']={'ready':str(f.root/'ready'),'resume':str(f.root/'resume'),'per_rank':True};f.save()
        command=[sys.executable,str(f.repo/'scripts/public_cli.py'),'model','info',f.spec['spec_id'],
                 '--spec-file',str(f.spec_path),'--full','--json']
        process=subprocess.Popen(command,cwd=f.repo,env=f.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            identities=self.wait_for_verifiers(f.root,process,range(3))
            (f.root/'resume-1').touch()
            out,err=process.communicate(timeout=15)
            response=json.loads(out)
            self.assertNotEqual(process.returncode,0,err)
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['code'],'prerequisite_failed',response)
            self.assertIn('SHA-256',response['error']['message'])
            self.assertTrue(all(process_identity(identity[0])!=identity for identity in identities.values()))
            self.assertEqual({p:p.read_bytes() for p in old_records},old_records)
            self.assertEqual(list((f.root/'tmp').iterdir()),[])
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=5)

    def test_named_aliases_share_one_verification_per_physical_copy(self):
        from release_spec.serving import freeze
        f=self.fixture(2);self.acquire_candidate(f,nodes=2,home=1)
        recipe=copy.deepcopy(f.spec['recipe']);manifest=recipe['model'].pop('snapshot_manifest')
        recipe['required_snapshots']={'draft':copy.deepcopy(recipe['model'])}
        recipe['engine_args'] += ['--speculative_config.model','pulsar-snapshot:draft']
        f.spec=freeze({'schema_version':2,'kind':'pulsar-recipe-draft','source':f.spec['source'],'recipe':recipe},
                      {'target':manifest,'draft':manifest})
        f.spec_path.write_bytes(pretty_json_bytes(f.spec))
        self.success(f.run('prepare','--yes',spec=True))
        f.cfg['trace_verification']=True;f.save()
        result=self.success(f.run('info','--full',spec=True))
        self.assertEqual(result['snapshots']['target'],result['snapshots']['draft'])
        self.assertEqual(sum(row['bytes'] for row in f.events('verification-read')),2*manifest['total_bytes'])

    def test_bundled_checkpoint_keeps_complete_verification_and_reuses_named_bytes(self):
        from release_spec.serving import freeze
        f = self.fixture(2)
        f.cfg['files']['dflash/config.json'] = {'data': base64.b64encode(b'{"model_type":"synthetic-draft"}').decode(), 'lfs': False}
        f.cfg['files']['dflash/model.safetensors'] = {'data': base64.b64encode(b'synthetic draft weights').decode(), 'lfs': True}
        f.save()
        self.acquire_candidate(f, nodes=2, home=1)
        recipe = copy.deepcopy(f.spec['recipe'])
        manifest = recipe['model'].pop('snapshot_manifest')
        recipe['required_snapshots'] = {'draft': copy.deepcopy(recipe['model'])}
        recipe['engine_args'] += ['--speculative_config.model', 'pulsar-snapshot:draft/dflash']
        f.spec = freeze({'schema_version': 2, 'kind': 'pulsar-recipe-draft',
                        'source': f.spec['source'], 'recipe': recipe}, {'target': manifest, 'draft': manifest})
        f.spec_path.write_bytes(pretty_json_bytes(f.spec))
        self.success(f.run('prepare', '--yes', spec=True))
        f.cfg['trace_verification'] = True
        f.save()
        result = self.success(f.run('info', '--full', spec=True))
        self.assertEqual(result['snapshots']['target'], result['snapshots']['draft'])
        self.assertEqual(sum(row['bytes'] for row in f.events('verification-read')), 2*manifest['total_bytes'])
        view = next(row for row in Store(f.state).views(spec_id=f.spec['spec_id']) if not row['is_home_view'])
        path = Path(view['path'])/'weights.bin'
        original = path.read_bytes()
        path.write_bytes(b'x'*len(original))
        self.failure(f.run('info', '--full', spec=True), 'SHA-256')

    def test_public_info_and_check_retain_remote_lease_failure_and_reap_peers(self):
        from model_library.verification_process import process_identity
        f=self.fixture(2);self.acquire_candidate(f,nodes=2,home=1)
        self.success(f.run('prepare','--yes',spec=True))
        records={p:p.read_bytes() for namespace in ('homes','views') for p in (f.state/namespace).glob('*.json')}
        f.cfg['block_verification']={'ready':str(f.root/'ready'),'resume':str(f.root/'resume'),'per_rank':True}
        f.cfg['node_lease']={'operation':'verify','rank':1,'seconds':.5};f.save()
        for operation in ('info','check'):
            with self.subTest(operation=operation):
                result=subprocess.run([sys.executable,str(f.repo/'scripts/public_cli.py'),'model',operation,
                    f.spec['spec_id'],'--spec-file',str(f.spec_path),'--full','--json'],
                    cwd=f.repo,env=f.env,text=True,capture_output=True,timeout=20)
                self.assertNotEqual(result.returncode,0)
                response=json.loads(result.stdout)
                self.assertEqual(response['error']['code'],'prerequisite_failed',response)
                self.assertIn('controller lease expired',response['error']['message'])
                for marker in f.root.glob('ready-*'):
                    identity=json.loads(marker.read_text())
                    self.assertNotEqual(process_identity(identity[0]),identity)
                self.assertEqual({p:p.read_bytes() for p in records},records)
                self.assertEqual(list((f.root/'tmp').iterdir()),[])
        observation=Store(f.state).get('observations',f.spec['spec_id'])
        self.assertEqual(observation['local_state'],'unknown')
        self.assertIn('controller lease expired',' '.join(observation['blockers']))

    def test_invalid_worker_limits_fail_before_node_operations(self):
        f=self.fixture()
        for value in ('0','-1','1.5','true',''):
            self.failure(f.run('info','--verification-jobs',value),'positive integer')
        self.assertEqual(f.events('node-operation'),[])

    def speculative_fixture(self, nodes=2, draft_home=0):
        from release_spec.serving import freeze
        f=self.fixture(nodes)
        self.acquire_candidate(f,nodes=nodes)
        target=copy.deepcopy(f.spec)
        target_manifest=target['recipe']['model']['snapshot_manifest']
        f.cfg['revision']='e'*40;f.save()
        f.manifest_path=f.root/'draft-manifest.json'
        second=self.success(f.acquire(draft_home))['manifest']
        draft={'schema_version':2,'kind':'pulsar-recipe-draft','source':target['source'],'recipe':copy.deepcopy(target['recipe'])}
        draft['recipe']['model'].pop('snapshot_manifest')
        draft['recipe']['required_snapshots']={'draft':{'model_id':second['model_id'],'model_commit':second['snapshot_revision']}}
        draft['recipe']['engine_args'] += ['--speculative_config.model','pulsar-snapshot:draft']
        f.spec=freeze(draft,{'target':target_manifest,'draft':second})
        f.spec_path.write_bytes(pretty_json_bytes(f.spec))
        return f

    def recipe_variant(self, fixture):
        from release_spec.serving import freeze
        previous=copy.deepcopy(fixture.spec)
        recipe=copy.deepcopy(previous['recipe'])
        models={'target':recipe['model'],**recipe.get('required_snapshots',{})}
        manifests={name:model.pop('snapshot_manifest') for name,model in models.items()}
        recipe['image_digest']='sha256:'+('c' if recipe['image_digest']!='sha256:'+'c'*64 else 'd')*64
        draft={'schema_version':previous['schema_version']-1,'kind':'pulsar-recipe-draft','source':previous['source'],'recipe':recipe}
        fixture.spec=freeze(draft,manifests if previous['schema_version']==3 else manifests['target'])
        fixture.spec_path.write_bytes(pretty_json_bytes(fixture.spec))
        return previous

    def test_recipe_change_reuses_three_node_content_and_preserves_pins(self):
        f=self.fixture(nodes=3)
        self.acquire_candidate(f,nodes=3,home=2)
        self.success(f.run('prepare','--yes',spec=True))
        self.success(f.run('pin','--yes',spec=True))
        old=self.recipe_variant(f)
        before=Store(f.state).views(spec_id=old['spec_id'])
        paths={v['node_id']:v['path'] for v in before}
        inodes={v['node_id']:Path(v['path']).stat().st_ino for v in before}
        transfers=f.events('transfer')
        f.cfg['trace_verification']=True;f.save()
        records={p:p.read_bytes() for p in f.root.rglob('*.json') if '/views/' in str(p) or p.is_relative_to(f.state)}
        plan=self.success(f.run('prepare','--plan',spec=True))
        self.assertEqual([v['action'] for v in plan['actions']],['bind','bind','home-view'])
        self.assertEqual({p:p.read_bytes() for p in records},records)
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        self.success(f.run('prepare','--yes',spec=True))
        info=self.success(f.run('info',spec=True))
        from scripts.runtime_binding import prepared_set
        prepared_set(info,f.spec,f.topology['topology_id'])
        self.assertEqual({v['node_id']:v['path'] for v in info['ranks']},paths)
        self.assertEqual({v['node_id']:Path(v['path']).stat().st_ino for v in info['ranks']},inodes)
        self.assertEqual(f.events('transfer'),transfers)
        self.assertEqual(f.events('verification-read'),[])
        self.assertTrue(all(v['pinned'] for v in Store(f.state).views(spec_id=old['spec_id'])))
        self.assertFalse(any(v['pinned'] for v in info['ranks']))
        purge=self.success(f.run('purge','--plan',spec=True))
        self.assertTrue(all(v['action']=='release-binding' for v in purge['plan']['actions']))
        self.success(f.run('purge','--yes',spec=True))
        f.spec=old;f.spec_path.write_bytes(pretty_json_bytes(old))
        self.success(f.run('info',spec=True))
        self.success(f.run('unpin','--yes',spec=True))
        self.success(f.run('purge','--yes',spec=True))
        for row in before:
            self.assertEqual(Path(row['path']).exists(),row['is_home_view'])

    def test_shared_named_snapshots_resume_after_lost_binding_reply(self):
        f=self.speculative_fixture(nodes=2,draft_home=1)
        self.success(f.run('prepare','--yes',spec=True))
        old=self.recipe_variant(f)
        transfers=f.events('transfer')
        f.cfg['node_fault']={'operation':'bind-view','rank':1,'after':True};f.save()
        self.failure(f.run('prepare','--yes',spec=True),'binding failed')
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        self.failure(f.run('info',spec=True))
        f.cfg.pop('node_fault');f.save()
        self.success(f.run('prepare','--yes',spec=True))
        info=self.success(f.run('info',spec=True))
        from scripts.container_runtime import prepared_snapshots
        prepared_snapshots(f.spec,info,f.topology['topology_id'])
        self.assertEqual(set(info['snapshots']),{'target','draft'})
        self.assertEqual(f.events('transfer'),transfers)
        old_rows=Store(f.state).views(spec_id=old['spec_id'])
        new_rows=Store(f.state).views(spec_id=f.spec['spec_id'])
        self.assertEqual(len(new_rows),4)
        self.assertEqual({(v['node_id'],v['snapshot_manifest_id'],v['path']) for v in old_rows},
                         {(v['node_id'],v['snapshot_manifest_id'],v['path']) for v in new_rows})
        f.cfg['trace_verification']=True;f.save()
        self.success(f.run('prepare','--full','--yes',spec=True))
        size=sum(v['snapshot_manifest']['total_bytes'] for v in
                 [f.spec['recipe']['model'],*f.spec['recipe']['required_snapshots'].values()])
        self.assertEqual(sum(v['bytes'] for v in f.events('verification-read')),2*size)

    def test_new_placement_copies_only_missing_content_and_corruption_blocks_reuse(self):
        f=self.fixture(nodes=3)
        self.acquire_candidate(f,nodes=2)
        self.success(f.run('prepare','--yes',spec=True))
        f.candidate(nodes=3)
        transfers=f.events('transfer')
        node_events=len(f.events('node-operation'))
        f.cfg['trace_verification']=True;f.save()
        plan=self.success(f.run('prepare','--plan',spec=True))
        self.assertEqual([v['action'] for v in plan['actions']],['home-view','bind','copy'])
        self.success(f.run('prepare','--yes',spec=True))
        added=f.events('transfer')[len(transfers):]
        self.assertTrue(added)
        self.assertTrue(all(Path(v['destination']).is_relative_to(f.cfg['nodes'][2]['view_root']) for v in added))
        copies=[v for v in f.events('node-operation')[node_events:] if v['operation']=='begin-view']
        self.assertEqual([v['rank'] for v in copies],[2])
        size=f.spec['recipe']['model']['snapshot_manifest']['total_bytes']
        self.assertEqual({rank:sum(v['bytes'] for v in f.events('verification-read') if v['rank']==rank)
                          for rank in range(3)},{0:0,1:0,2:size})
        bad=next(v for v in Store(f.state).views(spec_id=f.spec['spec_id']) if v['node_id']=='node-1')
        (Path(bad['path'])/'weights.bin').write_bytes(b'corrupt weights')
        self.recipe_variant(f)
        transfers=f.events('transfer')
        self.failure(f.run('prepare','--yes',spec=True),'failed verification')
        self.assertEqual(f.events('transfer'),transfers)
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])

    def test_speculative_combined_budget_and_interrupted_preparation(self):
        f=self.speculative_fixture(draft_home=0)
        limit=max(m['snapshot_manifest']['total_bytes'] for m in
                  [f.spec['recipe']['model'],*f.spec['recipe']['required_snapshots'].values()])
        f.env['PULSAR_HOT_BUDGET_BYTES']=str(limit)
        before=len([e for e in f.events('node-operation') if e['operation']=='begin-view'])
        self.failure(f.run('prepare','--yes',spec=True),'combined')
        self.assertEqual(len([e for e in f.events('node-operation') if e['operation']=='begin-view']),before)
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        f.env['PULSAR_HOT_BUDGET_BYTES']=str(32*1024**2)
        f.cfg['node_fault']={'operation':'begin-view','rank':1,'after':True,
            'manifest_id':f.spec['recipe']['required_snapshots']['draft']['snapshot_manifest']['manifest_id']};f.save()
        self.failure(f.run('prepare','--yes',spec=True))
        self.assertEqual(len(Store(f.state).views(spec_id=f.spec['spec_id'])),2)
        target_transfers=f.events('transfer')
        self.assertTrue(target_transfers)
        target_sources={event['source'] for event in target_transfers}
        node_state=Store(Path(f.cfg['nodes'][1]['view_root'])/'.pulsar-node')
        pending=node_state.records('transactions')
        self.assertEqual(len(pending),1)
        stage=pending[0]['stage']
        self.failure(f.run('info',spec=True),'snapshot')
        f.cfg.pop('node_fault');f.save()
        self.success(f.run('prepare','--yes',spec=True))
        self.assertEqual(node_state.records('transactions'),[])
        self.assertFalse(Path(stage).exists())
        self.assertEqual([event for event in f.events('transfer') if event['source'] in target_sources],target_transfers)
        self.assertGreater(len(f.events('transfer')),len(target_transfers))
        self.assertEqual(len(Store(f.state).views(spec_id=f.spec['spec_id'])),4)

    def test_prepare_reuses_verified_copies_and_hashes_new_destinations(self):
        for home in (0, 2):
            with self.subTest(home=home), tempfile.TemporaryDirectory(dir=self.root) as temp:
                f = Fixture(Path(temp), 3)
                self.acquire_candidate(f, nodes=3, home=home)
                f.cfg['trace_verification'] = True; f.save()
                size = f.spec['recipe']['model']['snapshot_manifest']['total_bytes']
                for attempt in range(2):
                    before = len(f.events('verification-read'))
                    self.success(f.run('prepare', '--yes', spec=True))
                    reads = f.events('verification-read')[before:]
                    self.assertEqual({rank: sum(r['bytes'] for r in reads if r['rank'] == rank)
                                      for rank in range(3)},
                                     {rank: size if attempt == 0 and rank != home else 0
                                      for rank in range(3)})
                    self.assertEqual(list((f.root / 'tmp').iterdir()), [])
                before = len(f.events('verification-read'))
                self.success(f.run('info', '--full', spec=True))
                self.assertEqual(sum(r['bytes'] for r in f.events('verification-read')[before:]), 3 * size)

    def test_explicit_full_preparation_hashes_every_reused_copy_once(self):
        f=self.fixture(nodes=2)
        self.acquire_candidate(f,nodes=2)
        self.success(f.run('prepare','--yes',spec=True))
        f.cfg['trace_verification']=True;f.save()
        self.success(f.run('prepare','--full','--yes',spec=True))
        size=f.spec['recipe']['model']['snapshot_manifest']['total_bytes']
        reads=f.events('verification-read')
        self.assertEqual({rank:sum(row['bytes'] for row in reads if row['rank']==rank)
                          for rank in range(2)},{0:size,1:size})
        self.success(f.run('prepare','--yes',spec=True))
        self.assertEqual(f.events('verification-read'),reads)

    def test_speculative_prepare_reuses_planning_and_final_set_verification(self):
        f = self.speculative_fixture(nodes=3, draft_home=2)
        f.cfg['trace_verification'] = True; f.save()
        store = Store(f.state)
        for home in store.records('homes'):
            home['verified_at'] = '2000-01-01T00:00:00Z'
            store.put('homes', home['snapshot_manifest_id'], home)
        self.success(f.run('prepare', '--yes', spec=True))
        size = sum(model['snapshot_manifest']['total_bytes'] for model in
                   [f.spec['recipe']['model'], *f.spec['recipe']['required_snapshots'].values()])
        reads = f.events('verification-read')
        self.assertEqual({rank: sum(r['bytes'] for r in reads if r['rank'] == rank)
                          for rank in range(3)},
                         {rank: sum(model['snapshot_manifest']['total_bytes'] for model in
                                    [f.spec['recipe']['model'], *f.spec['recipe']['required_snapshots'].values()]
                                    if store.home(model['snapshot_manifest']['manifest_id'])['node_id']
                                    != f.cfg['nodes'][rank]['node_id']) for rank in range(3)})
        self.assertEqual(list((f.root / 'tmp').iterdir()), [])
        self.assertTrue(all(home['verified_at'] == '2000-01-01T00:00:00Z'
                            for home in store.records('homes')))

    def test_registered_acquisition_reuses_verification_and_hashes_invalidated_stamp(self):
        f=self.fixture(nodes=2)
        acquired=self.acquire_candidate(f,nodes=2)
        store=Store(f.state)
        home=store.home(acquired['manifest']['manifest_id'])
        home['verified_at']='2000-01-01T00:00:00Z'
        store.put('homes',home['snapshot_manifest_id'],home)
        f.cfg['trace_verification']=True;f.save()
        before_sources=len([e for e in f.events('node-operation') if e['operation']=='source-verify'])
        for use_spec in (True,False):
            args=[] if use_spec else ['--model-id',f.cfg['model_id'],'--model-commit',f.cfg['revision']]
            reused=self.success(f.run('acquire',*args,'--yes',spec=use_spec))
            self.assertEqual(reused['home']['verification']['method'],'metadata')
            self.assertEqual(reused['home']['verified_at'],home['verified_at'])
        self.assertEqual(f.events('verification-read'),[])
        self.assertEqual(len([e for e in f.events('node-operation') if e['operation']=='source-verify']),before_sources)
        path=Path(home['path'])/'weights.bin';observed=path.stat()
        os.utime(path,ns=(observed.st_atime_ns,observed.st_mtime_ns+1_000_000))
        refreshed=self.success(f.run('acquire','--yes',spec=True))
        self.assertEqual(sum(e['bytes'] for e in f.events('verification-read')),acquired['manifest']['total_bytes'])
        self.assertNotEqual(refreshed['home']['verified_at'],home['verified_at'])
        before=len(f.events('verification-read'))
        self.success(f.run('acquire','--yes',spec=True))
        self.assertEqual(len(f.events('verification-read')),before)
        store.remove('homes',home['snapshot_manifest_id'])
        self.success(f.run('acquire','--yes',spec=True))
        self.assertEqual(sum(e['bytes'] for e in f.events('verification-read')[before:]),acquired['manifest']['total_bytes'])
        self.assertEqual(len(f.events('download')),1)

    def test_prepare_final_barrier_rejects_changed_published_copy(self):
        f = self.fixture(3)
        self.acquire_candidate(f, nodes=3)
        f.cfg['node_mutation'] = {'operation': 'publish-view', 'rank': 1}; f.save()
        self.failure(f.run('prepare', '--yes', spec=True), 'SHA-256')
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']), [])
        self.assertEqual(list((f.root / 'tmp').iterdir()), [])

    def test_speculative_preparation_retention_and_archive_coverage(self):
        f=self.speculative_fixture(draft_home=1)
        self.failure(f.run('archive','create','--yes',spec=True),'--snapshot')
        self.success(f.run('prepare','--yes',spec=True))
        prepared=self.success(f.run('info','--full',spec=True))
        self.assertEqual(set(prepared['snapshots']),{'target','draft'})
        from scripts.container_runtime import prepared_snapshots
        prepared_snapshots(f.spec,prepared,f.topology['topology_id'])
        views=Store(f.state).views(spec_id=f.spec['spec_id'])
        self.assertEqual(len(views),4)
        self.assertEqual(len({(v['node_id'],v['path']) for v in views}),4)
        self.success(f.run('pin','--yes',spec=True))
        self.assertTrue(all(v['pinned'] for v in Store(f.state).views(spec_id=f.spec['spec_id'])))
        self.failure(f.run('purge','--yes',spec=True),'pinned')
        self.success(f.run('unpin','--yes',spec=True))
        draft_view=next(v for v in views if v['snapshot_manifest_id']==f.spec['recipe']['required_snapshots']['draft']['snapshot_manifest']['manifest_id'])
        rank=next(n for n in f.cfg['nodes'] if n['node_id']==draft_view['node_id'])
        rank['containers']=[{'Id':'stopped-draft','State':{'Running':False},'Config':{'Labels':{}},'Mounts':[{'Source':draft_view['path']}]}];f.save()
        self.failure(f.run('purge','--yes',spec=True),'container')
        rank['containers']=[];f.save()
        self.success(f.run('archive','create','--snapshot','target','--yes',spec=True))
        self.failure(f.run('archive','verify',spec=True),'draft')
        self.success(f.run('archive','create','--snapshot','draft','--yes',spec=True))
        proof=self.success(f.run('archive','verify',spec=True))
        self.assertEqual(set(proof['snapshots']),{'target','draft'})
        observed=self.success(f.run('check','--full',spec=True))['observation']
        self.assertEqual(set(observed['snapshots']),{'target','draft'})
        self.assertEqual(observed['local_state'],'ready')
        self.assertEqual(observed['archive_state'],'verified')
        self.success(f.run('purge','--yes',spec=True))
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        self.assertEqual(len(Store(f.state).records('homes')),2)
        self.success(f.run('remove','--snapshot','draft','--yes',spec=True))
        self.assertIsNotNone(Store(f.state).home(f.spec['recipe']['model']['snapshot_manifest']['manifest_id']))
        self.success(f.run('restore','--snapshot','draft','--node','1','--yes',spec=True))
        self.assertEqual(len(Store(f.state).records('homes')),2)

    def test_fixture_refuses_external_data_before_node_execution(self):
        f = self.fixture()
        with patch.dict("os.environ", f.env):
            for path in ("/var/tmp/pulsar-hot", str(ROOT / ".model-library"), "/outside-model-data"):
                with self.assertRaisesRegex(RuntimeError, "outside temporary root"):
                    contained(path)
                with self.assertRaises(RuntimeError):
                    node_request({"path": path}, f.cfg, f.cfg["nodes"][0])

    def catalog(self, fixture):
        # A synthetic catalog fixture exercises retention without metadata gates.
        # This is never published and does not claim physical qualification.
        spec = copy.deepcopy(fixture.spec)
        spec = verify_spec(spec)
        (fixture.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))

    def test_single_node_full_storage_lifecycle(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        home_path = Path(acquired["home"]["path"])
        prepared = self.success(f.run("prepare", "--yes", spec=True))
        self.assertEqual(prepared["prepared"], 1)
        info = self.success(f.run("info", "--full", spec=True))
        self.assertEqual(info["home_node_id"], "node-0")
        self.assertEqual(len(info["ranks"]), 1)
        self.success(f.run("pin", "--yes", spec=True))
        self.failure(f.run("purge", "--yes", spec=True), "pinned")
        self.assertTrue(home_path.is_dir())
        # Preparing identical pinned bytes must preserve the pin and reuse them.
        self.success(f.run("prepare", "--yes", spec=True))
        self.assertTrue(self.success(f.run("info", spec=True))["ranks"][0]["pinned"])
        self.success(f.run("unpin", "--yes", spec=True))
        self.success(f.run("purge", "--yes", spec=True))
        self.assertTrue(home_path.is_dir())
        archive = self.success(f.run("archive", "create", "--yes", spec=True))
        self.assertTrue(archive["verified"])
        self.success(f.run("archive", "verify", spec=True))
        self.catalog(f)
        self.success(f.run("remove", "--yes", spec=True))
        self.assertFalse(home_path.exists())
        self.assertIsNone(Store(f.state).home(f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]))
        # Restoration uses only the frozen spec and archive; HF is unavailable.
        f.cfg["hub_unavailable"] = True; f.save()
        downloads = len(f.events("download"))
        shutil.rmtree(f.state)
        self.success(f.run("restore", f.spec["spec_id"], "--node", "node-0", "--yes"))
        self.assertEqual(len(f.events("download")), downloads)
        self.assertTrue(home_path.is_dir())
        self.success(f.run("prepare", "--yes", spec=True))
        self.success(f.run("info", "--full", spec=True))
        self.assertFalse(any("receipt" in str(p.relative_to(f.state)) for p in f.state.rglob("*")))

    def test_insufficient_copy_budget_reports_blocker_without_publishing(self):
        f=self.fixture(nodes=2)
        self.acquire_candidate(f,nodes=2)
        f.env['PULSAR_HOT_BUDGET_BYTES']='0'
        result=f.run('prepare','--yes',spec=True)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        plan=json.loads(result.stdout)
        self.assertFalse(plan['eligible'])
        self.assertTrue(any('insufficient copy budget' in item for item in plan['blockers']))
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        self.assertEqual(f.events('transfer'),[])

    def test_preparation_preview_and_budget_inspection_share_allowances(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        f.env.update(PULSAR_HOT_RESERVE_BYTES='5', PULSAR_HOT_BUDGET_BYTES='3000')
        before = {p: p.read_bytes() for p in f.state.rglob('*.json')}
        plan = self.success(f.run('prepare', '--plan', spec=True))
        manifest = f.spec['recipe']['model']['snapshot_manifest']
        self.assertEqual(plan['total_bytes'], manifest['total_bytes'])
        self.assertEqual(plan['file_count'], manifest['file_count'])
        report = self.success(f.run('budget'))
        for index, node in enumerate(report['nodes']):
            self.assertEqual(node['reserve'], 5)
            self.assertEqual(node['limit'], 3000)
            self.assertEqual(node['path'], f.cfg['nodes'][index]['view_root'])
            self.assertEqual(plan['budgets'][node['node_id']]['reserve'], node['reserve'])
            self.assertEqual(plan['budgets'][node['node_id']]['limit'], node['limit'])
        self.assertEqual({p: p.read_bytes() for p in f.state.rglob('*.json')}, before)
        self.assertEqual(f.events('transfer'), [])

    def test_restore_preview_shows_payload_and_optional_space_without_staging(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        shutil.rmtree(acquired['home']['hub_path'])  # Synthetic loss, entirely inside the fixture.
        before = {p: p.read_bytes() for p in f.state.rglob('*.json')}
        count = len(f.events('node-operation'))
        plan = self.success(f.run('restore', '--node', 'node-1', '--plan', spec=True))
        manifest = f.spec['recipe']['model']['snapshot_manifest']
        self.assertEqual(plan['total_bytes'], manifest['total_bytes'])
        self.assertEqual(plan['file_count'], manifest['file_count'])
        self.assertEqual(plan['destination_root'], f.cfg['nodes'][1]['home_root'])
        self.assertIsInstance(plan['destination_space']['available'], int)
        self.assertTrue({event['operation'] for event in f.events('node-operation')[count:]} <= {'find-source', 'path-state', 'roots', 'space'})
        f.cfg['node_fault'] = dict(operation='space', rank=1); f.save()
        plan = self.success(f.run('restore', '--node', 'node-1', '--plan', spec=True))
        self.assertIsNone(plan['destination_space'])
        self.assertEqual({p: p.read_bytes() for p in f.state.rglob('*.json')}, before)
        self.assertEqual(f.events('transfer'), [])

    def test_two_nodes_with_remote_home(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2, home=1)
        self.assertEqual(acquired["home"]["node_id"], "node-1")
        self.success(f.run("prepare", "--yes", spec=True))
        info = self.success(f.run("info", "--full", spec=True))
        self.assertEqual([r["node_id"] for r in info["ranks"]], ["node-0", "node-1"])
        self.assertTrue(info["ranks"][1]["is_home_view"])
        self.assertNotEqual(info["ranks"][0]["path"], info["ranks"][1]["path"])
        self.assertTrue(f.events("transfer"))
        self.success(f.run("archive", "create", "--yes", spec=True))
        self.success(f.run("archive", "verify", spec=True))
        self.success(f.run("purge", "--yes", spec=True))
        self.success(f.run("remove", "--yes", spec=True))
        f.cfg["hub_unavailable"] = True; f.save()
        self.success(f.run("restore", "--node", "node-1", "--yes", spec=True))
        self.success(f.run("prepare", "--yes", spec=True))

    def test_selected_quiet_pair_retains_controller_home_and_pinned_history(self):
        from release_spec.serving import freeze
        f = self.fixture(nodes=3)
        f.cfg['files']['dflash/config.json'] = {'data': base64.b64encode(b'{"model_type":"synthetic-draft"}').decode(), 'lfs': False}
        f.save()
        self.acquire_candidate(f, nodes=2, home=0)
        recipe = copy.deepcopy(f.spec['recipe'])
        manifest = recipe['model'].pop('snapshot_manifest')
        recipe['required_snapshots'] = {}
        recipe['engine_args'] += ['--speculative-config', '{"model":"pulsar-snapshot:target/dflash","method":"dflash"}']
        f.spec = freeze({'schema_version': 2, 'kind': 'pulsar-recipe-draft', 'source': f.spec['source'], 'recipe': recipe}, {'target': manifest})
        f.spec_path.write_bytes(pretty_json_bytes(f.spec))
        self.success(f.run('prepare', '--yes', spec=True))
        self.success(f.run('pin', '--yes', spec=True))
        before = copy.deepcopy(Store(f.state).views(spec_id=f.spec['spec_id']))
        content = {row['path']: {str(path.relative_to(row['path'])): (path.read_bytes(), path.stat().st_ino)
                   for path in Path(row['path']).rglob('*') if path.is_file()} for row in before}
        home = Store(f.state).home(manifest['manifest_id'])
        selection = ','.join(f.cfg['nodes'][index]['node_id'] for index in (2, 1))
        plan = self.success(f.run('prepare', '--placement-nodes', selection, '--plan', spec=True))
        actions = plan['snapshots'][0]['actions']
        self.assertEqual([row['action'] for row in actions], ['copy', 'reuse'])
        self.success(f.run('prepare', '--placement-nodes', selection, '--yes', spec=True))
        f.cfg['trace_verification'] = True
        f.save()
        result = self.success(f.run('info', '--placement-nodes', selection, '--full', spec=True))
        target = result['snapshots']['target']
        self.assertEqual([row['node_id'] for row in target['ranks']], [f.cfg['nodes'][index]['node_id'] for index in (2, 1)])
        self.assertEqual(target['home_node_id'], home['node_id'])
        self.assertEqual(result['topology_id'], f.topology['topology_id'])
        self.assertEqual(sum(row['bytes'] for row in f.events('verification-read')), 3*manifest['total_bytes'])
        after = Store(f.state).views(spec_id=f.spec['spec_id'])
        for old in before:
            current = next(row for row in after if row['node_id'] == old['node_id'])
            for field in ('path', 'hub_path', 'rank', 'pinned', 'is_home_view', 'snapshot_manifest_id'):
                self.assertEqual(current[field], old[field])
            self.assertEqual({str(path.relative_to(old['path'])): (path.read_bytes(), path.stat().st_ino)
                             for path in Path(old['path']).rglob('*') if path.is_file()}, content[old['path']])

    def test_selection_rejects_wrong_count_and_duplicate_nodes_before_prepare(self):
        f = self.fixture(nodes=3)
        self.acquire_candidate(f, nodes=2)
        ids = [node['node_id'] for node in f.cfg['nodes']]
        before = len(f.events('node-operation'))
        for selected in (ids[1], ids[1]+','+ids[1], ids[1]+',missing', ids[1]+','+ids[2]+','):
            self.failure(f.run('prepare', '--placement-nodes', selected, '--yes', spec=True))
        self.assertEqual(len(f.events('node-operation')), before)

    def test_source_plan_is_not_acquisition_and_execution_requires_exact_commit(self):
        f = self.fixture(nodes=2)
        plan = self.success(f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", "main", "--plan"))
        self.assertEqual(plan["snapshot_revision"], f.cfg["revision"])
        self.assertFalse(f.events("download"))
        self.assertEqual(Store(f.state).records("homes"), [])
        self.failure(f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", "main", "--yes"), "exact commit")
        self.failure(f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", f.cfg["revision"]), "requires --yes")
        self.assertFalse(f.events("download"))

    def test_verified_reuse_does_not_download_again(self):
        f = self.fixture(nodes=2)
        first = self.acquire_candidate(f, nodes=2, home=1)
        self.assertEqual(len(f.events("download")), 1)
        again = self.success(f.run("acquire", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(again["home"]["path"], first["home"]["path"])
        self.assertEqual(len(f.events("download")), 1)
        self.failure(f.run("acquire", "--node", "node-0", "--yes", spec=True), "explicit move")
        self.assertEqual(len(f.events("download")), 1)

    def test_missing_or_changed_bytes_never_trigger_download_fallback(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.success(f.run("prepare", "--yes", spec=True))
        path = Path(acquired["home"]["path"])
        (path / "unexpected").write_bytes(b"extra")
        count = len(f.events("download"))
        self.failure(f.run("info", spec=True))
        self.failure(f.run("prepare", "--yes", spec=True))
        self.assertEqual(len(f.events("download")), count)
        (path / "unexpected").unlink()
        (path / "weights.bin").unlink()
        self.failure(f.run("info", "--full", spec=True))
        self.assertEqual(len(f.events("download")), count)

    def test_unreachable_node_blocks_acquisition_and_destructive_actions(self):
        f = self.fixture(nodes=2)
        f.cfg["nodes"][1]["available"] = False; f.save()
        self.failure(f.acquire())
        self.assertFalse(f.events("download"))
        f.cfg["nodes"][1]["available"] = True; f.save()
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        f.cfg["nodes"][1]["available"] = False; f.save()
        self.failure(f.run("purge", "--yes", spec=True))
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())

    def test_stopped_container_reference_blocks_purge(self):
        f = self.fixture()
        self.acquire_candidate(f)
        self.success(f.run("prepare", "--yes", spec=True))
        info = self.success(f.run("info", spec=True))
        f.cfg["nodes"][0]["containers"] = [{"Id": "synthetic-stopped-container", "State": {"Running": False},
            "Mounts": [{"Source": info["ranks"][0]["hub_path"]}], "Config": {"Labels": {}}}]
        f.save()
        self.failure(f.run("purge", "--yes", spec=True), "container")
        self.assertEqual(len(Store(f.state).views(spec_id=f.spec["spec_id"])), 1)

    def test_catalog_home_cannot_be_discarded_without_archive(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.catalog(f)
        self.failure(f.run("remove", "--yes", spec=True), "archive")
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())

    def test_archive_corruption_blocks_removal_and_no_delete_interface_exists(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.success(f.run("archive", "create", "--yes", spec=True))
        self.catalog(f)
        archive_file = next(f.archive.rglob("weights.bin")); archive_file.write_bytes(b"corrupt")
        self.failure(f.run("archive", "verify", spec=True))
        self.failure(f.run("remove", "--yes", spec=True))
        self.failure(f.run("archive", "delete", "--yes", spec=True))
        self.assertTrue(archive_file.exists())
        self.assertTrue(Path(acquired["home"]["path"]).exists())

    def test_archive_verify_is_read_only(self):
        f = self.fixture()
        self.acquire_candidate(f)
        self.success(f.run("archive", "create", "--yes", spec=True))
        manifest_id = f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        store = Store(f.state)
        store.remove("archives", manifest_id)
        before = sorted(path.relative_to(f.state) for path in f.state.rglob("*"))
        verified = self.success(f.run("archive", "verify", spec=True))
        after = sorted(path.relative_to(f.state) for path in f.state.rglob("*"))
        self.assertTrue(verified["verified"])
        self.assertEqual(after, before)
        self.assertIsNone(Store(f.state).get("archives", manifest_id))

    def test_lab_discard_requires_explicit_acknowledgement(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.failure(f.run("remove", "--yes", spec=True), "discard")
        self.success(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertFalse(Path(acquired["home"]["path"]).exists())
        self.assertTrue(f.spec_path.exists())

    def test_unarchived_lab_discard_with_no_archive_configuration(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        f.env["PULSAR_COLD_ROOT"] = ""
        self.success(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertFalse(Path(acquired["home"]["path"]).exists())

    def test_check_saves_preparation_and_archive_presence_separately(self):
        f = self.fixture()
        self.acquire_candidate(f)
        unprepared = f.run("check", spec=True)
        self.failure(unprepared)
        before = json.loads(unprepared.stdout)["observation"]
        self.assertNotEqual(before["local_state"], "ready")
        self.success(f.run("prepare", "--yes", spec=True))
        self.success(f.run("archive", "create", "--yes", spec=True))
        observed = self.success(f.run("check", spec=True))["observation"]
        self.assertEqual(observed["local_state"], "ready")
        self.assertEqual(observed["archive_state"], "present")
        saved = Store(f.state).get("observations", f.spec["spec_id"])
        self.assertEqual(saved["local_state"], "ready")
        self.assertIn("checked_at", saved)

    def test_movement_requires_dependencies_cleared(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        self.failure(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.success(f.run("purge", "--yes", spec=True))
        moved = self.success(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(moved["home"]["node_id"], "node-1")
        self.assertFalse(Path(acquired["home"]["path"]).exists())
        self.success(f.run("prepare", "--yes", spec=True))

    def test_home_operations_accept_the_hostname_that_output_shows(self):
        f = self.fixture(nodes=2)
        acquired = f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", f.cfg["revision"],
                         "--node", "rank-1", "--manifest-out", f.manifest_path, "--yes")
        self.assertEqual(self.success(acquired)["home"]["node_id"], "node-1")
        self.assertIn("[acquire 1/4] staging on rank-1", acquired.stderr)
        manifest_id = f.candidate(2)["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        self.success(f.run("archive", "create", "--yes", spec=True))
        moved = self.success(f.run("move", "--node", "rank-0", "--yes", spec=True))
        # Records keep the node_id; the hostname only selected the node.
        self.assertEqual(moved["home"]["node_id"], "node-0")
        self.assertEqual(Store(f.state).home(manifest_id)["node_id"], "node-0")
        self.success(f.run("remove", "--yes", spec=True))
        f.cfg["hub_unavailable"] = True; f.save()
        restored = self.success(f.run("restore", "--node", "rank-1", "--yes", spec=True))
        self.assertEqual(restored["home"]["node_id"], "node-1")
        self.assertEqual(Store(f.state).home(manifest_id)["node_id"], "node-1")
        self.assertEqual(len(f.events("download")), 1)

    def test_home_operations_refuse_a_node_selector_that_names_no_node_or_several(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2, home=1)
        # node-0's hostname is node-1's node_id: as an operator selector
        # "node-1" names both nodes, while the home record still names node-1.
        with open(f.env["BASH_ENV"], "a") as stream:
            stream.write('eval "confirmed_$(declare -f load_cluster_topology)"\n'
                         "load_cluster_topology() { confirmed_load_cluster_topology; CLUSTER_NODE_HOSTNAMES=(node-1 rank-1); }\n")
        contacted = len(f.events("node-operation"))
        for selector, reason in (("node-1", "node selector 'node-1' is ambiguous in the confirmed topology"),
                                 ("spark-9", "node 'spark-9' is not present in the confirmed topology")):
            for operation in ("acquire", "restore", "move"):
                with self.subTest(operation=operation, selector=selector):
                    result = f.run(operation, "--node", selector, "--plan", spec=True)
                    self.failure(result, reason)
                    self.assertIn(f"--node '{selector}' does not select exactly one confirmed node", result.stderr)
        self.assertEqual(len(f.events("node-operation")), contacted)
        plan = self.success(f.run("move", "--node", "rank-1", "--plan", spec=True))
        self.assertEqual((plan["destination_node"], plan["transfer_route"]), ("node-1", "already-home"))
        # Saved records are matched by node_id alone, so the ambiguous
        # hostname does not affect operations that start from the home record.
        archive = self.success(f.run("archive", "create", "--yes", spec=True))
        self.assertTrue(archive["verified"])
        manifest_id = f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        self.assertEqual(Store(f.state).home(manifest_id)["node_id"], "node-1")

    def test_unconfirmed_overlay_placement_is_named_instead_of_node_option(self):
        f = self.fixture()
        self.acquire_candidate(f)
        overlay = json.loads((f.root / "overlay.json").read_text())
        overlay["defaults"]["placement"] = {"node_id": "node-9"}
        (f.root / "overlay.json").write_text(json.dumps(overlay))
        for operation in ("acquire", "restore", "move"):
            with self.subTest(operation=operation):
                result = f.run(operation, "--plan", spec=True)
                self.failure(result, "overlay placement.node_id 'node-9' does not select exactly one confirmed "
                                     "node; correct the placement in the deployment overlay")
                self.assertNotIn("--node", result.stderr)

    def test_wrong_expected_git_sha256_never_publishes_a_home(self):
        f = self.fixture()
        files = [{"path": name, "size": len(base64.b64decode(row["data"])),
                  "sha256": hashlib.sha256(base64.b64decode(row["data"])).hexdigest()}
                 for name, row in sorted(f.cfg["files"].items())]
        files[0]["sha256"] = "e" * 64
        manifest = build_snapshot_manifest(model_id=f.cfg["model_id"], snapshot_revision=f.cfg["revision"], files=files)
        f.manifest_path.write_bytes(pretty_json_bytes(manifest))
        f.candidate()
        self.failure(f.run("acquire", "--node", "node-0", "--yes", spec=True))
        self.assertEqual(Store(f.state).records("homes"), [])
        parent = Path(f.cfg["nodes"][0]["home_root"]) / "pulsar-homes"
        published = [p for p in parent.iterdir() if not p.name.startswith(".pending-")] if parent.exists() else []
        self.assertEqual(published, [])

    def test_lost_prepared_publication_reply_blocks_removal_until_explicit_cleanup(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        f.cfg["node_fault"] = {"operation": "publish-view", "rank": 1, "after": True}; f.save()
        self.failure(f.run("prepare", "--yes", spec=True))
        self.failure(f.run("info", spec=True))
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())
        f.cfg.pop("node_fault"); f.save()
        self.success(f.run("purge", "--yes", spec=True))
        self.success(f.run("prepare", "--yes", spec=True))
        self.assertEqual(len(self.success(f.run("info", "--full", spec=True))["ranks"]), 2)

    def test_partial_pin_remains_protected_and_explicit_retry_recovers(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        f.cfg["node_fault"] = {"operation": "pin-view", "rank": 1, "after": True}; f.save()
        self.failure(f.run("pin", "--yes", spec=True))
        node_state = Store(Path(f.cfg["nodes"][1]["view_root"]) / ".pulsar-node")
        self.assertTrue(any(row["pinned"] for row in node_state.views(spec_id=f.spec["spec_id"])))
        self.failure(f.run("purge", "--yes", spec=True), "pinned")
        f.cfg.pop("node_fault"); f.save()
        self.success(f.run("pin", "--yes", spec=True))
        self.assertTrue(all(row["pinned"] for row in self.success(f.run("info", spec=True))["ranks"]))
        self.success(f.run("unpin", "--yes", spec=True))
        self.success(f.run("purge", "--yes", spec=True))

    def test_overlay_home_root_and_offline_verified_reuse(self):
        f = self.fixture()
        files = [{"path": name, "size": len(base64.b64decode(row["data"])),
                  "sha256": hashlib.sha256(base64.b64decode(row["data"])).hexdigest()}
                 for name, row in sorted(f.cfg["files"].items())]
        manifest = build_snapshot_manifest(model_id=f.cfg["model_id"], snapshot_revision=f.cfg["revision"], files=files)
        f.manifest_path.write_bytes(pretty_json_bytes(manifest)); f.candidate()
        selected = f.root / "selected-model-storage"
        f.cfg["permitted_roots"].append(str(selected)); f.save()
        overlay_path = f.root / "overlay.json"
        overlay = json.loads(overlay_path.read_text()); overlay["defaults"]["cache_root"] = str(selected)
        overlay_path.write_text(json.dumps(overlay))
        result = self.success(f.run("acquire", "--yes", spec=True))
        self.assertTrue(Path(result["home"]["path"]).is_relative_to(selected))
        f.cfg["hub_unavailable"] = True; f.save()
        reused = self.success(f.run("acquire", "--yes", spec=True))
        self.assertEqual(result["home"]["path"], reused["home"]["path"])
        self.assertEqual(len(f.events("download")), 1)

    def test_controller_home_publication_failure_is_not_false_success(self):
        f = self.fixture()
        f.cfg["controller_fault"] = "save-home"; f.save()
        failed = f.acquire()
        self.failure(failed)
        self.assertEqual(Store(f.state).records("homes"), [])
        root = Path(f.cfg["nodes"][0]["home_root"]) / "pulsar-homes"
        manifests = list(root.glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = read_json(manifests[0])
        verify_tree(manifests[0].parent / "snapshots" / manifest["snapshot_revision"], manifest)
        f.cfg.pop("controller_fault"); f.save()
        self.success(f.acquire())
        self.assertEqual(len(f.events("download")), 1)
        self.assertEqual(len(Store(f.state).records("homes")), 1)

    def test_controller_prepared_publication_failure_retains_node_proof(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        f.cfg["controller_fault"] = "save-views"; f.save()
        failed = f.run("prepare", "--yes", spec=True)
        self.failure(failed)
        self.assertNotIn('"prepared": 2', failed.stdout)
        self.assertEqual(Store(f.state).views(spec_id=f.spec["spec_id"]), [])
        for node in f.cfg["nodes"]:
            records = Store(Path(node["view_root"]) / ".pulsar-node").views(spec_id=f.spec["spec_id"])
            self.assertEqual(len(records), 1)
            verify_tree(records[0]["path"], f.spec["recipe"]["model"]["snapshot_manifest"])
        self.failure(f.run("info", spec=True))
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        f.cfg.pop("controller_fault"); f.save()
        transfers = len(f.events("transfer"))
        self.success(f.run("prepare", "--yes", spec=True))
        self.assertEqual(len(f.events("transfer")), transfers)
        self.assertEqual(len(self.success(f.run("info", spec=True))["ranks"]), 2)

    def test_changed_metadata_is_rehashed_once_and_cached_without_transfer(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        info = self.success(f.run("info", spec=True))
        transfers = len(f.events("transfer"))
        for record in info["ranks"]:
            path = Path(record["path"]) / "weights.bin"
            observed = path.stat()
            os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
        refreshed = self.success(f.run("info", spec=True))
        self.assertEqual([row["verification"]["method"] for row in refreshed["ranks"]], ["sha256", "sha256"])
        cached = self.success(f.run("info", spec=True))
        self.assertEqual([row["verification"]["method"] for row in cached["ranks"]], ["metadata", "metadata"])
        self.assertEqual(len(f.events("transfer")), transfers)
        node_store = Store(Path(f.cfg["nodes"][1]["view_root"]) / ".pulsar-node")
        self.assertEqual(node_store.views(spec_id=f.spec["spec_id"])[0]["verification"]["files"],
                         cached["ranks"][1]["verification"]["files"])

    def test_lost_registered_home_restores_on_another_confirmed_node(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("archive", "create", "--yes", spec=True))
        previous = Store(f.state).home(f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"])
        shutil.rmtree(acquired["home"]["hub_path"])
        f.cfg["hub_unavailable"] = True; f.save()
        restored = self.success(f.run("restore", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(restored["home"]["node_id"], "node-1")
        registered = Store(f.state).home(f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"])
        self.assertNotEqual(registered["path"], previous["path"])
        verify_tree(registered["path"], f.spec["recipe"]["model"]["snapshot_manifest"])
        self.assertEqual(len(f.events("download")), 1)

    def test_move_retry_repairs_registration_after_source_retirement(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        f.cfg["controller_fault"] = "save-home"; f.save()
        self.failure(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.assertFalse(Path(acquired["home"]["hub_path"]).exists())
        manifest = f.spec["recipe"]["model"]["snapshot_manifest"]
        destination = Path(f.cfg["nodes"][1]["home_root"]) / "pulsar-homes" / manifest["manifest_id"]
        verify_tree(payload(destination, manifest), manifest)
        self.assertEqual(Store(f.state).home(manifest["manifest_id"])["node_id"], "node-0")
        transfers = len(f.events("transfer"))
        f.cfg.pop("controller_fault"); f.save()
        repaired = self.success(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(repaired["home"]["node_id"], "node-1")
        self.assertEqual(Store(f.state).home(manifest["manifest_id"])["path"], str(payload(destination, manifest)))
        self.assertEqual(len(f.events("transfer")), transfers)

    def test_home_rank_requires_actual_home_and_node_records_cannot_be_transplanted(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        manifest = f.spec["recipe"]["model"]["snapshot_manifest"]
        controller = Store(f.state)
        original = next(row for row in controller.views(spec_id=f.spec["spec_id"]) if row["node_id"] == "node-0")
        duplicate, stamp = copy_snapshot(Path(acquired["home"]["path"]),
            Path(f.cfg["nodes"][0]["view_root"]) / "duplicate", manifest)
        alternate = {**original, "hub_path": str(duplicate), "path": str(payload(duplicate, manifest)), "verification": stamp}
        controller.put("views", view_key(f.spec["spec_id"], "node-0"), alternate)
        self.failure(f.run("info", spec=True), "home directly")
        self.assertTrue(Path(acquired["home"]["path"]).exists())
        controller.put("views", view_key(f.spec["spec_id"], "node-0"), original)
        other_node = Store(Path(f.cfg["nodes"][1]["view_root"]) / ".pulsar-node")
        other_node.put("views", view_key(f.spec["spec_id"], "node-0"), original)
        self.failure(f.run("prepare", "--yes", spec=True))
        self.failure(f.run("purge", "--yes", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())


if __name__ == "__main__": unittest.main()
