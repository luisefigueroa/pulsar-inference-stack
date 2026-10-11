"""Verify actual temporary catalog trees, not only isolated schema examples."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_release_contribution import make_contribution
from release_spec import pretty_json_bytes, spec_id_for, runtime_contract_id
from release_spec.identity import argv_from_identity
from release_spec.contribution import verify_compact_evidence
from release_spec.summary import summary_document, archive_proof, verify_summary
module_spec=importlib.util.spec_from_file_location('catalog_check',ROOT/'scripts/check-catalog.py')
catalog=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(catalog)
compat_spec=importlib.util.spec_from_file_location('launch_compatibility',ROOT/'scripts/check-launch-compatibility.py')
compat=importlib.util.module_from_spec(compat_spec);compat_spec.loader.exec_module(compat)


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

    def test_schema_valid_catalog_and_empty_checkout(self):
        result=self.check()
        self.assertEqual(result['spec_count'],1)
        self.assertEqual(result['declared_evidence_count'],7)
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(catalog.check_catalog(empty)['spec_count'],0)
            root=Path(empty);(root/'results').mkdir();(root/'results/README.md').write_text('# Qualification evidence\n')
            self.assertEqual(catalog.check_catalog(root)['spec_count'],0)

    def test_launch_compatibility_is_separate_from_catalog_membership(self):
        with tempfile.TemporaryDirectory() as temp:
            spec,run,summary,path,directory=catalog_fixture(temp,2)
            self.assertTrue(catalog.check_catalog(temp)['verified'])
            with self.assertRaisesRegex(compat.CompatibilityError,'schema 2'):
                compat.check_launch_compatibility(spec)

    def test_schema_valid_but_unsupported_platform_remains_catalogable(self):
        from release_spec import serving
        spec=serving.load_spec(ROOT/'tests/fixtures/contracts/spec.json')
        spec['recipe']['geometry']['platform_id']='future-platform'
        spec['spec_id']=serving.spec_id(spec['recipe'])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'releases').mkdir()
            (root/'releases'/(spec['spec_id']+'.json')).write_bytes(pretty_json_bytes(spec))
            self.assertTrue(catalog.check_catalog(root)['verified'])
            with self.assertRaises(compat.CompatibilityError): compat.check_launch_compatibility(spec)

    def test_current_candidate_specs_need_no_measurements_for_catalog_admission(self):
        from release_spec import serving
        fixtures = ROOT / 'tests/fixtures/contracts'
        manifest = json.loads((fixtures / 'manifest.json').read_text())
        for version in (1, 2):
            draft = json.loads((fixtures / 'draft.json').read_text())
            draft['schema_version'] = version
            if version == 2:
                draft['recipe']['required_snapshots'] = {}
            spec = serving.freeze(draft, manifest if version == 1 else {'target': manifest})
            with self.subTest(schema=spec['schema_version']), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'releases').mkdir()
                path = root / 'releases' / (spec['spec_id'] + '.json')
                raw = pretty_json_bytes(spec)
                path.write_bytes(raw)
                result = catalog.check_catalog(root)
                self.assertTrue(result['verified'])
                self.assertEqual(result['spec_count'], 1)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(json.loads(path.read_bytes())['state'], 'candidate')
                self.assertFalse((root / 'results').exists())

    def test_withdrawn_recipe_retains_same_baseline_provenance(self):
        self.spec['review'].update(status='withdrawn',reason='A later observation requires caution.')
        self.write_spec();self.assertTrue(self.check()['verified'])

    def test_experimental_recipe_is_in_the_catalog(self):
        self.spec['review']['status']='experimental'
        self.write_spec();self.assertTrue(self.check()['verified'])

    def test_catalog_does_not_gate_on_evidence_or_extra_results(self):
        (self.directory/'serve-smoke.json').write_text('{}')
        extra=self.root/'results/raw/captures.jsonl';extra.parent.mkdir();extra.write_text('synthetic output')
        self.assertTrue(self.check()['verified'])
        with self.assertRaises(ValueError):
            verify_compact_evidence(self.path,self.root,self.directory/'run.json')

    def test_summary_is_closed_and_exactly_bound(self):
        for change in ('private','criterion','archive','version','evidence'):
            summary=copy.deepcopy(self.summary)
            if change=='private':summary['raw_capture']='must not enter public contribution'
            elif change=='criterion':summary['criteria'].pop()
            elif change=='archive':summary['archive_verification']['snapshot_manifest_id']='d'*64
            elif change=='version':summary['schema_version']=True
            else:summary['evidence_sha256']['serve-smoke']='e'*64
            (self.directory/'summary.json').write_bytes(pretty_json_bytes(summary))
            with self.subTest(change=change),self.assertRaises(ValueError):
                verify_summary(summary,self.spec,self.run)

    def test_summary_cannot_report_pass_for_failed_baseline(self):
        self.spec['measurements'][0]['outcome']='fail'
        self.run['proposed_status']='failed'
        proof=self.summary['archive_verification']
        with self.assertRaisesRegex(ValueError,'six passing'):
            summary_document(self.spec,self.run,proof,'2026-09-04T00:00:00Z')

    def test_malformed_spec_and_wrong_filename_are_rejected(self):
        self.path.write_text('{}')
        with self.assertRaises(ValueError):self.check()
        self.write_spec();self.path.rename(self.root/'releases'/('d'*64+'.json'))
        with self.assertRaisesRegex(ValueError,'filename'):self.check()

    def test_state_and_review_do_not_gate_catalog_membership(self):
        for state,review in ((None,None),('measured',{}),('released',None)):
            with self.subTest(state=state,review=review):
                self.spec['state']=state;self.spec['review']=review
                self.write_spec();self.assertTrue(self.check()['verified'])

    def test_symlink_and_special_file_do_not_hide_private_content(self):
        outside=self.root/'outside.json';outside.write_bytes(self.path.read_bytes())
        self.path.unlink();self.path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'regular file'):self.check()
        self.path.unlink();os.mkfifo(self.path)
        with self.assertRaisesRegex(ValueError,'regular file'):self.check()

    def test_operator_overlay_and_platform_environment_do_not_change_ci_projection(self):
        with patch.dict(os.environ,{'PULSAR_OVERLAY_PATH':'/nonexistent/operator-overlay','PULSAR_PLATFORM_FILE':'/nonexistent/operator-platform','PULSAR_RELEASES_ROOT':'/nonexistent/operator-catalog','VLLM_IMAGE_MAINLINE':'other/image'}):
            from release_spec import serving
            current=serving.load_spec(ROOT/'tests/fixtures/contracts/spec.json')
            self.assertTrue(compat.check_launch_compatibility(current)['compatible'])

    def test_removed_spec_leaves_catalog_and_evidence_together(self):
        ledger=self.root/'catalog-removals.json'
        entry={'spec_id':self.spec['spec_id'],'removed_at':'2026-09-26','reason':'Superseded'}
        ledger.write_text(json.dumps({'schema_version':1,'kind':'pulsar-catalog-removals','removals':[entry]}))
        with self.assertRaisesRegex(catalog.CatalogError,'recorded as removed'):self.check()
        self.path.unlink()
        with self.assertRaisesRegex(catalog.CatalogError,'evidence remains'):self.check()
        shutil.rmtree(self.directory)
        self.assertEqual(self.check()['spec_count'],0)
        ledger.write_text(json.dumps({'schema_version':1,'kind':'pulsar-catalog-removals','removals':[{**entry,'spec_id':'E'*64}]}))
        with self.assertRaisesRegex(ValueError,'complete spec id'):self.check()
        ledger.unlink();ledger.symlink_to(self.root/'missing.json')
        with self.assertRaisesRegex(catalog.CatalogError,'regular file'):self.check()

    def test_schema_and_evidence_clis_are_independent(self):
        schema=subprocess.run([
            sys.executable,str(ROOT/'scripts/verify-contribution.py'),
            '--spec',str(self.path),'--evidence-root',str(self.root),
            '--run',str(self.directory/'run.json'),
        ],text=True,capture_output=True)
        self.assertEqual(schema.returncode,0,schema.stderr)
        self.assertIn('were not used as catalog gates',schema.stdout)
        evidence=subprocess.run([
            sys.executable,str(ROOT/'scripts/verify-evidence.py'),
            '--spec',str(self.path),'--evidence-root',str(self.root),
            '--run',str(self.directory/'run.json'),
            '--summary',str(self.directory/'summary.json'),'--require-pass',
        ],text=True,capture_output=True)
        self.assertEqual(evidence.returncode,0,evidence.stderr)
        (self.directory/'serve-smoke.json').write_text('{}')
        broken=subprocess.run([
            sys.executable,str(ROOT/'scripts/verify-evidence.py'),
            '--spec',str(self.path),'--evidence-root',str(self.root),
            '--run',str(self.directory/'run.json'),
        ],text=True,capture_output=True)
        self.assertNotEqual(broken.returncode,0)


if __name__=='__main__':unittest.main()
