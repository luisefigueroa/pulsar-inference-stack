"""Evidence stays bound to the effective recipe, with no commit-equality gate."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from release_spec.evidence_v2 import evaluate_measurements, verify_evidence, evidence_summary
from release_spec.normalize import pretty_json_bytes
from release_spec.run_v3 import verify_run
from tests.test_container_runtime import fixture
from scripts.container_runtime import observe_rank

ROOT=Path(__file__).resolve().parents[1]


def make_run(root, speculative=False):
    spec,_,_,plan,containers,images=fixture(speculative=speculative)
    root.mkdir(parents=True,exist_ok=True)
    (root/'measurements').mkdir()
    (root/'spec.json').write_bytes(pretty_json_bytes(spec))
    (root/'policy.json').write_bytes((ROOT/'policy/baseline-v1.json').read_bytes())
    for path in (ROOT/'tests/fixtures/baseline/measurements').glob('*.json'):
        value=json.loads(path.read_text())
        if value['operation']=='verify-snapshot-manifest':
            payload=value['verify-snapshot-manifest'];manifest=spec['recipe']['model']['snapshot_manifest']
            payload.update(spec_id=spec['spec_id'],manifest_id=manifest['manifest_id'],
                           expected_file_count=manifest['file_count'],matched_file_count=manifest['file_count'])
            if speculative:
                from release_spec import serving
                value['schema_version']=2
                value['verify-snapshot-manifest']={'spec_id':spec['spec_id'],'snapshots':{
                    name:{'manifest_id':m['snapshot_manifest']['manifest_id'],
                        'expected_file_count':m['snapshot_manifest']['file_count'],'matched_file_count':m['snapshot_manifest']['file_count'],
                        'mismatched_file_count':0,'missing_file_count':0,'extra_file_count':0}
                    for name,m in serving.required_snapshots(spec).items()}}
        (root/'measurements'/path.name).write_bytes(pretty_json_bytes(value))
    evaluation,_=evaluate_measurements(spec,root/'policy.json',root/'measurements')
    observed=observe_rank(plan,0,containers[0],images[0])
    rank={key:observed[key] for key in ('rank','running','owned','spec_id','image_digest','snapshots' if speculative else 'snapshot_manifest_id','boot_witness')}
    for member in rank.get('snapshots',{}).values(): member['files_verified']=True
    rank.update(files_verified=True,container_configuration=observed['public_container_configuration'])
    soak=json.loads((root/'measurements/validate-soak.json').read_text())['validate-soak']
    start=soak['started_at'];finish=soak['ended_at']
    record={'schema_version':4 if speculative else 3,'kind':'pulsar-baseline-run','run_id':'campaign-1','spec_id':spec['spec_id'],
        'policy_digest':evaluation['policy_digest'],'workbench_commit':'a'*40,
        'stack_observers':[{'stack_commit':None,'working_tree_dirty':None}]*2,
        'ranks_before':[rank],'ranks_after':[copy.deepcopy(rank)],
        'gates':[{'name':name,'started_at':start,'ended_at':finish if name=='validate-soak' else start,'rc':0}
                 for name in ('verify-snapshot-manifest','serve-smoke','run-gates','evaluate-gsm8k','validate-soak')],
        'outcome':evaluation['outcome'],'observation_complete':True,'same_boot':True,
        'measurement_sha256':evaluation['measurement_sha256'],'error_codes':[],
        'input_sha256':{'policy':hashlib.sha256((root/'policy.json').read_bytes()).hexdigest()}}
    record['input_sha256']['dataset']=json.loads((root/'measurements/evaluate-gsm8k.json').read_text())['evaluate-gsm8k']['dataset_file_sha256']
    (root/'run.json').write_bytes(pretty_json_bytes(record))
    return spec,record


class CurrentEvidence(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.spec,self.record=make_run(self.root)

    def check(self):
        return verify_evidence(self.root/'spec.json',self.root/'run.json',self.root)

    def test_complete_snapshot_evidence_and_missing_draft_rejection(self):
        root=self.root/'speculative'
        spec,record=make_run(root,speculative=True)
        self.assertEqual(verify_evidence(root/'spec.json',root/'run.json',root)['outcome'],'pass')
        for change in ('missing','wrong'):
            bad=copy.deepcopy(record)
            snapshots=bad['ranks_after'][0]['snapshots']
            if change=='missing': del snapshots['draft']
            else: snapshots['draft']['snapshot_manifest_id']='f'*64
            with self.subTest(change=change),self.assertRaises(ValueError): verify_run(bad,spec)
        path=root/'measurements/verify-snapshot-manifest.json'
        value=json.loads(path.read_text());del value['verify-snapshot-manifest']['snapshots']['draft']
        path.write_bytes(pretty_json_bytes(value))
        result,_=evaluate_measurements(spec,root/'policy.json',root/'measurements')
        self.assertNotEqual(result['outcome'],'pass')

    def test_legacy_file_flags_do_not_gate_qualification_or_continuity(self):
        for speculative in (False, True):
            with self.subTest(speculative=speculative):
                root=self.root/('legacy-flags-'+str(speculative))
                spec,record=make_run(root,speculative=speculative)
                rank=record['ranks_after'][0]
                rank['files_verified']=False
                for member in rank.get('snapshots',{}).values():
                    member['files_verified']=False
                self.assertEqual(verify_run(record,spec),record)
                (root/'run.json').write_bytes(pretty_json_bytes(record))
                self.assertEqual(verify_evidence(root/'spec.json',root/'run.json',root)['outcome'],'pass')
                self.assertTrue(record['same_boot'])
                self.assertFalse(record['ranks_after'][0]['files_verified'])

    def test_speculative_evidence_package_verifies_without_workbench(self):
        from release_spec import serving
        root=self.root/'speculative-package-run'
        spec,record=make_run(root,speculative=True)
        verified=verify_evidence(root/'spec.json',root/'run.json',root)
        proof={'schema_version':2,'kind':'pulsar-archive-verification','spec_id':spec['spec_id'],'verified':True,
            'snapshots':{name:{'schema_version':1,'kind':'pulsar-archive-verification','verified':True,
                'snapshot_manifest_id':m['snapshot_manifest']['manifest_id'],'file_count':m['snapshot_manifest']['file_count'],
                'total_bytes':m['snapshot_manifest']['total_bytes']} for name,m in serving.required_snapshots(spec).items()}}
        archive={'schema_version':2,'kind':'pulsar-archive-observation','observed_at':'2026-09-09T00:00:00Z','verification':proof}
        summary=evidence_summary(verified,spec,archive)
        path=root/'summary.json';path.write_bytes(pretty_json_bytes(summary))
        result=subprocess.run([sys.executable,str(ROOT/'scripts/verify-evidence.py'),
            '--spec',str(root/'spec.json'),'--run',str(root/'run.json'),'--evidence-root',str(root),
            '--summary',str(path),'--json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        package=self.root/'package'
        artifacts={f"releases/{spec['spec_id']}.json":(root/'spec.json').read_bytes()}
        prefix=f"results/baseline-v1/{spec['spec_id']}/{record['run_id']}"
        for source in [root/'run.json',root/'policy.json',root/'summary.json',*(root/'measurements').glob('*.json')]:
            artifacts[prefix+'/'+source.name]=source.read_bytes()
        for name,data in artifacts.items():
            output=package/name;output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(data)
        (package/'package.json').write_bytes(pretty_json_bytes({'schema_version':2,'kind':'pulsar-contribution-package',
            'spec_id':spec['spec_id'],'files':{name:hashlib.sha256(data).hexdigest() for name,data in artifacts.items()}}))
        result=subprocess.run([str(ROOT/'pulsar'),'contribution','verify','--package',str(package),'--json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        del proof['snapshots']['draft']
        with self.assertRaises(ValueError): evidence_summary(verified,spec,archive)

    def test_complete_evidence_with_unknown_observer_commit(self):
        self.assertEqual(self.check()['outcome'],'pass')
        before=(self.root/'spec.json').read_bytes()
        self.record['stack_observers']=[{'stack_commit':'b'*40,'working_tree_dirty':False},
                                        {'stack_commit':'c'*40,'working_tree_dirty':False}]
        (self.root/'run.json').write_bytes(pretty_json_bytes(self.record))
        self.assertEqual(self.check()['outcome'],'pass')
        self.assertEqual((self.root/'spec.json').read_bytes(),before)

    def test_actual_recipe_mismatch_and_restart_cannot_be_relabelled_as_success(self):
        for change in ('configuration','boot','producer','missing-hash'):
            record=copy.deepcopy(self.record)
            if change=='configuration': record['ranks_after'][0]['container_configuration']['network_mode']='host'
            elif change=='boot': record['ranks_after'][0]['boot_witness']='f'*64
            elif change=='producer': record['gates'][-1]['rc']=1
            else: record['measurement_sha256'].pop('validate-soak')
            with self.subTest(change=change),self.assertRaises(ValueError):
                verify_run(record,self.spec)

    def test_tampered_measurement_is_rejected(self):
        path=self.root/'measurements/validate-soak.json'
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'hash'):
            self.check()

    def test_catalog_metadata_does_not_change_measurement_identity(self):
        self.spec['state']='released'
        self.spec['review']={}
        (self.root/'spec.json').write_bytes(pretty_json_bytes(self.spec))
        self.assertEqual(self.check()['outcome'],'pass')

    def test_soak_must_fit_the_recorded_producer_window(self):
        self.record['gates'][-1]['ended_at']=self.record['gates'][-1]['started_at']
        (self.root/'run.json').write_bytes(pretty_json_bytes(self.record))
        with self.assertRaisesRegex(ValueError,'outside'):
            self.check()

    def test_different_policy_cannot_claim_unchanged_baseline(self):
        policy=json.loads((self.root/'policy.json').read_text())
        policy['accuracy_floor_overrides']={'example/model':'0.1'}
        (self.root/'policy.json').write_bytes(pretty_json_bytes(policy))
        with self.assertRaises(ValueError): self.check()

    def test_incomplete_run_without_any_measurements_is_valid_history(self):
        for path in (self.root/'measurements').iterdir(): path.unlink()
        self.record.update(ranks_before=[],ranks_after=[],gates=[],outcome='incomplete',
            observation_complete=False,same_boot=False,measurement_sha256={},error_codes=['observation_failed'])
        (self.root/'run.json').write_bytes(pretty_json_bytes(self.record))
        self.assertEqual(self.check()['outcome'],'incomplete')

    def test_archive_observation_is_optional_timestamped_and_snapshot_bound(self):
        verified=self.check()
        self.assertIsNone(evidence_summary(verified,self.spec)['archive_observation'])
        manifest=self.spec['recipe']['model']['snapshot_manifest']
        archive={'schema_version':2,'kind':'pulsar-archive-observation','observed_at':'2026-09-09T00:00:00Z',
            'verification':{'schema_version':1,'kind':'pulsar-archive-verification','verified':True,
                'snapshot_manifest_id':manifest['manifest_id'],'file_count':manifest['file_count'],
                'total_bytes':manifest['total_bytes']}}
        result=evidence_summary(verified,self.spec,{'schema_version':1,'ok':True,'result':archive})
        self.assertEqual(result['archive_observation'],archive)
        archive['verification']['snapshot_manifest_id']='f'*64
        with self.assertRaises(ValueError): evidence_summary(verified,self.spec,archive)
        with self.assertRaises(ValueError): evidence_summary(verified,self.spec,archive['verification'])

    def test_standalone_verifier_accepts_current_summary_and_rejects_tampering(self):
        path=self.root/'summary.json'
        summary=evidence_summary(self.check(),self.spec)
        path.write_bytes(pretty_json_bytes(summary))
        command=[sys.executable,str(ROOT/'scripts/verify-evidence.py'),
            '--spec',str(self.root/'spec.json'),'--run',str(self.root/'run.json'),
            '--evidence-root',str(self.root),'--summary',str(path),'--json']
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        summary['outcome']='fail'
        path.write_bytes(pretty_json_bytes(summary))
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)


if __name__=='__main__': unittest.main()
