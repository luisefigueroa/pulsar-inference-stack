#!/usr/bin/env python3
"""Descriptor-rooted immutable-directory primitives.

This helper hardens candidate directories that are rooted at a descriptor
file. It is not a schema owner: each caller validates its own documents.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable


class ImmutableDescriptorDirectoryError(ValueError):
    """A candidate directory is missing, unsafe, mutated, or inconsistent."""


READ_STABILITY_HOOK: Callable[[str | None], None] | None = None
VERIFY_AFTER_SCAN_HOOK: Callable[[], None] | None = None

UNSAFE_PATH_COMPONENTS = {"", ".", ".."}


def fail(message: str) -> None:
    raise ImmutableDescriptorDirectoryError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_safe_component(name: str, *, label: str) -> str:
    if name in UNSAFE_PATH_COMPONENTS or "/" in name or os.sep in name or "\x00" in name:
        fail(f"{label} has an unsafe path component")
    return name


def lexical_parts(path: Path, *, label: str) -> tuple[str, ...]:
    """Return non-root parts after rejecting empty, dot, and dot-dot names."""
    parts = path.parts[1:] if path.is_absolute() else path.parts
    return tuple(require_safe_component(part, label=label) for part in parts)


def safe_absolute(path: Path, *, base: Path | None = None, label: str = "path") -> Path:
    """Return an absolute path without following or collapsing through '..'."""
    if not path.is_absolute():
        if base is None:
            fail(f"{label} must be absolute")
        path = base.joinpath(*lexical_parts(path, label=label))
    return Path("/").joinpath(*lexical_parts(path, label=label))


def close_quietly(fd: int | None) -> None:
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def fsync_dir_fd(dir_fd: int) -> None:
    os.fsync(dir_fd)


def stat_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _is_enoent(exc: OSError) -> bool:
    return exc.errno == errno.ENOENT


def _is_permission(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EPERM}


def classify_os_error(exc: OSError, *, label: str) -> None:
    if _is_enoent(exc):
        fail(f"{label} is missing")
    if _is_permission(exc):
        fail(f"{label} is unreadable")
    if exc.errno in {errno.ELOOP, errno.EINVAL}:
        fail(f"{label} must not be a symlink")
    fail(f"{label} is unreadable")


def require_same_file(
    observed: os.stat_result,
    expected: os.stat_result,
    *,
    label: str,
) -> None:
    if stat_fingerprint(observed) != stat_fingerprint(expected):
        fail(f"{label} changed during read")


def stat_at(dir_fd: int, name: str, *, label: str) -> os.stat_result:
    require_safe_component(name, label=label)
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        classify_os_error(exc, label=label)
        raise


def open_at(
    dir_fd: int,
    name: str,
    *,
    flags: int,
    mode: int = 0o600,
    label: str,
) -> int:
    require_safe_component(name, label=label)
    try:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    except OSError as exc:
        classify_os_error(exc, label=label)
        raise


def file_read_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def dir_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def require_same_dir(
    opened: os.stat_result,
    preview: os.stat_result,
    *,
    label: str,
) -> None:
    if stat.S_ISLNK(opened.st_mode) or not stat.S_ISDIR(opened.st_mode):
        fail(f"{label} must be a directory")
    if dir_identity(opened) != dir_identity(preview):
        fail(f"{label} changed during open")


def open_dir_at_matching(
    parent_fd: int,
    name: str,
    preview: os.stat_result,
    *,
    label: str,
) -> int:
    fd = open_at(
        parent_fd,
        name,
        flags=os.O_RDONLY | os.O_DIRECTORY,
        label=label,
    )
    try:
        require_same_dir(os.fstat(fd), preview, label=label)
    except Exception:
        close_quietly(fd)
        raise
    return fd


def mode_of(fd: int) -> int:
    return stat.S_IMODE(os.fstat(fd).st_mode)


def require_fd_mode(fd: int, expected: int, *, label: str) -> None:
    observed = mode_of(fd)
    if observed != expected:
        fail(f"{label} mode is not {expected:04o}")


def stable_read_fd(
    fd: int,
    *,
    preview: os.stat_result,
    label: str,
    hook_key: str | None = None,
) -> bytes:
    opened = os.fstat(fd)
    if stat.S_ISLNK(opened.st_mode):
        fail(f"{label} must not be a symlink")
    if not stat.S_ISREG(opened.st_mode):
        fail(f"{label} must be a regular file")
    require_same_file(opened, preview, label=label)
    hook = READ_STABILITY_HOOK
    if hook is not None:
        hook(hook_key)
    data = read_fd(fd)
    after = os.fstat(fd)
    require_same_file(after, preview, label=label)
    if after.st_size != len(data):
        fail(f"{label} changed during read")
    return data


def open_directory_from_root(
    parts: tuple[str, ...],
    *,
    label: str,
    create: bool = False,
    on_directory: Callable[[int], None] | None = None,
) -> int:
    """Walk absolute parts with no-follow opens. Optionally create missing directories."""
    if not parts:
        fail(f"{label} is too broad")
    first = "/" + parts[0]
    try:
        preview = os.lstat(first)
    except OSError as exc:
        if create and _is_enoent(exc):
            fail(f"{label} is missing")
        classify_os_error(exc, label=label)
        raise
    if stat.S_ISLNK(preview.st_mode):
        fail(f"{label} must not be a symlink")
    if not stat.S_ISDIR(preview.st_mode):
        fail(f"{label} must be a directory")
    try:
        current = os.open(first, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        classify_os_error(exc, label=label)
        raise
    try:
        require_same_dir(os.fstat(current), preview, label=label)
        if on_directory is not None:
            on_directory(current)
        for part in parts[1:]:
            require_safe_component(part, label=label)
            created = False
            try:
                preview = os.stat(part, dir_fd=current, follow_symlinks=False)
            except OSError as exc:
                if create and _is_enoent(exc):
                    try:
                        os.mkdir(part, 0o700, dir_fd=current)
                    except OSError as mkdir_exc:
                        classify_os_error(mkdir_exc, label=label)
                    preview = os.stat(part, dir_fd=current, follow_symlinks=False)
                    created = True
                else:
                    classify_os_error(exc, label=label)
                    raise
            if stat.S_ISLNK(preview.st_mode):
                fail(f"{label} must not be a symlink")
            if not stat.S_ISDIR(preview.st_mode):
                fail(f"{label} must be a directory")
            next_fd = open_dir_at_matching(current, part, preview, label=label)
            close_quietly(current)
            current = next_fd
            if created:
                os.fchmod(current, 0o700)
            if on_directory is not None:
                on_directory(current)
        return current
    except Exception:
        close_quietly(current)
        raise


def open_file_from_root(
    parts: tuple[str, ...],
    *,
    label: str,
    hook_key: str | None = None,
) -> tuple[int, os.stat_result]:
    del hook_key
    if not parts:
        fail(f"{label} must be a regular file")
    parent = open_directory_from_root(parts[:-1], label=label, create=False)
    try:
        preview = stat_at(parent, parts[-1], label=label)
        if stat.S_ISLNK(preview.st_mode):
            fail(f"{label} must not be a symlink")
        if not stat.S_ISREG(preview.st_mode):
            fail(f"{label} must be a regular file")
        fd = open_at(parent, parts[-1], flags=file_read_flags(), label=label)
        try:
            opened = os.fstat(fd)
            require_same_file(opened, preview, label=label)
        except Exception:
            close_quietly(fd)
            raise
        return fd, preview
    finally:
        close_quietly(parent)


def read_absolute_file(path: Path, *, label: str, hook_key: str | None = None) -> bytes:
    absolute = safe_absolute(path, label=label)
    fd, preview = open_file_from_root(
        lexical_parts(absolute, label=label),
        label=label,
        hook_key=hook_key,
    )
    try:
        return stable_read_fd(fd, preview=preview, label=label, hook_key=hook_key)
    finally:
        close_quietly(fd)


def reject_json_constant(value: str) -> None:
    fail(f"JSON contains non-standard constant {value}")


def unique_object_pairs(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def parse_strict_json(raw: bytes, *, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail(f"{label} is not valid UTF-8")
    try:
        return json.loads(
            text,
            parse_constant=reject_json_constant,
            object_pairs_hook=unique_object_pairs,
            strict=True,
        )
    except json.JSONDecodeError:
        fail(f"{label} is malformed JSON")
    except ValueError as exc:
        fail(f"{label}: {exc}")


def read_child_file(
    dir_fd: int,
    name: str,
    *,
    label: str,
    file_mode: int = 0o600,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    preview = stat_at(dir_fd, name, label=label)
    if stat.S_ISLNK(preview.st_mode):
        fail(f"{label} must not be a symlink")
    if not stat.S_ISREG(preview.st_mode):
        fail(f"{label} must be a regular file")
    fd = open_at(dir_fd, name, flags=file_read_flags(), label=label)
    try:
        require_fd_mode(fd, file_mode, label=label)
        data = stable_read_fd(fd, preview=preview, label=label, hook_key=name)
        after = os.fstat(fd)
        return data, stat_fingerprint(after)
    finally:
        close_quietly(fd)


@dataclass
class ImmutableDirectorySnapshot:
    root_identity: tuple[int, int]
    root_mode: int
    root_names: tuple[str, ...]
    subdirs: dict[str, tuple[int, tuple[int, int], int, tuple[str, ...]]] = field(
        default_factory=dict
    )
    files: dict[str, tuple[str, bytes, tuple[int, int, int, int, int, int]]] = field(
        default_factory=dict
    )

    def close(self) -> None:
        for handle, _identity, _mode, _names in self.subdirs.values():
            close_quietly(handle)
        self.subdirs.clear()

    def file_bytes(self, relative: str) -> bytes:
        if relative not in self.files:
            fail("candidate file is missing")
        return self.files[relative][1]

    def file_digest(self, relative: str) -> str:
        if relative not in self.files:
            fail("candidate file is missing")
        return self.files[relative][0]

    def relative_names(self) -> set[str]:
        return set(self.files)


def scan_immutable_directory(
    dest_fd: int,
    *,
    allowed_subdirs: set[str] | None = None,
    dir_mode: int = 0o700,
    file_mode: int = 0o600,
    label: str = "candidate",
) -> ImmutableDirectorySnapshot:
    require_fd_mode(dest_fd, dir_mode, label=f"{label} directory")
    root_stat = os.fstat(dest_fd)
    try:
        root_names = tuple(sorted(os.listdir(dest_fd)))
    except OSError:
        fail(f"{label} directory is unreadable")
    snapshot = ImmutableDirectorySnapshot(
        root_identity=dir_identity(root_stat),
        root_mode=stat.S_IMODE(root_stat.st_mode),
        root_names=root_names,
    )
    allowed = set() if allowed_subdirs is None else set(allowed_subdirs)
    try:
        for name in root_names:
            preview = stat_at(dest_fd, name, label=f"{label} entry")
            if stat.S_ISLNK(preview.st_mode):
                fail(f"{label} must not contain a symlink")
            if name.startswith("."):
                fail(f"{label} cannot retain scratch files")
            if stat.S_ISDIR(preview.st_mode):
                if name not in allowed:
                    fail(f"{label} contains an unexpected directory")
                child_fd = open_dir_at_matching(
                    dest_fd,
                    name,
                    preview,
                    label=f"{label} subdirectory",
                )
                require_fd_mode(child_fd, dir_mode, label=f"{label} subdirectory")
                try:
                    child_names = tuple(sorted(os.listdir(child_fd)))
                except OSError:
                    fail(f"{label} directory is unreadable")
                if not child_names:
                    fail(f"{label} contains an unexpected directory")
                snapshot.subdirs[name] = (
                    child_fd,
                    dir_identity(os.fstat(child_fd)),
                    dir_mode,
                    child_names,
                )
                for child_name in child_names:
                    if child_name.startswith("."):
                        fail(f"{label} cannot retain scratch files")
                    child_preview = stat_at(
                        child_fd, child_name, label=f"{label} file"
                    )
                    if stat.S_ISLNK(child_preview.st_mode):
                        fail(f"{label} must not contain a symlink")
                    if stat.S_ISDIR(child_preview.st_mode):
                        fail(f"{label} contains an unexpected directory")
                    if not stat.S_ISREG(child_preview.st_mode):
                        fail(f"{label} file must be a regular file")
                    data, fingerprint = read_child_file(
                        child_fd,
                        child_name,
                        label=f"{label} file",
                        file_mode=file_mode,
                    )
                    snapshot.files[f"{name}/{child_name}"] = (
                        sha256_bytes(data),
                        data,
                        fingerprint,
                    )
                continue
            if not stat.S_ISREG(preview.st_mode):
                fail(f"{label} file must be a regular file")
            data, fingerprint = read_child_file(
                dest_fd,
                name,
                label=f"{label} file",
                file_mode=file_mode,
            )
            snapshot.files[name] = (sha256_bytes(data), data, fingerprint)
    except Exception:
        snapshot.close()
        raise
    return snapshot


def recheck_immutable_directory(
    dest_fd: int,
    snapshot: ImmutableDirectorySnapshot,
    dest: Path,
    *,
    dir_mode: int = 0o700,
    label: str = "candidate",
) -> None:
    root_now = os.fstat(dest_fd)
    if dir_identity(root_now) != snapshot.root_identity:
        fail(f"{label} directory identity changed")
    if stat.S_IMODE(root_now.st_mode) != dir_mode:
        fail(f"{label} directory mode is not {dir_mode:04o}")
    try:
        root_names = tuple(sorted(os.listdir(dest_fd)))
    except OSError:
        fail(f"{label} directory is unreadable")
    if root_names != snapshot.root_names:
        fail(f"{label} directory entries changed")
    for name, (handle, identity, _mode, names) in snapshot.subdirs.items():
        preview = stat_at(dest_fd, name, label=f"{label} subdirectory")
        if stat.S_ISLNK(preview.st_mode):
            fail(f"{label} subdirectory must not be a symlink")
        if not stat.S_ISDIR(preview.st_mode):
            fail(f"{label} subdirectory must be a directory")
        if dir_identity(preview) != identity:
            fail(f"{label} subdirectory identity changed")
        named_fd = open_dir_at_matching(
            dest_fd,
            name,
            preview,
            label=f"{label} subdirectory",
        )
        try:
            if dir_identity(os.fstat(named_fd)) != identity:
                fail(f"{label} subdirectory identity changed")
            now = os.fstat(handle)
            if dir_identity(now) != identity:
                fail(f"{label} subdirectory identity changed")
            if stat.S_IMODE(now.st_mode) != dir_mode:
                fail(f"{label} subdirectory mode is not {dir_mode:04o}")
            try:
                current_names = tuple(sorted(os.listdir(named_fd)))
            except OSError:
                fail(f"{label} directory is unreadable")
            if current_names != names:
                fail(f"{label} directory entries changed")
        finally:
            close_quietly(named_fd)
    for relative, (_digest, _data, fingerprint) in snapshot.files.items():
        parts = lexical_parts(PurePosixPath(relative), label=f"{label} file")
        if len(parts) == 1:
            parent_fd = dest_fd
            name = parts[0]
        else:
            parent_fd = snapshot.subdirs[parts[0]][0]
            name = parts[1]
        preview = stat_at(parent_fd, name, label=f"{label} file")
        if stat_fingerprint(preview) != fingerprint:
            fail(f"{label} file changed")
    fresh = open_directory_from_root(
        lexical_parts(dest, label=f"{label} directory"),
        label=f"{label} directory",
        create=False,
    )
    try:
        if dir_identity(os.fstat(fresh)) != snapshot.root_identity:
            fail(f"{label} path no longer identifies the same directory")
    finally:
        close_quietly(fresh)


def open_and_scan_immutable_directory(
    dest: Path,
    *,
    allowed_subdirs: set[str] | None = None,
    dir_mode: int = 0o700,
    file_mode: int = 0o600,
    label: str = "candidate",
    invoke_after_scan_hook: bool = True,
) -> tuple[int, ImmutableDirectorySnapshot]:
    normalized = safe_absolute(dest, label=f"{label} directory")
    dest_fd = open_directory_from_root(
        lexical_parts(normalized, label=f"{label} directory"),
        label=f"{label} directory",
        create=False,
    )
    snapshot = None
    try:
        snapshot = scan_immutable_directory(
            dest_fd,
            allowed_subdirs=allowed_subdirs,
            dir_mode=dir_mode,
            file_mode=file_mode,
            label=label,
        )
        if invoke_after_scan_hook:
            hook = VERIFY_AFTER_SCAN_HOOK
            if hook is not None:
                hook()
        return dest_fd, snapshot
    except Exception:
        if snapshot is not None:
            snapshot.close()
        close_quietly(dest_fd)
        raise
