"""Structured node-local filesystem operations, transported by the Bash boundary."""
from __future__ import annotations
import json
import os
from pathlib import Path
from contextlib import nullcontext
import shutil
import sys

from .integrity import StorageError, atomic_json, read_json, verify_manifest, verify_tree
from .local import (begin_staging, copy_snapshot, finish_staging, home_record,
                    location, payload, remove_managed_hub, restore, verify_archive)
from .filesystem import require_serving_filesystem
from .state import (Store, checked_id, ensure_directory, validate_home, validate_view,
                    view_record_key, view_destination, shared_view, reconcile_views,
                    copy_references, VIEW_IDENTITY_FIELDS, now)


def run(request: dict) -> dict:
    if sys.version_info < (3, 11):
        raise StorageError('node storage operations require Python 3.11 or newer')
    op = request['operation']
    home_root = Path(os.path.realpath(request.get('home_root') or os.environ.get('PULSAR_HOME_ROOT') or Path.home()/'.cache/pulsar-inference-stack'))
    view_root = Path(os.path.realpath(request.get('view_root') or os.environ.get('PULSAR_HOT_ROOT') or Path.home()/'.cache/pulsar-inference-stack/views'))
    if op == 'path-state':
        path=Path(request['path'])
        if not path.is_absolute() or '..' in path.parts: raise StorageError('invalid managed path')
        try:
            value=path.lstat()
        except FileNotFoundError:
            return {'state':'missing'}
        import stat
        if not stat.S_ISDIR(value.st_mode): raise StorageError('managed path is not a regular directory')
        return {'state':'present'}
    if op == 'roots':
        return {'home_root': str(home_root), 'view_root': str(view_root)}
    if (view_root/'.pulsar-node').is_symlink():
        raise StorageError('node state must not be an internal symlink')
    node_store = Store(view_root/'.pulsar-node')
    owned_operations = {'view-records', 'refresh-view', 'save-view', 'pin-view',
                        'forget-view', 'remove-staging', 'begin-view', 'publish-view',
                        'bind-view', 'remove-view'}
    # Serialize sharing and final deletion on the physical node, including calls
    # from different controllers. A read of absent state must stay read-only.
    lock = (node_store.lock(exclusive=op != 'view-records') if op in owned_operations
            and (op != 'view-records' or node_store.root.exists()) else nullcontext())
    with lock:
        return run_node(request, node_store, home_root, view_root)


def run_node(request: dict, node_store: Store, home_root: Path, view_root: Path) -> dict:
    op = request['operation']
    if op == 'view-records':
        return {'views': node_store.views(spec_id=request.get('spec_id'),manifest_id=request.get('snapshot_manifest_id')),
                'transactions': [v for v in node_store.records('transactions')
                    if (not request.get('spec_id') or v.get('spec_id')==request['spec_id'])
                    and (not request.get('snapshot_manifest_id') or v.get('snapshot_manifest_id')==request['snapshot_manifest_id'])]}
    if op == 'refresh-view':
        record=validate_view(request['view'])
        key=view_record_key(record)
        previous=node_store.get('views',key)
        if previous is None: return {'refreshed':False}
        if any(previous[f]!=record[f] for f in ('spec_id','node_id','path','hub_path','snapshot_manifest_id')):
            raise StorageError('node record changed during verification')
        previous['verification']=record['verification']
        previous['verified_at']=record['verified_at']
        node_store.put('views',key,previous)
        return {'refreshed':True}
    if op in {'save-view', 'pin-view', 'forget-view'}:
        record=validate_view(request['view'])
        key=view_record_key(record)
        previous=node_store.get('views',key)
        if op=='forget-view':
            fields=('spec_id','node_id','rank','topology_id','hub_path','path','snapshot_manifest_id','pinned','is_home_view')
            if previous is not None and any(previous[f]!=record[f] for f in fields):
                raise StorageError('node view changed before cleanup')
            node_store.remove('views',key)
            return {'removed':True}
        if previous and previous.get('pinned') is True and record['pinned'] is False and op!='pin-view':
            raise StorageError('preparation cannot clear an existing pin')
        if previous and previous['schema_version'] == 3:
            record = shared_view(record)
        node_store.put('views',key,record)
        node_store.remove('transactions',key)
        return {'saved':True}
    if op == 'remove-staging':
        import re
        from .integrity import directory
        transaction=request['transaction']
        key=view_record_key(transaction)
        if node_store.get('transactions',key)!=transaction or transaction.get('pinned') is not False:
            raise StorageError('incomplete preparation ownership is not proven')
        if transaction['snapshot_manifest_id']!=request['manifest']['manifest_id']:
            raise StorageError('incomplete preparation identity differs')
        stage=Path(transaction['stage']); destination=Path(transaction['destination'])
        if stage.parent!=view_root or not re.fullmatch(r'\.pending-[0-9a-f]{32}',stage.name) or destination!=view_destination(view_root,transaction):
            raise StorageError('incomplete preparation path escapes managed view root')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise StorageError('platform cannot safely remove staging')
        for path in (stage,destination):
            if not path.exists() and not path.is_symlink(): continue
            with directory(path.parent) as parent:
                info=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
                if [info.st_dev,info.st_ino]!=transaction['hub_identity']:
                    raise StorageError('staging directory was replaced')
                shutil.rmtree(path.name,dir_fd=parent)
                os.fsync(parent)
        node_store.remove('transactions',key)
        return {'removed':True}
    if op == 'space':
        root = Path(request['path'])
        while not root.exists() and root != root.parent:
            root = root.parent
        usage = shutil.disk_usage(root)
        used=0
        target=Path(request['path'])
        if target.exists():
            from .integrity import directory, file_names, file_at
            with directory(target) as fd:
                for name in file_names(fd):
                    with file_at(fd,name) as source: used+=os.fstat(source).st_size
        return {'available': usage.free, 'total': usage.total, 'used':used}
    if op == 'find-source':
        homes=[]
        parent=home_root/'pulsar-homes'
        if not parent.exists() and not parent.is_symlink():
            return {'homes': []}
        from .integrity import directory
        with directory(parent) as fd:
            for name in sorted(os.listdir(fd)):
                if name.startswith('.pending-'):
                    continue
                checked_id(name)
                hub=parent/name
                existing=verify_manifest(read_json(hub/'manifest.json'))
                if existing['manifest_id'] != name:
                    raise StorageError('managed home name and manifest differ')
                if existing['model_id'] == request['model_id'] and existing['snapshot_revision'] == request['snapshot_revision']:
                    require_serving_filesystem(hub)
                    previous = next((validate_home(record) for record in request.get('known_homes', [])
                        if record.get('snapshot_manifest_id') == existing['manifest_id']
                        and record.get('node_id') == request['node_id']
                        and record.get('hub_path') == str(hub)
                        and record.get('path') == str(payload(hub, existing))), None)
                    stamp=verify_tree(payload(hub,existing),existing,
                        stamp=previous['verification'] if previous else None,
                        full=request.get('full', False))
                    home=home_record(existing,request['node_id'],hub,stamp)
                    if stamp['method'] == 'metadata':
                        home['verified_at'] = previous['verified_at']
                    homes.append({'manifest':existing,'home':home})
        return {'homes':homes}
    if op == 'source-verify':
        from .source import verify_download
        result = verify_download(Path(request['path']), request['source'], expected_manifest=request.get('manifest'))
        return {'manifest': result}
    if op == 'source-prepare':
        from .source import prepare_download
        return prepare_download(request['stage'], request['source'])
    if op == 'source-clean':
        from .source import clean_download_metadata
        clean_download_metadata(request['stage'], request['source'])
        return {'cleaned': True}
    if op == 'begin-source':
        require_serving_filesystem(home_root)
        from .source import validate_source
        source = validate_source(request['source'])
        stage = begin_staging(home_root/'pulsar-homes')
        path = stage/'snapshots'/source['snapshot_revision']
        ensure_directory(path)
        return {'stage': str(stage), 'path': str(path)}
    manifest = verify_manifest(request['manifest'])
    view_identity = {'schema_version':request.get('view_schema',1), 'spec_id':request.get('spec_id'),
                     'node_id':request.get('node_id'), 'snapshot_manifest_id':manifest['manifest_id']}
    if op == 'exists':
        require_serving_filesystem(home_root)
        hub = location(home_root, manifest)
        if not hub.exists() and not hub.is_symlink():
            return {'exists': False, 'hub_path': str(hub)}
        if read_json(hub/'manifest.json')!=manifest: raise StorageError('existing home manifest differs')
        return {'exists': True, 'hub_path': str(hub),
                'verification': verify_tree(payload(hub,manifest), manifest, full=True)}
    if op == 'verify':
        if request.get('for_runtime') is True: require_serving_filesystem(request['path'])
        return {'verification': verify_tree(request['path'], manifest,
                    stamp=request.get('stamp'), full=request.get('full', True))}
    if op == 'begin-home':
        require_serving_filesystem(home_root)
        dest = location(home_root,manifest)
        if dest.exists() or dest.is_symlink():
            raise StorageError('home destination already exists; verify and reuse explicitly')
        stage = begin_staging(dest.parent)
        ensure_directory(payload(stage,manifest))
        return {'stage':str(stage),'destination':str(dest),'path':str(payload(stage,manifest))}
    if op == 'publish-home':
        require_serving_filesystem(home_root)
        destination = location(home_root, manifest)
        checked = finish_staging(Path(request['stage']), destination, manifest)
        return {'home': home_record(manifest, request['node_id'], destination, checked)}
    if op == 'begin-view':
        require_serving_filesystem(view_root)
        dest = view_destination(view_root,view_identity)
        if dest.exists() or dest.is_symlink():
            raise StorageError('prepared-view destination exists; inspect and explicitly purge first')
        previous = node_store.get('transactions',view_record_key(view_identity))
        if previous is not None:
            from .integrity import directory
            import re
            if (any(previous.get(k)!=v for k,v in view_identity.items() if k!='schema_version')
                    or previous.get('schema_version',1)!=view_identity['schema_version']
                    or previous.get('rank')!=request['rank'] or previous.get('topology_id')!=request['topology_id']
                    or previous.get('pinned') is not False or previous.get('destination')!=str(dest)):
                raise StorageError('incomplete preparation differs; inspect before retrying')
            stage=Path(previous['stage'])
            if stage.parent!=view_root or not re.fullmatch(r'\.pending-[0-9a-f]{32}',stage.name):
                raise StorageError('incomplete preparation escapes managed storage')
            with directory(stage) as fd:
                observed=os.fstat(fd)
                if [observed.st_dev,observed.st_ino]!=previous['hub_identity']:
                    raise StorageError('incomplete preparation directory was replaced')
            if previous['path']!=str(payload(stage,manifest)):
                raise StorageError('incomplete preparation snapshot path differs')
            return previous
        stage=begin_staging(dest.parent)
        ensure_directory(payload(stage,manifest))
        result={**({'schema_version':2} if request.get('view_schema')==2 else {}),
                'stage':str(stage),'destination':str(dest),'path':str(payload(stage,manifest)),
                'spec_id':request['spec_id'],'snapshot_manifest_id':manifest['manifest_id'],
                'node_id':request['node_id'],'rank':request['rank'],'topology_id':request['topology_id'],
                'pinned':False,'kind':'pulsar-preparation-staging',
                'hub_identity':[stage.stat().st_dev,stage.stat().st_ino]}
        node_store.put('transactions',view_record_key(view_identity),result)
        return result
    if op == 'publish-view':
        require_serving_filesystem(view_root)
        dest=view_destination(view_root,view_identity)
        checked=finish_staging(Path(request['stage']),dest,manifest,reuse_verification=True)
        from .local import prepared_record
        home=home_record(manifest,request['node_id'],dest,checked)
        record=prepared_record(home,spec_id=request['spec_id'],topology_id=request['topology_id'],rank=request['rank'],schema_version=request.get('view_schema',1))
        node_store.put('views',view_record_key(record),record)
        node_store.remove('transactions',view_record_key(record))
        return {'home':home,'view':record}
    if op in {'begin-archive', 'archive'}:
        from .local import _overlap
        archive_target=location(request['archive_root'],manifest,archive=True)
        if any(_overlap(archive_target, boundary) for boundary in (home_root/'pulsar-homes',view_root)):
            raise StorageError('archive location overlaps a managed home or working-copy namespace')
    if op == 'begin-archive':
        root=Path(request['archive_root'])
        if not root.is_dir(): raise StorageError('configured archive directory must already exist')
        dest=location(root,manifest,archive=True)
        if dest.exists() or dest.is_symlink(): raise StorageError('archive destination already exists')
        stage=begin_staging(dest.parent,archive=True)
        ensure_directory(payload(stage,manifest),private=False)
        return {'stage':str(stage),'destination':str(dest),'path':str(payload(stage,manifest))}
    if op == 'publish-archive':
        dest=location(request['archive_root'],manifest,archive=True)
        finish_staging(Path(request['stage']),dest,manifest,archive=True)
        return verify_archive(request['archive_root'],manifest)
    if op == 'archive':
        root = Path(request['archive_root'])
        if not root.is_dir():
            raise StorageError('configured archive directory must already exist')
        hub,_stamp=copy_snapshot(Path(request['path']),root,manifest,archive=True)
        return {**verify_archive(root,manifest),'hub_path':str(hub)}
    if op == 'archive-verify':
        return verify_archive(request['archive_root'],manifest)
    if op == 'restore':
        hub,stamp=restore(request['archive_root'],home_root,manifest)
        return {'home':home_record(manifest,request['node_id'],hub,stamp)}
    if op == 'remove-home':
        remove_managed_hub(Path(request['hub_path']),home_root,manifest,verification=request['verification'])
        return {'removed':True}
    if op == 'remove-view':
        return release_view(request, node_store, view_root, manifest)
    if op == 'bind-view':
        return bind_view(request, node_store, view_root, manifest)
    raise StorageError(f'unknown node operation {op}')


def bind_view(request: dict, store: Store, root: Path, manifest: dict) -> dict:
    source = validate_view(request['source_view'])
    previous = store.get('views', view_record_key(source))
    if previous is None or any(previous[f] != source[f] for f in VIEW_IDENTITY_FIELDS):
        raise StorageError('prepared source ownership changed before binding')
    if source['is_home_view'] or source['snapshot_manifest_id'] != manifest['manifest_id']:
        raise StorageError('binding requires the exact working-copy manifest')
    if source['node_id'] != request['node_id'] or not Path(source['hub_path']).is_relative_to(root) or Path(source['hub_path']) == root:
        raise StorageError('prepared source is outside this node working-copy storage')
    if source['path'] != str(payload(Path(source['hub_path']), manifest)) or read_json(Path(source['hub_path'])/'manifest.json') != manifest:
        raise StorageError('prepared source hub differs from its manifest')
    require_serving_filesystem(source['path'])
    stamp = verify_tree(source['path'], manifest, stamp=source['verification'], full=False)
    source = {**source, 'verification': stamp,
              'verified_at': now() if stamp['method'] == 'sha256' else source['verified_at']}
    target = {k: v for k, v in source.items() if k != 'binding_schema'}
    record = shared_view({**target, 'schema_version': request.get('view_schema', 1),
                          'spec_id': request['spec_id'], 'rank': request['rank'],
                          'topology_id': request['topology_id'], 'pinned': False})
    key = view_record_key(record)
    existing = store.get('views', key)
    if existing and any(existing[f] != record[f] for f in VIEW_IDENTITY_FIELDS):
        raise StorageError('target recipe already owns a different prepared copy')
    if store.get('transactions', key) is not None:
        raise StorageError('target recipe has an incomplete preparation; resume or purge it first')
    # Upgrade every old owner before adding an alias. A failed upgrade leaves
    # protected records and the original files; retry completes the conversion.
    for owner in copy_references(source, store.views()):
        store.put('views', view_record_key(owner), shared_view(owner))
    if existing:
        record['pinned'] = existing['pinned']
    store.put('views', key, record)
    return {'view': record}


def release_view(request: dict, store: Store, root: Path, manifest: dict) -> dict:
    record = validate_view(request['view'])
    if record['is_home_view'] or record['snapshot_manifest_id'] != manifest['manifest_id']:
        raise StorageError('working-copy release requires the exact manifest')
    key = view_record_key(record)
    current = store.get('views', key)
    if current and any(current[f] != record[f] for f in VIEW_IDENTITY_FIELDS):
        raise StorageError('prepared ownership changed before release')
    records = reconcile_views([*request['all_views'], *store.views()])
    owners = copy_references(record, records)
    if record['pinned'] or any(v['pinned'] for v in owners if view_record_key(v) == key):
        raise StorageError('prepared binding is pinned')
    remaining = [v for v in owners if view_record_key(v) != key]
    hub = Path(record['hub_path'])
    removed = False
    if not remaining and (hub.exists() or hub.is_symlink()):
        if current is None:
            raise StorageError('node ownership is missing; refusing physical deletion')
        for pending in store.records('transactions'):
            for field in ('stage', 'destination', 'path'):
                path = Path(pending[field])
                if path == hub or hub in path.parents or path in hub.parents:
                    raise StorageError('an incomplete preparation still references the copy')
        remove_managed_hub(hub, root, manifest, verification=record['verification'])
        removed = True
    # Retain node ownership until the controller has acknowledged forgetting
    # this binding. Lost replies must leave proof for a safe cleanup retry.
    return {'removed': removed, 'release_ready': True, 'retained_bindings': len(remaining)}


def main() -> int:
    try:
        request=json.load(sys.stdin)
        print(json.dumps(run(request),sort_keys=True))
        return 0
    except (StorageError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'model storage: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
