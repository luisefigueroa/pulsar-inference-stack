"""Actual configuration mismatches must fail independently of commit labels."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from release_spec import serving
from scripts import container_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


def fixture(nodes=1):
    draft = serving.example(nodes)
    draft['recipe']['model'] = {'model_id': 'example/model', 'model_commit': 'a'*40}
    draft['recipe']['image_digest'] = 'sha256:'+'b'*64
    manifest = json.loads((ROOT/'tests/fixtures/contracts/manifest.json').read_text())
    spec = serving.freeze(draft, manifest)
    ranks = [dict(rank=i, node_id=f'node-{i}', hostname=f'rank-{i}', ssh_host='local' if i==0 else f'rank-{i}',
                  control_ip=f'192.0.2.{i+1}', control_if='eth0', hcas='' if nodes==1 else 'mlx5_0') for i in range(nodes)]
    prepared = dict(schema_version=1, kind='pulsar-prepared-set', spec_id=spec['spec_id'],
        topology_id='c'*64, snapshot_manifest_id=manifest['manifest_id'], revision='a'*40,
        home_node_id='node-0', ranks=[dict(rank=i,node_id=f'node-{i}',hub_path=f'/var/tmp/fixture-rank-{i}',
            path=f'/var/tmp/fixture-rank-{i}/snapshots/'+('a'*40)) for i in range(nodes)])
    facts = dict(port=8000,served_name='example',topology_id='c'*64,ranks=ranks)
    with patch.dict('os.environ', {'API_KEY':'','VLLM_API_KEY':''}):
        plan = runtime.build_plan(spec, spec['spec_id'], facts, prepared)
    images, containers = [], []
    for rank in range(nodes):
        expected=runtime.rank_spec(plan,rank)
        argv=runtime.docker_argv(plan,rank,include_secrets=False)
        image_ref=spec['source']['image_repository']+'@'+spec['recipe']['image_digest']
        image={'Id':'sha256:'+'d'*64,'RepoDigests':[image_ref], 'Config':{'Env':['PATH=/usr/bin'],'Entrypoint':['engine']}}
        health={'Test':['NONE']} if nodes>1 else {'Test':['CMD-SHELL','curl -fs http://localhost:8000/health || exit 1'],
            'Interval':30000000000,'Timeout':5000000000,'Retries':3,'StartPeriod':900000000000}
        container={'Id':str(rank+1)*64,'Image':image['Id'],
            'State':{'Running':True,'StartedAt':'2026-09-09T00:00:00Z'},
            'Config':{'Image':image_ref,'Labels':expected['labels'],'Entrypoint':['engine'],
                      'Cmd':argv[argv.index(image_ref)+1:],'Env':['PATH=/usr/bin']+(['HF_TOKEN='] if nodes==1 else [])+
                       [f'{k}={v}' for k,v in runtime.environment(plan,rank).items()], 'Healthcheck':health},
            'HostConfig':{'NetworkMode':'bridge' if nodes==1 else 'host','IpcMode':'host','ShmSize':67108864,
                'Memory':0,'NanoCpus':0,'RestartPolicy':{'Name':'no','MaximumRetryCount':0},
                'Ulimits':[{'Name':'memlock','Soft':-1,'Hard':-1},{'Name':'stack','Soft':67108864,'Hard':67108864}],
                'DeviceRequests':[{'Driver':'','Count':-1,'DeviceIDs':None,'Capabilities':[['gpu']]}],
                'Devices':[] if nodes==1 else [{'PathOnHost':'/dev/infiniband/uverbs0','PathInContainer':'/dev/infiniband/uverbs0','CgroupPermissions':'rwm'}],
                'PortBindings':{'8000/tcp':[{'HostIp':'','HostPort':'8000'}]} if nodes==1 else {}},
            'Mounts':[{'Source':expected['mounts'][0]['source'],'Destination':expected['mounts'][0]['target'],'RW':False}]}
        images.append(image);containers.append(container)
    return spec, facts, prepared, plan, containers, images


class ContainerRuntime(unittest.TestCase):
    def test_all_ranks_observable_without_stack_commit(self):
        for count in (1,2):
            spec,facts,prepared,plan,containers,images=fixture(count)
            self.assertNotIn('stack_build',plan)
            for rank in range(count):
                expected_name=('vllm-' if count==1 else 'vllm-cluster-')+spec['spec_id']
                self.assertEqual(plan['container_name'],expected_name)
                self.assertNotIn('io.pulsar.gb10.stack-build',containers[rank]['Config']['Labels'])
                result=runtime.observe_rank(plan,rank,containers[rank],images[rank])
                self.assertEqual(result['spec_id'],spec['spec_id'])
                self.assertEqual(result['container_configuration']['network_mode'],'bridge' if count==1 else 'host')

    def test_host_configuration_changes_are_not_hidden_by_matching_labels(self):
        spec,facts,prepared,plan,containers,images=fixture()
        mutations=[('NetworkMode','host'),('IpcMode','private'),('Memory',1073741824),('NanoCpus',100000000),
                   ('CpuQuota',10000),('CpusetCpus','0'),('CpuShares',512),('MemoryReservation',1024),
                   ('Privileged',True),('CapAdd',['SYS_NICE']),
                   ('Ulimits',[]),('DeviceRequests',[]),('Devices',[{'PathOnHost':'/dev/other'}]),
                   ('PortBindings',{}),('RestartPolicy',{'Name':'always','MaximumRetryCount':0})]
        for field,value in mutations:
            with self.subTest(field=field),self.assertRaises(ValueError):
                container=copy.deepcopy(containers[0]);container['HostConfig'][field]=value
                runtime.observe_rank(plan,0,container,images[0])
        container=copy.deepcopy(containers[0]);container['Config']['Healthcheck']['Interval']=1
        with self.assertRaisesRegex(ValueError,'healthcheck'):
            runtime.observe_rank(plan,0,container,images[0])
        container=copy.deepcopy(containers[0]);container['Mounts'].append({'Source':'/var/tmp/other','Destination':'/usr/local/lib','RW':True})
        with self.assertRaisesRegex(ValueError,'mount'):
            runtime.observe_rank(plan,0,container,images[0])

    def test_boot_and_recipe_drift_are_separate(self):
        spec,facts,prepared,plan,containers,images=fixture()
        before=runtime.observe_rank(plan,0,containers[0],images[0])
        containers[0]['State']['StartedAt']='2026-09-09T01:00:00Z'
        after=runtime.observe_rank(plan,0,containers[0],images[0])
        self.assertNotEqual(before['boot_witness'],after['boot_witness'])
        effective=serving.apply_overrides(spec,{'container':{'network_mode':'host'}})
        other=runtime.build_plan(effective,spec['spec_id'],facts,prepared,selected_spec=spec)
        self.assertFalse(other['matches_selected_spec'])
        self.assertEqual(other['service_id'],plan['service_id'])
        self.assertNotEqual(other['spec_id'],plan['spec_id'])
        with self.assertRaises(ValueError):
            runtime.observe_rank(other,0,containers[0],images[0])

    def test_plan_cannot_change_after_binding(self):
        _,_,_,plan,_,_=fixture()
        plan['port']=9000
        with self.assertRaisesRegex(ValueError,'digest'):
            runtime.validate_plan(plan)

    def test_equivalent_cpu_limits_normalize_but_conflicting_limits_fail(self):
        _,_,_,_,containers,images=fixture()
        host=containers[0]['HostConfig']
        host.update(NanoCpus=2000000000,CpuQuota=200000,CpuPeriod=100000)
        self.assertEqual(runtime.container_configuration(containers[0],images[0])['cpu_limit_nanos'],2000000000)
        host['NanoCpus']=0
        self.assertEqual(runtime.container_configuration(containers[0],images[0])['cpu_limit_nanos'],2000000000)
        host['NanoCpus']=1000000000
        with self.assertRaisesRegex(ValueError,'conflicting CPU'):
            runtime.container_configuration(containers[0],images[0])

    def test_prepared_rank_or_snapshot_mismatch_fails_before_launch(self):
        spec,facts,prepared,plan,_,_=fixture(2)
        prepared['ranks'][1]['path']='/var/tmp/wrong-snapshot'
        with self.assertRaises(ValueError):
            runtime.build_plan(spec,spec['spec_id'],facts,prepared)


if __name__=='__main__':
    unittest.main()
