"""JSON booleans and decimal numbers never stand in for integer versions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from release_spec import load_spec, verify_spec, verify_snapshot_manifest
from release_spec.measurement import validate_measurement
from release_spec.baseline_policy import verify_policy, load_policy
from release_spec.run_record import verify_run_record
from test_release_contribution import make_contribution
from test_runtime_binding import fixture
from scripts.launch_plan import validate_launch_plan
from scripts.runtime_binding import prepared_set


class VersionTypes(unittest.TestCase):
    def test_all_contracts_refuse_noninteger_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,run,_,_=make_contribution(root)
            other=root/'runtime';other.mkdir();candidate,path,prepared,facts,plan,*_=fixture(other)
            measurement=json.loads((ROOT/'tests/fixtures/baseline/measurements/serve-smoke.json').read_text())
            policy,digest=load_policy(ROOT/'policy/baseline-v1.json')
            cases=[('spec',spec,verify_spec),('manifest',spec['identity']['snapshot_manifest'],verify_snapshot_manifest),('measurement',measurement,validate_measurement),('policy',policy,verify_policy),('run',run,lambda d:verify_run_record(d,spec,digest)),('plan',plan,validate_launch_plan),('prepared',prepared,lambda d:prepared_set(d,candidate,'a'*64))]
            for label,document,verify in cases:
                for version in (True,False,1.0,'1',None):
                    malformed=copy.deepcopy(document);malformed['schema_version']=version
                    with self.subTest(contract=label,version=version),self.assertRaises(ValueError):verify(malformed)


if __name__=='__main__':unittest.main()
