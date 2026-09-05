"""Explicit migration of recognized legacy prepared copies and their pins.

Only the retained schema-3 ownership format is understood here. Old profiles
never authorize a recipe; the supplied current spec owns that identity.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from release_spec import load_spec, pretty_json_bytes, verify_spec
from .integrity import StorageError, directory, read_json, verify_manifest, verify_tree
from .local import begin_staging, finish_staging, home_record, payload, prepared_record, _overlap
from .migration import _absolute, _materialize, verify_legacy
from .state import Store, view_key

FIELDS = {'schema_version', 'state', 'profile', 'model_id', 'revision', 'identity_key',
          'home_node_id', 'topology_id', 'content_id', 'content_digest', 'integrity',
          'validation', 'backend', 'bytes_logical', 'activated_at', 'pinned', 'budget_bytes_accounted'}


def legacy_stamp(instance, legacy_root, topology_id, manifest):
    """Validate only the known retired ownership layout, without importing it."""
    instance, legacy_root = _absolute(instance), _absolute(legacy_root)
    stamp = read_json(instance / '.pulsar/hot.json')
    if not isinstance(stamp, dict) or not FIELDS <= set(stamp) <= FIELDS | {'transport'}:
        raise StorageError('legacy prepared metadata fields are unknown or incomplete; prepare again')
    if type(stamp['schema_version']) is not int or stamp['schema_version'] != 3:
        raise StorageError('legacy prepared format is unsupported; prepare again')
    for key in ('profile', 'model_id', 'identity_key', 'home_node_id', 'topology_id',
                'content_id', 'backend', 'activated_at'):
        if not isinstance(stamp[key], str) or not stamp[key] or any(c in stamp[key] for c in '\0\n\r'):
            raise StorageError(f'legacy ownership {key} is invalid')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', stamp['profile']):
        raise StorageError('legacy profile path is invalid')
    if not isinstance(topology_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', topology_id):
        raise StorageError('legacy topology path is invalid')
    if stamp['topology_id'] != topology_id:
        raise StorageError('legacy topology differs from explicit selected mapping')
    if type(stamp['pinned']) is not bool or stamp['state'] not in ('ready', 'pinned') or (
            stamp['state'] == 'pinned') != stamp['pinned']:
        raise StorageError('legacy pin ownership is ambiguous; retain old files and prepare again')
    expected_identity = f"{manifest['model_id']}@{manifest['snapshot_revision']}"
    if (stamp['model_id'], stamp['revision'], stamp['identity_key'], stamp['content_digest']) != (
            manifest['model_id'], manifest['snapshot_revision'], expected_identity, manifest['manifest_id']):
        raise StorageError('legacy identity differs from the supplied complete manifest')
    if stamp['integrity'] != {'scheme': 'sha256-snapshot-manifest-v1', 'manifest': manifest}:
        raise StorageError('legacy integrity manifest differs from supplied manifest')
    observed = {key: manifest[key] for key in ('model_id', 'snapshot_revision', 'manifest_id')}
    if stamp['validation'] != {'identity_status': 'receipt-occupancy', 'expected_seal': None, 'observed_seal': observed}:
        raise StorageError('legacy recorded identity provenance is unknown or damaged')
    for key in ('bytes_logical', 'budget_bytes_accounted'):
        if type(stamp[key]) is not int or stamp[key] != manifest['total_bytes']:
            raise StorageError('legacy size accounting differs from the supplied manifest')
    content = hashlib.sha256(f'{expected_identity}|validation:receipt-occupancy|{manifest["manifest_id"]}'.encode()).hexdigest()[:12]
    if stamp['content_id'] != content or instance != legacy_root / f'{stamp["profile"]}-{topology_id[:12]}' / content:
        raise StorageError('legacy prepared root is not the recognized owned instance layout')
    if 'transport' in stamp and (not isinstance(stamp['transport'], str) or not stamp['transport']):
        raise StorageError('legacy transport metadata is damaged')
    return stamp


def preview_view(*, manifest, spec, legacy_instance, legacy_root, legacy_topology_id,
                 topology_id, node_id, rank, destination_root, state_root, legacy_home_hub=None):
    manifest, spec = verify_manifest(manifest), verify_spec(spec)
    if spec['identity']['snapshot_manifest'] != manifest:
        raise StorageError('current spec and supplied manifest differ')
    if not isinstance(topology_id, str) or not topology_id or type(rank) is not int or not 0 <= rank < spec['identity']['geometry']['nodes']:
        raise StorageError('explicit current topology and serving rank are required')
    key = view_key(spec['spec_id'], node_id)
    instance, root = _absolute(legacy_instance), _absolute(destination_root)
    store = Store(state_root)
    if _overlap(store.root, instance):
        raise StorageError('new state must not overlap the legacy instance')
    stamp = legacy_stamp(instance, legacy_root, legacy_topology_id, manifest)
    home = store.home(manifest['manifest_id'])
    if home is None:
        raise StorageError('migrate or acquire the verified home before importing prepared copies')
    if store.get('views', key) is not None:
        raise StorageError('a current prepared view already exists; retain its current pin and use normal preparation controls')
    home_view = home['node_id'] == node_id
    from .filesystem import require_serving_filesystem
    require_serving_filesystem(home['path'] if home_view else root)
    if spec['identity']['geometry']['nodes'] == 1 and not home_view:
        raise StorageError('one-node prepared view must reference its migrated home')
    hub = instance / 'hub' / ('models--' + manifest['model_id'].replace('/', '--'))
    if hub.is_symlink():
        if not home_view or legacy_home_hub is None:
            raise StorageError('legacy home view requires explicit legacy home hub and the migrated home node')
        expected_hub = _absolute(legacy_home_hub)
        target = Path(os.readlink(hub))
        actual_hub = Path(os.path.normpath(target if target.is_absolute() else hub.parent / target))
        if actual_hub != expected_hub:
            raise StorageError('legacy home link differs from explicit selected home')
        with directory(hub.parent):
            pass
        hub = expected_hub
    elif legacy_home_hub is not None:
        raise StorageError('explicit legacy home hub applies only to a legacy home link')
    verified = verify_legacy(hub, payload(hub, manifest), manifest)
    if home_view:
        destination = Path(home['hub_path'])
        verify_tree(Path(home['path']), manifest, full=True)
        method = 'home-reference'
    else:
        with directory(root) as fd:
            device = os.fstat(fd).st_dev
        destination = root / spec['spec_id']
        if destination.exists() or destination.is_symlink():
            raise StorageError('new prepared destination exists; inspect and explicitly prepare or purge')
        if _overlap(instance, destination) or _overlap(hub, destination) or _overlap(store.root, destination):
            raise StorageError('prepared migration source, destination and state must not overlap')
        method = 'hardlink' if all(v['metadata'][0] == device for v in verified['files'].values()) else 'copy'
    plan = {'schema_version': 1, 'kind': 'pulsar-view-migration-plan', 'spec_id': spec['spec_id'],
        'snapshot_manifest_id': manifest['manifest_id'], 'legacy_instance': str(instance),
        'legacy_hub': str(hub), 'legacy_topology_id': legacy_topology_id,
        'legacy_stamp_sha256': hashlib.sha256(pretty_json_bytes(stamp)).hexdigest(),
        'topology_id': topology_id, 'node_id': node_id, 'rank': rank, 'pinned': stamp['pinned'],
        'home_node_id': home['node_id'], 'destination': str(destination), 'is_home_view': home_view,
        'method': method, 'bytes_to_copy': manifest['total_bytes'] if method == 'copy' else 0,
        'source_verification': verified, 'legacy_metadata': 'retained; never modified'}
    plan['plan_digest'] = hashlib.sha256(pretty_json_bytes(plan)).hexdigest()
    return plan


def apply_view(*, expected_plan_digest, guard, **arguments):
    manifest, spec = verify_manifest(arguments['manifest']), verify_spec(arguments['spec'])
    store = Store(arguments['state_root'])
    if (_overlap(store.root, arguments['legacy_instance']) or
            (arguments.get('legacy_home_hub') is not None and _overlap(store.root, arguments['legacy_home_hub']))):
        raise StorageError('new state must not overlap the legacy instance')
    with store.lock():
        plan = preview_view(**arguments)
        if plan['plan_digest'] != expected_plan_digest:
            raise StorageError('view migration plan changed; inspect a new preview')
        destination = Path(plan['destination'])
        paths = [Path(plan['legacy_instance']), Path(plan['legacy_hub']), destination]
        guard(plan['node_id'], paths, plan['topology_id'], plan['rank'])
        if plan['is_home_view']:
            checked = verify_tree(payload(destination, manifest), manifest, full=True)
        else:
            stage = begin_staging(destination.parent)
            try:
                _materialize(plan['source_verification']['files'], payload(stage, manifest), manifest,
                             hardlink=plan['method'] == 'hardlink', archive=False)
                verify_tree(payload(stage, manifest), manifest, full=True)
                verify_legacy(Path(plan['legacy_hub']), payload(Path(plan['legacy_hub']), manifest), manifest)
                # Source pin and ownership must still be the reviewed values before publishing.
                current = legacy_stamp(arguments['legacy_instance'], arguments['legacy_root'],
                                       arguments['legacy_topology_id'], manifest)
                if hashlib.sha256(pretty_json_bytes(current)).hexdigest() != plan['legacy_stamp_sha256']:
                    raise StorageError('legacy pin or ownership changed during conversion')
                guard(plan['node_id'], paths, plan['topology_id'], plan['rank'])
                checked = finish_staging(stage, destination, manifest)
            except Exception as exc:
                raise StorageError(f'prepared migration incomplete; owned staging retained at {stage}: {exc}') from exc
        if plan['is_home_view']:
            current = legacy_stamp(arguments['legacy_instance'], arguments['legacy_root'],
                                   arguments['legacy_topology_id'], manifest)
            if hashlib.sha256(pretty_json_bytes(current)).hexdigest() != plan['legacy_stamp_sha256']:
                raise StorageError('legacy pin or ownership changed during conversion')
            guard(plan['node_id'], paths, plan['topology_id'], plan['rank'])
        view = prepared_record(home_record(manifest, plan['node_id'], destination, checked),
            spec_id=spec['spec_id'], topology_id=plan['topology_id'], rank=plan['rank'],
            is_home_view=plan['is_home_view'], pinned=plan['pinned'])
        store.put('views', view_key(spec['spec_id'], plan['node_id']), view, replace=False)
        return {'schema_version': 1, 'kind': 'pulsar-view-migration-result', 'spec_id': spec['spec_id'],
                'snapshot_manifest_id': manifest['manifest_id'], 'pinned': plan['pinned'],
                'method': plan['method'], 'destination': str(destination), 'legacy_metadata': plan['legacy_metadata']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('preview-view', 'apply-view'))
    for name in ('manifest', 'spec-file', 'legacy-instance', 'legacy-root', 'destination-root', 'state-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--legacy-home-hub', type=Path)
    parser.add_argument('--legacy-topology-id', required=True)
    parser.add_argument('--topology-id', required=True)
    parser.add_argument('--node', dest='node_id', required=True)
    parser.add_argument('--rank', type=int, required=True)
    parser.add_argument('--expected-plan-digest')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        arguments = {k: getattr(args, k) for k in ('legacy_instance', 'legacy_root', 'destination_root',
            'state_root', 'legacy_home_hub', 'legacy_topology_id', 'topology_id', 'node_id', 'rank')}
        arguments.update(manifest=read_json(args.manifest.absolute()), spec=load_spec(args.spec_file))
        def guard(node, paths, topology, rank):
            home = Store(args.state_root).home(arguments['manifest']['manifest_id'])
            if home is None:
                raise StorageError('migrated home is no longer registered')
            command = [str(Path(__file__).resolve().parents[1] / 'scripts/guard-storage.sh'), '--local-only',
                '--node', node, '--expected-topology-id', topology, '--expected-rank', str(rank),
                '--spec-file', str(args.spec_file.absolute()), '--expected-home-node', home['node_id'], '--json']
            for path in paths:
                command += ['--path', str(path)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            if json.loads(result.stdout).get('safe') is not True:
                raise StorageError('current node/topology and storage safety were not established')
        if args.command == 'preview-view':
            result = preview_view(**arguments)
            guard(result['node_id'], [Path(result['legacy_instance']), Path(result['legacy_hub']),
                                     Path(result['destination'])], result['topology_id'], result['rank'])
        else:
            if not args.yes or not args.expected_plan_digest:
                raise StorageError('apply-view requires --yes and reviewed --expected-plan-digest')
            result = apply_view(**arguments, expected_plan_digest=args.expected_plan_digest, guard=guard)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print('Prepared-copy migration ' + ('preview' if args.command == 'preview-view' else 'complete'))
            print(f"  Method: {result['method']}\n  Pinned: {str(result['pinned']).lower()}")
            print(f"  Destination:\n    {result['destination']}")
            if 'plan_digest' in result:
                print(f"  Plan digest:\n    {result['plan_digest']}")
            print('  Legacy records and files remain in place.')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'view migration: {exc}', file=sys.stderr)
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
