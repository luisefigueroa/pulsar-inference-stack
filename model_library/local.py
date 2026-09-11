"""Node-local transfer, archive and restoration primitives.

These operate only on explicit paths and manifests. The Bash boundary supplies
confirmed nodes, enforces lifecycle ownership and chooses the copy transport.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import shutil
import stat
import uuid

from .integrity import (StorageError, atomic_json, directory, file_at, file_names,
                        metadata, read_json, rename_no_replace, verify_manifest, verify_tree)
from .state import ensure_directory, now, validate_home, validate_view


def location(root: str | Path, manifest: dict, *, archive: bool = False) -> Path:
    manifest = verify_manifest(manifest)
    root = Path(os.path.realpath(root))
    return root / ('pulsar-snapshots' if archive else 'pulsar-homes') / manifest['manifest_id']


def payload(hub: Path, manifest: dict) -> Path:
    return hub / 'snapshots' / manifest['snapshot_revision']


def _overlap(a: Path, b: Path) -> bool:
    a, b = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    return a == b or a in b.parents or b in a.parents


def copy_files(source: Path, destination: Path, manifest: dict, *, archive: bool = False) -> None:
    manifest = verify_manifest(manifest)
    if _overlap(source, destination):
        raise StorageError('copy source and destination must not overlap')
    ensure_directory(destination, private=not archive)
    with directory(source) as src:
        expected = [item['path'] for item in manifest['files']]
        if file_names(src) != expected:
            raise StorageError('copy source file set differs from the expected manifest')
        for item in manifest['files']:
            target = destination / item['path']
            ensure_directory(target.parent, private=not archive)
            with file_at(src, item['path']) as incoming, directory(target.parent) as parent:
                before = metadata(os.fstat(incoming))
                if before[3] != item['size']:
                    raise StorageError('copy source size differs from the expected manifest')
                out = os.open(target.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                              0o666 if archive else 0o600, dir_fd=parent)
                digest = hashlib.sha256()
                try:
                    with os.fdopen(out, 'wb') as stream:
                        while chunk := os.read(incoming, 4 * 1024 * 1024):
                            digest.update(chunk)
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(parent)
                except BaseException:
                    # Keep only this operation's incomplete staging for explicit recovery.
                    raise
                if before != metadata(os.fstat(incoming)) or digest.hexdigest() != item['sha256']:
                    raise StorageError(f'copy source changed or hash differs: {item["path"]}')
        if file_names(src) != expected:
            raise StorageError('copy source file set changed during transfer')


def begin_staging(parent: Path, *, archive: bool = False) -> Path:
    ensure_directory(parent, private=not archive)
    stage = parent / f'.pending-{uuid.uuid4().hex}'
    # Only a caller with this unguessable operation path can finish/clean this stage.
    with directory(parent) as fd:
        os.mkdir(stage.name, mode=0o777 if archive else 0o700, dir_fd=fd)
        os.fsync(fd)
    return stage


def finish_staging(stage: Path, destination: Path, manifest: dict, *, archive: bool = False) -> dict:
    manifest = verify_manifest(manifest)
    if stage.parent != destination.parent or not stage.name.startswith('.pending-'):
        raise StorageError('publication requires owned same-filesystem staging')
    checked = verify_tree(payload(stage, manifest), manifest, full=True)
    atomic_json(stage / 'manifest.json', manifest, replace=False, private=not archive)
    rename_no_replace(stage, destination)
    # Saved metadata names the final directory; its root may have changed on rename.
    return verify_tree(payload(destination, manifest), manifest, full=True)


def copy_snapshot(source: Path, root: str | Path, manifest: dict, *, archive: bool = False) -> tuple[Path, dict]:
    manifest = verify_manifest(manifest)
    if not archive:
        from .filesystem import require_serving_filesystem
        require_serving_filesystem(root)
    destination = location(root, manifest, archive=archive)
    source_scope = source.parent.parent if source.parent.name == 'snapshots' and source.name == manifest['snapshot_revision'] else source
    if _overlap(source_scope, destination):
        raise StorageError('snapshot source and archive/home destination overlap')
    if destination.exists() or destination.is_symlink():
        if read_json(destination / 'manifest.json') != manifest:
            raise StorageError('existing snapshot manifest differs; refusing replacement')
        return destination, verify_tree(payload(destination, manifest), manifest, full=True)
    stage = begin_staging(destination.parent, archive=archive)
    try:
        copy_files(source, payload(stage, manifest), manifest, archive=archive)
        stamp = finish_staging(stage, destination, manifest, archive=archive)
    except Exception as exc:
        raise StorageError(f'snapshot was not published; incomplete staging remains at {stage}: {exc}') from exc
    return destination, stamp


def home_record(manifest: dict, node_id: str, hub: Path, stamp: dict) -> dict:
    return validate_home({'schema_version': 1, 'kind': 'pulsar-home',
        'snapshot_manifest_id': manifest['manifest_id'], 'node_id': node_id,
        'hub_path': str(hub), 'path': str(payload(hub, manifest)),
        'verification': stamp, 'verified_at': now()})


def prepared_record(home: dict, *, spec_id: str, topology_id: str, rank: int,
                    is_home_view: bool = False, pinned: bool = False, schema_version: int = 1) -> dict:
    record = {**validate_home(home), 'schema_version': schema_version, 'kind': 'pulsar-prepared-view', 'spec_id': spec_id,
              'topology_id': topology_id, 'rank': rank, 'pinned': pinned,
              'is_home_view': is_home_view}
    return validate_view(record)


def verify_archive(root: str | Path, manifest: dict) -> dict:
    manifest = verify_manifest(manifest)
    hub = location(root, manifest, archive=True)
    if read_json(hub / 'manifest.json') != manifest:
        raise StorageError('archive manifest differs from the selected spec')
    verify_tree(payload(hub, manifest), manifest, full=True)
    return {'schema_version': 1, 'kind': 'pulsar-archive-verification',
            'snapshot_manifest_id': manifest['manifest_id'], 'verified': True,
            'file_count': manifest['file_count'], 'total_bytes': manifest['total_bytes']}


def restore(root: str | Path, home_root: str | Path, manifest: dict) -> tuple[Path, dict]:
    verify_archive(root, manifest)
    source = payload(location(root, manifest, archive=True), manifest)
    return copy_snapshot(source, home_root, manifest)


def _protect_nested_storage(hub: Path) -> None:
    """Removal never descends into a recovery namespace or another mount."""
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError as exc:
        raise StorageError('cannot inspect mount boundaries before removal') from exc
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            raise StorageError('mount boundary information is incomplete')
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), fields[4]))
        if mount == hub or hub in mount.parents:
            raise StorageError('managed removal cannot cross a mounted storage boundary')
    with directory(hub) as root:
        device = os.fstat(root).st_dev
        def inspect(fd):
            for name in os.listdir(fd):
                value = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if name == 'pulsar-snapshots':
                    raise StorageError('recovery archive namespace is nested in the removal target')
                if stat.S_ISDIR(value.st_mode):
                    if value.st_dev != device:
                        raise StorageError('managed removal cannot cross a filesystem boundary')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        inspect(child)
                    finally:
                        os.close(child)
        inspect(root)


def remove_managed_hub(hub: Path, allowed_root: Path, manifest: dict, *, verification: dict | None = None) -> None:
    """Delete only an exact managed hub, after caller's complete live preflight.

    Archives deliberately have no removal primitive. Use a home/view root only.
    """
    root = Path(os.path.realpath(allowed_root))
    hub = Path(hub)
    if hub == root or root not in hub.parents or 'pulsar-snapshots' in hub.relative_to(root).parts:
        raise StorageError('deletion target is not an owned home or prepared view')
    if not shutil.rmtree.avoids_symlink_attacks:
        raise StorageError('platform cannot safely delete an anchored directory')
    _protect_nested_storage(hub)
    with directory(hub.parent) as parent:
        st = os.stat(hub.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise StorageError('deletion target is not a regular directory')
        if read_json(hub / 'manifest.json') != verify_manifest(manifest):
            raise StorageError('deletion target manifest differs from selected snapshot')
        if verification is not None:
            with directory(payload(hub,manifest)) as snapshot:
                actual=os.fstat(snapshot)
                if verification.get('root',[])[:2] != [actual.st_dev,actual.st_ino]:
                    raise StorageError('owned snapshot directory was replaced; refusing deletion')
        current = os.stat(hub.name, dir_fd=parent, follow_symlinks=False)
        if metadata(current) != metadata(st):
            raise StorageError('deletion target changed during preflight')
        shutil.rmtree(hub.name, dir_fd=parent)
        os.fsync(parent)
