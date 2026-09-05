"""Operator-selected recovery root; dotenv is parsed as data, never executed."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import json

from .integrity import StorageError, directory, file_at
from .state import ensure_directory

KEY = 'PULSAR_COLD_ROOT'
ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?PULSAR_COLD_ROOT\s*=(.*)$')
MENTION = re.compile(r'^\s*(?:export\s+)?PULSAR_COLD_ROOT(?:\s|=|$)')


def _decode(raw):
    quote = None
    escaped = False
    for char in raw:
        if escaped:
            escaped = False
            continue
        if char == '\\' and quote != "'":
            escaped = True
            continue
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == '#' and quote is None:
            break
        if quote != "'" and (char in '$`' or (quote is None and char in ';|&<>()')):
            raise StorageError('archive root assignment must be a literal path; dynamic shell expressions are unsupported')
    try:
        words = shlex.split(raw, comments=True, posix=True)
    except ValueError as exc:
        raise StorageError('archive root assignment has invalid quoting') from exc
    if len(words) > 1:
        raise StorageError('archive root assignment has trailing tokens; quote paths containing spaces')
    return words[0] if words else ''


def _read(repo):
    target = Path(repo) / '.env'
    with directory(target.parent) as parent:
        try:
            os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return b'', 0o600
        with file_at(parent, target.name) as fd:
            before = os.fstat(fd)
            data = bytearray()
            while chunk := os.read(fd, 64 * 1024):
                data.extend(chunk)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise StorageError('configuration changed while reading')
            return bytes(data), stat.S_IMODE(before.st_mode)


def parse(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise StorageError('dotenv must be UTF-8') from exc
    found = []
    for index, line in enumerate(text.splitlines(keepends=True)):
        match = ASSIGNMENT.match(line.rstrip('\r\n'))
        if match:
            found.append((index, _decode(match.group(1))))
        elif MENTION.match(line):
            raise StorageError('archive root assignment is malformed')
    if len(found) > 1:
        raise StorageError('archive root assignment is duplicated; resolve it explicitly')
    return found[0] if found else (None, None)


def effective(repo, environ=None):
    env = os.environ if environ is None else environ
    persisted_error = None
    try:
        data, _ = _read(Path(repo))
        _, persisted = parse(data)
    except (StorageError, OSError) as exc:
        if KEY not in env:
            raise
        persisted = None
        persisted_error = 'saved setting could not be read as a literal value'
    if KEY in env:
        value, source = env[KEY], 'process'
    elif persisted is not None:
        value, source = persisted, 'dotenv'
    else:
        value, source = None, 'absent'
    result = {'schema_version': 1, 'kind': 'pulsar-archive-root-configuration',
              'source': source, 'path': value, 'persisted_path': persisted, 'persisted_error': persisted_error,
              'status': 'not-configured' if value is None else 'disabled' if value == '' else 'configured',
              'health': {'directory_exists': None, 'readable': None, 'writable': None}}
    if value:
        if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts or any(c in value for c in '\0\n\r'):
            raise StorageError('configured archive root must be an absolute literal path without parent traversal')
        path = Path(value)
        result['health'] = {'directory_exists': path.is_dir(),
                            'readable': os.access(path, os.R_OK), 'writable': os.access(path, os.W_OK)}
    return result


@contextmanager
def lock(repo, *, exclusive=False):
    """Archive operations share this lock; configuration changes take it exclusively."""
    parent = Path(repo).resolve() / '.pulsar'
    ensure_directory(parent)
    with directory(parent) as fd:
        descriptor = os.open('archive-config.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StorageError('archive configuration lock is not regular')
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)


def set_root(repo, value):
    repo = Path(repo).resolve()
    if value:
        if not Path(value).is_absolute() or '..' in Path(value).parts or any(c in value for c in '\0\n\r'):
            raise StorageError('archive root must be an absolute path without parent traversal')
        if not Path(value).is_dir():
            raise StorageError('selected archive directory must already exist')
    with lock(repo, exclusive=True):
        data, mode = _read(repo)
        index, _ = parse(data)
        lines = data.decode('utf-8').splitlines(keepends=True)
        line = KEY + '=' + shlex.quote(value) + '\n'
        if index is None:
            if lines and not lines[-1].endswith(('\r', '\n')):
                lines[-1] += '\n'
            lines.append(line)
        else:
            lines[index] = line
        content = ''.join(lines).encode('utf-8')
        with directory(repo) as parent:
            name = '.env.archive-' + os.urandom(12).hex()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Refuse swapping a symlink or special file even if a concurrent writer ignored the lock.
                try:
                    target = os.stat('.env', dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(target.st_mode):
                        raise StorageError('dotenv is not a regular file')
                except FileNotFoundError:
                    pass
                os.replace(name, '.env', src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
    return effective(repo)


def run_locked(repo, command, *, allow_unconfigured=False):
    if not command:
        raise StorageError('run requires a child command after --')
    with lock(repo):
        state = effective(repo)
        if not allow_unconfigured and state['status'] != 'configured':
            raise StorageError('configure an archive root before using archive operations')
        if not allow_unconfigured and not state['health']['directory_exists']:
            raise StorageError('configured archive directory is unavailable')
        env = dict(os.environ)
        env[KEY] = state['path'] or ''
        env['PULSAR_ARCHIVE_CONFIG_LOCKED'] = '1'
        return subprocess.run(command, env=env).returncode


def display(state):
    print('Recovery archive location')
    print(f"  Status: {state['status']}\n  Source: {state['source']}")
    if state['path']:
        print(f"  Path:\n    {state['path']}")
        health = state['health']
        print('  Directory: ' + ('available' if health['directory_exists'] else 'unavailable'))
        print('  Access observed:')
        print(f"    Readable: {str(health['readable']).lower()}\n    Writable: {str(health['writable']).lower()}")
    if state.get('persisted_error'):
        print('  Saved setting could not be read as a literal value.')
    if state['source'] == 'process' and state['path'] != state['persisted_path']:
        print('  Process setting overrides the saved setting.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest='command', required=True)
    show = sub.add_parser('show')
    show.add_argument('--json', action='store_true')
    change = sub.add_parser('set')
    change.add_argument('path')
    change.add_argument('--yes', action='store_true')
    change.add_argument('--json', action='store_true')
    disable = sub.add_parser('disable')
    disable.add_argument('--yes', action='store_true')
    disable.add_argument('--json', action='store_true')
    run = sub.add_parser('run')
    run.add_argument('--allow-unconfigured', action='store_true')
    run.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return run_locked(args.repo_root, args.argv[1:] if args.argv[:1] == ['--'] else args.argv,allow_unconfigured=args.allow_unconfigured)
        if args.command in ('set', 'disable'):
            if not args.yes:
                raise StorageError('changing the saved archive location requires --yes')
            state = set_root(args.repo_root, args.path if args.command == 'set' else '')
        else:
            state = effective(args.repo_root)
        if args.json:
            print(json.dumps(state, indent=2, sort_keys=True))
        else:
            display(state)
    except (ValueError, OSError) as exc:
        print(f'archive configuration: {exc}', file=sys.stderr)
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
