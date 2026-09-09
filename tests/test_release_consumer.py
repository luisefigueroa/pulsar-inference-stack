"""Spec-only catalog and exact serving contract tests."""
import copy
import json
import pathlib
import sys
import tempfile
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import load_spec, pretty_json_bytes, verify_snapshot_manifest
from scripts import release_consumer as consumer


class Consumer(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name)
        self.spec=load_spec(ROOT/'tests/fixtures/contracts/spec.json')
        self.spec['review']={'status':'stable','reviewer':'maintainer','reviewed_at':'2026-09-09T00:00:00Z'}
        (self.root/'releases').mkdir()
        (self.root/'releases'/f'{self.spec["spec_id"]}.json').write_bytes(pretty_json_bytes(self.spec))

    def test_empty_catalog(self):
        self.assertEqual(consumer.list_releases(self.root/'new'),[])

    def test_manifest_verification_catches_changed_files(self):
        manifest=copy.deepcopy(self.spec['recipe']['model']['snapshot_manifest'])
        self.assertEqual(verify_snapshot_manifest(manifest),manifest)
        manifest['files'][0]['sha256']='b'*64
        with self.assertRaises(ValueError):verify_snapshot_manifest(manifest)

    def test_site_values_need_no_local_storage(self):
        variables=consumer.spec_shell_values(self.spec,dict(port=8000,served_name='example',placement=None,cache_root=None),'example/image')
        self.assertEqual(variables['MODEL'],'example/model')
        self.assertEqual(variables['GPU_MEM_UTIL'],'0.8')
        self.assertEqual(variables['NODES'],'1')

    def test_withdrawal_reason_and_exact_start_adapter(self):
        self.spec['review'].update(status='withdrawn',reason='Later behavior warrants caution.')
        path=self.root/'releases'/f'{self.spec["spec_id"]}.json';path.write_bytes(pretty_json_bytes(self.spec))
        rows=consumer.list_releases(self.root)
        self.assertEqual(rows[0]['withdrawal_reason'],'Later behavior warrants caution.')
        variables=consumer.spec_shell_values(consumer.load_release(self.root,self.spec['spec_id']),dict(port=8000,served_name='example'),'example/image')
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

    def test_site_settings_do_not_change_spec_identity(self):
        before=copy.deepcopy(self.spec)
        variables=consumer.spec_shell_values(self.spec,dict(port=9000,served_name='other',cache_root='/var/tmp/example'),'example/image')
        self.assertEqual(variables['PORT'],'9000')
        self.assertEqual(variables['HF_CACHE'],'/var/tmp/example')
        self.assertEqual(self.spec,before)

    def test_overlay_rejects_recipe_keys(self):
        path=self.root/'overlay.json'
        path.write_text(json.dumps(dict(schema_version=1,kind='pulsar-deployment-overlay',defaults=dict(engine_args=['--bad']),specs={})))
        with self.assertRaisesRegex(ValueError,'recipe'):consumer.load_overlay(path)


if __name__=='__main__':unittest.main()
