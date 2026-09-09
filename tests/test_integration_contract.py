"""The stack advertises its supported workbench boundary."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import integration_contract


class IntegrationContract(unittest.TestCase):
    def test_contract_names_schema_only_catalog_and_public_projector(self):
        document=integration_contract.contract()
        self.assertEqual(document['kind'],'pulsar-stack-integration-contract')
        self.assertEqual(document['recipe_projector'],'release_spec.build_profile_identity')
        self.assertFalse(document['catalog']['state_gate'])
        self.assertFalse(document['catalog']['review_gate'])
        self.assertFalse(document['catalog']['evidence_gate'])
        self.assertTrue(document['catalog']['nullable_state'])
        self.assertTrue(document['catalog']['nullable_review'])

    def test_root_cli_emits_contract_json(self):
        result=subprocess.run([str(ROOT/'pulsar'),'contract','--json'],
                              text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),integration_contract.contract())


if __name__=='__main__':
    unittest.main()
