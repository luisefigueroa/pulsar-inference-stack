"""The public spec/evidence gate audits actual working and staged bytes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
CHECKER=ROOT/'scripts/check_publishable_privacy.py'


class PublishablePrivacy(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name)
        subprocess.run(['git','init','-q'],cwd=self.repo,check=True)
        (self.repo/'releases').mkdir()
        self.path=self.repo/'releases'/('a'*64+'.json')

    def check(self,*args):
        return subprocess.run([sys.executable,str(CHECKER),'--repo-root',str(self.repo),*args],capture_output=True,text=True)

    def test_untracked_release_is_audited(self):
        self.path.write_text(json.dumps({'hostname':'fixture-host'+'.internal'}))
        result=self.check()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('hostname',result.stderr)

    def test_staged_blob_cannot_be_hidden_by_clean_working_copy(self):
        self.path.write_text(json.dumps({'node_id':'private-identity-fixture'}))
        subprocess.run(['git','add','releases'],cwd=self.repo,check=True)
        self.path.write_text(json.dumps({'rank':0,'geometry':{'nodes':2}}))
        self.assertEqual(self.check().returncode,0)
        result=self.check('--staged')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('durable node',result.stderr)

    def test_forced_staged_site_configuration_is_rejected_without_printing_value(self):
        path=self.repo/'.env'
        path.write_text('VLLM_API_KEY=custom-test-credential\n')
        subprocess.run(['git','add','-f','.env'],cwd=self.repo,check=True)
        result=self.check('--staged')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('private-operational-state',result.stderr)
        self.assertNotIn('custom-test-credential',result.stderr)

    def test_source_code_host_and_address_are_audited(self):
        path=self.repo/'scripts/tool.py';path.parent.mkdir()
        hostname='dgx-spark-'+'99.example.invalid'
        address='10.23.'+'45.67'
        path.write_text(f'HOST="{hostname}"\nCONTROL_IP="{address}"\n')
        result=self.check()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('stable-hostname',result.stderr)
        self.assertIn('network-address',result.stderr)

    def test_generic_rank_and_public_model_identity_are_allowed(self):
        self.path.write_text(json.dumps({'rank':0,'model_id':'example/model','geometry':{'nodes':2,'platform_id':'dgx-spark-gb10'}}))
        self.assertEqual(self.check().returncode,0)

    def test_commit_metadata_scanner_rejects_site_identity_without_echoing_it(self):
        subprocess.run(['git','config','user.name','Fixture'],cwd=self.repo,check=True)
        subprocess.run(['git','config','user.email','fixture@example.invalid'],cwd=self.repo,check=True)
        (self.repo/'README.md').write_text('fixture\n')
        subprocess.run(['git','add','README.md'],cwd=self.repo,check=True)
        subprocess.run(['git','commit','-qm','safe base'],cwd=self.repo,check=True)
        (self.repo/'README.md').write_text('next\n')
        subprocess.run(['git','add','README.md'],cwd=self.repo,check=True)
        private_email='operator@dgx-spark-'+'99.example.invalid'
        env={**os.environ,'GIT_AUTHOR_NAME':'Fixture','GIT_AUTHOR_EMAIL':private_email,
             'GIT_COMMITTER_NAME':'Fixture','GIT_COMMITTER_EMAIL':private_email}
        subprocess.run(['git','commit','-qm','next'],cwd=self.repo,env=env,check=True)
        result=subprocess.run([
            sys.executable,str(ROOT/'scripts/check_commit_privacy.py'),
            '--repo-root',str(self.repo),'--range','HEAD~1..HEAD',
        ],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('stable-hostname',result.stderr)
        self.assertNotIn(private_email,result.stderr)

    def test_commit_versions_are_allowed_but_network_context_and_secrets_are_rejected(self):
        for name,value in [('user.name','Fixture'),('user.email','fixture@example.invalid')]:
            subprocess.run(['git','config',name,value],cwd=self.repo,check=True)
        subprocess.run(['git','commit','--allow-empty','-qm','Base'],cwd=self.repo,check=True)
        address='10.23.'+'45.67'
        cases=[('Upgrade 1.2.3.4 to 1.2.3.5',True),
               ('Set control address to '+address,False),
               ('Connect to https://'+address,False),
               ('hostname=private-fixture-node',False),
               ('Credential '+'hf_'+'a'*40,False)]
        for message,allowed in cases:
            subprocess.run(['git','commit','--allow-empty','-qm',message],cwd=self.repo,check=True)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/check_commit_privacy.py'),
                '--repo-root',str(self.repo),'--range','HEAD^..HEAD'],text=True,capture_output=True)
            with self.subTest(allowed=allowed,message_type=message.split()[0]):
                self.assertEqual(result.returncode==0,allowed,result.stderr)
        # Identity fields remain strict even without network words in the name.
        env={**os.environ,'GIT_AUTHOR_NAME':address}
        subprocess.run(['git','commit','--allow-empty','-qm','Ordinary change'],cwd=self.repo,env=env,check=True)
        result=subprocess.run([sys.executable,str(ROOT/'scripts/check_commit_privacy.py'),
            '--repo-root',str(self.repo),'--range','HEAD^..HEAD'],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)

if __name__=='__main__': unittest.main()
