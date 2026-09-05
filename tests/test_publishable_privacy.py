"""The public spec/evidence gate audits actual working and staged bytes."""
import importlib.util
import json
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

    def test_generic_rank_and_public_model_identity_are_allowed(self):
        self.path.write_text(json.dumps({'rank':0,'model_id':'example/model','geometry':{'nodes':2,'platform_id':'dgx-spark-gb10'}}))
        self.assertEqual(self.check().returncode,0)

if __name__=='__main__': unittest.main()
