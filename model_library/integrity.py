"""Full snapshot verification and metadata stamps using the canonical spec manifest.

Metadata accelerates previously verified reads only. Every file set is checked;
a changed fingerprint requires a full rehash. No receipt or filesystem tree can
supply its own expected identity.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator

from release_spec import build_snapshot_manifest, pretty_json_bytes


class StorageError(ValueError):
    pass


def verify_manifest(value: Any) -> dict:
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int:
        raise StorageError("snapshot manifest must be an object with an integer schema version")
    try:
        expected = build_snapshot_manifest(model_id=value["model_id"],
            snapshot_revision=value["snapshot_revision"], files=value["files"])
    except (KeyError, ValueError, TypeError) as exc:
        raise StorageError(f"invalid snapshot manifest: {exc}") from exc
    if value != expected:
        raise StorageError("snapshot manifest fields or digest do not match the canonical manifest")
    return expected


def metadata(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


@contextlib.contextmanager
def directory(path: Path) -> Iterator[int]:
    """Open every directory component without following links."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise StorageError("managed paths must be absolute without parent traversal")
    current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=current)
            os.close(current)
            current = nxt
        yield current
    except OSError as exc:
        raise StorageError(f"cannot open managed directory {path}: {exc.strerror}") from exc
    finally:
        os.close(current)


@contextlib.contextmanager
def file_at(root_fd: int, relative: str) -> Iterator[int]:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or '..' in parts:
        raise StorageError("file name escapes snapshot")
    parent = os.dup(root_fd)
    opened = None
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=parent)
            os.close(parent)
            parent = nxt
        opened = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
        if not stat.S_ISREG(os.fstat(opened).st_mode):
            raise StorageError(f"snapshot file is not regular: {relative}")
        yield opened
    except OSError as exc:
        raise StorageError(f"cannot read snapshot file {relative}: {exc.strerror}") from exc
    finally:
        if opened is not None:
            os.close(opened)
        os.close(parent)


def file_names(root_fd: int) -> list[str]:
    result: list[str] = []

    def visit(fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(fd)):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode):
                result.append(relative)
            else:
                raise StorageError(f"snapshot contains a link or special file: {relative}")
    try:
        visit(root_fd, '')
    except OSError as exc:
        raise StorageError(f"snapshot inventory is unobservable: {exc.strerror}") from exc
    return sorted(result)


def verify_tree(path: str | Path, manifest: dict, *, stamp: dict | None = None,
                full: bool = True) -> dict:
    manifest = verify_manifest(manifest)
    path = Path(path)
    expected = [f['path'] for f in manifest['files']]
    with directory(path) as fd:
        root_before = metadata(os.fstat(fd))
        actual = file_names(fd)
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise StorageError(f"snapshot file set differs: missing={missing[:5]} extra={extra[:5]}")
        fingerprints: dict[str, list[int]] = {}
        for item in manifest['files']:
            with file_at(fd, item['path']) as source:
                observed = os.fstat(source)
                if observed.st_size != item['size']:
                    raise StorageError(f"snapshot size differs: {item['path']}")
                fingerprints[item['path']] = metadata(observed)
        fast = (not full and isinstance(stamp, dict)
                and stamp.get('kind') == 'pulsar-verification-stamp'
                and type(stamp.get('schema_version')) is int and stamp['schema_version'] == 1
                and stamp.get('snapshot_manifest_id') == manifest['manifest_id']
                and stamp.get('path') == str(path)
                and stamp.get('root') == root_before
                and stamp.get('files') == fingerprints)
        if not fast:
            for item in manifest['files']:
                with file_at(fd, item['path']) as source:
                    before = metadata(os.fstat(source))
                    digest = hashlib.sha256()
                    while chunk := os.read(source, 4 * 1024 * 1024):
                        digest.update(chunk)
                    if before != metadata(os.fstat(source)) or before != fingerprints[item['path']]:
                        raise StorageError(f"snapshot changed during verification: {item['path']}")
                    if digest.hexdigest() != item['sha256']:
                        raise StorageError(f"snapshot SHA-256 differs: {item['path']}")
        # Re-open paths: replacement after reading an open descriptor is not verification.
        for item in manifest['files']:
            with file_at(fd, item['path']) as source:
                if metadata(os.fstat(source)) != fingerprints[item['path']]:
                    raise StorageError(f"snapshot changed during verification: {item['path']}")
        if file_names(fd) != expected or metadata(os.fstat(fd)) != root_before:
            raise StorageError("snapshot directory changed during verification")
        with directory(path) as current:
            if metadata(os.fstat(current)) != root_before:
                raise StorageError("snapshot directory was replaced during verification")
        return {'schema_version': 1, 'kind': 'pulsar-verification-stamp',
                'snapshot_manifest_id': manifest['manifest_id'], 'path': str(path),
                'root': root_before, 'files': fingerprints, 'method': 'metadata' if fast else 'sha256'}


def read_json(path: str | Path) -> Any:
    path = Path(path)
    with directory(path.parent) as root:
        with file_at(root, path.name) as fd:
            try:
                with os.fdopen(os.dup(fd), 'r', encoding='utf-8') as stream:
                    def unique(pairs: list) -> dict:
                        result: dict = {}
                        for key, value in pairs:
                            if key in result:
                                raise StorageError(f"duplicate JSON field: {key}")
                            result[key] = value
                        return result
                    return json.load(stream, object_pairs_hook=unique,
                                     parse_constant=lambda v: (_ for _ in ()).throw(StorageError(f"invalid JSON number {v}")))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise StorageError(f"invalid JSON in {path.name}: {exc}") from exc


def atomic_json(path: Path, value: Any, *, replace: bool = True, private: bool = True) -> None:
    """Publish a regular file through an anchored parent directory; fsync both."""
    payload = pretty_json_bytes(value)
    path = Path(path)
    with directory(path.parent) as parent:
        name = f'.{path.name}.{os.urandom(12).hex()}.tmp'
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600 if private else 0o666, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                try:
                    st = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        raise StorageError("refusing to replace non-regular state file")
                except FileNotFoundError:
                    pass
                os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


_RENAME_NOREPLACE = 1
_UNSUPPORTED_RENAMEAT2 = {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}


def _renameat2_no_replace(src_fd: int, src_name: bytes, dest_fd: int, dest_name: bytes) -> bool:
    """Return True if published. False if this filesystem rejected RENAME_NOREPLACE."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        return False
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    if rename(src_fd, src_name, dest_fd, dest_name, _RENAME_NOREPLACE) == 0:
        return True
    code = ctypes.get_errno()
    if code in _UNSUPPORTED_RENAMEAT2:
        return False
    raise StorageError(f"cannot publish snapshot without replacement: {os.strerror(code)}")


def _move_tree_without_replacement(source_fd: int, destination_fd: int) -> None:
    """Move one owned tree using exclusive mkdir/link operations only."""
    names = sorted(os.listdir(source_fd), key=lambda name: (name == 'manifest.json', name))
    for name in names:
        observed = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            os.mkdir(name, stat.S_IMODE(observed.st_mode), dir_fd=destination_fd)
            child_source = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=source_fd,
            )
            child_destination = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=destination_fd,
            )
            try:
                _move_tree_without_replacement(child_source, child_destination)
            finally:
                os.close(child_destination)
                os.close(child_source)
            os.rmdir(name, dir_fd=source_fd)
        elif stat.S_ISREG(observed.st_mode):
            os.link(
                name, name, src_dir_fd=source_fd, dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            os.unlink(name, dir_fd=source_fd)
        else:
            raise StorageError(f"staging contains a link or special file: {name}")
        os.fsync(destination_fd)
        os.fsync(source_fd)


def _publish_reserved_destination(src_fd: int, src_name: str, dest_fd: int, dest_name: str) -> None:
    """Reserve an absent directory, then publish without replacing any entry.

    The canonical ``manifest.json`` is moved last. A failed move can leave a
    reserved but incomplete destination, which readers reject because the
    manifest is absent; explicit recovery can inspect both owned locations.
    """
    source = os.open(
        src_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=src_fd,
    )
    try:
        mode = stat.S_IMODE(os.fstat(source).st_mode)
        try:
            os.mkdir(dest_name, mode, dir_fd=dest_fd)
        except FileExistsError as exc:
            raise StorageError(
                "cannot publish snapshot without replacement: File exists"
            ) from exc
        destination = os.open(
            dest_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=dest_fd,
        )
        try:
            _move_tree_without_replacement(source, destination)
        finally:
            os.close(destination)
        os.rmdir(src_name, dir_fd=src_fd)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(
            f"cannot publish snapshot without replacement: {exc.strerror}"
        ) from exc
    finally:
        os.close(source)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Publish a directory without replacing an existing destination.

    Prefer Linux renameat2(RENAME_NOREPLACE). Filesystems that reject that
    flag (NFSv3 EINVAL) fall back to an exclusive destination reservation and
    per-entry no-replace moves. The manifest moves last, so incomplete reserved
    destinations never look published.
    """
    with directory(source.parent) as src, directory(destination.parent) as dest:
        if _renameat2_no_replace(src, os.fsencode(source.name), dest, os.fsencode(destination.name)):
            os.fsync(dest)
            os.fsync(src)
            return
        _publish_reserved_destination(src, source.name, dest, destination.name)
        os.fsync(dest)
        os.fsync(src)
