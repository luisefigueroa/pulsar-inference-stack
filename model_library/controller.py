"""Controller-side manifest records and pure action planning for the Bash CLI."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from release_spec import load_spec
from .integrity import StorageError, read_json, verify_manifest
from .local import prepared_record
from .planning import preparation_plan, purge_plan, removal_plan
from .state import Store, checked_id, now, validate_home, validate_view, view_record_key


from release_spec.serving import identity_fields, required_snapshots


def resolve(repo: Path, query: dict) -> dict:
    if query.get('spec_file'):
        spec=load_spec(query['spec_file'])
        from release_spec.serving import verify_spec as require_current
        require_current(spec)
        if query.get('spec_id') and query['spec_id']!=spec['spec_id']:
            raise StorageError('selected spec id differs from supplied spec file')
        snapshots = required_snapshots(spec)
        name = query.get('snapshot') or 'target'
        if name not in snapshots: raise StorageError('unknown required snapshot: ' + name)
        return {'spec':spec,'manifest':snapshots[name]['snapshot_manifest'],'snapshots':snapshots}
    if query.get('spec_id'):
        spec=load_spec(repo/'releases'/f"{checked_id(query['spec_id'])}.json")
        from release_spec.serving import verify_spec as require_current
        require_current(spec)
        snapshots = required_snapshots(spec)
        name = query.get('snapshot') or 'target'
        if name not in snapshots: raise StorageError('unknown required snapshot: ' + name)
        return {'spec':spec,'manifest':snapshots[name]['snapshot_manifest'],'snapshots':snapshots}
    if query.get('manifest_file'):
        if query.get('snapshot'): raise StorageError('--snapshot requires a frozen spec')
        return {'spec':None,'manifest':verify_manifest(read_json(query['manifest_file']))}
    raise StorageError('select a spec id, --spec-file, or an explicit source --manifest')


def published(repo: Path, manifest_id: str) -> bool:
    # Invalid release files abort rather than making deletion look unpromoted.
    for path in sorted((repo/'releases').glob('*.json')):
        spec=load_spec(path)
        if any(v['snapshot_manifest']['manifest_id']==manifest_id for v in required_snapshots(spec).values()):
            return True
    return False


def run(store: Store, repo: Path, request: dict) -> dict | list:
    op=request['operation']
    if op=='resolve':
        return resolve(repo,request)
    if op=='homes':
        return [validate_home(v) for v in store.records('homes')]
    if op=='views':
        return store.views(manifest_id=request.get('snapshot_manifest_id'),spec_id=request.get('spec_id'))
    if op=='home':
        return {'home':store.home(request['snapshot_manifest_id'])}
    if op=='refresh-verification':
        record=request['record']
        if record.get('kind')=='pulsar-home':
            validate_home(record); namespace='homes'; key=record['snapshot_manifest_id']
        else:
            validate_view(record); namespace='views'; key=view_record_key(record)
        previous=store.get(namespace,key)
        if previous is None: return {'refreshed':False}
        fields=('snapshot_manifest_id','node_id','path','hub_path')
        if any(previous[f]!=record[f] for f in fields):
            raise StorageError('record changed during verification')
        # A cache refresh cannot change retention, topology, or ownership.
        previous['verification']=record['verification']
        previous['verified_at']=record['verified_at']
        store.put(namespace,key,previous)
        return {'refreshed':True}
    if op=='save-home':
        manifest=verify_manifest(request['manifest'])
        home=validate_home(request['home'])
        if home['snapshot_manifest_id']!=manifest['manifest_id']:
            raise StorageError('home registration and expected manifest differ')
        previous=store.home(manifest['manifest_id'])
        if previous and (previous['node_id'],previous['path'])!=(home['node_id'],home['path']):
            if request.get('expected_home')!=previous:
                raise StorageError('another home is registered; use explicit verified move')
        store.save_manifest(manifest)
        store.put('homes',manifest['manifest_id'],home)
        return {'home':home}
    if op=='save-views':
        manifest=verify_manifest(request['manifest'])
        records=[validate_view(v) for v in request['views']]
        expected=request['expected_node_ids']
        if [v['node_id'] for v in sorted(records,key=lambda v:v['rank'])]!=expected:
            raise StorageError('prepared publication does not cover every selected rank')
        if [v['rank'] for v in sorted(records,key=lambda v:v['rank'])]!=list(range(len(expected))):
            raise StorageError('prepared publication has missing or duplicate rank roles')
        if any(v['snapshot_manifest_id']!=manifest['manifest_id'] or v['spec_id']!=request['spec_id'] for v in records):
            raise StorageError('prepared publication identity differs')
        store.save_manifest(manifest)
        for record in records:
            store.put('views',view_record_key(record),record)
        return {'prepared':len(records),'spec_id':request['spec_id']}
    if op=='record-view':
        return prepared_record(request['home'],spec_id=request['spec_id'],topology_id=request['topology_id'],rank=request['rank'],is_home_view=request.get('is_home_view',False),pinned=request.get('pinned',False),schema_version=request.get('view_schema',1))
    if op=='pin':
        records=store.views(spec_id=request['spec_id'])
        if not records:
            raise StorageError('no prepared copies to pin or unpin')
        for record in records:
            record['pinned']=request['pinned']
            store.put('views',view_record_key(record),record)
        return {'spec_id':request['spec_id'],'pinned':request['pinned'],'copies':len(records)}
    if op=='forget-view':
        record=validate_view(request['view'])
        key=view_record_key(record)
        previous=store.get('views',key)
        fields=('spec_id','node_id','rank','topology_id','hub_path','path','snapshot_manifest_id','pinned','is_home_view')
        if previous is not None and any(previous[f]!=record[f] for f in fields):
            raise StorageError('prepared record changed before removal')
        store.remove('views',key)
        return {'removed_record':True}
    if op=='forget-home':
        home=validate_home(request['home'])
        if store.home(home['snapshot_manifest_id'])!=home:
            raise StorageError('home record changed before removal')
        store.remove('homes',home['snapshot_manifest_id'])
        return {'removed_record':True}
    if op=='plan-prepare':
        return preparation_plan(spec=request['spec'],home=request['home'],node_ids=request['node_ids'],topology_id=request['topology_id'],observations=request['observations'],views=request.get('views',store.views(spec_id=request['spec']['spec_id'])),budgets=request['budgets'],snapshot=request.get('snapshot','target'))
    if op=='plan-purge':
        return purge_plan(views=request.get('views',store.views(spec_id=request['spec_id'])),node_ids=request['node_ids'],observations=request['observations'])
    if op=='plan-remove':
        home=store.home(request['snapshot_manifest_id'])
        if home is None:
            raise StorageError('no registered home')
        plan=removal_plan(home=home,views=request.get('views',store.views(manifest_id=request['snapshot_manifest_id'])),node_ids=request['node_ids'],observations=request['observations'],published=published(repo,request['snapshot_manifest_id']),archive_verified=request.get('archive_verified',False),discard_unpromoted=request.get('discard_unpromoted',False))
        if request.get('transactions'):
            plan['eligible']=False
            plan['blockers'].append('incomplete preparations still depend on this snapshot; explicitly purge them first')
        return plan
    if op=='save-observation':
        store.put('observations',request['spec_id'],{'schema_version':1,'kind':'pulsar-saved-observation','spec_id':request['spec_id'],'checked_at':now(),**request['observation']})
        return {'saved':True}
    if op=='observation':
        return {'observation':store.get('observations',request['spec_id'])}
    if op=='archive-record':
        result=request['result']
        if result.get('verified') is not True or result.get('snapshot_manifest_id')!=request['snapshot_manifest_id']:
            raise StorageError('archive verification did not prove expected identity')
        store.put('archives',request['snapshot_manifest_id'],{**result,'root':request['root'],'verified_at':now()})
        return result
    raise StorageError(f'unknown controller operation {op}')


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',required=True)
    parser.add_argument('--repo-root',required=True)
    args=parser.parse_args()
    try:
        result=run(Store(args.state_root),Path(args.repo_root),json.load(sys.stdin))
        print(json.dumps(result,sort_keys=True))
        return 0
    except (StorageError,OSError,ValueError,KeyError,TypeError) as exc:
        print(f'model library: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
