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
from release_spec import load_spec, spec_id_for, pretty_json_bytes, runtime_contract_id
from release_spec.identity import argv_from_identity
from scripts.release_consumer import spec_profile_variables
from scripts.runtime_binding import bind_plan, observe_rank, prepared_set
from scripts.launch_plan import rank_docker_argv, rank_container_spec, DEFAULT_RUNTIME


def fixture(root,nodes=1):
    root=Path(root)
    spec=load_spec(ROOT/'release_spec/tests/fixtures/golden_measured.json')
    spec['identity']['engine_args']=['--max-model-len','4096','--gpu-memory-utilization','0.8']
    if nodes>1:
        spec['identity']['engine_args'] += ['--distributed-executor-backend','mp']
        spec['identity']['container_env'] += ['NCCL_IB_QPS_PER_CONNECTION=4']
        spec['identity']['container_env'].sort()
    spec['identity']['geometry'].update(nodes=nodes,tp=nodes,pp=1,fabric='local' if nodes==1 else 'roce-v2')
    spec['spec_id']=spec_id_for(spec['identity'])
    spec['launch_contract']['argv']=argv_from_identity(spec['identity'])
    spec['launch_contract']['stack_version']='e'*40
    path=root/'spec.json';path.write_bytes(pretty_json_bytes(spec))
    identity=spec['identity'];manifest=identity['snapshot_manifest']
    variables=spec_profile_variables(spec,dict(port=8000,served_name='example'),'example/image')
    ranks=[];prepared=[]
    for rank in range(nodes):
        hub=f'/var/tmp/example-rank-{rank}/models--'+identity['model_id'].replace('/','--')
        ranks.append(dict(rank=rank,node_id=f'node-{rank}',hostname=f'rank-{rank}',ssh_host='local' if rank==0 else f'rank-{rank}',control_ip=f'192.0.2.{rank+1}',control_if='eth0',hcas='' if nodes==1 else 'mlx5_0,mlx5_1'))
        prepared.append(dict(rank=rank,node_id=f'node-{rank}',hub_path=hub,path=f'{hub}/snapshots/{identity["snapshot_revision"]}',verification=dict(verified=True),pinned=False,is_home_view=rank==0))
    doc=dict(schema_version=1,kind='pulsar-prepared-set',spec_id=spec['spec_id'],snapshot_manifest_id=manifest['manifest_id'],topology_id='a'*64,home_node_id='node-0',revision=identity['snapshot_revision'],home={},ranks=prepared)
    runtime=copy.deepcopy(DEFAULT_RUNTIME);runtime.update(engine_args=variables['ENGINE_ARGS'],container_env=variables['CONTAINER_ENV'])
    facts=dict(lifecycle_action='dry-run',profile=spec['spec_id'],platform_id='dgx-spark-gb10',stack_build='e'*40,served_name='example',model_id=identity['model_id'],image=variables['IMAGE'],nodes=nodes,port=8000,gpu_mem_util=0.8,topology_id='a'*64,launch_contract_id=runtime_contract_id(spec),spec_decode=dict(enabled=False,source='profile-default'),storage=dict(mechanism='local-files',identity_status='manifest-verified',revision=identity['snapshot_revision'],home_node_id='node-0',content_id=manifest['manifest_id'][:12],hub_path=prepared[0]['hub_path'],container_model_path=f'/root/.cache/huggingface/hub/models--{identity["model_id"].replace("/","--")}/snapshots/{identity["snapshot_revision"]}',transport='ssh-control' if nodes==1 else 'ssh-roce'),ranks=ranks,runtime=runtime,memory=dict(advisory=True,result='pass'))
    plan=bind_plan(facts,path,doc)
    containers=[];images=[]
    for rank in range(nodes):
        rs=rank_container_spec(plan,rank);args=rank_docker_argv(plan,rank,detach=True);image_pos=args.index(plan['image'])
        env=['PATH=/usr/bin']+[args[i+1] for i,x in enumerate(args[:image_pos]) if x=='-e']
        image=dict(Id='sha256:'+'d'*64,RepoDigests=[plan['image']],Config=dict(Env=['PATH=/usr/bin'],Entrypoint=['python3','-m','vllm.entrypoints.openai.api_server']))
        container=dict(Id=str(rank+1)*64,Image=image['Id'],Config=dict(Image=plan['image'],Labels=rs['labels'],Cmd=args[image_pos+1:],Entrypoint=image['Config']['Entrypoint'],Env=env),State=dict(Running=True,StartedAt='2026-09-05T00:00:00.000Z'),Mounts=[dict(Source=rs['mounts'][0]['source'],Destination=rs['mounts'][0]['target'],RW=False)])
        containers.append(container);images.append(image)
    return spec,path,doc,facts,plan,containers,images


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.spec,self.path,self.prepared,self.facts,self.plan,self.containers,self.images=fixture(self.root,2)

    def test_rank_local_mounts(self):
        self.assertNotEqual(rank_container_spec(self.plan,0)['mounts'][0]['source'],rank_container_spec(self.plan,1)['mounts'][0]['source'])
        self.assertEqual(rank_container_spec(self.plan,1)['labels']['io.pulsar.gb10.stack-build'],'e'*40)
        self.assertTrue(observe_rank(self.plan,1,self.containers[1],self.images[1],self.spec)['files_verified'])
        argv=rank_docker_argv(self.plan,1,detach=True)
        self.assertEqual(argv.count('NCCL_IB_QPS_PER_CONNECTION=4'),1)

    def test_drift_is_rejected_before_launch(self):
        for field,value in [('engine_args',['--max-model-len','1']),('container_env',['CHANGED=1']),('extra_env',['CHANGED=1']),('vllm_extra_args',['--foo']),('nccl_ib_qps','99')]:
            facts=copy.deepcopy(self.facts);facts['runtime'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):bind_plan(facts,self.path,self.prepared)

    def test_missing_or_wrong_prepared_rank(self):
        for change in ('missing','wrong-node','wrong-path','wrong-spec'):
            doc=copy.deepcopy(self.prepared)
            if change=='missing':doc['ranks'].pop()
            if change=='wrong-node':doc['ranks'][1]['node_id']='other'
            if change=='wrong-path':doc['ranks'][1]['path']='/var/tmp/other'
            if change=='wrong-spec':doc['spec_id']='b'*64
            with self.subTest(change=change),self.assertRaises(ValueError):bind_plan(copy.deepcopy(self.facts),self.path,doc)

    def test_actual_runtime_corruption(self):
        for change in ('image','command','environment','entrypoint','mount','ownership','stack-build','stopped'):
            container=copy.deepcopy(self.containers[1]);image=copy.deepcopy(self.images[1])
            if change=='image':image['RepoDigests']=['example/image@sha256:'+'f'*64]
            if change=='command':container['Config']['Cmd'] += ['--max-model-len','1']
            if change=='environment':container['Config']['Env'] += ['UNEXPECTED=1']
            if change=='entrypoint':container['Config']['Entrypoint']=['other']
            if change=='mount':container['Mounts'][0]['RW']=True
            if change=='ownership':container['Config']['Labels']['io.pulsar.gb10.node-id']='other'
            if change=='stack-build':container['Config']['Labels']['io.pulsar.gb10.stack-build']='other'
            if change=='stopped':container['State']['Running']=False
            with self.subTest(change=change),self.assertRaises(ValueError):observe_rank(self.plan,1,container,image,self.spec)

    def test_boot_witness_detects_restart(self):
        before=observe_rank(self.plan,1,self.containers[1],self.images[1],self.spec)
        self.containers[1]['State']['StartedAt']='2026-09-05T00:01:00.000Z'
        after=observe_rank(self.plan,1,self.containers[1],self.images[1],self.spec)
        self.assertNotEqual(before['boot_witness'],after['boot_witness'])


class ObservationShell(unittest.TestCase):
    def run_scenario(self,nodes,mode='ok',launcher=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,path,prepared,facts,plan,containers,images=fixture(root,nodes)
            for rank in range(nodes):
                (root/f'container-{rank}.json').write_text(json.dumps(containers[rank]));(root/f'image-{rank}.json').write_text(json.dumps(images[rank]))
            (root/'prepared.json').write_text(json.dumps(prepared))
            (root/'overlay.json').write_text(json.dumps(dict(schema_version=1,kind='pulsar-deployment-overlay',defaults=dict(port=8000,served_name='example',cache_root=None,placement=None),specs={})))
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
  CLUSTER_TOPOLOGY_COUNT={nodes}; CLUSTER_TOPOLOGY_ID={'a'*64}; CLUSTER_TOPOLOGY_LOADED=1
  CLUSTER_NODE_IDS=(node-0 node-1)
  CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1)
  CLUSTER_NODE_SSH_HOSTS=(local rank-1)
  CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2)
  CLUSTER_NODE_CONTROL_IFS=(eth0 eth0)
  CLUSTER_PROFILE_HCAS=('mlx5_0,mlx5_1' 'mlx5_0,mlx5_1')
}}
require_profile_topology() {{ load_cluster_topology; }}
stack_build_revision() {{ printf '%s\n' {'e'*40}; }}
resolve_single_node_placement() {{ load_cluster_topology || return; SINGLE_NODE_INDEX=0; SINGLE_NODE_ID=node-0; SINGLE_NODE_HOSTNAME=rank-0; SINGLE_NODE_SSH_HOST=local; SINGLE_NODE_CONTROL_IP=192.0.2.1; SINGLE_NODE_REMOTE=0; SINGLE_NODE_TOPOLOGY_ID="$CLUSTER_TOPOLOGY_ID"; }}
library_hot_info_for_profile() {{ [ "$FIXTURE_MODE" != corrupt-files ] || return 2; [ "$FIXTURE_MODE" != missing-files ] || return 1; cat "$FIXTURE_ROOT/prepared.json"; }}
ssh_node() {{ local rank="$1"; shift; FIXTURE_RANK="$rank" python3 "$FIXTURE_ROOT/docker.py" $([ "${{1#docker image}}" != "$1" ] && echo image || echo inspect); }}
''')
            env={**os.environ,'BASH_ENV':str(envfile),'FIXTURE_ROOT':str(root),'FIXTURE_MODE':mode,'PULSAR_DOCKER':str(tool),'PULSAR_MODEL_LIBRARY_DIR':str(root/'library'),'PULSAR_SPEC_FILE':str(path),'PULSAR_OVERLAY_PATH':str(root/'overlay.json'),'VLLM_IMAGE_MAINLINE':'example/image','VLLM_EXTRA_ARGS':'','EXTRA_ENV':''}
            script=ROOT/('serve.sh' if nodes==1 else 'cluster/start-cluster.sh') if launcher else ROOT/'scripts/observe-serving.sh'
            flags=['--dry-run'] if launcher else ['--json']
            if launcher and nodes>1:flags += ['--skip-preflight']
            return subprocess.run(['bash',str(script),spec['spec_id'],*flags],env=env,text=True,capture_output=True)

    def test_one_and_two_nodes(self):
        for nodes in (1,2):
            result=self.run_scenario(nodes)
            self.assertEqual(result.returncode,0,result.stderr)
            doc=json.loads(result.stdout);self.assertEqual(len(doc['ranks']),nodes)

    def test_low_level_dry_run_uses_spec_and_distinct_mounts(self):
        for nodes in (1,2):
            result=self.run_scenario(nodes,launcher=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('example-rank-0',result.stdout)
            if nodes==2:self.assertIn('example-rank-1',result.stdout)

    def test_refusals(self):
        for mode in ('rank-loss','restart','unowned','corrupt-files','missing-files','no-topology'):
            with self.subTest(mode=mode):
                result=self.run_scenario(2,mode)
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertNotIn('"files_verified": true',result.stdout)


if __name__=='__main__':unittest.main()
