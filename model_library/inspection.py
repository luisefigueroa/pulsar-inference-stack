"""Prepared-copy inspection planning and deterministic result assembly.

The caller owns node selection, transport and record updates. These functions
only join canonical specs, existing records and completed verification results.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from release_spec.serving import required_snapshots, verify_spec
from .integrity import StorageError, read_json
from .preparation_verification import cache_path
from .state import validate_home, validate_view


def plan(store, spec, node_ids, topology_id, *, full=False, cache=None):
    spec=verify_spec(spec)
    if len(node_ids)!=spec['recipe']['geometry']['nodes'] or len(set(node_ids))!=len(node_ids):
        raise StorageError('inspection requires the exact selected physical nodes')
    snapshots=required_snapshots(spec)
    names=['target']+sorted(name for name in snapshots if name!='target')
    views=store.views(spec_id=spec['spec_id'])
    jobs=[]; physical={}; members={}
    for name in names:
        manifest=snapshots[name]['snapshot_manifest']
        home=store.home(manifest['manifest_id'])
        member={'home':home,'manifest':manifest,'ranks':[],'rc':0,'error':''}
        members[name]=member
        if home is None:
            member.update(rc=1,error='no home is registered; acquire or restore the exact snapshot')
            continue
        if home['node_id'] not in node_ids:
            member.update(rc=1,error='home is outside selected serving nodes; explicitly move it first')
            continue
        selected=[]
        for rank,node in enumerate(node_ids):
            rows=[v for v in views if v['node_id']==node and v['rank']==rank
                  and v['topology_id']==topology_id and v['snapshot_manifest_id']==manifest['manifest_id']]
            if len(rows)!=1:
                member.update(rc=1,error=f'rank {rank}: required prepared copy is missing or placement changed')
                break
            row=rows[0]
            if node==home['node_id']:
                if not row['is_home_view'] or any(row[key]!=home[key] for key in ('path','hub_path')):
                    member.update(rc=2,error='home node must use its verified home directly')
                    break
            elif row['is_home_view']:
                member.update(rc=2,error='non-home node requires a verified working copy')
                break
            selected.append(row)
        if member['rc']:
            continue
        for rank,row in enumerate(selected):
            record=home if row['is_home_view'] else row
            key=(record['node_id'],record['snapshot_manifest_id'],record['path'])
            if key not in physical:
                candidate=record
                mode=full
                if cache:
                    path=cache_path(cache,record)
                    if path.exists():
                        candidate={**record,**read_json(path)}
                        mode=False
                index=len(jobs);physical[key]=index
                jobs.append({'index':index,'node_slot':rank,'record':record,'candidate':candidate,
                             'manifest':manifest,'full':mode})
            index=physical[key]
            if jobs[index]['record']['hub_path']!=record['hub_path']:
                raise StorageError('one physical copy has conflicting hub identities')
            member['ranks'].append({'record':row,'job':index})
            if row['is_home_view']: member['home_job']=index
    return {'spec_id':spec['spec_id'],'spec_schema':spec['schema_version'],'topology_id':topology_id,
            'node_ids':node_ids,'members':members,'jobs':jobs}


def write_jobs(value, directory):
    root=Path(directory)/'jobs';root.mkdir()
    if any(member['rc'] for member in value['members'].values()):
        batch={'first_error':None,'returncode':1,'results':[
            {'index':job['index'],'returncode':125,'error':'verification not started because the prepared set is incomplete'}
            for job in value['jobs']]}
        (Path(directory)/'batch.json').write_text(json.dumps(batch))
        return assemble(value,directory,batch)
    for job in value['jobs']:
        record=job['candidate']
        request={'operation':'verify','manifest':job['manifest'],'path':record['path'],
                 'stamp':record['verification'],'full':job['full'],'for_runtime':True}
        for suffix,data in (('request',request),('record',job['record']),('candidate',record)):
            (root/f"{job['index']}.{suffix}.json").write_text(json.dumps(data))
        print(f"{job['index']}\t{job['node_slot']}")
    return 0


def assemble(value, directory, batch):
    root=Path(directory); members={}; first=0
    results={row['index']:row for row in batch['results']}
    for name in ['target']+sorted(name for name in value['members'] if name!='target'):
        member=value['members'][name]
        rc,error=member['rc'],member['error']
        if not rc:
            failed=[(row,results.get(row['job'],{'returncode':125})) for row in member['ranks']
                    if results.get(row['job'],{'returncode':125})['returncode']!=0]
            if failed:
                actual=[item for item in failed if item[1]['returncode'] not in (125,129,130,143)]
                binding,result=(actual or failed)[0]
                code=result['returncode']
                rc=255 if code in (125,129,130,143,255) else 2
                error=f"rank {binding['record']['rank']}: "+result.get('error','verification was not completed after another failure')
        prepared=None
        if not rc:
            home=read_json(root/'jobs'/f"{member['home_job']}.verified.json")
            validate_home(home)
            ranks=[]
            for binding in member['ranks']:
                verified=read_json(root/'jobs'/f"{binding['job']}.verified.json")
                row={**binding['record'],'verification':verified['verification'],'verified_at':verified['verified_at']}
                validate_view(row);ranks.append(row)
            prepared={'schema_version':1,'kind':'pulsar-prepared-set','spec_id':value['spec_id'],
                      'snapshot_manifest_id':member['manifest']['manifest_id'],'topology_id':value['topology_id'],
                      'home_node_id':home['node_id'],'revision':member['manifest']['snapshot_revision'],
                      'home':home,'ranks':ranks}
        if rc and (not first or (first==255 and rc!=255)): first=rc
        members[name]={'rc':rc,'error':error,'prepared':prepared}
    (root/'members.json').write_text(json.dumps(members))
    if not first:
        result=members['target']['prepared'] if value['spec_schema']==2 else {
            'schema_version':2,'kind':'pulsar-prepared-set','spec_id':value['spec_id'],
            'topology_id':value['topology_id'],'snapshots':{name:row['prepared'] for name,row in members.items()}}
        (root/'prepared.json').write_text(json.dumps(result))
    return first


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['jobs','assemble','member','errors'])
    parser.add_argument('directory');parser.add_argument('name',nargs='?')
    args=parser.parse_args();root=Path(args.directory)
    if args.operation=='jobs':
        return write_jobs(read_json(root/'plan.json'),root)
    if args.operation=='assemble':
        return assemble(read_json(root/'plan.json'),root,read_json(root/'batch.json'))
    if args.operation=='errors':
        batch=read_json(root/'batch.json')
        if batch['first_error'] is not None:
            result=batch['results'][batch['first_error']]
            print(result.get('error','verification failed'),file=sys.stderr)
        else:
            for name,member in read_json(root/'members.json').items():
                if member['rc']:
                    print(f"required snapshot {name}: {member['error']}",file=sys.stderr)
        return 0
    member=read_json(root/'members.json')[args.name]
    if member['rc']:
        print(f"required snapshot {args.name}: {member['error']}",file=sys.stderr)
    else:
        print(json.dumps(member['prepared']))
    return member['rc']


if __name__=='__main__':
    try: raise SystemExit(main())
    except (StorageError,OSError,ValueError,KeyError) as exc:
        print(f'inspection: {exc}',file=sys.stderr);raise SystemExit(2)
