"""CPU-only guard failure, identity and orchestration acceptance tests."""

import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from release_spec import serving
from release_spec.normalize import canonical_json_digest
from scripts import container_runtime as runtime
from serving_guard import controller, node, program
from serving_guard import runtime as guard_runtime
from tests.test_container_runtime import fixture

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


def guarded_fixture(max_host_swap_growth_bytes=None):
    spec, facts, prepared, _, containers, images = fixture(3)
    container = copy.deepcopy(spec['recipe']['container'])
    container['memory_limit_bytes'] = 16 * GIB
    container['guard'] = program.template(['engine'], minimum=8 * GIB, startup=10, timeout=20,
                                         max_host_swap_growth_bytes=max_host_swap_growth_bytes)
    spec = serving.apply_overrides(spec, {'container': container})
    prepared['spec_id'] = spec['spec_id']
    plan = runtime.build_plan(spec, spec['spec_id'], facts, prepared)
    ref = spec['source']['image_repository'] + '@' + spec['recipe']['image_digest']
    for rank, info in enumerate(containers):
        argv = runtime.docker_argv(plan, rank, include_secrets=False)
        info['Config'].update(Labels=runtime.rank_spec(plan, rank)['labels'],
                              Entrypoint=['python3'], Cmd=argv[argv.index(ref)+1:], OpenStdin=True)
        info['HostConfig'].update(Memory=16*GIB, MemorySwap=16*GIB, PidsLimit=512,
                                  CgroupnsMode='private', AutoRemove=True)
        info['Mounts'].append({'Type':'bind','Source':'/proc/meminfo', 'Destination':'/pulsar-guard-host-meminfo', 'RW':False})
    return spec, facts, prepared, plan, containers, images


class GuardContracts(unittest.TestCase):
    def test_schema_two_allowance_changes_identity_and_is_observed(self):
        spec, _, _, plan, containers, images = guarded_fixture(256 * 1024**2)
        self.assertEqual(spec['recipe']['container']['guard']['schema_version'], 2)
        for rank in range(3):
            self.assertTrue(runtime.observe_rank(plan, rank, containers[rank], images[rank])['running'])
            command = containers[rank]['Config']['Cmd']
            index = command.index('-c') + 2
            context = json.loads(command[index])
            self.assertEqual(context['limits']['max_host_swap_growth_bytes'], 256 * 1024**2)
            changed = copy.deepcopy(containers[rank])
            context['limits']['max_host_swap_growth_bytes'] = 128 * 1024**2
            changed['Config']['Cmd'][index] = json.dumps(context, sort_keys=True)
            with self.assertRaises(ValueError):
                runtime.observe_rank(plan, rank, changed, images[rank])
        container = copy.deepcopy(spec['recipe']['container'])
        container['guard']['max_host_swap_growth_bytes'] = 128 * 1024**2
        self.assertNotEqual(serving.apply_overrides(spec, {'container': container})['spec_id'], spec['spec_id'])

    def test_guard_versions_are_closed_and_allowance_is_bounded(self):
        from release_spec.serving_guard import validate
        spec, *_ = guarded_fixture()
        container = spec['recipe']['container']
        original = copy.deepcopy(container['guard'])
        self.assertEqual(validate(original, container), original)
        self.assertEqual(serving.verify_spec(spec), spec)
        self.assertNotIn('max_host_swap_growth_bytes', original)
        for value in (0, 64 * 1024**2, 256 * 1024**2):
            guard = {**original, 'schema_version': 2, 'max_host_swap_growth_bytes': value}
            self.assertEqual(validate(guard, container), guard)
        for value in (False, True, None, -1, 1.5, '64', 256 * 1024**2 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({**original, 'schema_version': 2, 'max_host_swap_growth_bytes': value}, container)
        for guard in ({**original, 'schema_version': 2},
                      {**original, 'max_host_swap_growth_bytes': 0},
                      {**original, 'schema_version': 3}):
            with self.assertRaises(ValueError):
                validate(guard, container)

    def test_public_template_default_and_explicit_zero_or_budget(self):
        from scripts.public_cli import dispatch
        from scripts.integration_contract import contract
        default = dispatch('guarded', ['template', '--entrypoint-json', '["engine"]'])
        self.assertEqual(default['schema_version'], 1)
        self.assertNotIn('max_host_swap_growth_bytes', default)
        for value in (0, 256 * 1024**2):
            result = dispatch('guarded', ['template', '--entrypoint-json', '["engine"]',
                '--max-host-swap-growth-bytes', str(value)])
            self.assertEqual(result['schema_version'], 2)
            self.assertEqual(result['max_host_swap_growth_bytes'], value)
        for value in (-1, 256 * 1024**2 + 1):
            with self.assertRaises(ValueError):
                dispatch('guarded', ['template', '--entrypoint-json', '["engine"]',
                    '--max-host-swap-growth-bytes', str(value)])
        self.assertEqual(contract()['serving_guard_schema_versions'], [1, 2])

    def test_historical_guard_bytes_remain_observable_but_not_current_launchable(self):
        with patch.object(program, 'program', return_value="print('retained synthetic guard')\n"):
            spec, _, _, plan, containers, images = guarded_fixture()
        self.assertEqual(runtime.validate_plan(plan), plan)
        runtime.observe_rank(plan, 0, containers[0], images[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'old-spec.json'
            path.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, 'differs'):
                controller.initialize(path, spec['spec_id'], root / 'run')
            self.assertFalse((root / 'run').exists())

    def test_public_run_help_is_available_without_physical_prerequisites(self):
        for flag in ('--help','-h'):
            result=subprocess.run([str(ROOT/'pulsar'),'guarded','run',flag],cwd=ROOT,
                env={**os.environ,'PULSAR_DOCKER':'/unavailable-fixture-docker'},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--spec-id',result.stdout)
            self.assertIn('No pulls',result.stdout)

    def test_old_recipe_and_launch_behavior_unchanged(self):
        spec, _, _, plan, _, _ = fixture(3)
        self.assertEqual(serving.verify_spec(spec), spec)
        self.assertNotIn('guard', spec['recipe']['container'])
        self.assertEqual(plan['schema_version'], 3)
        self.assertIn('-d', runtime.docker_argv(plan, 1))

    def test_guard_policy_changes_effective_spec_and_binds_no_swap(self):
        spec, _, _, plan, containers, images = guarded_fixture()
        self.assertEqual(plan['schema_version'], 6)
        for rank in range(3):
            observed = runtime.observe_rank(plan, rank, containers[rank], images[rank])
            self.assertTrue(observed['running'])
            args = runtime.docker_argv(plan, rank)
            self.assertNotIn('-d', args)
            self.assertEqual(args[args.index('--memory-swap')+1], str(16*GIB))
        altered = copy.deepcopy(spec['recipe']['container'])
        altered['guard']['timeout_seconds'] += 1
        self.assertNotEqual(serving.apply_overrides(spec, {'container': altered})['spec_id'], spec['spec_id'])

    def test_incomplete_invalid_and_unbounded_policy_rejected(self):
        spec, *_ = guarded_fixture()
        for change in ({'guard':None}, {'memory_limit_bytes':0}, {'restart_policy':'always'},
                       {'guard':{**spec['recipe']['container']['guard'],'timeout_seconds':False}},
                       {'guard':{**spec['recipe']['container']['guard'],'startup_timeout_seconds':21}}):
            c = {**spec['recipe']['container'], **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                serving.apply_overrides(spec, {'container': c})

    def test_guard_program_tamper_rejected_even_with_rehashed_plan(self):
        _, _, _, plan, *_ = guarded_fixture()
        plan['guard_program'] += '\npass\n'
        plan['plan_id'] = canonical_json_digest({k:v for k,v in plan.items() if k!='plan_id'})
        with self.assertRaisesRegex(ValueError, 'guard program'):
            runtime.validate_plan(plan)

    def test_observation_uses_retained_program_not_current_checkout(self):
        _, _, _, plan, containers, images = guarded_fixture()
        with patch.object(program, 'program', side_effect=AssertionError('must use retained code')):
            runtime.validate_plan(plan)
            runtime.observe_rank(plan, 0, containers[0], images[0])

    def test_observer_rejects_broken_guard_and_preserves_auth_redaction(self):
        _, _, _, plan, containers, images = guarded_fixture()
        for mutate in (lambda c:c['HostConfig'].update(MemorySwap=32*GIB),
                       lambda c:c['HostConfig'].update(AutoRemove=False),
                       lambda c:c['HostConfig'].update(CgroupnsMode='host'),
                       lambda c:c['Mounts'][-1].update(RW=True),
                       lambda c:c['Config'].update(Entrypoint=['engine']),
                       lambda c:c['Config']['Labels'].update({runtime.PREFIX+'guard-run':'e'*64})):
            changed=copy.deepcopy(containers[0]);mutate(changed)
            with self.assertRaises(ValueError):runtime.observe_rank(plan,0,changed,images[0])

    def test_cleanup_preserves_unrelated_container(self):
        _, _, _, plan, containers, _ = guarded_fixture()
        unrelated=copy.deepcopy(containers[0]);unrelated['Config']['Labels'][runtime.PREFIX+'guard-run']='e'*64
        with patch.object(node,'inspect_named',return_value=unrelated),patch.object(node,'docker') as docker:
            with self.assertRaisesRegex(RuntimeError,'unrelated'):node.cleanup(plan,0)
        docker.assert_not_called()

    def test_guarded_observation_redacts_api_credential(self):
        spec,facts,prepared,_,containers,images=guarded_fixture()
        with patch.dict(os.environ,{'VLLM_API_KEY':'synthetic-guard-secret'}):
            plan=runtime.build_plan(spec,spec['spec_id'],facts,prepared)
            argv=runtime.docker_argv(plan,0)
        ref=spec['source']['image_repository']+'@'+spec['recipe']['image_digest']
        containers[0]['Config'].update(Labels=runtime.rank_spec(plan,0)['labels'],Cmd=argv[argv.index(ref)+1:])
        result=runtime.observe_rank(plan,0,containers[0],images[0])
        self.assertNotIn('synthetic-guard-secret',json.dumps(result))
        self.assertIn('<credential>',result['container_configuration']['command'])

    def test_bad_container_readback_never_releases_model(self):
        _,_,_,plan,containers,images=guarded_fixture()
        containers[0]['HostConfig']['MemorySwap']=32*GIB
        process=MagicMock();process.poll.return_value=None
        context={'plan':plan,'rank':0,'argv':runtime.docker_argv(plan,0),'ready_file':'unused'}
        with tempfile.TemporaryDirectory() as temp,patch.object(node,'preflight',return_value=({},images[0])),\
             patch.object(node,'inspect_named',return_value=containers[0]),\
             patch.object(node.subprocess,'Popen',return_value=process),\
             patch.object(node,'cleanup') as cleanup:
            with self.assertRaisesRegex(ValueError,'swap limit'):
                node.execute(context,Path(temp))
        process.stdin.write.assert_not_called()
        cleanup.assert_called_once_with(plan,0)

    def test_cancelled_node_driver_retains_guard_report(self):
        _,_,_,plan,containers,images=guarded_fixture()
        cancelled={'signal':None};state={'done':False};process=MagicMock()
        process.poll.side_effect=lambda:0 if state['done'] else None
        process.returncode=0
        @contextmanager
        def signals():yield cancelled
        def launch(*args,**kwargs):
            def control(data):
                if data==b'G':cancelled['signal']=signal.SIGTERM
                if data==b'Q':
                    report={**node.identity(plan,0),'kind':'pulsar-serving-guard-rank',
                            'stopped':True,'cgroup_peak_bytes':123}
                    kwargs['stdout'].write(json.dumps(report)+'\n');kwargs['stdout'].flush()
                    state['done']=True
            process.stdin.write.side_effect=control
            return process
        context={'plan':plan,'rank':0,'argv':runtime.docker_argv(plan,0),'ready_file':'unused'}
        with tempfile.TemporaryDirectory() as temp,patch.object(node,'preflight',return_value=({},images[0])),\
             patch.object(node,'inspect_named',return_value=containers[0]),\
             patch.object(node.subprocess,'Popen',side_effect=launch),\
             patch.object(node,'cancellation_signals',signals),patch.object(node,'cleanup'):
            report=node.execute(context,Path(temp))
        self.assertTrue(report['stopped']);self.assertEqual(report['cgroup_peak_bytes'],123)
        self.assertEqual([call.args[0] for call in process.stdin.write.call_args_list],[b'G',b'Q'])

    def test_cleanup_addresses_exact_owned_id(self):
        _, _, _, plan, containers, _ = guarded_fixture()
        with patch.object(node,'inspect_named',side_effect=[containers[0],None]),patch.object(node,'docker') as docker:
            self.assertTrue(node.cleanup(plan,0)['cleanup_verified'])
        docker.assert_called_once_with('rm','-f',containers[0]['Id'])

    def test_stop_refuses_wrong_invocation_or_reused_pid(self):
        _, _, _, plan, *_ = guarded_fixture()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'active-plan.json').write_text(json.dumps(plan))
            (root/'controller.json').write_text(json.dumps({'run_id':plan['guard_run_id'],'owner':[12345,'99']}))
            with patch.object(controller,'process_identity',return_value=[12345,'100']),patch.object(controller.os,'kill') as kill:
                with self.assertRaises(ValueError):controller.stop(root,'e'*64)
                with self.assertRaisesRegex(ValueError,'no longer alive'):controller.stop(root,plan['guard_run_id'])
            kill.assert_not_called()


class GuardRuntime(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cg=self.root/'cgroup';self.cg.mkdir()
        for k,v in {'memory.max':16*GIB,'memory.swap.max':0,'memory.current':1,'memory.peak':1,
                    'memory.events':'oom 0\noom_kill 0\n'}.items():(self.cg/k).write_text(str(v))
        self.mem=self.root/'meminfo';self.mem.write_text('MemAvailable: 104857600 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        self.marker=self.root/'child';self.children=[];self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.children:
            if p.poll() is None:p.kill()
            p.wait(timeout=5)
            for stream in (p.stdin,p.stdout,p.stderr):
                if stream:stream.close()

    def start(self, *, startup=5, timeout=10, lease=.6, worker=None, allowance=None):
        ctx={'rank':0,'run_id':'a'*64,'spec_id':'b'*64,'limits':{
            'memory_bytes':16*GIB,'min_host_available_bytes':8*GIB,
            'startup_timeout_seconds':startup,'timeout_seconds':timeout}}
        if allowance is not None:
            ctx['limits']['max_host_swap_growth_bytes'] = allowance
        worker=worker or f'import os,time;from pathlib import Path;Path({str(self.marker)!r}).write_text(str(os.getpid()));time.sleep(60)'
        code=("from pathlib import Path;import sys,json;from serving_guard.runtime import run,emit_report;"
              f"r=run({ctx!r},[sys.executable,'-c',{worker!r}],cgroup=Path({str(self.cg)!r}),meminfo=Path({str(self.mem)!r}),lease={lease});"
              "emit_report(r)")
        p=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(p);return p

    def send(self,p,data):p.stdin.write(data);p.stdin.flush()

    def wait_started(self):
        deadline=time.monotonic()+3
        while not self.marker.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(self.marker.exists())

    def result(self,p,close=True):
        if close:p.stdin.close();p.stdin=None
        out,err=p.communicate(timeout=5);self.assertEqual(p.returncode,0,err)
        value=json.loads(out.splitlines()[-1])
        if self.marker.exists():self.assertFalse(Path('/proc',self.marker.read_text()).exists())
        return value

    def test_no_model_before_release_and_eof_stops(self):
        p=self.start();r=self.result(p);self.assertFalse(self.marker.exists());self.assertIn('disconnected',r['error'])

    def test_lease_expiry_reaps_model_with_still_open_pipe(self):
        p=self.start();self.send(p,'G');p.wait(timeout=5)
        self.assertIn('lease expired',self.result(p)['error'])

    def test_operator_stop_reaps_child(self):
        p=self.start(lease=5);self.send(p,'G');self.wait_started();self.send(p,'HQ');p.wait(timeout=5)
        r=self.result(p);self.assertTrue(r['stopped']);self.assertTrue(r['ready'])

    def test_startup_and_whole_session_limits(self):
        for startup,timeout,beat,reason in ((.15,3,'G','startup time limit'),(3,.15,'GH','time limit')):
            self.marker.unlink(missing_ok=True);p=self.start(startup=startup,timeout=timeout,lease=5)
            self.send(p,'G');self.wait_started()
            if beat=='GH':self.send(p,'H')
            p.wait(timeout=5);self.assertIn(reason,self.result(p)['error'])

    def test_memory_pressure_after_release_stops_model(self):
        p=self.start(lease=5);self.send(p,'G');self.wait_started()
        (self.cg/'memory.events').write_text('oom 1\noom_kill 0\n')
        p.wait(timeout=5);self.assertIn('cgroup memory',self.result(p)['error'])

    def test_swap_growth_and_unavailable_sample_fail_closed(self):
        for mutate,reason in ((lambda:self.mem.write_text('MemAvailable: 104857600 kB\nSwapTotal: 1048576 kB\nSwapFree: 0 kB\n'),'swap growth'),
                              (lambda:self.mem.unlink(),'sample unavailable')):
            self.mem.write_text('MemAvailable: 104857600 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
            self.marker.unlink(missing_ok=True);p=self.start(lease=5);self.send(p,'G');self.wait_started();mutate()
            p.wait(timeout=5);self.assertIn(reason,self.result(p)['error'])

    def set_swap_used(self, mib):
        path = self.root / 'meminfo-next'
        path.write_text(f'MemAvailable: 104857600 kB\nSwapTotal: 1048576 kB\nSwapFree: {1048576-mib*1024} kB\n')
        path.replace(self.mem)

    def test_explicit_allowance_continues_and_records_baseline_without_reset(self):
        self.set_swap_used(100)
        p = self.start(lease=5, allowance=256 * 1024**2)
        self.send(p, 'G');self.wait_started()
        self.set_swap_used(190);self.send(p, '.')
        time.sleep(.35)
        self.assertIsNone(p.poll())
        self.send(p, 'Q')
        report = self.result(p, close=False)
        self.assertTrue(report['stopped'])
        self.assertEqual(report['baseline_sample']['swap_used_bytes'], 100 * 1024**2)
        self.assertEqual(report['last_sample']['swap_used_bytes'], 190 * 1024**2)
        self.assertEqual(report['max_observed_host_swap_growth_bytes'], 90 * 1024**2)
        self.assertEqual(report['host_swap_growth_limit_bytes'], 256 * 1024**2)
        self.assertEqual(report['effective_host_available_floor_bytes'], 84 * GIB)
        self.assertIsNone(report['trigger_sample'])

    def test_explicit_allowance_stop_retains_exact_trigger_and_reaps_child(self):
        p = self.start(lease=5, allowance=256 * 1024**2)
        self.send(p, 'G');self.wait_started()
        self.set_swap_used(257)
        p.wait(timeout=5)
        report = self.result(p)
        self.assertEqual(report['error'], 'host swap growth')
        self.assertEqual(report['baseline_sample']['swap_used_bytes'], 0)
        self.assertEqual(report['trigger_sample']['swap_used_bytes'], 257 * 1024**2)
        self.assertEqual(report['max_observed_host_swap_growth_bytes'], 257 * 1024**2)
        self.assertEqual(report['last_sample'], report['trigger_sample'])
        self.assertEqual(report['trigger_sample']['oom'], 0)
        self.assertGreaterEqual(report['trigger_sample']['elapsed_seconds'], 0)

    def test_invalid_allowance_never_releases_child(self):
        p = self.start(allowance=256 * 1024**2 + 1)
        _, error = p.communicate(timeout=5)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('invalid host swap growth allowance', error)
        self.assertFalse(self.marker.exists())

    def test_swap_boundaries_and_independent_guards(self):
        before = {'mem_available_bytes': 100 * GIB, 'swap_used_bytes': 100 * 1024**2}
        limits = {'memory_bytes': 16 * GIB, 'min_host_available_bytes': 8 * GIB, 'timeout_seconds': 20}
        sample = {**before, 'memory_current_bytes': 1, 'oom': 0, 'oom_kill': 0}
        for budget in (None, 0, 64 * 1024**2, 256 * 1024**2):
            selected = dict(limits)
            if budget is not None:
                selected['max_host_swap_growth_bytes'] = budget
            allowance = 64 * 1024**2 if budget is None else budget
            exact = {**sample, 'swap_used_bytes': before['swap_used_bytes'] + allowance}
            self.assertIsNone(guard_runtime.guard_reason(exact, before, selected, 0, 0))
            self.assertEqual(guard_runtime.guard_reason({**exact, 'swap_used_bytes': exact['swap_used_bytes'] + 1},
                before, selected, 0, 0), 'host swap growth')
        limits['max_host_swap_growth_bytes'] = 256 * 1024**2
        sample['swap_used_bytes'] += 90 * 1024**2
        for change, elapsed, age, reason in (({'mem_available_bytes': 83*GIB}, 0, 0, 'host available memory'),
                ({'memory_current_bytes': 16*GIB+1}, 0, 0, 'cgroup memory'),
                ({'oom': 1}, 0, 0, 'cgroup memory'), ({'oom_kill': 1}, 0, 0, 'cgroup memory'),
                ({}, 20, 0, 'time limit'), ({}, 0, 30, 'controller lease expired')):
            self.assertEqual(guard_runtime.guard_reason({**sample, **change}, before, limits, elapsed, age), reason)
        for value in (None, False, -1, 256 * 1024**2 + 1):
            with self.assertRaises(ValueError):
                guard_runtime.guard_reason(sample, before, {**limits, 'max_host_swap_growth_bytes': value}, 0, 0)

    def test_memory_check_precedes_release(self):
        p=self.start(lease=5);time.sleep(.08);(self.cg/'memory.current').write_text(str(17*GIB));self.send(p,'G');p.wait(timeout=5)
        self.assertIn('cgroup memory',self.result(p)['error']);self.assertFalse(self.marker.exists())

    def test_controller_pipe_loss_reaps_running_model(self):
        p=self.start(lease=5);self.send(p,'G');self.wait_started()
        self.assertIn('disconnected',self.result(p)['error'])

    def test_sigterm_reaps_running_model(self):
        p=self.start(lease=5);self.send(p,'G');self.wait_started();p.send_signal(signal.SIGTERM)
        p.wait(timeout=5);self.assertIn('interrupted',self.result(p)['error'])

    def test_invalid_swap_configuration_never_starts_child(self):
        (self.cg/'memory.swap.max').write_text('1');p=self.start();out,err=p.communicate(timeout=5)
        self.assertNotEqual(p.returncode,0);self.assertIn('swap must be disabled',err);self.assertFalse(self.marker.exists())

    def test_stalled_log_reader_does_not_block_lease_cleanup(self):
        worker=(f'import os;from pathlib import Path;Path({str(self.marker)!r}).write_text(str(os.getpid()));'
                'exec("while True: os.write(1,b\'x\'*65536)")')
        p=self.start(worker=worker);self.send(p,'G');self.wait_started()
        p.wait(timeout=5)
        self.assertFalse(Path('/proc',self.marker.read_text()).exists())
        # Full output is deliberately unavailable: the guard still exited and
        # reaped its child while nobody drained the bounded output channel.

    def test_final_escaped_report_larger_than_pipe_is_complete(self):
        source="from serving_guard.runtime import emit_report;raise SystemExit(0 if emit_report({'log_tail':'\\\\'*65536}) else 3)"
        result=subprocess.run([sys.executable,'-c',source],cwd=ROOT,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['log_tail'],'\\'*65536)

    def test_bundled_guard_runs_with_isolated_python(self):
        values={str(Path('/sys/fs/cgroup')/p.name):p.read_text() for p in self.cg.iterdir()}
        values['/pulsar-guard-host-meminfo']=self.mem.read_text()
        context={'rank':0,'run_id':'a'*64,'spec_id':'b'*64,'limits':{
            'memory_bytes':16*GIB,'min_host_available_bytes':8*GIB,
            'startup_timeout_seconds':10,'timeout_seconds':20}}
        child=f'import os,time;from pathlib import Path;Path({str(self.marker)!r}).write_text(str(os.getpid()));time.sleep(60)'
        prefix=("import pathlib,sys\n"
                f"values={values!r}\n"
                "original=pathlib.Path.read_text\n"
                "pathlib.Path.read_text=lambda path,*a,**kw:values[str(path)] if str(path) in values else original(path,*a,**kw)\n"
                f"sys.argv=['-c',{json.dumps(context)!r},sys.executable,'-c',{child!r}]\n"
                f"exec(compile({program.program()!r},'<frozen-program>','exec'))\n")
        p=subprocess.Popen([sys.executable,'-I','-S','-u','-c',prefix],cwd=ROOT,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(p);self.send(p,'G');self.wait_started();self.send(p,'HQ')
        self.assertTrue(self.result(p)['stopped'])


class GuardTransport(unittest.TestCase):
    def setUp(self):
        from tests.test_model_free_diagnostics import DiagnosticTransport
        DiagnosticTransport.setUp(self)
        import shutil
        for name in ('guarded-serving.sh','container_runtime.py'):
            shutil.copyfile(ROOT/'scripts'/name,self.root/'scripts'/name)
        with (self.root/'scripts/lib.sh').open('a') as f:
            f.write('\nacquire_model_library_hot_lock() { :; }\npersist_launch_plan_file() { :; }\n')
        with patch.object(program,'ROOT',self.root):
            spec,_,_,plan,*_=guarded_fixture()
        plan['ranks'][0]['control_ip']='192.0.2.1'
        (self.root/'guard-spec.json').write_text(json.dumps(spec))
        (self.root/'fixture-plan.json').write_text(json.dumps(plan))
        lib=self.root/'scripts/lib.sh';text=lib.read_text().replace('b'*64,'c'*64).replace('fixture-0 fixture-1 fixture-2','node-0 node-1 node-2');lib.write_text(text)
        (self.root/'scripts/up.sh').write_text('cp "$PULSAR_TEST_PLAN_SOURCE" "$PULSAR_LAUNCH_PLAN_OUT"\n')
        self.env['PULSAR_TEST_PLAN_SOURCE']=str(self.root/'fixture-plan.json')
        # Real Bash and supervised RPC, synthetic node workload and resources.
        # Runtime subprocess and Docker readback/ownership behavior have separate
        # tests above. This fixture never invokes a real Docker daemon or SSH.
        with (self.root/'serving_guard/node.py').open('a') as f:
            f.write('''
def preflight(plan,rank):
 if str(rank)==os.environ.get('PULSAR_TEST_REJECT_RANK'): raise RuntimeError('synthetic preflight failure')
 return {**identity(plan,rank),'ready':True},{}
def cleanup(plan,rank):
 marker=Path(os.environ['PULSAR_TEST_DOCKER_STATE'])/f'guard-{rank}'
 if marker.exists():
  if marker.read_text()!=plan['guard_run_id']:raise RuntimeError('unrelated preserved')
  marker.unlink()
 return {**identity(plan,rank),'cleanup_verified':True}
def execute(context,root):
 plan=context['plan'];rank=context['rank'];state=Path(os.environ['PULSAR_TEST_DOCKER_STATE'])
 (state/f'guard-{rank}').write_text(plan['guard_run_id'])
 (state/f'started-{rank}').touch()
 try:
  if str(rank)==os.environ.get('PULSAR_TEST_FAIL_RANK'):raise RuntimeError('synthetic peer failure')
  with cancellation_signals() as cancelled:
   while not cancelled['signal']:time.sleep(.05)
  return {**identity(plan,rank),'stopped':True}
 finally:cleanup(plan,rank)
''')
        self.spec_id=spec['spec_id'];self.output=self.root/'guard-evidence';self.processes=[]
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for p in self.processes:
            if p.poll() is None:p.terminate()
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
            for stream in (p.stdout,p.stderr):
                if stream:stream.close()

    def start(self,extra=None):
        p=subprocess.Popen(['bash',str(self.root/'scripts/guarded-serving.sh'),
            '--spec-file',str(self.root/'guard-spec.json'),'--spec-id',self.spec_id,
            '--output-dir',str(self.output),'--yes'],cwd=self.root,env={**self.env,**(extra or {})},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.processes.append(p);return p

    def wait_started(self,p):
        deadline=time.monotonic()+12
        state=self.root/'docker-state'
        while time.monotonic()<deadline and p.poll() is None:
            if all((state/f'started-{r}').exists() for r in range(3)):return
            time.sleep(.05)
        out,err=p.communicate(timeout=3);self.fail(out+'\n'+err)

    def test_preflight_failure_starts_no_rank_and_cleans_every_rank(self):
        p=self.start({'PULSAR_TEST_REJECT_RANK':'1'});out,err=p.communicate(timeout=20)
        self.assertNotEqual(p.returncode,0,err)
        result=json.loads((self.output/'result.json').read_text())
        self.assertTrue(result['phases']['cleanup']['complete'])
        self.assertFalse(list((self.root/'docker-state').glob('started-*')))

    def test_peer_failure_cleans_all_started_ranks(self):
        p=self.start({'PULSAR_TEST_FAIL_RANK':'1'});out,err=p.communicate(timeout=20)
        self.assertNotEqual(p.returncode,0,err)
        self.assertTrue(json.loads((self.output/'result.json').read_text())['phases']['cleanup']['complete'])
        self.assertFalse(list((self.root/'docker-state').glob('guard-*')))

    def test_explicit_stop_completes_owned_cleanup(self):
        p=self.start();self.wait_started(p)
        plan=json.loads((self.output/'active-plan.json').read_text())
        controller.stop(self.output,plan['guard_run_id'])
        out,err=p.communicate(timeout=20)
        self.assertEqual(p.returncode,0,err)
        result=json.loads(out);self.assertEqual(result['status'],'stopped')
        self.assertTrue(result['phases']['cleanup']['complete'])
        self.assertFalse(list((self.root/'docker-state').glob('guard-*')))

    def test_controller_sigkill_cancels_owned_node_workers(self):
        p=self.start();self.wait_started(p);p.kill();p.wait(timeout=5)
        deadline=time.monotonic()+12
        while list((self.root/'docker-state').glob('guard-*')) and time.monotonic()<deadline:
            time.sleep(.1)
        self.assertFalse(list((self.root/'docker-state').glob('guard-*')))
        self.assertFalse((self.output/'result.json').exists())


if __name__ == '__main__':
    unittest.main()
