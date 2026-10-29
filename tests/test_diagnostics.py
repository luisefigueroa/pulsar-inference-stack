"""Read-only diagnostics and explicit image staging over parameterized fake nodes."""
import copy
import json
import os
from pathlib import Path
import re
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
        for image in self.images:image.update(Architecture='arm64',Os='linux')
        (self.root/'image.json').write_text(json.dumps(self.images[0]))
        tool=self.root/'docker.py';tool.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['DIAG_ROOT']);args=sys.argv[1:];rank=int(os.environ.get('DIAG_RANK','0'));mode=os.environ['DIAG_MODE']
if args[0]=='info':print('nvidia' if '-f' in args else 'Runtimes: nvidia');sys.exit(0)
if args[:2]==['image','inspect']:
 if (mode in ('missing-pull','missing-ref-after-load') and rank==1 and not (root/'pulled').exists()) or (mode=='lost-and-missing' and rank==2) or (mode=='missing-controller' and rank==0):sys.exit(1)
 if mode in ('named-success','export-tag-collision','export-tag-unobservable','export-tag-drift','export-ref-missing') and rank>0 and not (root/('loaded-'+str(rank))).exists():sys.exit(1)
 image=json.loads((root/'image.json').read_text())
 # other-name: rank 1 holds the pinned content only under a local name, found by its digest.
 if mode in ('other-name','other-name-controller') and rank==(1 if mode=='other-name' else 0) and not (root/'pulled').exists():
  if not args[-1].startswith('sha256:'):sys.exit(1)
  image.update(Id=args[-1],RepoTags=['pulsar-local/model:latest'],RepoDigests=[])
 if mode=='wrong-image' and rank==1:image['RepoDigests']=['example/image@sha256:'+'f'*64]
 if mode=='legacy-image-id-only':image['RepoDigests']=[]
 if mode=='wrong-repository':image['RepoDigests']=['other/repository@'+image['RepoDigests'][0].split('@')[1]]
 if mode=='export-ref-missing' and rank>0:image['RepoDigests']=[]
 if len(args)==4 and '--format' not in args:
  tagged=dict(image)
  if mode=='export-tag-mismatch' or (mode=='export-tag-drift' and (root/'loaded-1').exists()):tagged['Id']='sha256:'+'f'*64
  print(json.dumps([image,tagged]));sys.exit(0)
 print(json.dumps(image));sys.exit(0)
if args[:2]==['image','ls']:
 if mode=='export-tag-unobservable':sys.exit(1)
 if mode=='export-tag-collision':print('sha256:'+'f'*64)
 sys.exit(0)
if args[0] in ('pull','save','load'):
 with (root/'mutations').open('a') as f:f.write(args[0]+' '+str(rank)+'\\n')
 if args[0]=='pull':(root/'pulled').touch()
 if args[0]=='save':
  with (root/'saved-references').open('a') as f:f.write(args[1]+'\\n')
  sys.stdout.write('image-fixture')
 if args[0]=='load':sys.stdin.read();(root/('loaded-'+str(rank))).touch()
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
 CLUSTER_TOPOLOGY_LOADED=1; CLUSTER_TOPOLOGY_ID={'c'*64}; CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_SSH_TRUSTED=1
 CLUSTER_NODE_IDS=(node-0 node-1 node-2); CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2)
 CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2); CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3)
 CLUSTER_NODE_CONTROL_IFS=(eth0 eth0 eth0); CLUSTER_PROFILE_HCAS=(mlx5_0 mlx5_0 mlx5_0)
}}
require_profile_topology() {{ load_cluster_topology; }}
require_cluster_nodes() {{ load_cluster_topology; [ "$1" -le "$CLUSTER_TOPOLOGY_COUNT" ]; }}
require_topology_ssh_trust() {{ load_cluster_topology; }}
mem_available_gib_local() {{ if [ "$DIAG_MODE" = memory-low ]; then echo 0; else echo 120; fi; }}
mem_available_gib_remote() {{ [ "$DIAG_MODE" != remote-memory-unreadable ] || return 1; echo 120; }}
profile_service_is_proven_running() {{ return 1; }}
probe_node_json_for_rank() {{
 [ "$DIAG_MODE" != remote-probe-fail ] || return 1
 local docker=true; [ "$DIAG_MODE" != remote-docker-down ] || [ "$1" = 0 ] || docker=false
 printf '{{"gpu":"NVIDIA GB10","arch":"aarch64","docker_ok":%s,"docker_nvidia":%s,"qualified":true,"node_id":"node-%s"}}\\n' "$docker" "$docker" "$1"
}}
ssh_node() {{
 local rank="$1" cmd="$2"
 if [ "$DIAG_MODE" = lost-and-missing ] && [ "$rank" = 1 ]; then return 255; fi
 [ "$cmd" != true ] || return 0
 case "$cmd" in
  'docker info'*) DIAG_RANK="$rank" "$PULSAR_DOCKER" info >/dev/null ;;
  # The command is shell-quoted; eval passes the inspected reference through.
  'docker image inspect'*) eval "DIAG_RANK=$rank $PULSAR_DOCKER ${{cmd#docker }}" ;;
  'docker image ls'*) DIAG_RANK="$rank" "$PULSAR_DOCKER" image ls ;;
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
                # Every failed row says what to do next, not only what is wrong.
                failed=[c['message'] for c in json.loads(result.stdout)['checks'] if c['level']=='fail']
                self.assertTrue(failed)
                for message in failed:
                    self.assertRegex(message,r'; (run|install|fix|free|inspect|start|this (host|node))|scripts/check-catalog',message)
                self.assertFalse((self.root/'mutations').exists())

    def test_remote_docker_down_leaves_its_nvidia_runtime_unobserved(self):
        result=self.run_tool('doctor.sh',['--json'],'remote-docker-down')
        rows={c['id']:c for c in json.loads(result.stdout)['checks']}
        self.assertEqual(rows['rank_1_docker_ok']['level'],'fail')
        self.assertIn('start or install Docker',rows['rank_1_docker_ok']['message'])
        # The runtime query needs Docker; its absence is not reported as a missing toolkit.
        self.assertEqual(rows['rank_1_docker_nvidia']['level'],'warn')
        self.assertNotIn('install the NVIDIA Container Toolkit',rows['rank_1_docker_nvidia']['message'])

    def test_ready_is_green_only_where_a_terminal_shows_it(self):
        # PULSAR_COLOR=always: the menu's pager shows this captured output on a terminal.
        terminal = {'NO_COLOR': '', 'TERM': 'xterm-256color'}
        for extra, colored in (({**terminal, 'PULSAR_COLOR': 'always'}, True), (terminal, False),
                               ({**terminal, 'PULSAR_COLOR': 'always', 'NO_COLOR': '1'}, False),
                               ({**terminal, 'PULSAR_COLOR': 'never'}, False)):
            with self.subTest(extra=extra):
                result = self.run_tool('doctor.sh', extra=extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('READY — host checks passed', re.sub(r'\x1b\[[0-9;]*m', '', result.stdout))
                self.assertEqual('\x1b[1;32mREADY\x1b[0m — ' in result.stdout, colored, result.stdout)
                # NO_COLOR keeps READY bold; never prints no escape codes.
                self.assertEqual('\x1b' in result.stdout, extra.get('PULSAR_COLOR') == 'always', result.stdout)

    def test_doctor_groups_checks_under_each_node(self):
        result = self.run_tool('doctor.sh', mode='remote-docker-down')
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(all(len(line) <= 48 for line in lines), result.stdout)
        headings = [line for line in lines if line and not line.startswith(' ') and ' — ' not in line]
        self.assertEqual(headings[1:], ['Cluster membership', 'rank-1', 'rank-2', 'Catalog'])
        self.assertTrue(headings[0].endswith(' (this node)'), headings[0])
        # Rows sit under their node: the hostname prefix stays in JSON only.
        self.assertNotIn('rank-1:', result.stdout)
        section = result.stdout.split('\nrank-1\n')[1].split('\n\n')[0]
        self.assertIn('  FAIL  Docker unavailable; start or install', section)
        self.assertIn('  warn  Docker NVIDIA runtime not observed', section)
        self.assertIn('  ok    120 GiB memory available', section)
        self.assertTrue(lines[-2].startswith('NOT READY — 2 blocking issues above'), lines[-2:])
        rows = {c['id']: c for c in json.loads(self.run_tool('doctor.sh', ['--json'], 'remote-docker-down').stdout)['checks']}
        self.assertEqual(rows['rank_1_memory']['message'], 'rank-1: MemAvailable 120 GiB')
        self.assertNotIn('`', json.dumps(rows))

    def test_doctor_marks_commands_and_counts_warnings(self):
        result = self.run_tool('doctor.sh', mode='memory-low', extra={'PULSAR_COLOR': 'always', 'NO_COLOR': '',
                                                                     'COLUMNS': '100'})
        self.assertIn('\x1b[1;31mFAIL\x1b[0m  ', result.stdout)
        self.assertIn('\x1b[36m./pulsar inventory\x1b[0m', result.stdout)
        self.assertIn('\x1b[33mwarn\x1b[0m  API key authentication is not configured', result.stdout)
        self.assertIn('\x1b[1;31mNOT READY\x1b[0m — 1 blocking issue above', result.stdout)
        self.assertNotIn('\x1b[32mok', result.stdout)
        # JSON keeps its message, without the marks.
        rows = {c['id']: c for c in json.loads(self.run_tool('doctor.sh', ['--json'], 'memory-low').stdout)['checks']}
        self.assertRegex(rows['memory']['message'], r'^MemAvailable 0 GiB < hard floor \d+ GiB; free memory or '
                                                    r'stop other GPU work \(\./pulsar inventory\)$')

    def test_narrow_doctor_and_image_output(self):
        result=self.run_tool('doctor.sh');self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('catalog is empty',result.stdout)
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

    def test_a_check_that_could_not_run_exits_3(self):
        # 0 pass, 1 the condition failed, 2 memory warning, 3 the check could not run.
        spec_id,wrong=self.spec['spec_id'],'f'*64
        for name,args in (('check-image.sh',[wrong]),('check-memory.sh',[wrong]),('check-weights.sh',[wrong]),
                          ('check-memory.sh',[spec_id,'--bogus']),('check-weights.sh',[spec_id,'--node'])):
            with self.subTest(name=name,args=args):
                result=self.run_tool(name,args)
                self.assertEqual(result.returncode,3,result.stderr)
        # pulsar image check runs check-image.sh directly: a usage mistake there exits 2.
        for args in ([],['--json'],[spec_id,'--bogus'],[spec_id,'--node'],[spec_id,'--node','--json']):
            with self.subTest(name='check-image.sh',args=args):
                result=self.run_tool('check-image.sh',args)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertTrue(result.stderr.startswith('error: '),result.stderr)
        # An unreadable node is a check that could not run, never a node with 0 GiB available.
        result=self.run_tool('check-memory.sh',[spec_id,'--json'],'remote-memory-unreadable')
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertIn('cannot read available memory on alias-1',result.stderr)
        result=self.run_tool('doctor.sh',['--json'],'remote-memory-unreadable')
        rows={c['id']:c for c in json.loads(result.stdout)['checks']}
        self.assertEqual(rows['rank_1_memory']['level'],'fail')
        self.assertIn('memory unavailable',rows['rank_1_memory']['message'])

    def test_the_check_convention_keeps_interrupts_and_usage_mistakes(self):
        # 3 is a check that could not run; an interrupt stays 128+signal even when a
        # child caught the signal and exited with it, and keep-usage keeps usage_die's 2.
        for body,status in (('exit 130',130),('exit 143',143),('exit 1',3),('exit 255',3),
                            ('usage_die "bad flag"',2),('check_result 1',1)):
            with self.subTest(body=body):
                result=subprocess.run(['bash','-c',f". {shlex.quote(str(ROOT/'scripts/lib.sh'))}; "
                                       f"check_exit_convention keep-usage; {body}"],text=True,capture_output=True)
                self.assertEqual(result.returncode,status,result.stderr)

    def test_image_preview_and_confirmation(self):
        spec_id=self.spec['spec_id']
        for flags in (['--plan'],[]):
            result=self.run_tool('sync-image.sh',[spec_id,*flags],'missing-pull')
            # Without --yes, staging refuses after looking (1), like every approval gate; 2 means nothing ran.
            self.assertEqual(result.returncode,0 if flags else 1,result.stderr)
            self.assertFalse((self.root/'mutations').exists())
        self.assertEqual(result.stderr.strip(),
                         f'error: the pinned image is missing on rank-1 (rank 1); nothing was staged. '
                         f'Next: ./pulsar image stage {spec_id} --plan, then rerun it with --yes')
        # The suggestion repeats the operator's selection and mode.
        result=self.run_tool('sync-image.sh',[spec_id,'--pull','--placement-nodes','rank-0,rank-1,rank-2'],'missing-pull')
        self.assertIn(f'Next: ./pulsar image stage {spec_id} --pull --placement-nodes rank-0,rank-1,rank-2 --plan',
                      result.stderr)
        # Following it keeps the registry fallback the operator already granted.
        result=self.run_tool('sync-image.sh',[spec_id,'--pull-if-stream-incomplete'],'missing-pull')
        self.assertIn(f'Next: ./pulsar image stage {spec_id} --pull-if-stream-incomplete --plan',result.stderr)
        # An image already present needs no approval.
        result=self.run_tool('sync-image.sh',[spec_id])
        self.assertEqual(result.returncode,0,result.stderr)
        # Staging copies from this node; when it lacks the image too, the next step pulls.
        result=self.run_tool('sync-image.sh',[spec_id],'missing-controller')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertIn('missing on rank-0 (rank 0); nothing was staged. This node lacks it too, so staging must pull '
                      f'the exact digest from its registry. Next: ./pulsar image stage {spec_id} --pull --plan',
                      ' '.join(result.stderr.split()))

    def test_image_check_names_the_nodes_and_one_next_step(self):
        spec_id=self.spec['spec_id']
        for mode,text,next_step in (
                ('missing-pull',f'the pinned image is missing on rank-1 (rank 1)',f'./pulsar image stage {spec_id} --plan'),
                ('missing-controller',f'the pinned image is missing on rank-0 (rank 0)',f'./pulsar image stage {spec_id} --pull --plan'),
                ('lost-and-missing','rank-1 (rank 1) could not be reached, so the image there is unknown; the pinned '
                                    'image is missing on rank-2 (rank 2)','./pulsar topology check')):
            with self.subTest(mode=mode):
                result=self.run_tool('check-image.sh',[spec_id],mode)
                self.assertEqual(result.returncode,1,result.stderr)
                *problem,last=result.stdout.splitlines()
                self.assertEqual(' '.join(' '.join(problem).split()),'FAIL image '+text)
                # The next command is never wrapped, so it can be copied whole.
                self.assertEqual(last.strip(),'Next: '+next_step)
        # Start runs the same check and gives its own next step.
        result=self.run_tool('check-image.sh',[spec_id],'missing-pull',{'PULSAR_RUN_BY_START':'1'})
        self.assertEqual(' '.join(result.stdout.split()),'FAIL image the pinned image is missing on rank-1 (rank 1)')
        result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--pull','--yes'],'missing-pull')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'mutations').read_text(),'pull 1\n')

    def test_stream_failure_never_silently_pulls(self):
        result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--yes'],'missing-ref-after-load')
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('pull',(self.root/'mutations').read_text())
        self.assertIn('rerun with --pull to pull the exact digest',result.stderr)

    def test_start_pull_permission_pulls_the_digest_a_stream_lost(self):
        result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--yes','--pull-if-stream-incomplete'],'missing-ref-after-load')
        self.assertEqual(result.returncode,0,result.stderr)
        mutations=(self.root/'mutations').read_text().splitlines()
        # save and load run concurrently in one pipeline; the exact-digest pull follows them.
        self.assertEqual((sorted(mutations[:2]),mutations[2:]),(['load 1','save 0'],['pull 1']))

    def test_named_export_preview_and_exact_reference_readback(self):
        tag=self.spec['source']['image_repository']+':staging-test'
        flags=[self.spec['spec_id'],'--export-tag',tag,'--json']
        result=self.run_tool('sync-image.sh',[*flags,'--plan'],'named-success')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['export_reference'],tag)
        self.assertFalse((self.root/'mutations').exists())
        result=subprocess.run([str(ROOT/'pulsar'),'image','stage',*flags,'--yes'],
            env={**self.env,'DIAG_MODE':'named-success'},cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertTrue(value['ok'])
        self.assertEqual(value['result']['state'],'ok')
        self.assertEqual(len(value['result']['ranks']),3)
        mutations=(self.root/'mutations').read_text().splitlines()
        self.assertCountEqual(mutations,['save 0','load 1','save 0','load 2'])
        self.assertEqual([item for item in mutations if item.startswith('load')],['load 1','load 2'])
        self.assertEqual((self.root/'saved-references').read_text().splitlines(), [tag, tag])

    def test_named_export_stages_only_two_selected_ranks_of_three_members(self):
        self.spec, self.path, *_ = fixture(self.root, 2)
        tag = self.spec['source']['image_repository']+':staging-test'
        before = (self.root/'env.sh').read_bytes()
        result = subprocess.run([str(ROOT/'pulsar'), 'image', 'stage', self.spec['spec_id'],
            '--spec-file', str(self.path), '--export-tag', tag, '--yes', '--json'],
            env={**self.env, 'DIAG_MODE': 'named-success'}, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)['result']
        self.assertEqual([row['topology_index'] for row in value['ranks']], [0, 1])
        self.assertEqual(value['image'], self.spec['source']['image_repository']+'@'+self.spec['recipe']['image_digest'])
        self.assertCountEqual((self.root/'mutations').read_text().splitlines(), ['save 0', 'load 1'])
        self.assertEqual((self.root/'env.sh').read_bytes(), before)

    def test_public_image_staging_and_memory_checks_use_ordered_remote_pair(self):
        self.spec, self.path, *_ = fixture(self.root, 2)
        tag = self.spec['source']['image_repository']+':staging-test'
        selected = 'node-2,node-1'
        result = subprocess.run([str(ROOT/'pulsar'), 'image', 'stage', self.spec['spec_id'],
            '--export-tag', tag, '--placement-nodes', selected, '--yes', '--json'],
            env={**self.env, 'DIAG_MODE': 'named-success'}, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row['topology_index'] for row in json.loads(result.stdout)['result']['ranks']], [2, 1])
        self.assertEqual([row for row in (self.root/'mutations').read_text().splitlines() if row.startswith('load')], ['load 2', 'load 1'])
        # Only the desktop/controller memory double is low; the selected pair is healthy.
        checked = self.run_tool('check-memory.sh', [self.spec['spec_id'], '--placement-nodes', selected, '--cold-start', '--json'], 'memory-low')
        self.assertEqual(checked.returncode, 0, checked.stderr+checked.stdout)
        self.assertEqual([row['available_gib'] for row in json.loads(checked.stdout)['rank_available_gib']], [120.0, 120.0])

    def test_ordinary_start_refuses_nondefault_pair_before_image_or_service_mutation(self):
        self.spec, self.path, *_ = fixture(self.root, 2)
        result = self.run_tool('up.sh', [self.spec['spec_id'], '--placement-nodes', 'node-2,node-1', '--yes'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires guarded run', result.stderr)
        self.assertFalse((self.root/'mutations').exists())

    def test_named_export_refuses_missing_peer_reference_without_pulling(self):
        tag = self.spec['source']['image_repository']+':staging-test'
        result = subprocess.run([str(ROOT/'pulsar'), 'image', 'stage', self.spec['spec_id'],
            '--export-tag', tag, '--yes', '--json'],
            env={**self.env, 'DIAG_MODE': 'export-ref-missing'}, cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertNotIn('pull', (self.root/'mutations').read_text())

    def test_guarded_dry_plan_admits_the_remote_head_and_checks_its_ports(self):
        from tests.test_serving_guard import guarded_fixture
        from scripts.container_runtime import validate_plan
        spec, _, prepared, _, _, _ = guarded_fixture(nodes=2)
        self.spec = spec; self.path.write_text(json.dumps(spec))
        for slot, physical in enumerate((2, 1)):
            prepared['ranks'][slot]['node_id'] = f'node-{physical}'
        (self.root/'prepared.json').write_text(json.dumps(prepared))
        with (self.root/'env.sh').open('a') as stream:
            stream.write('''
container_ownership_inspect_remote() { return 3; }
container_ownership_inspect_local() { return 3; }
library_hot_info_for_profile() { cat "$DIAG_ROOT/prepared.json"; }
port_free() { printf '%s %s\n' "$1" "${2:-local}" >>"$DIAG_ROOT/checked-ports"; return 0; }
''')
        plan_file = self.root/'dry-plan.json'
        result = subprocess.run([str(ROOT/'pulsar'), 'start', spec['spec_id'], '--spec-file', str(self.path),
            '--placement-nodes', 'node-2,node-1', '--dry-run'],
            env={**self.env, 'PULSAR_LAUNCH_PLAN_OUT':str(plan_file), 'PULSAR_MODEL_LIBRARY_DIR':str(self.root/'library')},
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        plan = validate_plan(json.loads(plan_file.read_text()))
        self.assertEqual([row['node_id'] for row in plan['ranks']], ['node-2','node-1'])
        self.assertEqual(plan['home_node_id'], 'node-0')
        self.assertEqual(plan['topology_id'], 'c'*64)
        self.assertEqual(plan['lifecycle_action'], 'dry-run')
        self.assertTrue(all(line.endswith('alias-2') for line in (self.root/'checked-ports').read_text().splitlines()))
        self.assertFalse((self.root/'mutations').exists())

    def test_image_id_or_another_repository_does_not_establish_pinned_reference(self):
        for mode in ('legacy-image-id-only', 'wrong-repository'):
            with self.subTest(mode=mode):
                result = subprocess.run([str(ROOT/'pulsar'), 'image', 'check', self.spec['spec_id'], '--json'],
                    env={**self.env, 'DIAG_MODE': mode}, cwd=ROOT, text=True, capture_output=True)
                report = json.loads(result.stdout)['result']
                self.assertNotEqual(report['state'], 'ok')
                self.assertFalse(any(rank['ok'] for rank in report['ranks']))
                self.assertFalse((self.root/'mutations').exists())

    # TUI-81: the pinned content under another name is named, and still fails until the pinned name exists.
    def test_pinned_content_under_another_name_is_named_and_still_fails(self):
        spec_id=self.spec['spec_id']
        result=subprocess.run([str(ROOT/'pulsar'),'image','check',spec_id,'--json'],
                              env={**self.env,'DIAG_MODE':'other-name'},cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)['result']
        self.assertEqual(report['state'],'missing-on-rank')
        self.assertEqual([rank['state'] for rank in report['ranks']],['ok','other-name','ok'])
        self.assertEqual((report['ranks'][1]['ok'],report['ranks'][1]['present_as']),(False,'pulsar-local/model:latest'))
        self.assertNotIn('present_as',report['ranks'][0])
        result=self.run_tool('check-image.sh',[spec_id],'other-name')
        self.assertEqual(result.returncode,1,result.stderr)
        *problem,last=result.stdout.splitlines()
        self.assertEqual(' '.join(' '.join(problem).split()),
                         'FAIL image the pinned content is present on rank-1 (rank 1) as pulsar-local/model:latest, '
                         'but the pinned name is not recorded; staging with --pull adds the pinned name, and the '
                         'content is already here')
        self.assertEqual(last.strip(),f'Next: ./pulsar image stage {spec_id} --pull --plan')
        self.assertFalse((self.root/'mutations').exists())
        # Staging without --pull suggests it; with --pull it pulls on that node only, and the check passes.
        result=self.run_tool('sync-image.sh',[spec_id],'other-name')
        self.assertIn('rank-1 (rank 1) holds the pinned content only under another name; nothing was staged. A '
                      'pull adds the pinned name and reuses the content already there. Next: ./pulsar image stage '
                      f'{spec_id} --pull --plan',' '.join(result.stderr.split()))
        self.assertNotIn('missing on',result.stderr)
        # On this node too: a pull, not "this node lacks it".
        result=self.run_tool('sync-image.sh',[spec_id],'other-name-controller')
        self.assertIn('rank-0 (rank 0) holds the pinned content only under another name; nothing was staged. A '
                      f'pull adds the pinned name',' '.join(result.stderr.split()))
        self.assertIn(f'Next: ./pulsar image stage {spec_id} --pull --plan',' '.join(result.stderr.split()))
        self.assertNotIn('lacks it too',result.stderr)
        self.assertFalse((self.root/'mutations').exists())
        result=self.run_tool('sync-image.sh',[spec_id,'--pull','--yes'],'other-name')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'mutations').read_text(),'pull 1\n')

    def test_image_check_json_returns_its_report_whatever_it_found(self):
        spec_id=self.spec['spec_id']
        for mode,state,ranks in (('missing-pull','missing-on-rank',['ok','missing','ok']),
                                 ('lost-and-missing','rank-unreachable',['ok','unreachable','missing'])):
            with self.subTest(mode=mode):
                result=subprocess.run([str(ROOT/'pulsar'),'image','check',spec_id,'--json'],
                                      env={**self.env,'DIAG_MODE':mode},cwd=ROOT,text=True,capture_output=True)
                # The check ran: its report is the result, and nothing is copied to stderr.
                self.assertEqual((result.returncode,result.stderr),(0,''))
                report=json.loads(result.stdout)['result']
                self.assertEqual((report['state'],[rank['state'] for rank in report['ranks']]),(state,ranks))
                # People and shell chains keep the check convention: 1 when the condition failed.
                human=subprocess.run([str(ROOT/'pulsar'),'image','check',spec_id],
                                     env={**self.env,'DIAG_MODE':mode},cwd=ROOT,text=True,capture_output=True)
                self.assertEqual(human.returncode,1,human.stderr)
        # A check that could not run printed no report: 3 in both modes, check_incomplete under --json.
        result=subprocess.run([str(ROOT/'pulsar'),'image','check','f'*64,'--json'],
                              env=self.env,cwd=ROOT,text=True,capture_output=True)
        self.assertEqual((result.returncode,json.loads(result.stdout)['error']['code']),(3,'check_incomplete'))
        human=subprocess.run([str(ROOT/'pulsar'),'image','check','f'*64],env=self.env,cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(human.returncode,3,human.stderr)

    def test_image_usage_mistakes_are_usage_errors(self):
        spec_id=self.spec['spec_id']
        for args in (['image','stage'],['image','stage','--plan'],['image','stage',spec_id,'--plan','--yes'],
                     ['image','stage',spec_id,'--node'],['image','stage',spec_id,'--bogus'],
                     ['image','check'],['image','check',spec_id,'--bogus']):
            with self.subTest(args=args):
                result=subprocess.run([str(ROOT/'pulsar'),*args,'--json'],env=self.env,cwd=ROOT,text=True,capture_output=True)
                self.assertEqual((result.returncode,json.loads(result.stdout)['error']['code']),(2,'usage_error'))
                human=subprocess.run([str(ROOT/'pulsar'),*args],env=self.env,cwd=ROOT,text=True,capture_output=True)
                self.assertEqual(human.returncode,2,human.stderr)
        self.assertFalse((self.root/'mutations').exists())

    def test_named_export_forbids_explicit_registry_fallback(self):
        tag = self.spec['source']['image_repository']+':staging-test'
        for flag in ('--pull', '--pull-if-stream-incomplete'):
            result = self.run_tool('sync-image.sh', [self.spec['spec_id'], '--export-tag', tag, flag, '--yes'])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root/'mutations').exists())

    def test_named_export_rejects_mismatch_collision_and_unknown_before_transfer(self):
        tag=self.spec['source']['image_repository']+':staging-test'
        for mode in ('export-tag-mismatch','export-tag-collision','export-tag-unobservable'):
            result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--export-tag',tag,'--yes'],mode)
            with self.subTest(mode=mode):
                self.assertNotEqual(result.returncode,0)
                self.assertFalse((self.root/'mutations').exists())
        for tag in ('another/repository:tag',tag.split(':')[0],tag+'@sha256:'+'a'*64):
            result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--export-tag',tag,'--yes'])
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((self.root/'mutations').exists())

    def test_named_export_source_drift_stops_before_next_worker(self):
        tag=self.spec['source']['image_repository']+':staging-test'
        result=self.run_tool('sync-image.sh',[self.spec['spec_id'],'--export-tag',tag,'--yes'],'export-tag-drift')
        self.assertNotEqual(result.returncode,0)
        self.assertCountEqual((self.root/'mutations').read_text().splitlines(),['save 0','load 1'])

    def test_public_image_check_help_and_already_present_json(self):
        for args in (['image','check',self.spec['spec_id'],'--json'],
                     ['image','stage',self.spec['spec_id'],'--plan','--json'],
                     ['image','stage',self.spec['spec_id'],'--yes','--json']):
            result=subprocess.run([str(ROOT/'pulsar'),*args],env=self.env,cwd=ROOT,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['result']['state'],'ok')
        for action in ('check','stage'):
            result=subprocess.run([str(ROOT/'pulsar'),'image',action,'--help'],env=self.env,cwd=ROOT,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('Usage:',result.stdout)
        self.assertFalse((self.root/'mutations').exists())

    def raw_inventory(self):
        names=['head','worker','rank-2']
        nodes={name:dict(node_id=f'node-{i}',confirmed=True,probe_status='ok',mem_available_gib=120,mem_total_gib=128,mem_status='ok',topology_index=i) for i,name in enumerate(names)}
        containers=[]
        for i,c in enumerate(self.containers):
            containers.append(dict(node=names[i],id=c['Id'],name='vllm-cluster-'+self.spec['spec_id'],image=c['Config']['Image'],labels=c['Config']['Labels'],cmd=c['Config']['Cmd'],running=True,status='running',pids=[]))
        return dict(profiles={},containers=containers,nodes=nodes,topology_id=self.plan['topology_id'],worker_status='ok',gpu_processes=[])

    def inventory(self,raw):
        path=self.root/'inventory-raw.json';path.write_text(json.dumps(raw))
        return self.run_tool('inventory.sh',['--from-fixture',str(path),'--json'])

    def test_uncatalogued_candidate_is_owned_without_claiming_recipe_verification(self):
        result=self.inventory(self.raw_inventory());self.assertEqual(result.returncode,0,result.stderr)
        service=json.loads(result.stdout)['services'][0]
        self.assertTrue(service['safe_to_stop']);self.assertTrue(service['complete'])
        self.assertFalse(service['recipe_verified'])
        self.assertNotIn('model_seal_id',service)
        # Each rank reports the port its container runs with, apart from today's configuration.
        self.assertEqual({rank['observed_api_port'] for rank in service['ranks']},{8000})

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
