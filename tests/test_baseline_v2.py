"""Five graded criteria with retained, ungraded repeatability evidence."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from release_spec.baseline_policy import load_supported_policy, SUPPORTED_POLICY_DIGESTS
from release_spec.evidence_v2 import evaluate_measurements, verify_evidence, evidence_summary
from release_spec.measurement import build_compare_measurement
from release_spec.normalize import pretty_json_bytes
from release_spec.package import verify_package
from release_spec.run_v3 import verify_run
from tests.test_current_evidence import make_run

ROOT=Path(__file__).resolve().parents[1]

class BaselineV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.spec,self.record=make_run(self.root,speculative=True)
        self.policy=self.root/'policy.json'
        self.policy.write_bytes((ROOT/'policy/baseline-v2.json').read_bytes())
        self.capture=self.root/'measurements/compare-captures.json'
        self.original=self.capture.read_bytes()

    def differences(self, text=False):
        value=json.loads(self.original)
        value['compare-captures'].update(identical_record_count=0,exact_text_count=0 if text else 2,
            diagnostic_verdict='divergent' if text else 'fp-equivalent',
            hard_disagreement_count=2 if text else 0,
            mean_prefix_match='0' if text else '1',min_prefix_match='0' if text else '1',
            max_matched_prefix_logprob_delta='2' if text else '0.1')
        self.capture.write_bytes(pretty_json_bytes(value))
        return value

    def evaluate(self):
        return evaluate_measurements(self.spec,self.policy,self.root/'measurements')[0]

    def finalize(self):
        result=self.evaluate()
        self.record.update(outcome=result['outcome'],policy_digest=result['policy_digest'],
                           measurement_sha256=result['measurement_sha256'])
        self.record['input_sha256']['policy']=hashlib.sha256(self.policy.read_bytes()).hexdigest()
        (self.root/'run.json').write_bytes(pretty_json_bytes(self.record))
        return result

    def test_policy_contract_and_remaining_thresholds(self):
        v1,d1=load_supported_policy(ROOT/'policy/baseline-v1.json')
        v2,d2=load_supported_policy(self.policy)
        self.assertEqual(v2['gates'],[g for g in v1['gates'] if g['operation']!='compare-captures'])
        self.assertEqual(d1,'0b79190daf6e03b81c4b847adf0895b6102daec575ac8d6b0fac712381085539')
        self.assertEqual(d2,SUPPORTED_POLICY_DIGESTS['baseline-v2'])
        result=subprocess.run([str(ROOT/'pulsar'),'policy','show','baseline-v2','--json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['result']['policy'],v2)
        result=subprocess.run([str(ROOT/'pulsar'),'contract','--json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        contract=json.loads(result.stdout)['result']
        self.assertEqual(contract['baseline_policies'],SUPPORTED_POLICY_DIGESTS)
        self.assertEqual(contract['baseline_policy_digest'],d1)
        v2['accuracy_floor_overrides']={'example/model':'0.1'}
        self.policy.write_bytes(pretty_json_bytes(v2))
        with self.assertRaises(ValueError):self.evaluate()

    def test_differences_pass_v2_and_fail_v1_with_all_six_hashes(self):
        for text in (False,True):
            with self.subTest(text=text):
                value=self.differences(text)
                result=self.finalize()
                self.assertEqual(result['outcome'],'pass')
                self.assertEqual(len(result['outcomes']),5)
                self.assertEqual(len(result['measurement_sha256']),6)
                self.assertEqual(result['diagnostics']['compare-captures'],value)
                self.assertEqual(verify_evidence(self.root/'spec.json',self.root/'run.json',self.root)['outcome'],'pass')
                old,_=evaluate_measurements(self.spec,ROOT/'policy/baseline-v1.json',self.root/'measurements')
                self.assertEqual(old['outcomes']['strict-same-boot-captures'],'fail')
                self.assertEqual(old['schema_version'],1)
                self.assertNotIn('diagnostics',old)

    def test_missing_unusable_or_malformed_capture_cannot_pass(self):
        self.capture.unlink()
        self.assertEqual(self.evaluate()['outcome'],'incomplete')
        self.capture.write_bytes(pretty_json_bytes(build_compare_measurement(completion='incomplete',reason='unusable-input')))
        self.assertEqual(self.evaluate()['outcome'],'incomplete')
        self.capture.write_text('{}')
        with self.assertRaises(ValueError):self.evaluate()

    def test_other_criteria_still_require_measurements(self):
        self.differences(True)
        for operation in ('verify-snapshot-manifest','serve-smoke','evaluate-gsm8k','validate-soak','benchmark-serving'):
            path=self.root/'measurements'/f'{operation}.json';raw=path.read_bytes();path.unlink()
            with self.subTest(operation=operation):self.assertNotEqual(self.evaluate()['outcome'],'pass')
            path.write_bytes(raw)

    def test_accuracy_and_soak_failures_are_still_graded(self):
        self.differences(True)
        for operation,changes,criterion in (
            ('evaluate-gsm8k',{'accuracy':'0.49','correct_count':49},'gsm8k-subset'),
            ('validate-soak',{'request_error_count':1},'soak-60')):
            path=self.root/'measurements'/f'{operation}.json';raw=path.read_bytes()
            value=json.loads(raw);value[operation].update(changes)
            path.write_bytes(pretty_json_bytes(value))
            with self.subTest(operation=operation):
                result=self.evaluate()
                self.assertEqual(result['outcome'],'fail')
                self.assertEqual(result['outcomes'][criterion],'fail')
            path.write_bytes(raw)

    def test_identity_runtime_producer_and_diagnostic_integrity_remain_required(self):
        self.differences(True);self.finalize()
        for change in ('snapshot','boot','producer','hash'):
            bad=copy.deepcopy(self.record)
            if change=='snapshot':del bad['ranks_after'][0]['snapshots']['draft']
            elif change=='boot':bad['ranks_after'][0]['boot_witness']='f'*64
            elif change=='producer':bad['gates'][2]['rc']=1
            else:del bad['measurement_sha256']['compare-captures']
            with self.subTest(change=change),self.assertRaises(ValueError):verify_run(bad,self.spec)
        self.capture.write_bytes(self.capture.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'hash'):
            verify_evidence(self.root/'spec.json',self.root/'run.json',self.root)

    def test_public_package_retains_diagnostic_and_binds_suite_directory(self):
        self.differences(True);self.finalize()
        verified=verify_evidence(self.root/'spec.json',self.root/'run.json',self.root)
        prefix=f"results/baseline-v2/{self.spec['spec_id']}/{self.record['run_id']}"
        artifacts={f"releases/{self.spec['spec_id']}.json":(self.root/'spec.json').read_bytes()}
        for source in [self.root/'run.json',self.policy,*(self.root/'measurements').glob('*.json')]:
            artifacts[prefix+'/'+source.name]=source.read_bytes()
        artifacts[prefix+'/summary.json']=pretty_json_bytes(evidence_summary(verified,self.spec))
        package=self.root/'package'
        def write_package(values):
            for name,data in values.items():
                path=package/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            manifest={'schema_version':2,'kind':'pulsar-contribution-package','spec_id':self.spec['spec_id'],
                      'files':{name:hashlib.sha256(data).hexdigest() for name,data in values.items()}}
            (package/'package.json').write_bytes(pretty_json_bytes(manifest))
        write_package(artifacts)
        self.assertIn(prefix+'/compare-captures.json',verify_package(package)['files'])
        result=subprocess.run([str(ROOT/'pulsar'),'contribution','verify','--package',str(package),'--json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        import shutil
        shutil.rmtree(package)
        write_package({name.replace('/baseline-v2/','/baseline-v1/'):data for name,data in artifacts.items()})
        with self.assertRaisesRegex(ValueError,'policy suite'):verify_package(package)

if __name__=='__main__':unittest.main()
