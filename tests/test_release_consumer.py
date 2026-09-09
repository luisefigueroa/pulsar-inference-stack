"""Spec-only catalog and exact serving contract tests."""
import copy
import json
import pathlib
import sys
import tempfile
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import (load_spec, pretty_json_bytes, spec_id_for,
                          runtime_contract_id, verify_snapshot_manifest,
                          build_profile_identity)
from release_spec.identity import argv_from_identity
from scripts import release_consumer as consumer


class Consumer(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name)
        self.spec=load_spec(ROOT/'release_spec/tests/fixtures/golden_released.json')
        self.spec['identity']['engine_args'] += ['--gpu-memory-utilization','0.8']
        self.spec['spec_id']=spec_id_for(self.spec['identity'])
        self.spec['launch_contract']['argv']=argv_from_identity(self.spec['identity'])
        (self.root/'releases').mkdir()
        (self.root/'releases'/f'{self.spec["spec_id"]}.json').write_bytes(pretty_json_bytes(self.spec))

    def test_empty_catalog(self):
        self.assertEqual(consumer.list_releases(self.root/'new'),[])

    def test_script_projector_is_the_supported_release_spec_api(self):
        self.assertIs(consumer.build_profile_identity, build_profile_identity)

    def test_manifest_verification_catches_changed_files(self):
        manifest=copy.deepcopy(self.spec['identity']['snapshot_manifest'])
        self.assertEqual(verify_snapshot_manifest(manifest),manifest)
        manifest['files'][0]['sha256']='b'*64
        with self.assertRaises(ValueError):verify_snapshot_manifest(manifest)

    def test_exact_contract_rejects_every_kind_of_drift(self):
        expected=consumer.comparable_contract_from_spec(self.spec)
        consumer.require_exact_contract(expected,expected)
        for field,change in [('argv',['--max-model-len','1']),('container_env',['DRIFT=1']),('image_digest','sha256:'+'d'*64),('geometry',{'nodes':7})]:
            actual=copy.deepcopy(expected);actual[field]=change
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,field):
                consumer.require_exact_contract(actual,expected)

    def test_projection_needs_no_local_storage(self):
        variables=consumer.spec_profile_variables(self.spec,dict(port=8000,served_name='example',placement=None,cache_root=None),'example/image')
        payload=consumer.project_profile(profile=self.spec['spec_id'],model_id=variables['MODEL'],image=variables['IMAGE'],nodes=int(variables['NODES']),gpu_mem_util=variables['GPU_MEM_UTIL'],engine_args=variables['ENGINE_ARGS'],container_env=variables['CONTAINER_ENV'],spec_decode_args=[],platform_id=variables['SPEC_PLATFORM_ID'],recommended_spec=False,library_dir=None,repo_root=self.root)
        self.assertEqual(payload['manifest'],'spec')
        self.assertEqual(payload['identities'][0]['comparison'],'equal')
        self.assertEqual(payload['identities'][0]['review_status'],'stable')
        self.assertNotIn('receipt',payload)

    def test_withdrawal_reason_and_exact_start_adapter(self):
        self.spec['review'].update(status='withdrawn',reason='Later behavior warrants caution.')
        path=self.root/'releases'/f'{self.spec["spec_id"]}.json';path.write_bytes(pretty_json_bytes(self.spec))
        rows=consumer.list_releases(self.root)
        self.assertEqual(rows[0]['withdrawal_reason'],'Later behavior warrants caution.')
        variables=consumer.spec_profile_variables(consumer.load_release(self.root,self.spec['spec_id']),dict(port=8000,served_name='example'),'example/image')
        self.assertEqual(variables['RECOMMENDED_SPEC'],'0')
        self.assertEqual(variables['CONF_NAME'],self.spec['spec_id'])

    def test_nullable_catalog_metadata_loads_and_lists(self):
        self.spec['state']=None;self.spec['review']=None
        path=self.root/'releases'/f'{self.spec["spec_id"]}.json'
        path.write_bytes(pretty_json_bytes(self.spec))
        loaded=consumer.load_release(self.root,self.spec['spec_id'])
        self.assertIsNone(loaded['state']);self.assertIsNone(loaded['review'])
        row=consumer.list_releases(self.root)[0]
        self.assertIsNone(row['state']);self.assertIsNone(row['review_status'])
        self.assertIsNone(row['withdrawal_reason'])

    def test_deployment_changes_do_not_change_runtime_id(self):
        before=runtime_contract_id(self.spec)
        changed=copy.deepcopy(self.spec);changed['launch_contract']['stack_version']='different-version'
        self.assertEqual(runtime_contract_id(changed),before)
        variables=consumer.spec_profile_variables(changed,dict(port=9000,served_name='other',cache_root='/var/tmp/example'),'example/image')
        self.assertEqual(variables['PORT'],'9000')
        self.assertEqual(variables['HF_CACHE'],'/var/tmp/example')

    def test_overlay_rejects_recipe_keys(self):
        path=self.root/'overlay.json'
        path.write_text(json.dumps(dict(schema_version=1,kind='pulsar-deployment-overlay',defaults=dict(engine_args=['--bad']),specs={})))
        with self.assertRaisesRegex(ValueError,'recipe'):consumer.load_overlay(path)


if __name__=='__main__':unittest.main()
