"""Read-only diagnostics and explicit image staging over parameterized fake nodes."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_runtime_binding import fixture


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.spec,self.path,self.prepared,self.facts,self.plan,self.containers,self.images=fixture(self.root,3)
        (self.root/'releases').mkdir();(self.root/'homes').mkdir();(self.root/'views').mkdir()
        for image in self.images:image['Architecture']='arm64'
        (self.root/'image.json').write_text(json.dumps(self.images[0]))
        tool=self.root/'docker.py';tool.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['DIAG_ROOT']);args=sys.argv[1:];rank=int(os.environ.get('DIAG_RANK','0'));mode=os.environ['DIAG_MODE']
if args[0]=='info':print('nvidia' if '-f' in args else 'Runtimes: nvidia');sys.exit(0)
if args[:2]==['image','inspect']:
 if (mode in ('missing-pull','missing-ref-after-load') and rank==1 and not (root/'pulled').exists()) or (mode=='lost-and-missing' and rank==2):sys.exit(1)
 image=json.loads((root/'image.json').read_text())
 if mode=='wrong-image' and rank==1:image['RepoDigests']=['example/image@sha256:'+'f'*64]
 print(json.dumps(image));sys.exit(0)
if args[0] in ('pull','save','load'):
 with (root/'mutations').open('a') as f:f.write(args[0]+' '+str(rank)+'\\n')
 if args[0]=='pull':(root/'pulled').touch()
 if args[0]=='save':sys.stdout.write('image-fixture')
 if args[0]=='load':sys.stdin.read()
 sys.exit(0)
if args[0]=='ps':sys.exit(0)
sys.exit(2)
''');tool.chmod(0o755)
        gpu=self.root/'gpu';gpu.write_text('#!/usr/bin/env bash\nif [ "$DIAG_MODE" = gpu-wrong ]; then echo Other; else echo "NVIDIA GB10"; fi\n');gpu.chmod(0o755)
        envfile=self.root/'env.sh';envfile.write_text(f'''
. {shlex.quote(str(ROOT/'scripts/lib.sh'))}
uname() {{ echo aarch64; }}
ss() {{ :; }}
curl() {{ return 1; }}
load_cluster_topology() {{
 [ "$DIAG_MODE" != no-topology ] || return 1
 CLUSTER_TOPOLOGY_LOADED=1; CLUSTER_TOPOLOGY_ID={'a'*64}; CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_SSH_TRUSTED=1
 CLUSTER_NODE_IDS=(node-0 node-1 node-2); CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2)
 CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2); CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3)
}}
require_profile_topology() {{ load_cluster_topology; }}
require_topology_ssh_trust() {{ load_cluster_topology; }}
mem_available_gib_local() {{ if [ "$DIAG_MODE" = memory-low ]; then echo 0; else echo 120; fi; }}
mem_available_gib_remote() {{ echo 120; }}
profile_service_is_proven_running() {{ return 1; }}
probe_node_json_for_rank() {{
 [ "$DIAG_MODE" != remote-probe-fail ] || return 1
 printf '{{"gpu":"NVIDIA GB10","arch":"aarch64","docker_ok":true,"docker_nvidia":true,"qualified":true,"node_id":"node-%s"}}\\n' "$1"
}}
ssh_node() {{
 local rank="$1" cmd="$2"
 if [ "$DIAG_MODE" = lost-and-missing ] && [ "$rank" = 1 ]; then return 255; fi
 [ "$cmd" != true ] || return 0
 case "$cmd" in
  'docker info'*) DIAG_RANK="$rank" "$PULSAR_DOCKER" info >/dev/null ;;
  'docker image inspect'*) DIAG_RANK="$rank" "$PULSAR_DOCKER" image inspect ;;
  'docker pull'*) DIAG_RANK="$rank" "$PULSAR_DOCKER" pull ;;
  'docker load'*) DIAG_RANK="$rank" "$PULSAR_DOCKER" load ;;
  *) return 2 ;;
 esac
}}
''')
        self.env={**os.environ,'BASH_ENV':str(envfile),'DIAG_ROOT':str(self.root),'DIAG_MODE':'ok','PULSAR_DOCKER':str(tool),'PULSAR_NVIDIA_SMI':str(gpu),'PULSAR_SPEC_FILE':str(self.path),'PULSAR_RELEASES_ROOT':str(self.root/'releases'),'PULSAR_OVERLAY_PATH':str(self.root/'absent'),'VLLM_IMAGE_MAINLINE':'example/image','PULSAR_HOME_ROOT':str(self.root/'homes'),'PULSAR_HOT_ROOT':str(self.root/'views'),'PULSAR_COLD_ROOT':'','VLLM_EXTRA_ARGS':'','EXTRA_ENV':'','COLUMNS':'48','NO_COLOR':'1'}

    def run_tool(self,name,args=(),mode='ok',extra=None):
        return subprocess.run(['bash',str(ROOT/'scripts'/name),*args],env={**self.env,'DIAG_MODE':mode,**(extra or {})},cwd=ROOT,text=True,capture_output=True)

    def test_doctor_empty_catalog_is_not_missing_library_health(self):
        result=self.run_tool('doctor.sh',['--json'])
        self.assertEqual(result.returncode,0,result.stderr)
        doc=json.loads(result.stdout);self.assertEqual(doc['kind'],'pulsar-doctor')
        self.assertNotEqual(doc['result'],'fail')
        self.assertTrue(any(c['id']=='catalog' and '0 catalog' in c['message'] for c in doc['checks']))
        self.assertFalse((self.root/'mutations').exists())

    def test_doctor_hardware_topology_and_memory_failures_are_visible(self):
        for mode in ('gpu-wrong','remote-probe-fail','no-topology','memory-low'):
            result=self.run_tool('doctor.sh',['--json'],mode)
            with self.subTest(mode=mode):
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertEqual(json.loads(result.stdout)['result'],'fail')
                self.assertFalse((self.root/'mutations').exists())

    def test_narrow_doctor_and_image_output(self):
        result=self.run_tool('doctor.sh');self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('selected-spec launch checks',result.stdout)
        result=self.run_tool('check-image.sh',[self.spec['spec_id']]);self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(all(len(line)<=48 for line in result.stdout.splitlines()))

    def test_images_all_ranks_and_failure_priority(self):
        result=self.run_tool('check-image.sh',[self.spec['spec_id'],'--json'])
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(len(json.loads(result.stdout)['ranks']),3)
        for mode,state in [('lost-and-missing','rank-unreachable'),('wrong-image','rank-docker-error')]:
            result=self.run_tool('check-image.sh',[self.spec['spec_id'],'--json'],mode)
            self.assertNotEqual(result.returncode,0);self.assertEqual(json.loads(result.stdout)['state'],state)
        self.assertFalse((self.root/'mutations').exists())

    def test_memory_json_and_low_memory(self):
        result=self.run_tool('check-memory.sh',[self.spec['spec_id'],'--json'])
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(len(json.loads(result.stdout)['rank_available_gib']),3)
        result=self.run_tool('check-memory.sh',[self.spec['spec_id'],'--json'],'memory-low')
        self.assertEqual(result.returncode,1,result.stderr);self.assertEqual(json.loads(result.stdout)['result'],'fail')

    def test_image_preview_and_confirmation(self):
        for flags in (['--plan'],[]):
            result=self.run_tool('sync-image.sh',[self.spec['spec_id'],*flags],'missing-pull')
            self.assertEqual(result.returncode,0 if flags else 2,result.stderr)
            self.assertFalse((self.root/'mutations').exists())
        result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--pull','--yes'],'missing-pull')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'mutations').read_text(),'pull 1\n')

    def test_stream_failure_never_silently_pulls(self):
        result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--yes'],'missing-ref-after-load')
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('pull',(self.root/'mutations').read_text())
        self.assertIn('explicit --pull',result.stderr)

    def raw_inventory(self):
        names=['head','worker','rank-2']
        nodes={name:dict(node_id=f'node-{i}',confirmed=True,probe_status='ok',mem_available_gib=120,mem_total_gib=128,mem_status='ok',topology_index=i) for i,name in enumerate(names)}
        containers=[]
        for i,c in enumerate(self.containers):
            containers.append(dict(node=names[i],id=c['Id'],name='vllm-cluster-'+self.spec['spec_id'],image=c['Config']['Image'],labels=c['Config']['Labels'],cmd=c['Config']['Cmd'],running=True,status='running',pids=[]))
        return dict(profiles={},containers=containers,nodes=nodes,topology_id='a'*64,worker_status='ok',gpu_processes=[])

    def inventory(self,raw):
        path=self.root/'inventory-raw.json';path.write_text(json.dumps(raw))
        return self.run_tool('inventory.sh',['--from-fixture',str(path),'--json'])

    def test_uncatalogued_candidate_is_owned_without_claiming_recipe_verification(self):
        result=self.inventory(self.raw_inventory());self.assertEqual(result.returncode,0,result.stderr)
        service=json.loads(result.stdout)['services'][0]
        self.assertTrue(service['safe_to_stop']);self.assertTrue(service['complete'])
        self.assertFalse(service['recipe_verified'])
        self.assertNotIn('model_seal_id',service)

    def test_inventory_wrong_physical_identity_and_unknown_rank(self):
        for mode in ('wrong-node','unknown-node'):
            raw=self.raw_inventory()
            if mode=='wrong-node':raw['containers'][1]['labels']['io.pulsar.gb10.node-id']='node-0'
            else:raw['nodes']['worker']['probe_status']='unreachable'
            result=self.inventory(raw);self.assertEqual(result.returncode,0,result.stderr)
            service=json.loads(result.stdout)['services'][0]
            self.assertFalse(service['safe_to_stop']);self.assertFalse(service['complete'])

    def test_quick_status_does_not_call_stopped_containers_nonblocking(self):
        raw=self.raw_inventory()
        for c in raw['containers']:c['running']=False;c['status']='exited'
        inv=self.inventory(raw);self.assertEqual(inv.returncode,0,inv.stderr)
        path=self.root/'inventory.json';path.write_text(inv.stdout)
        api=self.root/'api.json';api.write_text('{"data":[]}')
        result=self.run_tool('quick-status.sh',['--json'],extra={'QUICK_STATUS_INVENTORY_JSON':str(path),'QUICK_STATUS_API_JSON':str(api)})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(json.loads(result.stdout)['stale_managed']['nonblocking'])
        self.assertFalse((self.root/'mutations').exists())


if __name__=='__main__':unittest.main()
