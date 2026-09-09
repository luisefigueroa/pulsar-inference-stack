"""Synthetic acceptance/failure matrix for public independent qualification."""
import copy
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import load_spec, pretty_json_bytes, runtime_contract_id, verify_spec, spec_id_for
from release_spec.identity import argv_from_identity
from release_spec.baseline_evaluate import evaluate, OPERATION_FILES
from release_spec.baseline_policy import load_policy, applied_accuracy_floor
from release_spec.contribution import verify_compact_evidence, verify_contribution
from release_spec.run_record import GATE_NAMES

FIXTURES=ROOT/'tests/fixtures/baseline'


def make_contribution(root, nodes=1):
    root=pathlib.Path(root)
    spec=load_spec(FIXTURES/'input-spec.json')
    spec['identity']['geometry'].update(nodes=nodes,tp=nodes,fabric='local' if nodes==1 else 'roce-v2')
    spec['spec_id']=spec_id_for(spec['identity'])
    spec['launch_contract']['argv']=argv_from_identity(spec['identity'])
    spec['launch_contract']['stack_version']='e'*40
    policy,digest=load_policy(ROOT/'policy/baseline-v1.json')
    dest=root/'results/example';dest.mkdir(parents=True)
    docs={};rows=[]
    for op in OPERATION_FILES:
        doc=json.loads((FIXTURES/'measurements'/f'{op}.json').read_text())
        if op=='verify-snapshot-manifest':
            doc[op]['spec_id']=spec['spec_id']
        docs[op]=doc
        raw=pretty_json_bytes(doc);(dest/f'{op}.json').write_bytes(raw)
        rows.append(dict(id=op,lab_commit='d'*40,path=f'results/example/{op}.json',sha256=hashlib.sha256(raw).hexdigest()))
    spec,_,_=evaluate(spec=spec,policy=policy,policy_digest=digest,documents=docs,evidence_rows=rows,accuracy_floor=applied_accuracy_floor(policy,spec['identity']['model_id']))
    rank=dict(rank=0,running=True,owned=True,image_digest=spec['identity']['image']['digest'],launch_contract_id=runtime_contract_id(spec),boot_witness='a'*64,snapshot_manifest_id=spec['identity']['snapshot_manifest']['manifest_id'],files_verified=True)
    ranks=[dict(rank,rank=n) for n in range(nodes)]
    run=dict(schema_version=2,kind='pulsar-baseline-run',spec_id=spec['spec_id'],policy_digest=digest,lab_commit='d'*40,stack_commit='e'*40,image_digest=rank['image_digest'],launch_contract_id=rank['launch_contract_id'],snapshot_manifest_id=rank['snapshot_manifest_id'],ranks_before=ranks,ranks_after=copy.deepcopy(ranks),observation_complete=True,same_boot=True,proposed_status='stable',gates=[dict(name=n,started_at='2026-09-02T00:00:00Z',ended_at='2026-09-02T01:00:00Z' if n=='validate-soak' else '2026-09-02T00:00:00Z',rc=0) for n in GATE_NAMES])
    run['measurement_sha256']={row['id']:row['sha256'] for row in rows}
    raw=pretty_json_bytes(run);(dest/'run.json').write_bytes(raw)
    spec['evidence'].append(dict(id='baseline-run',lab_commit='d'*40,path='results/example/run.json',sha256=hashlib.sha256(raw).hexdigest()))
    spec.update(state='released',review=dict(status='stable',reviewer='example-reviewer',reviewed_at='2026-09-03T00:00:00Z'))
    path=root/'spec.json';path.write_bytes(pretty_json_bytes(spec))
    return spec,run,path,dest


class Contributions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name)
        self.spec,self.run,self.path,self.dest=make_contribution(self.root)

    def check(self):
        self.path.write_bytes(pretty_json_bytes(self.spec))
        return verify_compact_evidence(self.path,self.root,self.dest/'run.json')

    def check_catalog_schema(self):
        self.path.write_bytes(pretty_json_bytes(self.spec))
        return verify_contribution(self.path,self.root,self.dest/'run.json')

    def update_run(self):
        raw=pretty_json_bytes(self.run);(self.dest/'run.json').write_bytes(raw)
        next(e for e in self.spec['evidence'] if e['id']=='baseline-run')['sha256']=hashlib.sha256(raw).hexdigest()

    def test_complete_compact_contribution(self):
        self.assertTrue(self.check()['verified'])
        admission = self.check_catalog_schema()
        self.assertTrue(admission['schema_valid'])
        self.assertFalse(admission['evidence_verified'])

    def test_all_nodes_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            spec,run,path,dest=make_contribution(temp,2)
            self.assertTrue(verify_compact_evidence(path,temp,dest/'run.json')['verified'])
            run['ranks_after'][1]['boot_witness']='b'*64
            raw=pretty_json_bytes(run);(dest/'run.json').write_bytes(raw)
            next(e for e in spec['evidence'] if e['id']=='baseline-run')['sha256']=hashlib.sha256(raw).hexdigest()
            path.write_bytes(pretty_json_bytes(spec))
            with self.assertRaisesRegex(ValueError,'flags|same-boot'):
                verify_compact_evidence(path,temp,dest/'run.json')

    def test_missing_document(self):
        (self.dest/'validate-soak.json').unlink()
        with self.assertRaises(ValueError):self.check()

    def test_altered_bytes(self):
        (self.dest/'validate-soak.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'digest mismatch'):self.check()

    def test_hashes_alone_do_not_prove_pass(self):
        path=self.dest/'evaluate-gsm8k.json';doc=json.loads(path.read_text())
        doc['evaluate-gsm8k']['correct_count']=1
        doc['evaluate-gsm8k']['accuracy']='0.01'
        raw=pretty_json_bytes(doc);path.write_bytes(raw)
        next(e for e in self.spec['evidence'] if e['id']=='evaluate-gsm8k')['sha256']=hashlib.sha256(raw).hexdigest()
        self.run['measurement_sha256']['evaluate-gsm8k']=hashlib.sha256(raw).hexdigest();self.update_run()
        with self.assertRaises(ValueError):self.check()

    def test_run_hash_binding(self):
        self.run['measurement_sha256']['serve-smoke']='f'*64;self.update_run()
        with self.assertRaisesRegex(ValueError,'run measurement digest'):self.check()

    def test_omitted_gate(self):
        self.spec['measurements'].pop()
        with self.assertRaisesRegex(ValueError,'recorded outcomes'):self.check()

    def test_weakened_threshold(self):
        self.spec['measurements'][0]['thresholds'][0]['value']='0'
        with self.assertRaisesRegex(ValueError,'recorded outcomes'):self.check()

    def test_forged_policy(self):
        with patch('release_spec.contribution.APPROVED_POLICY_DIGEST','f'*64):
            with self.assertRaisesRegex(ValueError,'fixed policy'):self.check()

    def test_wrong_lab_revision(self):
        self.run['lab_commit']='a'*40;self.update_run()
        with self.assertRaisesRegex(ValueError,'lab revision'):self.check()

    def test_wrong_stack_revision(self):
        self.run['stack_commit']='a'*40;self.update_run()
        with self.assertRaisesRegex(ValueError,'stack_commit'):self.check()

    def test_restarted_service(self):
        self.run['ranks_after'][0]['boot_witness']='b'*64;self.update_run()
        with self.assertRaisesRegex(ValueError,'flags'):self.check()

    def test_failed_producer(self):
        self.run['gates'][2]['rc']=1;self.update_run()
        with self.assertRaisesRegex(ValueError,'successful'):self.check()

    def test_realistic_fractional_producer_timestamps(self):
        path=self.dest/'validate-soak.json';doc=json.loads(path.read_text())
        doc['validate-soak'].update(started_at='2026-09-02T00:00:00.123456Z',
                                   ended_at='2026-09-02T01:00:00.654321Z',
                                   duration_seconds='3600.530865')
        raw=pretty_json_bytes(doc);path.write_bytes(raw);digest=hashlib.sha256(raw).hexdigest()
        next(e for e in self.spec['evidence'] if e['id']=='validate-soak')['sha256']=digest
        self.run['measurement_sha256']['validate-soak']=digest
        self.run['gates'][-1].update(started_at='2026-09-02T00:00:00.100000Z',
                                      ended_at='2026-09-02T01:00:00.700000Z')
        self.update_run();self.assertTrue(self.check()['verified'])
        self.run['gates'][-1]['ended_at']='2026-09-02T01:00:00.654320Z'
        self.update_run()
        with self.assertRaisesRegex(ValueError,'outside'):self.check()

    def test_run_timestamps_reject_non_utc_and_excess_precision(self):
        for stamp in ('2026-09-02T00:00:00+00:00','2026-09-02T00:00:00.1234567Z','2026-02-30T00:00:00Z'):
            self.run['gates'][0]['started_at']=stamp;self.update_run()
            with self.subTest(stamp=stamp),self.assertRaises(ValueError):self.check()

    def test_borrowed_soak_window(self):
        self.run['gates'][-1]['ended_at']='2026-09-02T00:30:00Z';self.update_run()
        with self.assertRaisesRegex(ValueError,'outside'):self.check()

    def test_no_symlink_evidence(self):
        target=self.dest/'validate-soak.json';raw=target.read_bytes();target.unlink()
        external=self.root/'external.json';external.write_bytes(raw);target.symlink_to(external)
        with self.assertRaises(ValueError):self.check()

    def test_withdrawal_retains_qualification(self):
        self.spec['review'].update(status='withdrawn',reason='Later testing found inconsistent answers.')
        self.assertTrue(self.check()['verified'])
        del self.spec['review']['reason']
        with self.assertRaises(ValueError):self.check()

    def test_experimental_review_is_catalog_membership(self):
        self.spec['review']['status']='experimental'
        self.assertTrue(self.check()['verified'])

    def test_measured_and_nullable_metadata_are_catalog_schema_valid(self):
        self.spec['state']='measured';self.spec['review']={}
        self.assertTrue(self.check_catalog_schema()['schema_valid'])
        self.assertTrue(verify_compact_evidence(self.path,self.root,self.dest/'run.json')['verified'])
        self.spec['state']=None;self.spec['review']=None
        verified=self.check_catalog_schema()
        self.assertIsNone(verified['state']);self.assertIsNone(verified['review'])

    def test_catalog_schema_does_not_gate_on_optional_evidence(self):
        (self.dest/'validate-soak.json').unlink()
        self.assertTrue(self.check_catalog_schema()['schema_valid'])
        with self.assertRaises(ValueError):
            self.check()

    def test_compact_evidence_ignores_review_status(self):
        self.spec['review']['status']='experimental'
        self.path.write_bytes(pretty_json_bytes(self.spec))
        self.assertTrue(verify_compact_evidence(self.path,self.root,self.dest/'run.json')['verified'])

    def test_baseline_verifier_reports_but_does_not_reject_other_suites(self):
        deep=copy.deepcopy(self.spec['measurements'][0])
        deep.update(criterion_id='deep-example',suite='deep',policy_digest=None)
        self.spec['measurements'].append(deep)
        self.spec['review']['status']='validated'
        result=self.check()
        self.assertEqual(result['verified_suite'],'baseline-v1')
        self.assertEqual(result['unverified_suites'],['deep'])


if __name__=='__main__':unittest.main()
