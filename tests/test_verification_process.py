"""Real child-process cancellation with synthetic work and simulated SSH."""
import ctypes
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from model_library.verification_process import BOOTSTRAP, process_identity

ROOT=Path(__file__).resolve().parents[1]


class VerificationProcesses(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.children=[]
        self.addCleanup(self.cleanup)
        self.env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(ROOT)}
        self.env.pop('PULSAR_VERIFICATION_OWNER',None)
        self.env.pop('PULSAR_VERIFICATION_REPORT',None)

    def cleanup(self):
        for process in self.children:
            if process.poll() is None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            try: process.communicate(timeout=5)
            except (ValueError,subprocess.TimeoutExpired): pass

    def start(self, command, **kwargs):
        process=subprocess.Popen(command,cwd=ROOT,env=self.env,start_new_session=True,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs)
        self.children.append(process)
        return process

    def ready(self, path, process):
        deadline=time.monotonic()+8
        while True:
            try:
                return json.loads(path.read_text())
            except (OSError,ValueError):
                pass
            if process.poll() is not None:
                out,err=process.communicate()
                self.fail(f'worker ended before readiness: {out!r} {err!r}')
            if time.monotonic()>deadline: self.fail('worker did not become ready')
            time.sleep(.02)

    def gone(self, identity):
        deadline=time.monotonic()+4
        while process_identity(identity[0])==identity and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertNotEqual(process_identity(identity[0]),identity,'owned worker is still running')

    def program(self, label, *, stubborn=False, lease=.5, grace=.1, duration=60, result=0, descendant=False):
        ready=self.root/(label+'.json')
        completed=self.root/(label+'.complete')
        code=f'''import json,os,signal,time
from pathlib import Path
from model_library.verification_process import supervise_node,process_identity
def work():
 if {stubborn!r}: signal.signal(signal.SIGTERM,signal.SIG_IGN)
 child=None
 if {descendant!r}:
  child=os.fork()
  if child==0:
   signal.signal(signal.SIGTERM,signal.SIG_IGN)
   while True: time.sleep(.05)
 Path({str(ready)!r}).write_text(json.dumps({{'worker':process_identity(os.getpid()),'supervisor':process_identity(os.getppid()),'descendant':process_identity(child) if child else None}}))
 time.sleep({duration!r})
 Path({str(completed)!r}).write_text('verified')
 print(json.dumps({{'verified':True}}),flush=True)
 return {result!r}
raise SystemExit(supervise_node(work,_pulsar_control.fileno(),_pulsar_token,lease={lease!r},grace={grace!r}))
'''
        return code.encode(),ready,completed

    def node(self, program):
        process=self.start([sys.executable,'-c',BOOTSTRAP],stdin=subprocess.PIPE)
        token='a'*32
        process.stdin.write(f'{len(program)} {token}\n'.encode()+program+b'.')
        process.stdin.flush()
        return process

    def transport(self, program, command=None):
        process=self.start([sys.executable,'-m','model_library.verification_process',
                            '--owner',str(os.getpid()),'--',*(command or [sys.executable,'-c',BOOTSTRAP])],stdin=subprocess.PIPE)
        process.stdin.write(program);process.stdin.close();process.stdin=None
        return process

    def test_normal_completion_and_failure_preserve_status_and_json(self):
        for status in (0,7):
            with self.subTest(status=status):
                program,ready,completed=self.program(str(status),duration=0,result=status)
                process=self.transport(program)
                out,err=process.communicate(timeout=8)
                self.assertEqual(process.returncode,status,err)
                self.assertTrue(completed.exists())
                if status==0: self.assertEqual(json.loads(out),{'verified':True})
                else: self.assertEqual(out,b'')
                self.assertNotIn(b'pulsar-verifier:',err)
                record=json.loads(ready.read_text());self.gone(record['worker'])

    def test_early_transport_failure_is_not_success_or_confirmed_cleanup(self):
        process=self.transport(b'x'*200000,[sys.executable,'-c','raise SystemExit(7)'])
        out,err=process.communicate(timeout=6)
        self.assertNotEqual(process.returncode,0)
        self.assertEqual(out,b'')
        self.assertIn(b'cleanup unconfirmed',err)
        self.assertNotIn(b'Traceback',err)

    def test_eof_cancels_reaps_worker_and_does_not_emit_success(self):
        program,ready,completed=self.program('eof')
        process=self.node(program);record=self.ready(ready,process)
        process.stdin.close();process.stdin=None
        out,err=process.communicate(timeout=5)
        self.assertEqual(process.returncode,143,err)
        self.assertEqual(out,b'');self.assertFalse(completed.exists())
        self.assertIn(b':cancelled',err)
        self.assertFalse(Path(f"/proc/{record['worker'][0]}").exists())

    def test_silent_open_channel_expires_without_activity(self):
        program,ready,completed=self.program('lease',lease=.2)
        process=self.node(program);record=self.ready(ready,process)
        process.wait(timeout=5)
        process.stdin.close();process.stdin=None
        out,err=process.communicate()
        self.assertIn(b'lease expired',err)
        self.assertNotEqual(process.returncode,0)
        self.assertEqual(out,b'');self.assertFalse(completed.exists());self.gone(record['worker'])

    def test_heartbeats_keep_a_quiet_worker_alive(self):
        program,ready,completed=self.program('quiet',duration=.65,lease=.25)
        process=self.node(program);self.ready(ready,process)
        while process.poll() is None:
            try: process.stdin.write(b'.');process.stdin.flush()
            except BrokenPipeError: break
            time.sleep(.05)
        process.stdin.close();process.stdin=None
        out,err=process.communicate(timeout=5)
        self.assertEqual(process.returncode,0,err)
        self.assertEqual(json.loads(out),{'verified':True});self.assertTrue(completed.exists())

    def test_signals_cancel_only_the_owned_transport(self):
        sentinel=self.start([sys.executable,'-c','import time; time.sleep(60)'])
        for signum in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            with self.subTest(signum=signum):
                program,ready,completed=self.program(str(signum),stubborn=True)
                process=self.transport(program);record=self.ready(ready,process)
                process.send_signal(signum)
                out,err=process.communicate(timeout=6)
                self.assertNotEqual(process.returncode,0)
                self.assertEqual(out,b'');self.assertFalse(completed.exists())
                self.assertIn(b'worker cleanup confirmed',err)
                self.gone(record['worker']);self.gone(record['supervisor'])
                self.assertIsNone(sentinel.poll())

    def test_stubborn_descendant_is_killed_and_reaped(self):
        program,ready,_=self.program('descendants',stubborn=True,descendant=True)
        process=self.node(program);record=self.ready(ready,process)
        process.stdin.close();process.stdin=None
        _,err=process.communicate(timeout=5)
        self.assertIn(b':cancelled',err)
        for key in ('worker','descendant'):
            self.assertFalse(Path(f"/proc/{record[key][0]}").exists())

    def test_repeated_cancellation_is_idempotent(self):
        program,ready,_=self.program('repeat',stubborn=True,grace=.25)
        process=self.node(program);record=self.ready(ready,process)
        process.send_signal(signal.SIGTERM);time.sleep(.04);process.send_signal(signal.SIGINT)
        process.stdin.close();process.stdin=None
        out,err=process.communicate(timeout=5)
        self.assertEqual(process.returncode,143,err)
        self.assertEqual(err.count(b':cancelled'),1)
        self.assertEqual(out,b'');self.gone(record['worker'])

    def test_killed_supervisor_cannot_leave_verifier_running(self):
        program,ready,completed=self.program('parent-death',stubborn=True)
        process=self.node(program);record=self.ready(ready,process)
        process.kill();process.wait(timeout=3)
        process.stdin.close();process.stdin=None
        self.gone(record['worker']);self.assertFalse(completed.exists())

    def test_public_command_cancels_nested_worker_and_reports_cleanup(self):
        program,ready,completed=self.program('public',stubborn=True,lease=30,grace=2)
        payload=self.root/'program';payload.write_bytes(program)
        script=self.root/'audit.sh'
        script.write_text('''#!/usr/bin/env bash
set -euo pipefail
bootstrap=$(python3 scripts/node-bundle.py --bootstrap)
python3 -m model_library.verification_process --owner "$$" -- python3 -c "$bootstrap" < "$1"
''')
        code='''import json,sys
from scripts.public_cli import execute
from model_library.verification_process import Cancelled
try:
 execute(sys.argv[1],[sys.argv[2]],json_result=True)
except Cancelled as exc:
 print(json.dumps({'cancelled':True,'cleanup_confirmed':exc.confirmed}),flush=True)
 raise SystemExit(exc.exit_code)
'''
        process=self.start([sys.executable,'-c',code,str(script),str(payload)])
        record=self.ready(ready,process)
        process.terminate()
        out,err=process.communicate(timeout=6)
        self.assertEqual(json.loads(out),{'cancelled':True,'cleanup_confirmed':True},err)
        self.assertFalse(completed.exists());self.gone(record['worker']);self.gone(record['supervisor'])

    def test_unconfirmed_worker_receipt_prevents_success(self):
        script=self.root/'incomplete.sh'
        script.write_text('printf "%s:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:started\\n" "$PULSAR_VERIFICATION_REPORT" >&2\nprintf "{\\"verified\\":true}\\n"\n')
        code='''import json,sys
from scripts.public_cli import execute
from model_library.verification_process import Cancelled
try: execute(sys.argv[1],[],json_result=True)
except Cancelled as exc:
 print(json.dumps({'cleanup_confirmed':exc.confirmed}),flush=True)
 raise SystemExit(4)
raise SystemExit('unconfirmed worker was accepted')
'''
        process=self.start([sys.executable,'-c',code,str(script)])
        out,err=process.communicate(timeout=5)
        self.assertEqual(process.returncode,4,err)
        self.assertEqual(json.loads(out),{'cleanup_confirmed':False})

    def test_incomplete_program_frame_exits_without_starting_worker(self):
        process=self.start([sys.executable,'-c',BOOTSTRAP],stdin=subprocess.PIPE)
        out,err=process.communicate(b'100 '+b'a'*32+b'\npartial',timeout=5)
        self.assertNotEqual(process.returncode,0)
        self.assertEqual(out,b'')
        self.assertIn(b'incomplete node frame',err)

    def test_closed_channel_before_launch_does_not_start_worker(self):
        program,ready,completed=self.program('no-start')
        process=self.start([sys.executable,'-c',BOOTSTRAP],stdin=subprocess.PIPE)
        out,err=process.communicate(f'{len(program)} '.encode()+b'a'*32+b'\n'+program+b'.',timeout=5)
        self.assertEqual(process.returncode,143,err)
        self.assertEqual(out,b'')
        self.assertFalse(ready.exists());self.assertFalse(completed.exists())

    def test_public_cancellation_uses_the_existing_error_envelope(self):
        from scripts import public_cli
        from model_library.verification_process import Cancelled
        for confirmed in (True,False):
            output=io.StringIO()
            with patch.object(public_cli,'dispatch',side_effect=Cancelled('cancelled fixture',confirmed=confirmed)),redirect_stdout(output):
                status=public_cli.main(['observe','--json'])
            response=json.loads(output.getvalue())
            self.assertNotEqual(status,0)
            self.assertEqual(response['schema_version'],1)
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['code'],'cancelled' if confirmed else 'cleanup_incomplete')

    def test_public_cleanup_error_retains_redacted_original_failure(self):
        from scripts import public_cli
        from model_library.verification_process import Cancelled
        failure=Cancelled('worker cleanup unconfirmed',confirmed=False,
                          diagnostic='SHA-256 mismatch; password=synthetic-value')
        with patch.object(public_cli,'run_command',side_effect=failure):
            with self.assertRaises(Cancelled) as caught:
                public_cli.execute('fixture',[])
        self.assertFalse(caught.exception.confirmed)
        self.assertIn('SHA-256 mismatch',str(caught.exception))
        self.assertIn('cleanup unconfirmed',str(caught.exception))
        self.assertNotIn('synthetic-value',str(caught.exception))

    def test_lost_simulated_ssh_connection_stops_detached_remote_worker(self):
        # Adopt the simulated remote supervisor after its fake SSH parent dies;
        # this keeps the test responsible for reaping every process it creates.
        libc=ctypes.CDLL(None);previous=ctypes.c_int()
        libc.prctl(37,ctypes.byref(previous),0,0,0);libc.prctl(36,1,0,0,0)
        self.addCleanup(lambda:libc.prctl(36,previous.value,0,0,0))
        remote_pid=self.root/'ssh-remote.json'
        def cleanup_remote():
            if not remote_pid.exists(): return
            identity=json.loads(remote_pid.read_text())
            if process_identity(identity[0])==identity:
                os.killpg(identity[0],signal.SIGKILL)
            try: os.waitpid(identity[0],0)
            except ChildProcessError: pass
        self.addCleanup(cleanup_remote)
        proxy=self.root/'ssh.py'
        proxy.write_text(f'''import json,os,subprocess,sys,time
from pathlib import Path
from model_library.verification_process import BOOTSTRAP,process_identity
child=subprocess.Popen([sys.executable,'-c',BOOTSTRAP],start_new_session=True)
Path({str(remote_pid)!r}).write_text(json.dumps(process_identity(child.pid)))
raise SystemExit(child.wait())
''')
        program,ready,completed=self.program('remote',stubborn=True)
        process=self.transport(program,[sys.executable,str(proxy)])
        record=self.ready(ready,process)
        process.kill();process.wait(timeout=3)
        self.gone(record['worker']);self.gone(record['supervisor'])
        remote=json.loads(remote_pid.read_text())
        try: os.waitpid(remote[0],0)
        except ChildProcessError: pass
        self.assertFalse(completed.exists())


if __name__=='__main__': unittest.main()
