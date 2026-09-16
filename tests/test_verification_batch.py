"""Bounded verification scheduling with real supervised synthetic workers."""
import json
import os
from pathlib import Path
import signal
import sys
import unittest

from model_library.verification_process import BOOTSTRAP
from tests import test_verification_process as processes


class VerificationBatch(unittest.TestCase):
    setUp=processes.VerificationProcesses.setUp
    cleanup=processes.VerificationProcesses.cleanup
    start=processes.VerificationProcesses.start
    ready=processes.VerificationProcesses.ready
    gone=processes.VerificationProcesses.gone

    def tasks(self, nodes, *, limit=3, failure=None, incomplete=None, worker_signal=None, lease=30):
        root=self.root/str(len(list(self.root.iterdir())))
        root.mkdir();(root/'jobs').mkdir()
        (root/'active.json').write_text('[]')
        tasks=[]
        for index,node in enumerate(nodes):
            program=root/'jobs'/f'{index}.program'
            program.write_text(f'''import fcntl,json,os,time,sys
from pathlib import Path
from model_library.verification_process import supervise_node,process_identity
root=Path({str(root)!r})
transport=process_identity(os.getppid())
def change(add):
 with (root/'lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  rows=json.loads((root/'active.json').read_text())
  if add:
   assert len(rows)<{limit}, 'global worker limit exceeded'
   assert all(row[1]!={node} for row in rows), 'two verifiers on one node'
   rows.append([{index},{node}])
  else: rows.remove([{index},{node}])
  (root/'active.json').write_text(json.dumps(rows))
def work():
 change(True)
 (root/'{index}.ready').write_text(json.dumps({{'worker':process_identity(os.getpid()),'supervisor':process_identity(os.getppid()),'transport':transport}}))
 while not (root/'{index}.release').exists(): time.sleep(.01)
 change(False)
 if {index==failure and worker_signal is not None!r}:
  print('synthetic worker signal {worker_signal}',file=sys.stderr,flush=True)
  os.kill(os.getpid(),{int(worker_signal or 0)})
 if {index==failure!r}:
  print('SHA-256 mismatch in synthetic checkpoint',file=sys.stderr,flush=True)
  return 2
 print('{{"verified":true}}',flush=True)
 return 0
raise SystemExit(supervise_node(work,_pulsar_control.fileno(),_pulsar_token,lease={lease},grace=.1))
''')
            command=[sys.executable,'-m','model_library.verification_process','--owner',str(os.getpid()),'--',sys.executable,'-c',BOOTSTRAP]
            if index==incomplete:
                # A lost transport response leaves the live started receipt
                # unresolved; the public wrapper must reject the whole result.
                command=[sys.executable,'-c',"import os,sys; os.write(int(os.environ['PULSAR_VERIFICATION_REPORT_FD']),(os.environ['PULSAR_VERIFICATION_REPORT']+':'+('a'*32)+':started\\n').encode()); print('synthetic transport failure',file=sys.stderr); raise SystemExit(7)"]
            tasks.append({'index':index,'node_slot':node,'program':str(program),'command':command})
        (root/'tasks.json').write_text(json.dumps(tasks))
        return root

    def batch(self, root, jobs=3, *, public=False):
        command=[sys.executable,'-m','model_library.verification_process','batch',
                 '--tasks',str(root/'tasks.json'),'--directory',str(root),'--jobs',str(jobs)]
        if public:
            code='''import json,sys
from model_library.verification_process import run_command,Cancelled
try:
 result=run_command(sys.argv[1:])
 print(json.dumps({'returncode':result.returncode,'diagnostic':result.stderr}),flush=True)
 raise SystemExit(result.returncode)
except Cancelled as exc:
 print(json.dumps({'cancelled':True,'confirmed':exc.confirmed,'diagnostic':exc.diagnostic}),flush=True)
 raise SystemExit(exc.exit_code)
'''
            command=[sys.executable,'-c',code,*command]
        return self.start(command)

    def test_overlap_global_limit_and_one_worker_per_node(self):
        for limit in (1,2,3):
            with self.subTest(limit=limit):
                root=self.tasks([0,1,2,0,1,2],limit=limit)
                process=self.batch(root,limit)
                # A serial implementation cannot reach all these markers when
                # the limit is >1. Worker-side locked assertions check both
                # limits throughout the remaining scheduling, without timing.
                for index in range(limit): self.ready(root/f'{index}.ready',process)
                self.assertEqual(len(json.loads((root/'active.json').read_text())),limit)
                for index in range(6): (root/f'{index}.release').touch()
                out,err=process.communicate(timeout=8)
                self.assertEqual(process.returncode,0,err)
                report=json.loads((root/'batch.json').read_text())
                self.assertEqual([r['index'] for r in report['results']],list(range(6)))
                self.assertTrue(all(r['returncode']==0 for r in report['results']))
                self.assertEqual(json.loads((root/'active.json').read_text()),[])

    def test_blocked_node_does_not_block_available_other_node(self):
        root=self.tasks([0,0,1],limit=2)
        process=self.batch(root,2)
        for index in (0,2): self.ready(root/f'{index}.ready',process)
        self.assertFalse((root/'1.ready').exists())
        (root/'0.release').touch()
        self.ready(root/'1.ready',process)
        for index in (1,2): (root/f'{index}.release').touch()
        _,err=process.communicate(timeout=8)
        self.assertEqual(process.returncode,0,err)

    def test_hash_error_cancels_peers_preserves_cause_and_skips_queue(self):
        root=self.tasks([0,1,2,0,1,2],failure=1)
        process=self.batch(root,public=True)
        records=[self.ready(root/f'{index}.ready',process) for index in range(3)]
        (root/'1.release').touch()
        out,err=process.communicate(timeout=8)
        self.assertEqual(process.returncode,2,err)
        response=json.loads(out)
        self.assertNotIn('cancelled',response)
        report=json.loads((root/'batch.json').read_text())
        self.assertEqual(report['first_error'],1)
        self.assertIn('SHA-256 mismatch',report['results'][1]['error'])
        self.assertEqual([r['returncode'] for r in report['results'][3:]],[125]*3)
        self.assertTrue(all(not (root/f'{index}.ready').exists() for index in (3,4,5)))
        for record in records:
            self.gone(record['worker']);self.gone(record['supervisor'])

    def test_public_cancellation_sees_live_receipts_and_reaps_all_workers(self):
        root=self.tasks([0,1,2,0])
        process=self.batch(root,public=True)
        records=[self.ready(root/f'{index}.ready',process) for index in range(3)]
        process.send_signal(signal.SIGTERM)
        out,err=process.communicate(timeout=8)
        self.assertEqual(json.loads(out)['confirmed'],True,err)
        self.assertNotEqual(process.returncode,0)
        self.assertFalse((root/'3.ready').exists())
        for record in records:
            self.gone(record['worker']);self.gone(record['supervisor'])

    def test_worker_signal_is_failure_and_does_not_mask_primary_error_with_peer_cleanup(self):
        for signum in (signal.SIGHUP,signal.SIGINT,signal.SIGTERM):
            with self.subTest(signal=signum):
                root=self.tasks([0,1,0],failure=1,worker_signal=signum)
                process=self.batch(root,public=True)
                records=[self.ready(root/f'{index}.ready',process) for index in range(2)]
                (root/'1.release').touch()
                out,err=process.communicate(timeout=8)
                self.assertEqual(process.returncode,128+signum,err)
                self.assertNotIn('cancelled',json.loads(out))
                report=json.loads((root/'batch.json').read_text())
                self.assertEqual(report['outcome'],'failed')
                self.assertEqual(report['first_error'],1)
                self.assertIn('synthetic worker signal',report['results'][1]['error'])
                self.assertFalse((root/'2.ready').exists())
                for record in records:
                    self.gone(record['worker']);self.gone(record['supervisor'])

    def test_remote_lease_expiry_is_failure_without_a_caller_signal(self):
        root=self.tasks([0],lease=.2)
        process=self.batch(root,public=True)
        record=self.ready(root/'0.ready',process)
        out,err=process.communicate(timeout=8)
        self.assertEqual(process.returncode,143,err)
        self.assertNotIn('cancelled',json.loads(out))
        report=json.loads((root/'batch.json').read_text())
        self.assertEqual(report['outcome'],'failed')
        self.assertIn('controller lease expired',report['results'][0]['error'])
        self.gone(record['worker']);self.gone(record['supervisor'])

    def test_signal_to_one_transport_is_a_worker_failure(self):
        root=self.tasks([0,1,0])
        process=self.batch(root,public=True)
        records=[self.ready(root/f'{index}.ready',process) for index in range(2)]
        os.kill(records[1]['transport'][0],signal.SIGTERM)
        out,err=process.communicate(timeout=8)
        self.assertEqual(process.returncode,143,err)
        self.assertNotIn('cancelled',json.loads(out))
        report=json.loads((root/'batch.json').read_text())
        self.assertEqual(report['outcome'],'failed')
        self.assertEqual(report['first_error'],1)
        self.assertIn('interrupted',report['results'][1]['error'])
        self.assertFalse((root/'2.ready').exists())
        for record in records:
            for key in ('worker','supervisor','transport'): self.gone(record[key])

    def test_signal_to_batch_records_caller_cancellation_explicitly(self):
        root=self.tasks([0,1,0])
        process=self.batch(root)
        records=[self.ready(root/f'{index}.ready',process) for index in range(2)]
        process.terminate()
        _,err=process.communicate(timeout=8)
        self.assertEqual(process.returncode,143,err)
        report=json.loads((root/'batch.json').read_text())
        self.assertEqual(report['outcome'],'cancelled')
        self.assertIsNone(report['first_error'])
        self.assertFalse((root/'2.ready').exists())
        for record in records:
            self.gone(record['worker']);self.gone(record['supervisor'])

    def test_unconfirmed_cleanup_preserves_failure_diagnostic(self):
        root=self.tasks([0],incomplete=0)
        process=self.batch(root,public=True)
        out,err=process.communicate(timeout=8)
        self.assertNotEqual(process.returncode,0,err)
        self.assertFalse(json.loads(out)['confirmed'])
        report=json.loads((root/'batch.json').read_text())
        self.assertIn('synthetic transport failure',report['results'][0]['error'])


if __name__=='__main__': unittest.main()
