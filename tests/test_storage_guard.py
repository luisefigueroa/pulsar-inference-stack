"""Explicit migration scope and any-container path protection at Bash boundary."""
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


class Guard(unittest.TestCase):
    def run_guard(self,*,node='node-0',rank='0',home='node-0',topology='a'*64,mode='empty',local=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,path,*_=fixture(root,2)
            raw=[] if mode=='empty' else [dict(Id='f'*64,State=dict(Running=False),Config=dict(Labels={}),Mounts=[dict(Source='/var/tmp/view')])]
            (root/'containers.json').write_text(json.dumps(raw))
            docker=root/'docker.py';docker.write_text('''#!/usr/bin/env python3
import os,pathlib,sys
root=pathlib.Path(os.environ['GUARD_FIXTURE']);(root/'observed').touch()
if os.environ['GUARD_MODE']=='unreachable':sys.exit(1)
if sys.argv[1]=='ps':
 if os.environ['GUARD_MODE']!='empty':print('f'*64)
else:print((root/'containers.json').read_text())
''');docker.chmod(0o755)
            envfile=root/'env.sh';envfile.write_text(f'''
. {shlex.quote(str(ROOT/'scripts/lib.sh'))}
load_cluster_topology() {{
 CLUSTER_TOPOLOGY_LOADED=1; CLUSTER_TOPOLOGY_ID={'a'*64}; CLUSTER_TOPOLOGY_COUNT=3
 CLUSTER_NODE_IDS=(node-0 node-1 node-2); CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2)
}}
require_profile_topology() {{ load_cluster_topology; }}
require_topology_ssh_trust() {{ load_cluster_topology; }}
ssh_node() {{ [ "$GUARD_MODE" != unreachable ] || return 255; touch "$GUARD_FIXTURE/observed"; cat "$GUARD_FIXTURE/containers.json"; }}
''')
            env={**os.environ,'BASH_ENV':str(envfile),'GUARD_FIXTURE':str(root),'GUARD_MODE':mode,'PULSAR_DOCKER':str(docker),'PULSAR_OVERLAY_PATH':str(root/'absent')}
            args=['bash',str(ROOT/'scripts/guard-storage.sh'),'--node',node,'--path','/var/tmp/view/snapshots/revision','--expected-topology-id',topology,'--spec-file',str(path),'--expected-rank',rank,'--expected-home-node',home,'--json']
            if local:args+=['--local-only']
            result=subprocess.run(args,env=env,text=True,capture_output=True)
            return result,(root/'observed').exists()

    def test_exact_selected_scope(self):
        result,observed=self.run_guard();self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(observed)
        self.assertTrue(json.loads(result.stdout)['safe'])

    def test_wrong_topology_role_home_and_locality_refused_before_inspection(self):
        for kwargs in (dict(topology='b'*64),dict(node='node-1'),dict(node='node-1',rank='1',local=True),dict(home='node-2'),dict(rank='2')):
            with self.subTest(kwargs=kwargs):
                result,observed=self.run_guard(**kwargs);self.assertNotEqual(result.returncode,0,result.stdout);self.assertFalse(observed)

    def test_stopped_unowned_container_still_blocks_storage(self):
        result,observed=self.run_guard(mode='blocked');self.assertNotEqual(result.returncode,0,result.stderr);self.assertTrue(observed)
        self.assertFalse(json.loads(result.stdout)['safe'])

    def test_unobservable_node_refused(self):
        result,_=self.run_guard(mode='unreachable');self.assertNotEqual(result.returncode,0,result.stdout)


if __name__=='__main__':unittest.main()
