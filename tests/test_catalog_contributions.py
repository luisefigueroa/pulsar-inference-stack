"""Verify actual temporary catalog trees, not only isolated schema examples."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_release_contribution import make_contribution
from release_spec import pretty_json_bytes, spec_id_for, runtime_contract_id
from release_spec.identity import argv_from_identity
from release_spec.summary import summary_document, archive_proof, verify_summary
module_spec=importlib.util.spec_from_file_location('catalog_check',ROOT/'scripts/check-catalog.py')
catalog=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(catalog)


def catalog_fixture(root,nodes=1):
    root=Path(root)
    spec,run,old_path,old_directory=make_contribution(root,nodes)
    if nodes>1:spec['identity']['engine_args'] += ['--distributed-executor-backend','mp']
    spec['identity']['engine_args'] += ['--gpu-memory-utilization','0.8']
    spec['spec_id']=spec_id_for(spec['identity'])
    spec['launch_contract']['argv']=argv_from_identity(spec['identity'])
    spec_id=spec['spec_id'];contract_id=runtime_contract_id(spec)
    base=Path('results/baseline-v1')/spec_id;directory=root/base;directory.parent.mkdir(parents=True)
    old_directory.rename(directory);old_path.unlink()
    identity_file=directory/'verify-snapshot-manifest.json';identity=json.loads(identity_file.read_text())
    identity['verify-snapshot-manifest']['spec_id']=spec_id
    identity_bytes=pretty_json_bytes(identity);identity_file.write_bytes(identity_bytes)
    run['spec_id']=spec_id;run['launch_contract_id']=contract_id
    for side in ('ranks_before','ranks_after'):
        for row in run[side]:row['launch_contract_id']=contract_id
    run['measurement_sha256']['verify-snapshot-manifest']=hashlib.sha256(identity_bytes).hexdigest()
    run_bytes=pretty_json_bytes(run);(directory/'run.json').write_bytes(run_bytes)
    for row in spec['evidence']:
        filename='run.json' if row['id']=='baseline-run' else row['id']+'.json'
        row['path']=(base/filename).as_posix()
        row['sha256']=hashlib.sha256((directory/filename).read_bytes()).hexdigest()
    manifest=spec['identity']['snapshot_manifest']
    proof=dict(schema_version=1,kind='pulsar-archive-verification',snapshot_manifest_id=manifest['manifest_id'],verified=True,file_count=manifest['file_count'],total_bytes=manifest['total_bytes'])
    summary=summary_document(spec,run,proof,'2026-09-04T00:00:00.123456Z')
    (directory/'summary.json').write_bytes(pretty_json_bytes(summary))
    releases=root/'releases';releases.mkdir()
    path=releases/f'{spec_id}.json';path.write_bytes(pretty_json_bytes(spec))
    return spec,run,summary,path,directory


class CatalogContributions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.spec,self.run,self.summary,self.path,self.directory=catalog_fixture(self.root)

    def check(self):return catalog.check_catalog(self.root)

    def write_spec(self):self.path.write_bytes(pretty_json_bytes(self.spec))

    def test_actual_compact_contribution_and_empty_checkout(self):
        self.assertEqual(self.check()['evidence_file_count'],8)
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(catalog.check_catalog(empty)['spec_count'],0)
            root=Path(empty);(root/'results').mkdir();(root/'results/README.md').write_text('# Qualification evidence\n')
            self.assertEqual(catalog.check_catalog(root)['spec_count'],0)

    def test_two_node_current_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog_fixture(temp,2)
            self.assertTrue(catalog.check_catalog(temp)['verified'])

    def test_withdrawn_recipe_retains_same_baseline_provenance(self):
        self.spec['review'].update(status='withdrawn',reason='A later observation requires caution.')
        self.write_spec();self.assertTrue(self.check()['verified'])

    def test_experimental_recipe_is_in_the_catalog(self):
        self.spec['review']['status']='experimental'
        self.write_spec();self.assertTrue(self.check()['verified'])

    def test_changed_hash_missing_gate_and_weakened_threshold(self):
        for mode in ('hash','gate','threshold'):
            with tempfile.TemporaryDirectory() as temp:
                spec,run,summary,path,directory=catalog_fixture(temp)
                if mode=='hash':(directory/'serve-smoke.json').write_text('{}')
                elif mode=='gate':spec['measurements'].pop();path.write_bytes(pretty_json_bytes(spec))
                else:spec['measurements'][0]['thresholds'][0]['value']='0';path.write_bytes(pretty_json_bytes(spec))
                with self.subTest(mode=mode),self.assertRaises(ValueError):catalog.check_catalog(temp)

    def test_extra_raw_or_foreign_files_anywhere_in_results_are_rejected(self):
        for relative in ('raw/captures.jsonl','baseline-v1/unrelated.txt','baseline-v1/'+self.spec['spec_id']+'/raw.json'):
            path=self.root/'results'/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private raw capture')
            with self.subTest(path=relative),self.assertRaisesRegex(ValueError,'unreferenced'):self.check()
            path.unlink()
            while path.parent!=self.directory and path.parent!=self.root/'results' and not any(path.parent.iterdir()):
                path=path.parent;path.rmdir()

    def test_noncanonical_evidence_layout_rejected_even_if_all_hashes_match(self):
        target=self.directory/'serve-smoke-renamed.json';(self.directory/'serve-smoke.json').rename(target)
        next(r for r in self.spec['evidence'] if r['id']=='serve-smoke')['path']=target.relative_to(self.root).as_posix()
        self.write_spec()
        with self.assertRaisesRegex(ValueError,'canonical compact layout'):self.check()

    def test_summary_is_closed_and_exactly_bound(self):
        for change in ('private','criterion','archive','version','evidence'):
            summary=copy.deepcopy(self.summary)
            if change=='private':summary['raw_capture']='must not enter public contribution'
            elif change=='criterion':summary['criteria'].pop()
            elif change=='archive':summary['archive_verification']['snapshot_manifest_id']='d'*64
            elif change=='version':summary['schema_version']=True
            else:summary['evidence_sha256']['serve-smoke']='e'*64
            (self.directory/'summary.json').write_bytes(pretty_json_bytes(summary))
            with self.subTest(change=change),self.assertRaises(ValueError):self.check()

    def test_malformed_spec_and_wrong_filename_are_rejected(self):
        self.path.write_text('{}')
        with self.assertRaises(ValueError):self.check()
        self.write_spec();self.path.rename(self.root/'releases'/('d'*64+'.json'))
        with self.assertRaisesRegex(ValueError,'filename'):self.check()

    def test_unreleased_and_validated_without_deep_suite_are_rejected(self):
        measured=copy.deepcopy(self.spec);measured['state']='measured';measured['review']={}
        self.path.write_bytes(pretty_json_bytes(measured))
        with self.assertRaises(ValueError):self.check()
        self.spec['review']['status']='failed'
        self.write_spec();self.assertTrue(self.check()['verified'])
        self.spec['review']['status']='validated'
        self.write_spec()
        with self.assertRaises(ValueError):self.check()

    def test_symlink_and_special_file_do_not_hide_private_content(self):
        extra=self.directory/'extra';extra.symlink_to('/dev/null')
        with self.assertRaisesRegex(ValueError,'regular file'):self.check()
        extra.unlink();os.mkfifo(extra)
        with self.assertRaisesRegex(ValueError,'regular file'):self.check()

    def test_current_consumer_drift_is_detected(self):
        original=catalog.consumer.spec_profile_variables
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);result['ENGINE_ARGS'] += ['--max-model-len','1'];return result
        with patch.object(catalog.consumer,'spec_profile_variables',changed):
            with self.assertRaisesRegex(ValueError,'reproduce'):self.check()

    def test_operator_overlay_and_platform_environment_do_not_change_ci_projection(self):
        with patch.dict(os.environ,{'PULSAR_OVERLAY_PATH':'/nonexistent/operator-overlay','PULSAR_PLATFORM_FILE':'/nonexistent/operator-platform','PULSAR_RELEASES_ROOT':'/nonexistent/operator-catalog','VLLM_IMAGE_MAINLINE':'other/image'}):
            self.assertTrue(self.check()['verified'])


if __name__=='__main__':unittest.main()
