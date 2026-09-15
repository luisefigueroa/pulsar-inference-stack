"""Synthetic runtime contract and all-rank observation integration tests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import pretty_json_bytes


def fixture(root,nodes=2,speculative=False):
    # Shared current-contract fixture. Legacy profile/commit-label unit checks
    # are replaced by test_serving_spec and test_container_runtime.
    from tests.test_container_runtime import fixture as current_fixture
    from scripts.service_state import save
    from model_library.state import Store
    spec,facts,doc,plan,containers,images=current_fixture(nodes,speculative=speculative)
    path=root/'spec.json';path.write_bytes(pretty_json_bytes(spec))
    save(Store(root/'library'),plan)
    return spec,path,doc,facts,plan,containers,images


class ObservationShell(unittest.TestCase):
    def run_scenario(self,nodes,mode='ok',launcher=False,public=False,replacing=False,speculative=False,full=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,path,prepared,facts,plan,containers,images=fixture(root,nodes,speculative=speculative)
            for rank in range(nodes):
                (root/f'container-{rank}.json').write_text(json.dumps(containers[rank]));(root/f'image-{rank}.json').write_text(json.dumps(images[rank]))
            if mode=='missing-draft': del prepared['snapshots']['draft']
            (root/'prepared.json').write_text(json.dumps(prepared))
            (root/'overlay.json').write_text(json.dumps(dict(schema_version=1,kind='pulsar-deployment-overlay',defaults=dict(port=8000,served_name='example',cache_root=None,placement=None),specs={})))
            if mode=='changed-overlay':
                overlay=json.loads((root/'overlay.json').read_text())
                overlay['defaults'].update(port=9000,served_name='future-service',placement={'node_id':'node-1'})
                (root/'overlay.json').write_text(json.dumps(overlay))
            tool=root/'docker.py';tool.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['FIXTURE_ROOT']);rank=int(os.environ.get('FIXTURE_RANK','0'));mode=os.environ.get('FIXTURE_MODE','ok')
if mode=='rank-loss' and rank==1:sys.exit(255)
kind='image' if sys.argv[1]=='image' else 'container'
doc=json.loads((root/f'{kind}-{rank}.json').read_text())
if kind=='container':
 counter=root/f'calls-{rank}';n=int(counter.read_text()) if counter.exists() else 0;counter.write_text(str(n+1))
 if mode=='restart' and n:doc['State']['StartedAt']='2026-09-05T00:01:00Z'
 if mode=='unowned':doc['Config']['Labels']['io.pulsar.gb10.managed']='false'
print(json.dumps(doc))
''');tool.chmod(0o755)
            # Fixture names rank roles; the shared double consumes rank indexes,
            # never hostname branches. Actual Bash observation loop is exercised.
            envfile=root/'env.sh';envfile.write_text(f'''
. '{ROOT}/scripts/lib.sh'
load_cluster_topology() {{
  [ "$FIXTURE_MODE" != no-topology ] || return 1
  CLUSTER_TOPOLOGY_COUNT={nodes}; CLUSTER_TOPOLOGY_ID={'c'*64}; CLUSTER_TOPOLOGY_LOADED=1
  CLUSTER_NODE_IDS=(node-0 node-1)
  CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1)
  CLUSTER_NODE_SSH_HOSTS=(local rank-1)
  CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2)
  CLUSTER_NODE_CONTROL_IFS=(eth0 eth0)
  CLUSTER_PROFILE_HCAS=('mlx5_0,mlx5_1' 'mlx5_0,mlx5_1')
}}
require_profile_topology() {{ load_cluster_topology; }}
runtime_context_for_rank() {{ printf '{{"architecture":"fixture","kernel_release":"fixture","gpu_driver":null,"container_runtime_version":null}}\n'; }}
resolve_single_node_placement() {{ load_cluster_topology || return; SINGLE_NODE_INDEX=0; SINGLE_NODE_ID=node-0; SINGLE_NODE_HOSTNAME=rank-0; SINGLE_NODE_SSH_HOST=local; SINGLE_NODE_CONTROL_IP=192.0.2.1; SINGLE_NODE_REMOTE=0; SINGLE_NODE_TOPOLOGY_ID="$CLUSTER_TOPOLOGY_ID"; }}
library_hot_info_for_profile() {{ printf '%s\\n' "${{PULSAR_OBSERVE_FULL:-0}}" >>"$FIXTURE_ROOT/verification-modes"; [ "$FIXTURE_MODE" != corrupt-files ] || return 2; [ "$FIXTURE_MODE" != missing-files ] || return 1; cat "$FIXTURE_ROOT/prepared.json"; }}
ssh_node() {{ local rank="$1"; shift; FIXTURE_RANK="$rank" python3 "$FIXTURE_ROOT/docker.py" $([ "${{1#docker image}}" != "$1" ] && echo image || echo inspect); }}
''')
            env={**os.environ,'BASH_ENV':str(envfile),'FIXTURE_ROOT':str(root),'FIXTURE_MODE':mode,'PULSAR_DOCKER':str(tool),'PULSAR_MODEL_LIBRARY_DIR':str(root/'library'),'PULSAR_SPEC_FILE':str(path),'PULSAR_OVERLAY_PATH':str(root/'overlay.json'),'VLLM_IMAGE_MAINLINE':'example/image','VLLM_EXTRA_ARGS':'','EXTRA_ENV':''}
            if replacing:
                env['PULSAR_LAUNCH_RESULT_FILE']=str(root/'launch-result.json')
                with envfile.open('a') as stream:
                    stream.write('''
require_launch_operational_checks() { :; }
container_ownership_inspect_local() { return 0; }
container_ownership_inspect_remote() { return 0; }
verify_replacement_plan() {
  python3 - "$PLAN_FILE" "$PULSAR_LAUNCH_RESULT_FILE" <<'PY'
import json,sys
plan,result=[json.load(open(path)) for path in sys.argv[1:]]
assert plan['lifecycle_action']==result['lifecycle_action']=='replace'
assert plan['service_id']==result['service_id']
print('replacement-plan-verified')
PY
  exit 78
}
remove_stack_owned_single_at_resolved_node() { verify_replacement_plan; }
remove_stack_owned_cluster() { verify_replacement_plan; }
''')
            if mode=='changed-overlay':
                with envfile.open('a') as stream:
                    stream.write('''
library_hot_info_for_profile() {
  # Check the overlay that the real verification subprocess would inherit.
  bash -c 'load_conf "$1"; spec_overlay_node_selector node-0 >/dev/null' _ "$1" || return
  cat "$FIXTURE_ROOT/prepared.json"
}
''')
            script=ROOT/('serve.sh' if nodes==1 else 'cluster/start-cluster.sh') if launcher else ROOT/'scripts/observe-serving.sh'
            flags=['--replace'] if replacing else ['--dry-run'] if launcher else ['--json']
            if launcher and nodes>1:flags += ['--skip-preflight']
            command=['bash',str(script),spec['spec_id'],*flags]
            if public: command=[str(ROOT/'pulsar'),'observe','--service-id',plan['service_id'],'--json']
            if full: command += ['--full']
            result=subprocess.run(command,cwd=root if public else ROOT,env=env,text=True,capture_output=True)
            trace=root/'verification-modes'
            result.verification_modes=trace.read_text().splitlines() if trace.exists() else []
            return result

    def test_required_snapshots_through_public_observation_and_launcher(self):
        for nodes in (1,2):
            result=self.run_scenario(nodes,public=True,speculative=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            observed=json.loads(result.stdout)['result']
            self.assertEqual(observed['schema_version'],3)
            for rank in observed['ranks']:
                self.assertEqual(set(rank['snapshots']),{'target','draft'})
                self.assertTrue(all(m['files_verified'] is True for m in rank['snapshots'].values()))
            result=self.run_scenario(nodes,launcher=True,speculative=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertIn('/pulsar/snapshots/',result.stdout)
            result=self.run_scenario(nodes,mode='missing-draft',launcher=True,speculative=True)
            self.assertNotEqual(result.returncode,0,result.stdout)

    def test_full_hashing_is_explicit_through_the_public_observer(self):
        for full in (False,True):
            with self.subTest(full=full):
                result=self.run_scenario(2,public=True,full=full)
                self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                self.assertEqual(result.verification_modes,['1' if full else '0'])
                self.assertEqual(len(json.loads(result.stdout)['result']['ranks']),2)

    def test_one_and_two_nodes(self):
        for nodes in (1,2):
            result=self.run_scenario(nodes)
            self.assertEqual(result.returncode,0,result.stderr)
            doc=json.loads(result.stdout);self.assertEqual(len(doc['ranks']),nodes)

    def test_public_observation_from_an_unrelated_directory(self):
        result=self.run_scenario(1,public=True)
        self.assertEqual(result.returncode,0,result.stderr)
        document=json.loads(result.stdout)
        self.assertTrue(document['ok'])
        self.assertEqual(document['result']['schema_version'],2)
        self.assertIn('producer',document['result'])

    def test_recorded_service_ignores_future_overlay_placement_and_endpoint(self):
        result=self.run_scenario(1,mode='changed-overlay',public=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        observed=json.loads(result.stdout)['result']
        self.assertEqual(observed['served_name'],'example')
        self.assertTrue(observed['api_url'].endswith(':8000'))

    def test_low_level_dry_run_uses_spec_and_distinct_mounts(self):
        for nodes in (1,2):
            result=self.run_scenario(nodes,launcher=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('fixture-rank-0',result.stdout)
            if nodes==2:self.assertIn('fixture-rank-1',result.stdout)

    def test_replacement_is_recorded_before_any_service_removal(self):
        for nodes in (1,2):
            with self.subTest(nodes=nodes):
                result=self.run_scenario(nodes,launcher=True,replacing=True)
                self.assertEqual(result.returncode,78,result.stderr+result.stdout)
                self.assertIn('replacement-plan-verified',result.stdout)

    def test_refusals(self):
        for mode in ('rank-loss','restart','unowned','corrupt-files','missing-files','no-topology'):
            with self.subTest(mode=mode):
                result=self.run_scenario(2,mode)
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertNotIn('"files_verified": true',result.stdout)


if __name__=='__main__':unittest.main()
