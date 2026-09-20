"""Integrated source/CPU proof of the development diagnostic lifecycle."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.support.topology_fixture import Fixture
from tests.support import diagnostic_fixture as fixture
from release_spec.diagnostic import PROFILE, freeze_plan, complete_stopped_state, emit_entrypoint, parse_kmsg_record
from release_spec.diagnostic_state import observation, reduce_observation, snapshot_problem, final_result, lifecycle
from scripts import diagnostic_runtime as runtime
from scripts import diagnostic_run as data


class Integrated(unittest.TestCase):
    def setUp(self):
        # Adopt/reap the test-owned daemon/workload doubles after client exit.
        self.assertEqual(ctypes.CDLL(None).prctl(36,1,0,0,0),0)
        self.tmp=tempfile.TemporaryDirectory(prefix='pulsar-diag-test-')
        self.root=Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.close_owned)
        self.children=[]
        self.forced=[]
        # Exercise the proposed public dispatch without reopening the production
        # quarantine. This fixture copy changes only REPO_DIR and one route.
        dispatcher=(ROOT/'pulsar').read_text()
        declaration='REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)'
        self.assertEqual(dispatcher.count(declaration),1)
        dispatcher=dispatcher.replace(declaration,'REPO_DIR='+shlex.quote(str(ROOT)))
        if '  diagnostic) exec ' not in dispatcher:
            anchor='  ""|gum) exec '
            self.assertEqual(dispatcher.count(anchor),1)
            dispatcher=dispatcher.replace(anchor,'  diagnostic) exec "$REPO_DIR/scripts/diagnostic.sh" "$@" ;;\n'+anchor)
        self.public_cli=self.root/'pulsar-development'
        self.public_cli.write_text(dispatcher);self.public_cli.chmod(0o755)
        (self.root/'scenario.json').write_text('{}')
        (self.root/'boot').write_text(fixture.BOOT)
        (self.root/'meminfo').write_text('MemAvailable: 41943040 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        (self.root/'kernel-queue.jsonl').write_text('')
        inputs=self.root/'inputs';inputs.mkdir()
        script=inputs/'step.py';script.write_text('import time\ntime.sleep(0.8)\nprint("synthetic result")\n');script.chmod(0o644)
        self.definition={'schema_version':1,'kind':'pulsar-diagnostic-definition','image_reference':fixture.IMAGE,
            'image_id':fixture.IMAGE,'image_config_digest':fixture.IMAGE,'platform':'linux/arm64',
            'inputs':[{'name':'step.py','bytes':script.stat().st_size,'mode':0o644,'sha256':hashlib.sha256(script.read_bytes()).hexdigest()}],
            'steps':[{'argv':[sys.executable,'-I','-B','/pulsar-check/step.py'],'timeout_seconds':5}],
            'environment':{'PYTHONDONTWRITEBYTECODE':'1'},'working_directory':'/',
            'observer':{k:PROFILE[k] for k in ('sample_interval_seconds','max_observation_age_seconds','mem_available_floor_bytes',
                'swap_growth_limit_bytes','workload_deadline_seconds','operation_seconds','cleanup_reserve_seconds')}}
        self.definition['observer']['backend']='kmsg'
        (self.root/'definition.json').write_text(json.dumps(self.definition))
        proc=self.root/'proc/4242';proc.mkdir(parents=True)
        (proc/'stat').write_text('4242 (synthetic workload) '+' '.join(['S']+['0']*18+['999'])+'\n')
        (proc/'cgroup').write_text('0::/synthetic\n')
        group=self.root/'cgroup/synthetic';group.mkdir(parents=True)
        for k,v in {'memory.current':'1000','memory.peak':'2000','memory.swap.current':'0','memory.events':'oom 0\noom_kill 0\n'}.items():(group/k).write_text(v)
        topo=self.root/'topo';topo.mkdir()
        self.topology=Fixture(topo,nodes=1)
        binary=topo/'bin'
        (binary/'python3').write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ROOT)!r})\nfrom tests.support.diagnostic_fixture import shim_main\nraise SystemExit(shim_main())\n')
        (binary/'docker').write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ROOT)!r})\nfrom tests.support.diagnostic_fixture import docker_main\nraise SystemExit(docker_main())\n')
        for name,body in {'uname':'echo aarch64','nvidia-smi':'echo NVIDIA GB10','ss':'exit 0','curl':'exit 99','journalctl':'exit 99','lsof':'exit 99'}.items():
            (binary/name).write_text('#!/bin/sh\n'+body+'\n');(binary/name).chmod(0o755)
        self.env=dict(self.topology.env,DIAG_FIXTURE=str(self.root),PULSAR_MEMINFO_FILE=str(self.root/'meminfo'),
                      PULSAR_NVIDIA_SMI=str(binary/'nvidia-smi'),PULSAR_HOME_ROOT=str(self.root),PULSAR_HOT_ROOT=str(self.root),
                      PULSAR_COLD_ROOT='',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        self.attempt=self.root/'attempt'

    def configure(self,**kwargs):
        values=json.loads((self.root/'scenario.json').read_text());values.update(kwargs)
        fixture.save(self.root/'scenario.json',values)

    def close_owned(self):
        leaked=[]
        for child in self.children:
            if child.poll() is None:
                leaked.append(child.pid)
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=2)
        self.reap_adopted()
        records=self.root/'owned.jsonl'
        rows=[json.loads(s) for s in records.read_text().splitlines()] if records.exists() else []
        # Snapshot all descendants BEFORE forced cleanup, while parentage still
        # binds them to these test-owned processes. Never signal an unbound PID.
        seen={row['pid'] for row in rows}
        queue=list(rows)
        for row in queue:
            if row['start'] is None or fixture.start_ticks(row['pid'])!=row['start']:continue
            path=Path('/proc',str(row['pid']),'task',str(row['pid']),'children')
            try:children=[int(x) for x in path.read_text().split()]
            except OSError:children=[]
            for pid in children:
                if pid not in seen:
                    item={'pid':pid,'start':fixture.start_ticks(pid),'role':'owned-descendant'}
                    rows.append(item);queue.append(item);seen.add(pid)
        # Bounded teardown of exact test-owned identities, including adopted children.
        until=time.monotonic()+2
        alive=[]
        while True:
            alive=[]
            for row in rows:
                try:os.waitpid(row['pid'],os.WNOHANG)
                except ChildProcessError:pass
                if row['start'] is not None and fixture.start_ticks(row['pid'])==row['start']:
                    alive.append(row)
            if not alive or time.monotonic()>until:break
            time.sleep(.02)
        for row in reversed(alive):
            leaked.append(row['pid'])
            if fixture.start_ticks(row['pid'])==row['start']:
                os.kill(row['pid'],signal.SIGKILL)
                try:os.waitpid(row['pid'],0)
                except ChildProcessError:pass
        for row in alive:
            try:os.waitpid(row['pid'],0)
            except ChildProcessError:pass
        receipt=os.environ.get('DIAG_TEST_RECEIPTS')
        if receipt:
            with open(receipt,'a') as out:
                out.write(json.dumps({'test':self.id(),'owned':rows,'forced':leaked,'remaining':[r for r in rows if r['start'] is not None and fixture.start_ticks(r['pid'])==r['start']]})+'\n')
        if leaked:self.fail('owned test children leaked before teardown: '+repr(leaked))

    def command(self,*args,timeout=16):
        child=subprocess.Popen([str(self.public_cli),*args],cwd=self.root,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        self.children.append(child)
        stdout,stderr=self.communicate_owned(child,timeout)
        return subprocess.CompletedProcess(args,child.returncode,stdout,stderr)

    def reap_adopted(self):
        records=self.root/'owned.jsonl'
        roots={c.pid for c in self.children}
        existing=[json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
        seen={row['pid'] for row in existing}
        # Subreaper adoption is itself an exact parentage binding. Include
        # helper grandchildren such as Bash sleep, not only registered doubles.
        path=Path('/proc/self/task',str(os.getpid()),'children')
        for pid in (int(value) for value in path.read_text().split()):
            if pid in roots or pid in seen:continue
            with records.open('a') as output:
                output.write(json.dumps({'pid':pid,'start':fixture.start_ticks(pid),'role':'adopted-test-descendant'})+'\n')
        if not records.exists():return
        for line in records.read_text().splitlines():
            row=json.loads(line)
            if row['pid'] in roots or fixture.start_ticks(row['pid'])!=row['start']:continue
            try:os.waitpid(row['pid'],os.WNOHANG)
            except ChildProcessError:pass

    def communicate_owned(self,child,timeout):
        until=time.monotonic()+timeout
        while True:
            self.reap_adopted()
            try:return child.communicate(timeout=min(.025,max(.001,until-time.monotonic())))
            except subprocess.TimeoutExpired:
                if time.monotonic()>=until:raise

    def plan(self):
        reply=self.command('diagnostic','plan','--definition',str(self.root/'definition.json'),'--inputs',str(self.root/'inputs'),
                           '--node','fixture-node-0','--out',str(self.root/'plan.json'),'--json')
        self.assertEqual(reply.returncode,0,reply.stdout+reply.stderr)
        return json.loads(reply.stdout)['result']

    def run_attempt(self):
        self.plan()
        reply=self.command('diagnostic','run','--plan',str(self.root/'plan.json'),'--attempt-dir',str(self.attempt),'--yes','--json')
        self.assertTrue(reply.stdout.strip(),reply.stderr)
        result=json.loads(reply.stdout.splitlines()[-1]).get('result')
        self.assertIsNotNone(result,reply.stdout+reply.stderr)
        if result['outcome']!='succeeded':
            result['_debug_stderr']=reply.stderr
        return result

    def calls(self):
        path=self.root/'docker-calls.jsonl'
        return [json.loads(s)['argv'] for s in path.read_text().splitlines()] if path.exists() else []

    def test_normal_complete_public_cli(self):
        result=self.run_attempt()
        debug={p.name:p.read_text()[:3000] for p in self.attempt.glob('*.stderr') if p.stat().st_size}
        debug.update({p.name:p.read_text()[:4000] for p in (self.root/'workload.log',self.root/'workload-supervisor.stderr',self.root/'entrypoint.sh') if p.exists()})
        self.assertEqual(result['outcome'],'succeeded',repr(result)+repr(debug))
        self.assertTrue(result['coverage']['complete'])
        self.assertTrue(result['observer_closure']['waited'])
        self.assertEqual(result['workload']['steps'][0]['exit_code'],0)
        self.assertEqual(sum(c[0]=='start' for c in self.calls()),1)
        self.assertFalse((self.root/'docker.json').exists())
        self.assertGreater(result['observation']['drain_before_ns'],result['cleanup']['completed_monotonic_ns'])
        shown=self.command('diagnostic','show','--attempt-dir',str(self.attempt),'--json')
        self.assertEqual(json.loads(shown.stdout)['result']['outcome'],'succeeded')

    def test_changed_caller_cannot_break_owned_lifecycle(self):
        self.configure(mutate_caller=True)
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'succeeded',result)

    def test_identity_rejection_bars_stop_and_remove(self):
        self.configure(inspect_patch={'Config.Labels.io.pulsar.diagnostic.attempt-nonce':'foreign'})
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'cleanup_unconfirmed',result)
        self.assertFalse(any(c[0] in ('start','stop','rm') for c in self.calls()),self.calls())

    def test_failed_remove_never_becomes_clean(self):
        self.configure(rm_failure=True,kernel_on_remove=['6,1,1,-;NVRM: NV_ERR_NO_MEMORY\n'])
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'cleanup_unconfirmed',result)
        self.assertTrue(result['cleanup']['absent'])
        self.assertEqual(result['cleanup']['rm_rc'],1)

    def test_create_accepted_reply_lost_is_reconciled_once(self):
        self.configure(create_reply=1)
        result=self.run_attempt()
        self.assertEqual(sum(c[0]=='create' for c in self.calls()),1)
        self.assertFalse(any(c[0]=='start' for c in self.calls()))
        self.assertTrue(result['cleanup']['absent'],result)
        self.assertEqual(result['outcome'],'failed_clean',result)

    def test_denied_observer_bars_create(self):
        self.configure(deny_kmsg=True)
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'preflight_failed',result)
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()))

    def test_kernel_readiness_error_bars_create(self):
        fixture.kernel(self.root,['6,1,1,-;NVRM: NV_ERR_NO_MEMORY\n'])
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'preflight_failed',result)
        self.assertFalse(any(c[0]=='create' for c in self.calls()))

    def test_environment_drift_bars_start(self):
        self.configure(inspect_patch={'Config.Env':['BASH_ENV=/unapproved']})
        result=self.run_attempt()
        self.assertNotEqual(result['outcome'],'succeeded')
        self.assertFalse(any(c[0]=='start' for c in self.calls()))
        self.assertTrue(result['cleanup']['absent'],result)

    def launch(self):
        self.plan()
        child=subprocess.Popen([str(self.public_cli),'diagnostic','run','--plan',str(self.root/'plan.json'),
            '--attempt-dir',str(self.attempt),'--yes','--json'],cwd=self.root,env=self.env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        self.children.append(child)
        return child

    def wait_for(self,predicate,child,seconds=8):
        until=time.monotonic()+seconds
        while time.monotonic()<until and child.poll() is None:
            self.reap_adopted()
            if predicate():return
            time.sleep(.01)
        self.fail('test barrier not reached; '+repr(child.poll()))

    def finish(self,child):
        out,err=self.communicate_owned(child,12)
        if os.environ.get('DIAG_TEST_TRACE'):
            Path(os.environ['DIAG_TEST_TRACE']).write_text(err)
        self.assertTrue(out.strip(),err)
        return json.loads(out.splitlines()[-1])['result']

    def test_repeated_real_term_and_int_take_one_cleanup_path(self):
        if os.environ.get('DIAG_TEST_TRACE'):
            self.env['SHELLOPTS']='xtrace'
        script=self.root/'inputs/step.py'
        script.write_text('import time\ntime.sleep(10)\n')
        self.definition['inputs'][0].update(bytes=script.stat().st_size,sha256=hashlib.sha256(script.read_bytes()).hexdigest())
        self.definition['steps'][0]['timeout_seconds']=12
        (self.root/'definition.json').write_text(json.dumps(self.definition))
        child=self.launch()
        self.wait_for(lambda:(self.root/'workload-ready').exists() and
            (self.attempt/'observer-status.json').exists() and json.loads((self.attempt/'observer-status.json').read_text()).get('cgroup_samples',0)>0,child)
        for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGTERM,signal.SIGINT):
            if child.poll() is None:os.kill(child.pid,sig)
            time.sleep(.015)
        result=self.finish(child)
        self.assertNotEqual(result['outcome'],'succeeded',result)
        self.assertEqual(sum(c[0]=='start' for c in self.calls()),1)
        debug={p.name:p.read_text()[:1500] for p in self.attempt.glob('*') if p.is_file() and p.name in ('before-stop.stdout','identity.json','stopped-check.json','failure.json')}
        self.assertEqual(sum(c[0]=='stop' for c in self.calls()),1,repr(result)+repr(self.calls())+repr(debug))
        self.assertTrue(result['cleanup']['absent'],result)

    def test_concurrent_malformed_loser_cannot_touch_claim_or_observer(self):
        self.configure(hang='start')
        child=self.launch()
        self.wait_for(lambda:(self.attempt/'container-start.json').exists(),child)
        before=(self.attempt/'claim.json').read_bytes()
        reply=self.command('diagnostic','run','--plan',str(self.root/'missing-plan.json'),
            '--attempt-dir',str(self.attempt),'--yes','--json')
        self.assertNotEqual(reply.returncode,0)
        self.assertEqual((self.attempt/'claim.json').read_bytes(),before)
        result=self.finish(child)
        self.assertEqual(sum(c[0]=='create' for c in self.calls()),1)
        self.assertEqual(sum(c[0]=='start' for c in self.calls()),1)
        self.assertTrue(result['start_consumed'])

    def test_lost_start_reply_is_consumed_and_cleanup_is_owned(self):
        self.configure(start_reply=1)
        result=self.run_attempt()
        self.assertTrue(result['start_consumed'])
        self.assertEqual(sum(c[0]=='start' for c in self.calls()),1)
        self.assertTrue(result['cleanup']['absent'],result)
        self.assertNotEqual(result['outcome'],'succeeded')

    def test_helper_failure_enters_cleanup_without_start(self):
        self.configure(helper_failure='consume-start')
        result=self.run_attempt()
        self.assertFalse(any(c[0]=='start' for c in self.calls()))
        self.assertEqual(result['outcome'],'failed_clean',result)
        self.assertTrue(result['cleanup']['absent'])

    def test_sigkill_owner_only_allows_cleanup_without_recovered_window(self):
        child=self.launch()
        self.wait_for(lambda:(self.root/'started').exists(),child)
        os.kill(child.pid,signal.SIGKILL)
        self.communicate_owned(child,4)
        time.sleep(.3)
        original={p.name:p.read_bytes() for p in self.attempt.iterdir() if p.is_file() and not p.name.startswith('observer')}
        before=len(self.calls())
        reply=self.command('diagnostic','cleanup','--attempt-dir',str(self.attempt),'--yes','--json')
        result=json.loads(reply.stdout.splitlines()[-1])['result']
        self.assertTrue(result['cleanup_only'],repr(result)+reply.stderr)
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()[before:]))
        self.assertFalse((self.attempt/'result.json').exists())
        for name,content in original.items():
            self.assertEqual((self.attempt/name).read_bytes(),content,name)
        self.assertTrue((self.attempt/('cleanup-'+result['cleanup_operation_nonce'])/'absence.stdout').is_file())

    def test_missing_process_start_is_unknown_in_native_observer(self):
        (self.root/'proc/4242/stat').unlink()
        result=self.run_attempt()
        self.assertNotEqual(result['outcome'],'succeeded',result)
        self.assertIn('PID start',result['observation']['first_coverage_failure'])

    def test_missing_swap_is_unknown_in_native_observer(self):
        (self.root/'cgroup/synthetic/memory.swap.current').unlink()
        result=self.run_attempt()
        self.assertNotEqual(result['outcome'],'succeeded',result)
        self.assertIn('counters are unknown',result['observation']['first_coverage_failure'])

    def test_late_cleanup_record_and_sequence_gap_are_retained(self):
        self.configure(kernel_on_remove=['6,1,1,-;benign\n','6,3,2,-;NVRM: NV_ERR_NO_MEMORY\n'])
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'failed_clean',result)
        self.assertEqual(result['observation']['kernel_records'],2)
        self.assertIsNotNone(result['observation']['first_coverage_failure'])
        self.assertIsNotNone(result['observation']['first_safety_failure'])

    def test_terminal_eof_is_not_fresh_eagain(self):
        self.configure(kernel_on_remove=[{'status':'eof'}])
        result=self.run_attempt()
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('EOF',result['observation']['first_coverage_failure'])

    def test_epipe_is_irreversible_coverage_failure(self):
        self.configure(kernel_on_remove=[{'status':'epipe'}])
        result=self.run_attempt()
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('EPIPE',result['observation']['first_coverage_failure'])

    def test_flood_does_not_starve_memory_or_claim_readiness(self):
        self.configure(flood=True)
        result=self.run_attempt()
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()))
        self.assertGreaterEqual(result['observation']['samples'],5,result)
        self.assertFalse(result['coverage']['complete'])

    def test_failed_positive_absence_query_is_cleanup_unknown(self):
        self.configure(absence_failure=True)
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'cleanup_unconfirmed',result)
        self.assertFalse(result['cleanup']['absent'])

    def test_failed_stop_cannot_become_clean(self):
        self.configure(stop_failure=True,start_reply=1)
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'cleanup_unconfirmed',result)
        self.assertEqual(result['cleanup']['stop_rc'],1)

    def test_empty_stopped_state_bars_removal(self):
        self.configure(empty_stopped=True)
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'cleanup_unconfirmed',result)
        self.assertFalse(any(c[0]=='rm' for c in self.calls()))

    def set_payload(self,text,timeout=12):
        script=self.root/'inputs/step.py';script.write_text(text)
        self.definition['inputs'][0].update(bytes=script.stat().st_size,sha256=hashlib.sha256(script.read_bytes()).hexdigest())
        self.definition['steps'][0]['timeout_seconds']=timeout
        (self.root/'definition.json').write_text(json.dumps(self.definition))

    def bound_barrier(self,child):
        self.wait_for(lambda:(self.root/'workload-ready').exists() and (self.attempt/'observer-status.json').exists()
            and json.loads((self.attempt/'observer-status.json').read_text()).get('cgroup_samples',0)>0,child)

    def test_transient_floor_breach_remains_after_recovery(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        child=self.launch();self.bound_barrier(child)
        (self.root/'meminfo').write_text('MemAvailable: 1048576 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        time.sleep(.3)
        (self.root/'meminfo').write_text('MemAvailable: 41943040 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        result=self.finish(child)
        self.assertIsNotNone(result['safety']['first_failure'])
        self.assertEqual(result['observation']['mem_available_bytes'],40*1024**3)
        self.assertNotEqual(result['outcome'],'succeeded')

    def test_transient_swap_breach_remains_after_recovery(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        child=self.launch();self.bound_barrier(child)
        (self.root/'meminfo').write_text('MemAvailable: 41943040 kB\nSwapTotal: 1048576 kB\nSwapFree: 524288 kB\n')
        time.sleep(.3)
        (self.root/'meminfo').write_text('MemAvailable: 41943040 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        result=self.finish(child)
        self.assertIn('swap',result['safety']['first_failure'])
        self.assertNotEqual(result['outcome'],'succeeded')

    def test_counter_rollback_is_unknown(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        child=self.launch();self.bound_barrier(child)
        (self.root/'cgroup/synthetic/memory.peak').write_text('1000')
        result=self.finish(child)
        self.assertIn('regressed',result['observation']['first_coverage_failure'])

    def test_guard_reacts_while_start_client_hangs(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        self.configure(hang_after_start=True)
        child=self.launch()
        self.wait_for(lambda:(self.root/'workload-ready').exists(),child)
        begin=time.monotonic_ns()
        (self.root/'meminfo').write_text('MemAvailable: 1048576 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        result=self.finish(child)
        rows=[json.loads(s) for s in (self.root/'docker-calls.jsonl').read_text().splitlines()]
        stop=next(r for r in rows if r['argv'][0]=='stop')
        self.assertLess(stop['ns']-begin,10**9,repr(result))
        self.assertTrue(result['start_consumed'])
        self.assertNotEqual(result['outcome'],'succeeded')

    def test_unreachable_second_configured_rank_bars_create(self):
        topo=self.root/'two-rank';topo.mkdir()
        configured=Fixture(topo,nodes=2)
        for key,value in configured.env.items():
            if key.startswith('CLUSTER_') or key in ('PULSAR_SSH','TOPOLOGY_FIXTURE'):self.env[key]=value
        self.env['TOPOLOGY_UNREACHABLE']='1'
        result=self.run_attempt()
        self.assertEqual(result['outcome'],'preflight_failed',result)
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()))

    def test_first_step_failure_preserves_exit_and_bars_second(self):
        self.definition['steps']=[{'argv':[sys.executable,'-I','-B','-c','raise SystemExit(7)'],'timeout_seconds':2},
            {'argv':[sys.executable,'-I','-B','-c','print("must not run")'],'timeout_seconds':2}]
        (self.root/'definition.json').write_text(json.dumps(self.definition))
        result=self.run_attempt()
        self.assertEqual([r['exit_code'] for r in result['workload']['steps']],[7],result)
        self.assertEqual(result['workload_exit_code'],7)

    def test_per_step_timeout_is_enforced(self):
        self.set_payload('import time\ntime.sleep(2)\n',timeout=1)
        result=self.run_attempt()
        self.assertEqual(result['workload_exit_code'],124,result)
        self.assertEqual(result['workload']['steps'][0]['exit_code'],124)

    def test_sealed_mutation_bars_start_but_not_cleanup(self):
        self.configure(mutate_sealed=True)
        result=self.run_attempt()
        self.assertFalse(any(c[0]=='start' for c in self.calls()))
        self.assertTrue(result['cleanup']['absent'],result)

    def test_malformed_whole_record_is_preserved_and_bars_create(self):
        fixture.kernel(self.root,['not a valid kernel record\n'])
        result=self.run_attempt()
        self.assertFalse(any(c[0]=='create' for c in self.calls()))
        self.assertFalse(result['coverage']['complete'])
        raw=json.loads((self.attempt/'invalid-kernel-record.json').read_text())
        self.assertEqual(raw['bytes'],len('not a valid kernel record\n'))
        self.assertFalse(raw['truncated'])

    def test_fragment_ambiguity_cannot_pass(self):
        fixture.kernel(self.root,['6,1,1,c;fragment\n','6,2,2,-;tail\n'])
        result=self.run_attempt()
        self.assertFalse(any(c[0]=='create' for c in self.calls()))
        self.assertIn('continuation',result['observation']['first_coverage_failure'])

    def test_boot_drift_in_native_producer_cannot_pass(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        child=self.launch();self.bound_barrier(child)
        (self.root/'boot').write_text('b'*32)
        result=self.finish(child)
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('boot',result['observation']['first_coverage_failure'])
        self.assertTrue(result['cleanup']['absent'])

    def test_dead_native_producer_is_not_a_fresh_snapshot(self):
        self.configure(observer_death_after_start=True)
        result=self.run_attempt()
        self.assertNotEqual(result['outcome'],'succeeded')
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(result['observer_closure']['waited'])
        self.assertEqual(result['observer_closure']['exit_code'],9)
        self.assertTrue(result['cleanup']['absent'])

    def test_terminal_flood_is_bounded_with_memory_samples(self):
        self.configure(flood=True,flood_after_remove=True)
        result=self.run_attempt()
        self.assertNotEqual(result['outcome'],'succeeded')
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(result['observer_closure']['waited'])
        self.assertIn(result['observation']['first_coverage_failure'],
            ('terminal drain exceeded bound','observer raw capture exhausted'))
        elapsed=result['observation']['published_monotonic_ns']-result['observation']['drain_request_ns']
        self.assertGreaterEqual(elapsed,2*10**9)
        self.assertLess(elapsed,3*10**9)
        self.assertLess(result['observation']['published_monotonic_ns']-result['observation']['last_sample_monotonic_ns'],350000000)
        self.assertTrue(result['cleanup']['absent'])

    def test_actual_output_limit_stops_later_steps(self):
        self.set_payload('import os\ntry: os.write(1,b"x"*300000)\nexcept OSError: pass\n')
        self.definition['steps'].append({'argv':[sys.executable,'-I','-B','-c','print("must not run")'],'timeout_seconds':2})
        (self.root/'definition.json').write_text(json.dumps(self.definition))
        result=self.run_attempt()
        self.assertNotEqual(result['outcome'],'succeeded',result)
        self.assertEqual(len(result['workload']['steps']),1,result)
        self.assertEqual(result['workload']['steps'][0]['stdout_bytes'],262144)
        self.assertEqual(result['workload']['steps'][0]['exit_code'],0)
        self.assertFalse(result['workload']['steps'][0]['capture_complete'])
        self.assertEqual(result['workload_exit_code'],125)
        self.assertLess(sum(p.stat().st_size for p in self.attempt.rglob('*') if p.is_file()),64*1024**2)

    def test_two_preclaim_contenders_have_one_winner(self):
        self.plan()
        contenders=[]
        for unused in range(2):
            child=subprocess.Popen([str(self.public_cli),'diagnostic','run','--plan',str(self.root/'plan.json'),
                '--attempt-dir',str(self.attempt),'--yes','--json'],cwd=self.root,env=self.env,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            self.children.append(child);contenders.append(child)
        replies=[]
        for child in contenders:
            stdout,stderr=self.communicate_owned(child,16)
            replies.append(json.loads(stdout.splitlines()[-1]))
        self.assertEqual(sum(r.get('result',{}).get('outcome')=='succeeded' for r in replies),1,replies)
        self.assertEqual(sum(r.get('error',{}).get('code')=='already_claimed' for r in replies),1,replies)
        self.assertEqual(sum(c[0]=='create' for c in self.calls()),1)
        self.assertEqual(sum(c[0]=='start' for c in self.calls()),1)

    def test_real_signal_after_create_acceptance_reconciles_once(self):
        self.configure(hang_after_create=True)
        child=self.launch();self.wait_for(lambda:(self.root/'created').exists(),child)
        os.kill(child.pid,signal.SIGINT)
        result=self.finish(child)
        self.assertEqual(sum(c[0]=='create' for c in self.calls()),1)
        self.assertFalse(any(c[0] in ('start','stop') for c in self.calls()))
        self.assertTrue(result['cleanup']['absent'],result)
        self.assertNotEqual(result['outcome'],'succeeded')

    def test_real_signal_after_start_acceptance_never_retries(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        self.configure(hang_after_start=True)
        child=self.launch();self.wait_for(lambda:(self.root/'workload-ready').exists(),child)
        os.kill(child.pid,signal.SIGTERM)
        result=self.finish(child)
        self.assertEqual(sum(c[0]=='start' for c in self.calls()),1)
        self.assertEqual(sum(c[0]=='stop' for c in self.calls()),1)
        self.assertTrue(result['cleanup']['absent'],result)
        self.assertTrue(result['start_consumed'])
        self.assertFalse(result['coverage']['complete'])

    def test_many_post_tail_records_include_the_last_error(self):
        fixture.kernel(self.root,[f'6,{i},{i},-;benign\n' for i in range(1,513)]+['6,513,513,-;NVRM: Xid 13\n'])
        result=self.run_attempt()
        self.assertFalse(any(c[0]=='create' for c in self.calls()))
        self.assertEqual(result['observation']['kernel_records'],513)
        self.assertEqual(result['observation']['first_kernel_fault']['sequence'],513)

    def test_same_pid_with_changed_start_is_unknown(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        child=self.launch();self.bound_barrier(child)
        path=self.root/'proc/4242/stat';path.write_text(path.read_text().replace('999','1000'))
        result=self.finish(child)
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('PID/start/cgroup changed',result['observation']['first_coverage_failure'])

    def test_client_descendant_cannot_outlive_clean_publication(self):
        self.configure(client_orphan_after='create')
        result=self.run_attempt()
        rows=[json.loads(row) for row in (self.root/'owned.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row['role']=='client-orphan' for row in rows),1,result)
        remaining=[row for row in rows if row['role']=='client-orphan' and fixture.start_ticks(row['pid'])==row['start']]
        self.assertFalse(remaining,repr(result)+repr(remaining))

    def test_ambiguous_create_without_bound_container_is_not_clean(self):
        self.configure(hang='create')
        result=self.run_attempt()
        self.assertEqual(sum(c[0]=='create' for c in self.calls()),1)
        self.assertTrue(result['cleanup']['absent'])
        self.assertEqual(result['outcome'],'cleanup_unconfirmed',result)

    def test_same_cgroup_path_with_replaced_inode_is_unknown(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        child=self.launch();self.bound_barrier(child)
        group=self.root/'cgroup/synthetic';group.rename(group.with_name('old'))
        group.mkdir()
        for path in group.with_name('old').iterdir():(group/path.name).write_bytes(path.read_bytes())
        result=self.finish(child)
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('directory was replaced',result['observation']['first_coverage_failure'])

    def test_initial_floor_breach_bars_create_and_start(self):
        (self.root/'meminfo').write_text('MemAvailable: 1048576 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        result=self.run_attempt()
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()))
        self.assertEqual(result['outcome'],'preflight_failed')
        self.assertFalse(result['create_consumed'])
        self.assertFalse(result['start_consumed'])
        self.assertFalse((self.attempt/'observer-started.json').exists())
        self.assertIn('fresh all-rank readiness/trust/doctor/idle failed',result['failures'])

    def test_floor_falls_after_admission_before_native_readiness(self):
        self.configure(floor_after_admission=True)
        result=self.run_attempt()
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()))
        self.assertEqual(result['outcome'],'preflight_failed')
        self.assertIn('floor',result['safety']['first_failure'])

    def test_workload_deadline_includes_start_dispatch(self):
        self.set_payload('import time\ntime.sleep(10)\n')
        self.definition['observer']['workload_deadline_seconds']=1
        (self.root/'definition.json').write_text(json.dumps(self.definition))
        result=self.run_attempt()
        self.assertTrue(result['start_consumed'])
        self.assertIn('workload deadline including start reached',result['failures'])
        start=json.loads((self.attempt/'container-start.json').read_text())['monotonic_ns']
        calls=[json.loads(s) for s in (self.root/'docker-calls.jsonl').read_text().splitlines()]
        stop=next(row for row in calls if row['argv'][0]=='stop')
        self.assertLess(stop['ns']-start,2500000000)

    def test_cleanup_reserve_bars_late_admission(self):
        self.definition['observer'].update(workload_deadline_seconds=1,operation_seconds=301)
        (self.root/'definition.json').write_text(json.dumps(self.definition))
        result=self.run_attempt()
        self.assertFalse(any(c[0] in ('create','start') for c in self.calls()))
        self.assertEqual(result['outcome'],'preflight_failed')
        self.assertIn('cleanup reserve',json.loads((self.attempt/'ready.json').read_text())['result']['reason'])


class Decisions(unittest.TestCase):
    def test_stopped_state_requires_real_phase_and_timestamps(self):
        for state in ({},{'Status':'exited'},{'Status':'exited','Running':False,'Restarting':False,'Pid':0,'ExitCode':0,'OOMKilled':False,'Error':'','StartedAt':'t','FinishedAt':'t'}):
            self.assertIsNotNone(complete_stopped_state(state))

    def test_whole_record_parser_rejects_combined_records(self):
        with self.assertRaises(Exception):parse_kmsg_record(b'6,1,1,-;one\n6,2,2,-;two\n')


if __name__=='__main__':unittest.main()
