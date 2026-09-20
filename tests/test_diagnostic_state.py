"""Closed control/observation decision tests; no external operations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.support.diagnostic_fixture import parse_create,IMAGE,BOOT,CID
from tests.test_diagnostic_container import definition
from release_spec import diagnostic as d
from release_spec import diagnostic_state as state
from scripts import diagnostic_runtime as runtime


class Controls(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        payload=self.root/'step.py';payload.write_text('pass\n');payload.chmod(0o644)
        doc=definition([{'name':'step.py','bytes':5,'sha256':d.sha256_file(payload),'mode':0o644}])
        doc['image_config_digest']=IMAGE
        self.plan=d.freeze_plan(doc,node_id='synthetic',inputs_root=str(self.root))
        self.good=parse_create(runtime.docker_create_argv(self.plan,name='synthetic',input_dir=str(self.root),attempt_nonce='a'*32))
        self.assertIsNone(self.problem(self.good))

    def problem(self,doc):
        return runtime.validate_created_container(doc,self.plan,input_dir=str(self.root),expected_cid=CID,attempt_nonce='a'*32)

    def test_every_required_host_control_omission_is_unknown(self):
        for key in self.good['HostConfig']:
            with self.subTest(key=key):
                inspected=copy.deepcopy(self.good);del inspected['HostConfig'][key]
                self.assertIsNotNone(self.problem(inspected))

    def test_every_stopped_state_omission_is_unknown(self):
        for key in self.good['State']:
            with self.subTest(key=key):
                inspected=copy.deepcopy(self.good);del inspected['State'][key]
                self.assertIsNotNone(self.problem(inspected))

    def test_required_config_omissions_are_unknown(self):
        for key in ('Entrypoint','Cmd','WorkingDir','User','Env','Labels','Healthcheck','StopSignal','StopTimeout'):
            with self.subTest(key=key):
                inspected=copy.deepcopy(self.good);del inspected['Config'][key]
                self.assertIsNotNone(self.problem(inspected))

    def test_effective_control_drift_and_extras(self):
        changes={'Privileged':True,'AutoRemove':True,'ReadonlyRootfs':False,'NetworkMode':'host','Runtime':'nvidia',
            'NanoCpus':2000000000.0,'PidsLimit':0,'Memory':0,'MemorySwap':-1,'CapAdd':['SYS_ADMIN'],
            'SecurityOpt':['no-new-privileges=false'],'IpcMode':'host','ShmSize':0,'PidMode':'host',
            'Tmpfs':{'/tmp':'rw,exec,size=1g'},'Binds':['/:/host:rw'],'VolumesFrom':['foreign'],
            'DeviceCgroupRules':['a *:* rwm'],'RestartPolicy':{'Name':'always','MaximumRetryCount':0},
            'MysteryEffectiveControl':True,'LogConfig':{'Type':'none','Config':{}}}
        for key,value in changes.items():
            with self.subTest(key=key):
                inspected=copy.deepcopy(self.good);inspected['HostConfig'][key]=value
                self.assertIsNotNone(self.problem(inspected))

    def test_unapproved_proc_protection_profile_is_not_accepted(self):
        inspected=copy.deepcopy(self.good)
        inspected['HostConfig']['MaskedPaths']=['/unapproved-protection-path']
        inspected['HostConfig']['ReadonlyPaths']=['/another-unapproved-path']
        self.assertIsNotNone(self.problem(inspected))

    def test_nested_booleans_are_not_numeric_zero(self):
        inspected=copy.deepcopy(self.good);inspected['HostConfig']['RestartPolicy']['MaximumRetryCount']=False
        self.assertIsNotNone(self.problem(inspected))
        inspected=copy.deepcopy(self.good);inspected['HostConfig']['DeviceRequests'][0]['Count']=False
        self.assertIsNotNone(self.problem(inspected))

    def test_every_mount_field_and_extra_is_bound(self):
        for key,value in {'Type':'volume','Source':'/','Destination':'/elsewhere','RW':True,'Propagation':'rshared','Mode':'z'}.items():
            with self.subTest(key=key):
                inspected=copy.deepcopy(self.good);inspected['Mounts'][0][key]=value
                self.assertIsNotNone(self.problem(inspected))
        inspected=copy.deepcopy(self.good);inspected['Mounts'].append({'Type':'bind','Source':'/','Destination':'/root','RW':True})
        self.assertIsNotNone(self.problem(inspected))
        inspected=copy.deepcopy(self.good);inspected['Mounts'][0]['ExtraWritablePath']='/unapproved'
        self.assertIsNotNone(self.problem(inspected))

    def test_command_path_environment_and_stop_signal_are_bound(self):
        for fields in ({'Env':['HOME=/tmp','BASH_ENV=/unapproved']},{'Cmd':['echo','pulsar-diagnostic-entrypoint.sh']},
                       {'StopSignal':'SIGKILL'},{'StopTimeout':10},{'Entrypoint':['/bin/sh']}):
            with self.subTest(fields=fields):
                inspected=copy.deepcopy(self.good);inspected['Config'].update(fields)
                self.assertIsNotNone(self.problem(inspected))
        inspected=copy.deepcopy(self.good);inspected['Path']='/bin/sh'
        self.assertIsNotNone(self.problem(inspected))

    def test_plan_data_is_independent_of_original_payload_and_binds_profile(self):
        (self.root/'step.py').unlink()
        self.assertEqual(d.verify_plan(self.plan),self.plan)
        changed=copy.deepcopy(self.plan);changed['control_profile']['readonly_rootfs']=1
        with self.assertRaises(Exception):d.verify_plan(changed)


class Observations(unittest.TestCase):
    def setUp(self):
        self.settings={k:d.PROFILE[k] for k in ('mem_available_floor_bytes','swap_growth_limit_bytes','max_observation_age_seconds')}
        self.now=time.monotonic_ns()
        self.claim={'plan_id':'a'*64,'attempt_nonce':'b'*32,'boot_id':BOOT}
        self.plan={'plan_id':self.claim['plan_id'],'definition':{'observer':dict(d.PROFILE),'image_id':IMAGE}}
        self.owner=state.lifecycle();self.owner['cleanup']['completed_monotonic_ns']=self.now-100000000
        self.good=state.observation();self.good.update(self.claim,observer_instance='f'*32)
        self.event_number=0
        state.reduce_observation(self.good,self.event('memory',self.now-10000000,mem_available_bytes=8*d.GIB,swap_used_bytes=0),self.settings)
        self.good.update(ready=True,phase='finished',snapshot_sequence=1,producer_exit_code=0,descriptor_closed=True,
            drain_complete=True,drain_request_ns=self.now-90000000,drain_before_ns=self.now-80000000,drain_after_ns=self.now-70000000,
            tail_before_ns=self.now-200000000,tail_after_ns=self.now-190000000,ready_eagain_before_ns=self.now-180000000,
            ready_eagain_after_ns=self.now-170000000,published_monotonic_ns=self.now-1000000)
        self.assertIsNone(self.problem(self.good))

    def event(self,kind,stamp,**kw):
        self.event_number+=1
        return {'kind':kind,'monotonic_ns':stamp,'event_sequence':self.event_number,
            **{k:self.good[k] for k in ('boot_id','observer_instance','plan_id','attempt_nonce','container_id','image_id','phase')},**kw}

    def problem(self,observed):
        return state.snapshot_problem(observed,self.plan,self.claim,self.owner,now_ns=self.now,terminal=True)

    def test_foreign_missing_stale_or_unclosed_terminal_cannot_certify(self):
        changes={'plan_id':'z','attempt_nonce':'z','boot_id':'f'*32,'observer_instance':None,'container_id':CID,
                 'snapshot_sequence':0,'last_sample_monotonic_ns':self.now-2*10**9,'descriptor_closed':False,
                 'producer_exit_code':1,'phase':'observing','drain_complete':False,'drain_before_ns':1,'drain_request_ns':None}
        for key,value in changes.items():
            with self.subTest(key=key):
                observed=copy.deepcopy(self.good);observed[key]=value
                self.assertIsNotNone(self.problem(observed))

    def test_terminal_resource_extrema_and_counters_cannot_regress(self):
        self.owner['last_snapshot']=copy.deepcopy(self.good)
        self.owner['snapshot_sequence']=1
        for key,value in {'samples':0,'event_sequence':0,'min_mem_available_bytes':16*d.GIB}.items():
            with self.subTest(key=key):
                observed=copy.deepcopy(self.good);observed['snapshot_sequence']=2;observed[key]=value
                self.assertIsNotNone(self.problem(observed))

    def test_transient_memory_and_swap_failures_latch(self):
        observed=copy.deepcopy(self.good)
        state.reduce_observation(observed,self.event('memory',self.now+1,mem_available_bytes=d.GIB,swap_used_bytes=512*1024**2),self.settings)
        state.reduce_observation(observed,self.event('memory',self.now+2,mem_available_bytes=8*d.GIB,swap_used_bytes=0),self.settings)
        self.assertIsNotNone(observed['first_safety_failure'])
        self.assertEqual(observed['min_mem_available_bytes'],d.GIB)
        self.assertEqual(observed['max_swap_used_bytes'],512*1024**2)

    def test_nonmatching_terminal_producer_instance_is_rejected(self):
        self.owner['observer_instance']='a'*32
        self.assertIsNotNone(self.problem(self.good))

    def test_terminal_numeric_booleans_and_float_schema_are_rejected(self):
        for key,value in (('producer_exit_code',False),('schema_version',2.0)):
            observed=copy.deepcopy(self.good);observed[key]=value
            self.assertIsNotNone(self.problem(observed))

    def test_removal_failure_dominates_safety_and_workload(self):
        self.owner.update(create_consumed=True,start_consumed=False)
        self.owner['cleanup'].update(absent=True,query_rc=0,rm_rc=1,stop_rc=0)
        self.owner['observer_closure'].update(waited=True,exit_code=0,forced=False,clients_closed=True)
        observed=copy.deepcopy(self.good);observed['first_safety_failure']='sampled breach'
        result=state.final_result(self.plan,self.claim,self.owner,observed)
        self.assertEqual(result['outcome'],'cleanup_unconfirmed')


if __name__=='__main__':unittest.main()
