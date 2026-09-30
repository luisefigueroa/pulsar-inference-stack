"""Sampling has moved to Stack; node metrics never imply a matching workload."""
import json
import pathlib
import tempfile
import unittest
import os
import signal
import select
import subprocess
import sys
import time
from unittest.mock import patch
from types import SimpleNamespace
from scripts import resource_sample as monitor

class ResourceSampling(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.tmpdir=pathlib.Path(self.temp.name)

    def test_proc_and_cgroup_readers(self) -> None:
        meminfo = self.tmpdir / "meminfo"
        meminfo.write_text(
            "MemAvailable: 1024 kB\nSwapTotal: 512 kB\nSwapFree: 128 kB\n",
            encoding="utf-8",
        )
        pressure = self.tmpdir / "pressure"
        pressure.write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=42\n",
            encoding="utf-8",
        )
        cgroup = self.tmpdir / "cgroup"
        cgroup.mkdir()
        (cgroup / "memory.current").write_text("100\n", encoding="utf-8")
        (cgroup / "memory.peak").write_text("150\n", encoding="utf-8")
        (cgroup / "memory.swap.current").write_text("3\n", encoding="utf-8")
        (cgroup / "memory.events").write_text(
            "low 0\nhigh 0\nmax 0\noom 2\noom_kill 1\n",
            encoding="utf-8",
        )
        self.assertEqual(
            monitor.read_meminfo(meminfo),
            {"mem_available_bytes": 1024 * 1024, "swap_used_bytes": 384 * 1024},
        )
        self.assertEqual(monitor.read_pressure_some_total(pressure), 42)
        self.assertEqual(
            monitor.read_cgroup(cgroup),
            {
                "memory_current_bytes": 100,
                "memory_peak_bytes": 150,
                "memory_swap_current_bytes": 3,
                "oom": 2,
                "oom_kill": 1,
            },
        )

    def test_sampler_attaches_only_after_matching_owned_container_appears(self):
        proc=self.tmpdir/'proc'/'42';proc.mkdir(parents=True)
        (proc/'cgroup').write_text('0::/owned\n')
        root=self.tmpdir/'cgroups';(root/'owned').mkdir(parents=True)
        labels={'io.pulsar.gb10.managed':'true','io.pulsar.gb10.spec-id':'a'*64,
                'io.pulsar.gb10.node-id':'node-0','io.pulsar.gb10.topology':'b'*64}
        value={'Config':{'Labels':labels},'State':{'Pid':42,'Running':True}}
        with patch.multiple(monitor,EXPECTED_SPEC_ID='a'*64,EXPECTED_NODE_ID='node-0',EXPECTED_TOPOLOGY_ID='b'*64):
            with patch.object(monitor.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')):
                self.assertIsNone(monitor.cgroup_for_container('vllm-example',proc_root=self.tmpdir/'proc',cgroup_root=root))
            for change in ('wrong-spec','unowned','matching'):
                data=json.loads(json.dumps(value))
                if change=='wrong-spec':data['Config']['Labels']['io.pulsar.gb10.spec-id']='c'*64
                if change=='unowned':data['Config']['Labels']['io.pulsar.gb10.managed']='false'
                with patch.object(monitor.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(data))) as run:
                    actual=monitor.cgroup_for_container('vllm-example',proc_root=self.tmpdir/'proc',cgroup_root=root)
                    self.assertEqual(actual,root/'owned' if change=='matching' else None)
                    self.assertIn('inspect',run.call_args.args[0])

    def test_invalid_interval_never_probes_docker(self):
        for interval in (float('nan'),float('inf'),0.01):
            with patch.object(monitor,'cgroup_for_container') as lookup:
                with self.assertRaises(ValueError):
                    monitor.collect(rank='single',container_name='vllm-example',interval=interval,session_token='a'*32)
                lookup.assert_not_called()

    def test_public_prelaunch_stream_needs_no_model_files_or_service(self):
        root=pathlib.Path(__file__).resolve().parents[1]
        binaries=self.tmpdir/'bin';binaries.mkdir()
        python=binaries/'python3'
        python.write_text('#!'+sys.executable+'\n'+'''
import json,os,sys,time
if any(arg.endswith('/resource_sample.py') for arg in sys.argv[1:]):
 rank=sys.argv[sys.argv.index('--rank-label')+1]
 while True:
  print(json.dumps({'schema_version':1,'kind':'pulsar-model-serving-resource-sample','rank':rank,'workload':None,'node':{'mem_available_bytes':1024}}),flush=True)
  time.sleep(.1)
os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
''');python.chmod(0o700)
        environment=self.tmpdir/'env.sh'
        environment.write_text(f'''
. '{root}/scripts/lib.sh'
load_cluster_topology() {{ CLUSTER_TOPOLOGY_ID={'b'*64}; CLUSTER_TOPOLOGY_COUNT=1; CLUSTER_TOPOLOGY_LOADED=1; CLUSTER_NODE_IDS=(node-0); CLUSTER_NODE_HOSTNAMES=(fixture); CLUSTER_NODE_SSH_HOSTS=(local); }}
resolve_single_node_placement() {{ load_cluster_topology; SINGLE_NODE_INDEX=0; SINGLE_NODE_REMOTE=0; SINGLE_NODE_ID=node-0; }}
''')
        process=subprocess.Popen([str(root/'pulsar'),'resources','--spec-file',str(root/'tests/fixtures/contracts/spec.json'),'--jsonl'],
            cwd=self.tmpdir,env={**os.environ,'PATH':str(binaries)+os.pathsep+os.environ['PATH'],'BASH_ENV':str(environment),
                'PULSAR_MODEL_LIBRARY_DIR':str(self.tmpdir/'state')},text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            ready,_,_=select.select([process.stdout],[],[],8)
            self.assertTrue(ready,'resource header was not emitted')
            header=json.loads(process.stdout.readline())
            self.assertEqual(header['kind'],'pulsar-resource-stream')
            ready,_,_=select.select([process.stdout],[],[],8)
            self.assertTrue(ready,'resource sample was not emitted')
            sample=json.loads(process.stdout.readline())
            self.assertIsNone(sample['workload'])
            self.assertEqual(sample['node']['mem_available_bytes'],1024)
            self.assertFalse((self.tmpdir/'state'/'services').exists())
        finally:
            if process.poll() is None: os.killpg(process.pid,signal.SIGTERM)
            try: process.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()

    def test_public_selected_prelaunch_stream_samples_only_the_ordered_remote_pair(self):
        from tests.test_container_runtime import fixture
        root=pathlib.Path(__file__).resolve().parents[1]
        spec=fixture(2)[0];path=self.tmpdir/'selected-spec.json';path.write_text(json.dumps(spec))
        environment=self.tmpdir/'selected-env.sh'
        environment.write_text(f'''
. '{root}/scripts/lib.sh'
load_cluster_topology() {{
 CLUSTER_TOPOLOGY_ID={'b'*64}; CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_LOADED=1
 CLUSTER_NODE_IDS=(node-0 node-1 node-2); CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2)
 CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2); CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3)
 CLUSTER_NODE_CONTROL_IFS=(eth0 eth0 eth0)
}}
require_cluster_nodes() {{ load_cluster_topology; [ "$1" -le 3 ]; }}
require_profile_topology() {{ load_cluster_topology; }}
require_topology_ssh_trust() {{ return 0; }}
ssh_node() {{
 printf '%s\\n' "$1" >>'{self.tmpdir}/samplers'
 python3 -u - "$@" <<'PY_SAMPLE'
import json,sys,time
rank=sys.argv[sys.argv.index('--rank-label')+1]
while True:
 print(json.dumps({{'schema_version':1,'kind':'pulsar-model-serving-resource-sample','rank':rank,'workload':None,'node':{{'mem_available_bytes':1024}}}}),flush=True)
 time.sleep(.1)
PY_SAMPLE
}}
''')
        process=subprocess.Popen([str(root/'pulsar'),'resources','--spec-file',str(path),
            '--placement-nodes','node-2,node-1','--jsonl'],cwd=self.tmpdir,
            env={**os.environ,'BASH_ENV':str(environment),'PULSAR_MODEL_LIBRARY_DIR':str(self.tmpdir/'state')},
            text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            self.assertTrue(select.select([process.stdout],[],[],8)[0])
            header=json.loads(process.stdout.readline());self.assertEqual(header['ranks'], ['0','1'])
            ranks=set()
            deadline=time.monotonic()+8
            while len(ranks)<2 and time.monotonic()<deadline:
                if select.select([process.stdout],[],[],1)[0]:
                    row=json.loads(process.stdout.readline())
                    if row.get('kind')=='pulsar-model-serving-resource-sample':ranks.add(row['rank'])
            self.assertEqual(ranks, {'0','1'})
            self.assertCountEqual((self.tmpdir/'samplers').read_text().splitlines(), ['2','1'])
            self.assertFalse((self.tmpdir/'state'/'services').exists())
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
            try:process.communicate(timeout=6)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.communicate()

if __name__=='__main__':unittest.main()
