"""Explicit one-time verified reuse of legacy home/archive bytes.

Legacy records locate candidates only. The supplied canonical manifest supplies
expected identity. This module never edits legacy metadata, imports a recipe,
downloads weights, or stops a container. Runtime modules do not read legacy state.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from release_spec import pretty_json_bytes
from .integrity import (StorageError, directory, file_at, metadata, read_json,
                        verify_manifest, verify_tree)
from .local import (begin_staging, finish_staging, home_record, location,
                    payload, verify_archive, _overlap)
from .state import Store, ensure_directory, now


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise StorageError('migration paths must be absolute without parent traversal')
    return path


def _inventory(path):
    """Inventory snapshot paths without following directory links."""
    found = {}
    def visit(fd, prefix):
        for name in sorted(os.listdir(fd)):
            rel = f'{prefix}/{name}' if prefix else name
            observed = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
                found[rel] = metadata(observed)
            else:
                raise StorageError(f'legacy snapshot contains a special file: {rel}')
    with directory(path) as fd:
        visit(fd, '')
    return found


def _resolve_leaf(hub, path):
    """Follow file links only, confined to an explicit legacy hub boundary."""
    seen = set()
    for _ in range(32):
        if path == hub or hub not in path.parents or path in seen:
            raise StorageError('legacy file link escapes its hub or loops')
        seen.add(path)
        with directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                return path
            if not stat.S_ISLNK(info.st_mode):
                raise StorageError('legacy link target is not a regular file')
            target = os.readlink(path.name, dir_fd=parent)
        candidate = Path(target) if os.path.isabs(target) else path.parent / target
        path = Path(os.path.normpath(candidate))
    raise StorageError('legacy file link chain is too long')


def verify_legacy(hub, snapshot, manifest):
    """Fully hash a legacy HF snapshot, allowing only contained file links."""
    hub, snapshot = _absolute(hub), _absolute(snapshot)
    manifest = verify_manifest(manifest)
    if snapshot == hub or hub not in snapshot.parents:
        raise StorageError('legacy snapshot must be below its explicit hub boundary')
    with directory(hub):
        pass
    before = _inventory(snapshot)
    expected = [item['path'] for item in manifest['files']]
    if sorted(before) != expected:
        raise StorageError('legacy snapshot file set differs from supplied manifest')
    files = {}
    for item in manifest['files']:
        name = item['path']
        target = _resolve_leaf(hub, snapshot / name)
        with directory(target.parent) as parent, file_at(parent, target.name) as fd:
            fingerprint = metadata(os.fstat(fd))
            if fingerprint[3] != item['size']:
                raise StorageError(f'legacy snapshot size differs: {name}')
            digest = hashlib.sha256()
            while chunk := os.read(fd, 4 * 1024 * 1024):
                digest.update(chunk)
            if fingerprint != metadata(os.fstat(fd)) or digest.hexdigest() != item['sha256']:
                raise StorageError(f'legacy snapshot changed or hash differs: {name}')
        current = _resolve_leaf(hub, snapshot / name)
        with directory(current.parent) as parent, file_at(parent, current.name) as fd:
            if current != target or metadata(os.fstat(fd)) != fingerprint:
                raise StorageError(f'legacy file changed after hashing: {name}')
        files[name] = {'target': str(target), 'metadata': fingerprint}
    for name, checked in files.items():
        current = _resolve_leaf(hub, snapshot / name)
        with directory(current.parent) as parent, file_at(parent, current.name) as fd:
            if str(current) != checked['target'] or metadata(os.fstat(fd)) != checked['metadata']:
                raise StorageError(f'legacy file changed during final verification: {name}')
    if _inventory(snapshot) != before:
        raise StorageError('legacy snapshot changed during verification')
    return {'entries': before, 'files': files}


def preview(*, manifest, legacy_hub, destination_root, kind, node_id,
            state_root, legacy_snapshot=None):
    manifest = verify_manifest(manifest)
    if kind not in ('home', 'archive'):
        raise StorageError('migration kind must be home or archive')
    if kind == 'home':
        from .filesystem import require_serving_filesystem
        require_serving_filesystem(destination_root)
    if not isinstance(node_id, str) or not node_id or any(c in node_id for c in '\x00\r\n'):
        raise StorageError('migration requires an explicit confirmed node identity')
    hub, root = _absolute(legacy_hub), _absolute(destination_root)
    snapshot = _absolute(legacy_snapshot) if legacy_snapshot else payload(hub, manifest)
    with directory(root) as fd:
        target_device = os.fstat(fd).st_dev
    destination = location(root, manifest, archive=(kind == 'archive'))
    if _overlap(hub, destination):
        raise StorageError('legacy hub and destination must not overlap')
    verified = verify_legacy(hub, snapshot, manifest)
    existing = destination.exists() or destination.is_symlink()
    if existing:
        if read_json(destination / 'manifest.json') != manifest:
            raise StorageError('existing destination manifest differs')
        verify_tree(payload(destination, manifest), manifest, full=True)
    method = 'reuse-destination' if existing else ('hardlink' if kind == 'home' and all(
        v['metadata'][0] == target_device for v in verified['files'].values()) else 'copy')
    store = Store(state_root)
    if _overlap(store.root, hub) or _overlap(store.root, destination):
        raise StorageError('new state directory must not overlap legacy or destination payloads')
    home = store.home(manifest['manifest_id'])
    if kind == 'home' and home is not None and (home['hub_path'] != str(destination) or home['node_id'] != node_id):
        raise StorageError('snapshot already has a different home; use explicit movement')
    plan = {'schema_version': 1, 'kind': 'pulsar-storage-migration-plan',
        'snapshot_manifest_id': manifest['manifest_id'], 'migration_kind': kind,
        'node_id': node_id, 'legacy_hub': str(hub), 'legacy_snapshot': str(snapshot),
        'destination_root': str(root), 'destination': str(destination), 'state_root': str(store.root),
        'method': method, 'bytes_to_copy': manifest['total_bytes'] if method == 'copy' else 0,
        'file_count': manifest['file_count'], 'source_verification': verified,
        'legacy_metadata': 'retained; never modified',
        'prepared_copies': 'not imported; retain old files and pins, then explicitly prepare and pin after ownership review'}
    plan['plan_digest'] = hashlib.sha256(pretty_json_bytes(plan)).hexdigest()
    return plan


def _materialize(source_files, destination, manifest, *, hardlink, archive):
    ensure_directory(destination, private=not archive)
    for item in manifest['files']:
        source = Path(source_files[item['path']]['target'])
        target = destination / item['path']
        ensure_directory(target.parent, private=not archive)
        with directory(source.parent) as src, directory(target.parent) as dest:
            if hardlink:
                os.link(source.name, target.name, src_dir_fd=src, dst_dir_fd=dest, follow_symlinks=False)
            else:
                with file_at(src, source.name) as incoming:
                    fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o666 if archive else 0o600, dir_fd=dest)
                    with os.fdopen(fd, 'wb') as stream:
                        while chunk := os.read(incoming, 4 * 1024 * 1024):
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
            os.fsync(dest)


def apply(*, expected_plan_digest, guard, **arguments):
    """Re-plan under the new store lock; real guard runs before writes/publication."""
    manifest = verify_manifest(arguments['manifest'])
    store = Store(arguments['state_root'])
    proposed_destination = location(arguments['destination_root'], manifest,
                                    archive=arguments['kind'] == 'archive')
    if _overlap(store.root, arguments['legacy_hub']) or _overlap(store.root, proposed_destination):
        raise StorageError('new state directory must not overlap legacy or destination payloads')
    with store.lock():
        plan = preview(**arguments)
        if plan['plan_digest'] != expected_plan_digest:
            raise StorageError('migration plan changed; inspect a fresh preview before applying')
        destination = Path(plan['destination'])
        paths = [Path(plan['legacy_hub']), destination]
        guard(plan['node_id'], paths)
        archive = plan['migration_kind'] == 'archive'
        stage = None
        if plan['method'] != 'reuse-destination':
            stage = begin_staging(destination.parent, archive=archive)
            try:
                _materialize(plan['source_verification']['files'], payload(stage, manifest), manifest,
                             hardlink=plan['method'] == 'hardlink', archive=archive)
                verify_tree(payload(stage, manifest), manifest, full=True)
                verify_legacy(Path(plan['legacy_hub']), Path(plan['legacy_snapshot']), manifest)
                guard(plan['node_id'], paths)
                stamp = finish_staging(stage, destination, manifest, archive=archive)
            except Exception as exc:
                raise StorageError(f'migration did not complete; owned staging retained at {stage}: {exc}') from exc
        else:
            stamp = verify_tree(payload(destination, manifest), manifest, full=True)
            guard(plan['node_id'], paths)
        store.save_manifest(manifest)
        if archive:
            result = verify_archive(plan['destination_root'], manifest)
            store.put('archives', manifest['manifest_id'], {**result, 'root': plan['destination_root'],
                      'node_id': plan['node_id'], 'verified_at': now()})
        else:
            result = home_record(manifest, plan['node_id'], destination, stamp)
            store.put('homes', manifest['manifest_id'], result)
        return {'schema_version': 1, 'kind': 'pulsar-storage-migration-result',
                'snapshot_manifest_id': manifest['manifest_id'], 'migration_kind': plan['migration_kind'],
                'method': plan['method'], 'destination': str(destination),
                'legacy_metadata': plan['legacy_metadata'], 'prepared_copies': plan['prepared_copies']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('preview', 'apply'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--legacy-hub', type=Path, required=True)
    parser.add_argument('--legacy-snapshot', type=Path)
    parser.add_argument('--destination-root', type=Path, required=True)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--kind', choices=('home', 'archive'), required=True)
    parser.add_argument('--node', '--node-id', dest='node_id', required=True)
    parser.add_argument('--expected-plan-digest')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        arguments = dict(manifest=read_json(args.manifest.absolute()), legacy_hub=args.legacy_hub,
            legacy_snapshot=args.legacy_snapshot, destination_root=args.destination_root,
            state_root=args.state_root, kind=args.kind, node_id=args.node_id)
        if args.command == 'preview':
            result = preview(**arguments)
        else:
            if not args.yes or not args.expected_plan_digest:
                raise StorageError('apply requires --yes and the reviewed --expected-plan-digest')
            def guard(node, paths):
                cmd = [str(Path(__file__).resolve().parents[1] / 'scripts/guard-storage.sh'),
                       '--node', node, '--local-only', '--json']
                for path in paths:
                    cmd += ['--path', str(path)]
                observed = subprocess.run(cmd, capture_output=True, text=True, check=True)
                if json.loads(observed.stdout).get('safe') is not True:
                    raise StorageError('storage guard did not establish safe migration')
            result = apply(**arguments, expected_plan_digest=args.expected_plan_digest, guard=guard)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print('Verified storage migration preview' if args.command == 'preview' else 'Storage migration complete')
            print(f"  Operation: {result.get('migration_kind')} ({result.get('method')})")
            print(f"  Files: {result.get('file_count', arguments['manifest']['file_count'])}")
            print(f"  Destination:\n    {result['destination']}")
            if 'plan_digest' in result:
                print(f"  Plan digest:\n    {result['plan_digest']}")
                print(f"  Bytes to copy: {result['bytes_to_copy']}")
            print('  Legacy files and metadata remain in place.')
            print('  Prepared copies and pins were not imported.')
            print('    Review ownership, then prepare and pin explicitly.')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'error: migration: {exc}', file=sys.stderr)
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
