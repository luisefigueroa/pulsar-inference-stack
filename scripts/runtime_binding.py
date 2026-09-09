#!/usr/bin/env python3
"""Pure checks joining frozen specs, prepared files and actual containers.

Bash owns confirmed topology, SSH, and process execution. This module accepts
those observed documents and never contacts infrastructure.
"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from release_spec import (load_spec, runtime_contract_id, canonical_json_digest,
                          nccl_qps_from_identity)
from scripts.release_consumer import spec_profile_variables, format_shell_assignments
from scripts import launch_plan


def fail(message):raise ValueError(message)


def prepared_set(document,spec,topology_id):
    if not isinstance(document,dict) or type(document.get('schema_version')) is not int or document.get('schema_version') != 1 or document.get('kind')!='pulsar-prepared-set':
        fail('invalid prepared-set document')
    identity=spec['identity'];manifest=identity['snapshot_manifest']
    for key,value in [('spec_id',spec['spec_id']),('snapshot_manifest_id',manifest['manifest_id']),('revision',identity['snapshot_revision']),('topology_id',topology_id)]:
        if not value or document.get(key)!=value:fail(f'prepared {key} differs from selected spec/topology')
    ranks=document.get('ranks')
    if not isinstance(ranks,list) or len(ranks)!=identity['geometry']['nodes']:fail('prepared set must include every expected rank')
    for index,rank in enumerate(ranks):
        if type(rank.get('rank')) is not int or rank['rank']!=index:fail('prepared ranks must be ordered exact job slots')
        for key in ('node_id','hub_path','path'):
            if not isinstance(rank.get(key),str) or not rank[key]:fail(f'prepared rank missing {key}')
        if not Path(rank['hub_path']).is_absolute() or Path(rank['path'])!=Path(rank['hub_path'])/'snapshots'/identity['snapshot_revision']:
            fail('prepared rank paths are not the exact snapshot below the hub')
    if len({rank['node_id'] for rank in ranks}) != len(ranks):fail('prepared ranks repeat a physical node')
    if not isinstance(document.get('home_node_id'),str) or not document['home_node_id']:fail('prepared set requires home location')
    return document


def prepared_shell(document,spec,topology_id):
    doc=prepared_set(document,spec,topology_id);identity=spec['identity'];manifest=identity['snapshot_manifest'];first=doc['ranks'][0]
    hub_name='models--'+identity['model_id'].replace('/','--')
    return format_shell_assignments(dict(LIBRARY_VIEW_INSTANCE_DIR=str(Path(first['hub_path']).parent),LIBRARY_VIEW_HUB_PATH=first['hub_path'],LIBRARY_VIEW_HOME_NODE_ID=doc['home_node_id'],LIBRARY_VIEW_CONTENT_ID=manifest['manifest_id'][:12],LIBRARY_VIEW_CONTENT_DIGEST=manifest['manifest_id'],LIBRARY_VIEW_TRANSPORT='ssh-control' if identity['geometry']['nodes']==1 else 'ssh-roce',LIBRARY_VIEW_INTEGRITY_SCHEME='sha256',LIBRARY_VIEW_MODEL_ID=identity['model_id'],LIBRARY_VIEW_REVISION=identity['snapshot_revision'],LIBRARY_VIEW_CONTAINER_MODEL_PATH=f'/root/.cache/huggingface/hub/{hub_name}/snapshots/{identity["snapshot_revision"]}',LIBRARY_VIEW_IDENTITY_STATUS='manifest-verified',LIBRARY_VIEW_VALIDATION_JSON='{}',LIBRARY_VIEW_PINNED='1' if first.get('pinned') else '0'))


def bind_plan(facts,spec_path,prepared):
    spec=load_spec(spec_path);identity=spec['identity']
    doc=prepared_set(prepared,spec,facts.get('topology_id'))
    expected=spec_profile_variables(spec,dict(port=facts['port'],served_name=facts['served_name']),facts['image'])
    runtime=facts['runtime']
    for field,key in [('engine_args','ENGINE_ARGS'),('container_env','CONTAINER_ENV')]:
        if runtime[field] != expected[key]:fail(f'launch recipe {field} differs from selected spec')
    if runtime['extra_env'] or runtime['vllm_extra_args'] or runtime['spec_decode_args'] or facts['spec_decode']['enabled']:
        fail('recipe overrides require a new candidate spec')
    for key in ('hf_hub_offline','vllm_logging_level','restart_policy','health_start_period','nccl_debug','master_port'):
        if runtime.get(key,launch_plan.DEFAULT_RUNTIME[key]) != launch_plan.DEFAULT_RUNTIME[key]:
            fail(f'implicit runtime setting {key} differs from fixed stack defaults; encode recipe environment in the spec')
    if runtime.get('nccl_ib_qps',launch_plan.DEFAULT_RUNTIME['nccl_ib_qps']) != nccl_qps_from_identity(identity):
        fail('launch NCCL QPs differ from the selected recipe identity')
    if Decimal(str(facts['gpu_mem_util']))!=Decimal(expected['GPU_MEM_UTIL']):fail('launch GPU memory setting differs from spec')
    if facts['image'].split('@')[-1]!=identity['image']['digest']:fail('launch image differs from spec')
    if facts['profile']!=spec['spec_id'] or facts['nodes']!=identity['geometry']['nodes'] or facts['platform_id']!=identity['geometry']['platform_id']:
        fail('launch model or geometry differs from spec')
    if facts['launch_contract_id']!=runtime_contract_id(spec):fail('launch contract identity differs from spec')
    if len(facts['ranks'])!=len(doc['ranks']):fail('launch ranks differ from prepared set')
    for rank,prepared_rank in zip(facts['ranks'],doc['ranks']):
        if rank['rank']!=prepared_rank['rank'] or rank['node_id']!=prepared_rank['node_id']:fail('prepared rank placement differs from confirmed launch topology')
        rank['hub_path']=prepared_rank['hub_path']
    return launch_plan.build_launch_plan(facts)


def observe_rank(plan,rank,container,image,spec):
    expected=launch_plan.rank_container_spec(plan,rank)
    labels=container.get('Config',{}).get('Labels') or {}
    for key,value in expected['labels'].items():
        if labels.get(key)!=value:fail(f'rank {rank}: ownership or identity label differs ({key})')
    if container.get('State',{}).get('Running') is not True:fail(f'rank {rank}: container is not running')
    if container.get('Image')!=image.get('Id') or not image.get('Id'):fail(f'rank {rank}: actual image identity differs')
    digest=spec['identity']['image']['digest']
    if not any(value.endswith('@'+digest) for value in image.get('RepoDigests',[])):
        fail(f'rank {rank}: image content digest is not the pinned spec image')
    if container.get('Config',{}).get('Image')!=plan['image']:fail(f'rank {rank}: container image reference differs')
    argv=launch_plan.rank_docker_argv(plan,rank,detach=True)
    image_index=argv.index(plan['image'])
    command=argv[image_index+1:]
    if container.get('Config',{}).get('Cmd')!=command:fail(f'rank {rank}: actual engine command differs from selected recipe')
    # Image entrypoint is immutable; detect --entrypoint replacement separately.
    if container.get('Config',{}).get('Entrypoint')!=image.get('Config',{}).get('Entrypoint'):
        fail(f'rank {rank}: container entrypoint differs from pinned image')
    env={}
    for item in image.get('Config',{}).get('Env') or []:
        key,_,value=item.partition('=');env[key]=value
    for i,item in enumerate(argv[:image_index]):
        if item=='-e':
            key,_,value=argv[i+1].partition('=');env[key]=value
    actual_env={}
    for item in container.get('Config',{}).get('Env') or []:
        key,_,value=item.partition('=')
        if key in actual_env:fail(f'rank {rank}: duplicate container environment key')
        actual_env[key]=value
    if actual_env != env:fail(f'rank {rank}: actual container environment differs')
    mount=expected['mounts'][0]
    matching=[item for item in container.get('Mounts',[]) if item.get('Destination')==mount['target']]
    if len(matching)!=1 or matching[0].get('Source')!=mount['source'] or matching[0].get('RW') is not False:
        fail(f'rank {rank}: model mount differs from verified prepared files')
    if any(item.get('Destination','').startswith(mount['target']+'/') for item in container.get('Mounts',[])):
        fail(f'rank {rank}: nested mount shadows verified files')
    identifier=container.get('Id');started=container.get('State',{}).get('StartedAt')
    if not isinstance(identifier,str) or not identifier or not isinstance(started,str) or not started:
        fail(f'rank {rank}: boot witness unavailable')
    return dict(rank=rank,running=True,owned=True,image_digest=digest,launch_contract_id=runtime_contract_id(spec),boot_witness=canonical_json_digest(dict(container_id=identifier,started_at=started)),snapshot_manifest_id=spec['identity']['snapshot_manifest']['manifest_id'],files_verified=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepared-shell','observe'])
    parser.add_argument('--spec',required=True);parser.add_argument('--topology-id',default='')
    parser.add_argument('--plan');parser.add_argument('--observations');parser.add_argument('--api-url');parser.add_argument('--served-name')
    args=parser.parse_args()
    try:
        spec=load_spec(args.spec)
        if args.command=='prepared-shell':print(prepared_shell(json.load(sys.stdin),spec,args.topology_id));return 0
        plan=launch_plan.validate_launch_plan(json.loads(Path(args.plan).read_text()))
        if plan['launch_contract_id']!=runtime_contract_id(spec):fail('observation plan differs from selected spec')
        observed=[]
        for rank in range(plan['nodes']):
            base=Path(args.observations)
            container=json.loads((base/f'container-{rank}.json').read_text())
            image=json.loads((base/f'image-{rank}.json').read_text())
            observed.append(observe_rank(plan,rank,container,image,spec))
        print(json.dumps(dict(schema_version=1,kind='pulsar-serving-observation',spec_id=spec['spec_id'],launch_contract_id=runtime_contract_id(spec),snapshot_manifest_id=spec['identity']['snapshot_manifest']['manifest_id'],api_url=args.api_url,served_name=args.served_name,ranks=observed),sort_keys=True))
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'error: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
