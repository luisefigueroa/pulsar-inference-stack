#!/usr/bin/env python3
"""Pure checks joining frozen specs, prepared files and actual containers.

Bash owns confirmed topology, SSH, and process execution. This module accepts
those observed documents and never contacts infrastructure.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from release_spec import load_spec
from scripts.release_consumer import format_shell_assignments
from scripts import launch_plan


from release_spec.serving import identity_fields


def fail(message):raise ValueError(message)


def prepared_set(document,spec,topology_id):
    if spec.get('schema_version') == 3:
        from scripts.container_runtime import prepared_snapshots
        prepared_snapshots(spec, document, topology_id)
        return document
    if not isinstance(document,dict) or type(document.get('schema_version')) is not int or document.get('schema_version') != 1 or document.get('kind')!='pulsar-prepared-set':
        fail('invalid prepared-set document')
    identity=identity_fields(spec);manifest=identity['snapshot_manifest']
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
    doc=prepared_set(document,spec,topology_id)
    if spec.get('schema_version') == 3: doc=doc['snapshots']['target']
    identity=identity_fields(spec);manifest=identity['snapshot_manifest'];first=doc['ranks'][0]
    hub_name='models--'+identity['model_id'].replace('/','--')
    return format_shell_assignments(dict(LIBRARY_VIEW_INSTANCE_DIR=str(Path(first['hub_path']).parent),LIBRARY_VIEW_HUB_PATH=first['hub_path'],LIBRARY_VIEW_HOME_NODE_ID=doc['home_node_id'],LIBRARY_VIEW_CONTENT_ID=manifest['manifest_id'][:12],LIBRARY_VIEW_CONTENT_DIGEST=manifest['manifest_id'],LIBRARY_VIEW_TRANSPORT='ssh-control' if identity['geometry']['nodes']==1 else 'ssh-roce',LIBRARY_VIEW_INTEGRITY_SCHEME='sha256',LIBRARY_VIEW_MODEL_ID=identity['model_id'],LIBRARY_VIEW_REVISION=identity['snapshot_revision'],LIBRARY_VIEW_CONTAINER_MODEL_PATH=f'/root/.cache/huggingface/hub/{hub_name}/snapshots/{identity["snapshot_revision"]}',LIBRARY_VIEW_IDENTITY_STATUS='manifest-verified',LIBRARY_VIEW_VALIDATION_JSON='{}',LIBRARY_VIEW_PINNED='1' if first.get('pinned') else '0'))


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
        if plan.get('schema_version') in (3, 4, 5, 6):
            from datetime import datetime, timezone
            from scripts.container_runtime import observe_rank as observe_current_rank
            if spec['spec_id'] != plan['selected_spec_id']:
                fail('observation selects another spec')
            base=Path(args.observations)
            prepared=prepared_set(json.loads((base/'prepared.json').read_text()),spec,plan['topology_id'])
            if spec['schema_version'] == 3:
                for name, member in prepared['snapshots'].items():
                    for expected, actual in zip(plan['ranks'], member['ranks']):
                        if expected['snapshots'][name]['hub_path'] != actual['hub_path'] or expected['snapshots'][name]['home_node_id'] != member['home_node_id']:
                            fail('recorded snapshot location differs from verified prepared set')
                prepared=prepared['snapshots']['target']
            for expected,actual in zip(plan['ranks'],prepared['ranks']):
                if expected['node_id'] != actual['node_id'] or expected['hub_path'] != actual['hub_path']:
                    fail('recorded service files or placement differ from verified prepared set')
            ranks=[]
            for rank in range(len(plan['ranks'])):
                item=observe_current_rank(plan,rank,json.loads((base/f'container-{rank}.json').read_text()),
                                          json.loads((base/f'image-{rank}.json').read_text()))
                item['files_verified']=True
                for snapshot in item.get('snapshots',{}).values(): snapshot['files_verified']=True
                context_file=base/f'context-{rank}.json'
                item['host_context']=json.loads(context_file.read_text()) if context_file.exists() else {'available':False}
                ranks.append(item)
            print(json.dumps(dict(schema_version=3 if spec["schema_version"]==3 else 2,kind='pulsar-serving-observation',
                observed_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
                service_id=plan['service_id'],selected_spec_id=plan['selected_spec_id'],spec_id=plan['spec_id'],
                matches_selected_spec=plan['matches_selected_spec'],effective_spec=plan['spec'],
                api_url=args.api_url,served_name=plan['served_name'],ranks=ranks),sort_keys=True))
            return 0
        fail('historical launch plans cannot be used for new observations')
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'error: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
