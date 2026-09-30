"""The stack advertises public commands instead of private Python imports."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import integration_contract


class IntegrationContract(unittest.TestCase):
    def test_contract_names_schema_only_catalog_and_public_commands(self):
        document=integration_contract.contract()
        self.assertEqual(document['kind'],'pulsar-stack-integration-contract')
        self.assertNotIn('recipe_projector',document)
        self.assertEqual(document['cli_contract_versions'],[1])
        self.assertEqual(document['spec_schema_versions'],[2,3])
        self.assertEqual(document['serving_guard_schema_versions'],[1,2])
        self.assertEqual({operation for operation in document['operations'] if operation.startswith('guarded.')},
                         {'guarded.template', 'guarded.validate', 'guarded.run', 'guarded.stop'})
        self.assertEqual({operation for operation in document['operations'] if operation.startswith('image.')},
                         {'image.check', 'image.stage'})
        self.assertFalse(any(operation.startswith('diagnostic.') for operation in document['operations']))
        self.assertIn('spec.freeze',document['operations'])
        self.assertFalse(document['catalog']['state_gate'])
        self.assertFalse(document['catalog']['review_gate'])
        self.assertFalse(document['catalog']['evidence_gate'])
        self.assertTrue(document['catalog']['nullable_state'])
        self.assertTrue(document['catalog']['nullable_review'])

    def test_root_cli_emits_contract_json(self):
        result=subprocess.run([str(ROOT/'pulsar'),'contract','--json'],
                              text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'schema_version':1,'ok':True,'result':integration_contract.contract()})

    def test_freeze_public_cli_from_unrelated_directory(self):
        import tempfile
        fixtures=ROOT/'tests/fixtures/contracts'
        with tempfile.TemporaryDirectory() as cwd:
            result=subprocess.run([str(ROOT/'pulsar'),'spec','freeze','--draft',str(fixtures/'draft.json'),
                '--manifest',str(fixtures/'manifest.json'),'--json'],cwd=cwd,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],json.loads((fixtures/'spec.json').read_text()))

    def test_schema_two_bare_speculative_models_remain_readable_but_cannot_freeze_or_launch(self):
        import tempfile
        from release_spec.normalize import snapshot_manifest_id
        fixtures=ROOT/'tests/fixtures/contracts'
        for arguments in (['--speculative_config.model','example/draft'],
                          ['--speculative-config','{"model":"example/draft","num_speculative_tokens":3}']):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                draft=json.loads((fixtures/'draft.json').read_text())
                draft['recipe']['engine_args'] += arguments
                draft_path=root/'draft.json';draft_path.write_text(json.dumps(draft))
                frozen=subprocess.run([str(ROOT/'pulsar'),'spec','freeze','--draft',str(draft_path),
                    '--manifest',str(fixtures/'manifest.json'),'--json'],cwd=root,text=True,capture_output=True)
                self.assertEqual(frozen.returncode,2,frozen.stderr+frozen.stdout)
                error=json.loads(frozen.stdout)['error']
                self.assertEqual(error['code'],'invalid_spec')
                self.assertIn('speculative model must reference',error['message'])

                # Reconstruct the old schema-2 identity without the tightened freezer.
                spec=json.loads((fixtures/'spec.json').read_text())
                spec['recipe']['engine_args'] += arguments
                payload=json.dumps({'schema_version':2,'recipe':spec['recipe']},sort_keys=True,
                                   separators=(',',':'),ensure_ascii=False).encode()
                spec['spec_id']=hashlib.sha256(payload).hexdigest()
                path=root/'spec.json';path.write_text(json.dumps(spec))
                verified=subprocess.run([str(ROOT/'pulsar'),'spec','verify','--file',str(path),'--json'],
                    cwd=root,text=True,capture_output=True)
                self.assertEqual(verified.returncode,0,verified.stderr+verified.stdout)
                self.assertEqual(json.loads(verified.stdout)['result'],spec)
                compatible=subprocess.run([sys.executable,str(ROOT/'scripts/check-launch-compatibility.py'),
                    '--spec',str(path),'--json'],cwd=root,text=True,capture_output=True)
                self.assertEqual(compatible.returncode,1,compatible.stderr+compatible.stdout)
                self.assertIn('speculative model must reference',compatible.stderr)

                draft['schema_version']=2
                manifest=json.loads((fixtures/'manifest.json').read_text())
                manifest['model_id']='example/draft'
                manifest['manifest_id']=snapshot_manifest_id(manifest)
                manifest_path=root/'draft-manifest.json';manifest_path.write_text(json.dumps(manifest))
                draft['recipe']['required_snapshots']={'draft':{
                    'model_id':manifest['model_id'],'model_commit':manifest['snapshot_revision']}}
                draft['recipe']['engine_args']=draft['recipe']['engine_args'][:-2]+[
                    arguments[0],arguments[1].replace('example/draft','pulsar-snapshot:draft')]
                draft_path.write_text(json.dumps(draft))
                reauthored=subprocess.run([str(ROOT/'pulsar'),'spec','freeze','--draft',str(draft_path),
                    '--manifest','target='+str(fixtures/'manifest.json'),
                    '--manifest','draft='+str(manifest_path),'--json'],
                    cwd=root,text=True,capture_output=True)
                self.assertEqual(reauthored.returncode,0,reauthored.stderr+reauthored.stdout)
                current=json.loads(reauthored.stdout)['result']
                self.assertEqual(current['schema_version'],3)
                self.assertNotEqual(current['spec_id'],spec['spec_id'])
                self.assertEqual(json.loads(path.read_text()),spec)

    def test_structured_argument_failure(self):
        result=subprocess.run([str(ROOT/'pulsar'),'spec','freeze','--json'],text=True,capture_output=True)
        self.assertEqual(result.returncode,2,result.stderr)
        response=json.loads(result.stdout)
        self.assertFalse(response['ok'])
        self.assertEqual(response['error']['details'][0]['field'],'arguments')

    def test_historical_read_does_not_enable_new_operations(self):
        path=ROOT/'release_spec/tests/fixtures/golden_measured.json'
        for command,flags,code in [('verify',[],2),('show',['--historical'],0)]:
            result=subprocess.run([str(ROOT/'pulsar'),'spec',command,'--file',str(path),*flags,'--json'],text=True,capture_output=True)
            self.assertEqual(result.returncode,code,result.stderr)
            self.assertEqual(json.loads(result.stdout)['ok'],code==0)

    def test_policy_and_measurement_commands_work_without_a_cluster(self):
        import tempfile
        result=subprocess.run([str(ROOT/'pulsar'),'policy','show','baseline-v1','--json'],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['result']['policy']['gates']),6)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'payload.json'
            golden=json.loads((ROOT/'tests/fixtures/baseline/measurements/validate-soak.json').read_text())
            path.write_text(json.dumps({'completion':golden['completion'],'reason':golden['reason'],
                                        'payload':golden['validate-soak']}))
            output=Path(temp)/'measurement.json'
            result=subprocess.run([str(ROOT/'pulsar'),'evidence','measurement','--operation','validate-soak',
                '--input',str(path),'--out',str(output),'--json'],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertEqual(json.loads(output.read_text()),golden)

    def test_recipe_only_package_needs_neither_git_nor_measurements(self):
        import tempfile,hashlib
        with tempfile.TemporaryDirectory() as temp:
            package=Path(temp)
            data=(ROOT/'tests/fixtures/contracts/spec.json').read_bytes()
            spec=json.loads(data)
            name=f"releases/{spec['spec_id']}.json"
            (package/'releases').mkdir();(package/name).write_bytes(data)
            manifest={'schema_version':2,'kind':'pulsar-contribution-package','spec_id':spec['spec_id'],
                      'files':{name:hashlib.sha256(data).hexdigest()}}
            (package/'package.json').write_text(json.dumps(manifest))
            result=subprocess.run([str(ROOT/'pulsar'),'contribution','verify','--package',str(package),'--json'],
                                  text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertEqual(json.loads(result.stdout)['result'],manifest)
            # Undeclared FIFO entries must be rejected without opening them.
            import os
            os.mkfifo(package/'unlisted-pipe')
            result=subprocess.run([str(ROOT/'pulsar'),'contribution','verify','--package',str(package),'--json'],
                                  text=True,capture_output=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('regular files',json.loads(result.stdout)['error']['message'])


if __name__=='__main__':
    unittest.main()
