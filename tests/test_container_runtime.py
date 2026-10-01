"""Actual configuration mismatches must fail independently of commit labels."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from release_spec import serving
from scripts import container_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


def fixture(nodes=1, speculative=False, subdirectory=None):
    draft = serving.example(nodes)
    draft['recipe']['model'] = {'model_id': 'example/model', 'model_commit': 'a'*40}
    draft['recipe']['image_digest'] = 'sha256:'+'b'*64
    manifest = json.loads((ROOT/'tests/fixtures/contracts/manifest.json').read_text())
    if subdirectory is not None:
        from release_spec import build_snapshot_manifest
        manifest = build_snapshot_manifest(model_id=manifest['model_id'],
            snapshot_revision=manifest['snapshot_revision'], files=[*manifest['files'],
                {'path': subdirectory+'/config.json', 'sha256': 'd'*64, 'size': 3}])
        draft['schema_version'] = 2
        draft['recipe']['required_snapshots'] = {}
        if not speculative:
            draft['recipe']['engine_args'] += ['--speculative-config', json.dumps({
                'model': 'pulsar-snapshot:target/'+subdirectory, 'method': 'dflash'})]
    spec = serving.freeze(draft, {'target': manifest} if subdirectory is not None else manifest)
    ranks = [dict(rank=i, node_id=f'node-{i}', hostname=f'rank-{i}', ssh_host='local' if i==0 else f'rank-{i}',
                  control_ip=f'192.0.2.{i+1}', control_if='eth0', hcas='' if nodes==1 else 'mlx5_0') for i in range(nodes)]
    prepared = dict(schema_version=1, kind='pulsar-prepared-set', spec_id=spec['spec_id'],
        topology_id='c'*64, snapshot_manifest_id=manifest['manifest_id'], revision='a'*40,
        home_node_id='node-0', ranks=[dict(rank=i,node_id=f'node-{i}',hub_path=f'/var/tmp/fixture-rank-{i}',
            path=f'/var/tmp/fixture-rank-{i}/snapshots/'+('a'*40)) for i in range(nodes)])
    if speculative:
        from release_spec.normalize import snapshot_manifest_id
        draft['schema_version']=2
        second=copy.deepcopy(manifest);second['snapshot_revision']='e'*40
        second['manifest_id']=snapshot_manifest_id(second)
        draft['recipe']['required_snapshots']={'draft':{'model_id':second['model_id'],'model_commit':second['snapshot_revision']}}
        draft['recipe']['engine_args'] += ['--speculative_config.model',
            'pulsar-snapshot:draft'+('/'+subdirectory if subdirectory is not None else '')]
        spec=serving.freeze(draft,{'target':manifest,'draft':second})
        members={}
        for name,model in serving.required_snapshots(spec).items():
            member=copy.deepcopy(prepared);member.update(spec_id=spec['spec_id'],snapshot_manifest_id=model['snapshot_manifest']['manifest_id'],revision=model['model_commit'])
            for row in member['ranks']:
                row['hub_path'] += '/'+name
                row['path']=row['hub_path']+'/snapshots/'+model['model_commit']
                row['snapshot_manifest_id']=model['snapshot_manifest']['manifest_id']
            members[name]=member
        prepared={'schema_version':2,'kind':'pulsar-prepared-set','spec_id':spec['spec_id'],
                  'topology_id':prepared['topology_id'],'snapshots':members}
    elif subdirectory is not None:
        for row in prepared['ranks']:
            row['snapshot_manifest_id'] = manifest['manifest_id']
        prepared = {'schema_version': 2, 'kind': 'pulsar-prepared-set',
                    'spec_id': spec['spec_id'], 'topology_id': prepared['topology_id'],
                    'snapshots': {'target': prepared}}
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
            'Mounts':[{'Source':mount['source'],'Destination':mount['target'],'RW':False} for mount in expected['mounts']]}
        images.append(image);containers.append(container)
    return spec, facts, prepared, plan, containers, images


class ContainerRuntime(unittest.TestCase):
    def test_checkpoint_subdirectories_keep_exact_readonly_mounts_on_every_rank(self):
        for nodes in (1, 2):
            for named in (False, True):
                with self.subTest(nodes=nodes, named=named):
                    spec, facts, prepared, plan, containers, images = fixture(nodes, speculative=named, subdirectory='dflash')
                    name = 'draft' if named else 'target'
                    model = serving.required_snapshots(spec)[name]
                    root = ('/pulsar/snapshots/'+model['snapshot_manifest']['manifest_id'] if named else
                            '/root/.cache/huggingface/hub/models--'+model['model_id'].replace('/', '--'))
                    path = root+'/snapshots/'+model['model_commit']+'/dflash'
                    for rank in range(nodes):
                        expected = runtime.rank_spec(plan, rank)
                        self.assertEqual(len(expected['mounts']), 2 if named else 1)
                        self.assertTrue(all(mount['mode'] == 'ro' for mount in expected['mounts']))
                        self.assertIn(path, ' '.join(expected['engine_args']))
                        observed = runtime.observe_rank(plan, rank, containers[rank], images[rank])
                        self.assertEqual(set(observed['snapshots']), {'target', 'draft'} if named else {'target'})
                        changed = copy.deepcopy(containers[rank])
                        mount = expected['mounts'][-1]
                        changed['Mounts'].append({'Source': mount['source'],
                            'Destination': path, 'RW': False})
                        with self.assertRaisesRegex(ValueError, 'mount'):
                            runtime.observe_rank(plan, rank, changed, images[rank])
                    del prepared['snapshots'][name]['ranks'][-1]
                    with self.assertRaises(ValueError):
                        runtime.build_plan(spec, spec['spec_id'], facts, prepared)

    def test_required_snapshots_mount_exact_commits_on_all_ranks(self):
        for nodes in (1,2):
            spec,facts,prepared,plan,containers,images=fixture(nodes,speculative=True)
            self.assertEqual(plan['schema_version'],4)
            for rank in range(nodes):
                expected=runtime.rank_spec(plan,rank)
                self.assertEqual(len(expected['mounts']),2)
                self.assertIn('/snapshots/'+'e'*40, ' '.join(expected['engine_args']))
                self.assertNotIn('pulsar-snapshot:', ' '.join(runtime.docker_argv(plan,rank)))
                observed=runtime.observe_rank(plan,rank,containers[rank],images[rank])
                self.assertEqual(set(observed['snapshots']),{'target','draft'})
                for mutation in ('missing','source','writable','shadow'):
                    changed=copy.deepcopy(containers[rank])
                    if mutation=='missing': changed['Mounts'].pop()
                    elif mutation=='source': changed['Mounts'][-1]['Source']='/var/tmp/wrong'
                    elif mutation=='writable': changed['Mounts'][-1]['RW']=True
                    else: changed['Mounts'].append({**changed['Mounts'][-1],'Destination':changed['Mounts'][-1]['Destination']+'/snapshots'})
                    with self.subTest(nodes=nodes,rank=rank,mutation=mutation),self.assertRaisesRegex(ValueError,'mount'):
                        runtime.observe_rank(plan,rank,changed,images[rank])
            del prepared['snapshots']['draft']['ranks'][-1]
            with self.assertRaises(ValueError): runtime.build_plan(spec,spec['spec_id'],facts,prepared)

    def test_schema_two_bare_speculative_models_cannot_enter_a_launch_plan(self):
        for nodes in (1,2):
            for arguments in (['--speculative_config.model','example/draft'],
                              ['--speculative-config','{"model":"example/draft"}']):
                with self.subTest(nodes=nodes,arguments=arguments):
                    spec,facts,prepared,_,_,_=fixture(nodes)
                    # An existing schema-2 spec remains valid independently of launch support.
                    spec=serving.apply_overrides(spec,{'engine_args':spec['recipe']['engine_args']+arguments})
                    prepared['spec_id']=spec['spec_id']
                    with self.assertRaisesRegex(ValueError,'speculative model must reference'):
                        runtime.build_plan(spec,spec['spec_id'],facts,prepared)

    def test_schema_two_non_checkpoint_speculation_keeps_launch_argument_tokens(self):
        for nodes in (1,2):
            for method in ('ngram','mtp'):
                with self.subTest(nodes=nodes,method=method):
                    spec,facts,prepared,_,_,_=fixture(nodes)
                    arguments=spec['recipe']['engine_args']+['--speculative-config',
                        '{ "method": "'+method+'", "num_speculative_tokens": 3 }']
                    spec=serving.apply_overrides(spec,{'engine_args':arguments})
                    prepared['spec_id']=spec['spec_id']
                    plan=runtime.build_plan(spec,spec['spec_id'],facts,prepared)
                    self.assertEqual(spec['schema_version'],2)
                    self.assertEqual(plan['schema_version'],3)
                    for rank in range(nodes):
                        self.assertEqual(runtime.rank_spec(plan,rank)['engine_args'],arguments)

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

    def test_extra_and_duplicate_ulimits_cannot_disappear_from_evidence(self):
        _,_,_,plan,containers,images=fixture()
        for extra in ({'Name':'nofile','Soft':128,'Hard':128},
                      {'Name':'memlock','Soft':-1,'Hard':-1}):
            container=copy.deepcopy(containers[0])
            container['HostConfig']['Ulimits'].append(extra)
            with self.subTest(name=extra['Name']),self.assertRaisesRegex(ValueError,'ulimits'):
                runtime.observe_rank(plan,0,container,images[0])

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
