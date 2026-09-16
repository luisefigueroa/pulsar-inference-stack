"""Small controller-local records. These locate bytes; manifests verify identity."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Iterator

from .integrity import StorageError, atomic_json, directory, read_json, verify_manifest

HEX = re.compile(r'^[0-9a-f]{64}$')


def checked_id(value: str) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise StorageError('expected a complete 64-character identity')
    return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def ensure_directory(path: Path, *, private: bool = True) -> Path:
    """Create internal directories without following an existing component link."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise StorageError('directory must be absolute without parent traversal')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700 if private else 0o777, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        raise StorageError(f'cannot create managed directory: {exc.strerror}') from exc
    finally:
        os.close(fd)
    return path


class Store:
    def __init__(self, root: str | Path):
        # The configured root may itself be a site-selected alias; internal paths may not.
        self.root = Path(os.path.realpath(root))

    @contextmanager
    def lock(self, *, exclusive: bool = True) -> Iterator[None]:
        ensure_directory(self.root)
        with directory(self.root) as parent:
            fd = os.open('lifecycle.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StorageError('lifecycle lock is not a regular file')
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield
            finally:
                os.close(fd)

    def _path(self, namespace: str, key: str) -> Path:
        if namespace not in {'manifests', 'homes', 'views', 'observations', 'archives', 'transactions', 'service-plans', 'services'}:
            raise StorageError('unknown record namespace')
        return self.root / namespace / f'{checked_id(key)}.json'

    def get(self, namespace: str, key: str) -> dict | None:
        path = self._path(namespace, key)
        try:
            path.parent.lstat()
        except FileNotFoundError:
            return None
        with directory(path.parent):
            try:
                path.lstat()
            except FileNotFoundError:
                return None
        value = read_json(path)
        if not isinstance(value, dict):
            raise StorageError(f'{namespace} record must be an object')
        return value

    def put(self, namespace: str, key: str, value: dict, *, replace: bool = True) -> None:
        path = self._path(namespace, key)
        ensure_directory(path.parent)
        atomic_json(path, value, replace=replace)

    def remove(self, namespace: str, key: str) -> None:
        path = self._path(namespace, key)
        if self.get(namespace, key) is None:
            return
        with directory(path.parent) as parent:
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)

    def records(self, namespace: str) -> list[dict]:
        parent = self.root / namespace
        try:
            parent.lstat()
        except FileNotFoundError:
            return []
        result = []
        with directory(parent) as fd:
            for name in sorted(os.listdir(fd)):
                if re.fullmatch(r'\.[0-9a-f]{64}\.json\.[0-9a-f]{24}\.tmp',name):
                    continue
                if not name.endswith('.json') or not HEX.fullmatch(name[:-5]):
                    raise StorageError(f'unrecognized {namespace} record; inspect state before mutation')
                result.append(self.get(namespace, name[:-5]))
        return result

    def save_manifest(self, manifest: dict) -> str:
        manifest = verify_manifest(manifest)
        key = manifest['manifest_id']
        existing = self.get('manifests', key)
        if existing is not None and existing != manifest:
            raise StorageError('stored manifest differs from expected identity')
        if existing is None:
            self.put('manifests', key, manifest, replace=False)
        return key

    def home(self, manifest_id: str) -> dict | None:
        record = self.get('homes', manifest_id)
        if record is not None:
            validate_home(record)
            if record['snapshot_manifest_id'] != manifest_id:
                raise StorageError('home record identity differs from its filename')
        return record

    def views(self, *, manifest_id: str | None = None, spec_id: str | None = None) -> list[dict]:
        result = []
        for record in self.records('views'):
            validate_view(record)
            if manifest_id is not None and record['snapshot_manifest_id'] != manifest_id:
                continue
            if spec_id is not None and record['spec_id'] != spec_id:
                continue
            result.append(record)
        return result


def view_key(spec_id: str, node_id: str, manifest_id: str | None = None) -> str:
    checked_id(spec_id)
    if not node_id or any(c in node_id for c in '\x00\r\n'):
        raise StorageError('view requires an explicit node identity')
    value = f'{spec_id}\0{node_id}'
    if manifest_id is not None:
        value = 'view-2\0' + value + '\0' + checked_id(manifest_id)
    return hashlib.sha256(value.encode()).hexdigest()


def view_record_key(record: dict) -> str:
    version = record.get('schema_version', 1)
    if type(version) is not int or version not in (1, 2, 3):
        raise StorageError('unsupported prepared record schema')
    if version == 3:
        version = record.get('binding_schema')
        if type(version) is not int or version not in (1, 2):
            raise StorageError('invalid shared-view binding schema')
    return view_key(record['spec_id'], record['node_id'],
                    record['snapshot_manifest_id'] if version == 2 else None)


def view_destination(root: Path, record: dict) -> Path:
    key = view_record_key(record)
    version = record.get('binding_schema', record.get('schema_version', 1))
    return root / (key if version == 2
                   else checked_id(record['spec_id']))


def validate_home(record: dict) -> dict:
    required = {'schema_version', 'kind', 'snapshot_manifest_id', 'node_id', 'hub_path',
                'path', 'verification', 'verified_at'}
    if not isinstance(record, dict) or set(record) != required or type(record['schema_version']) is not int or record['schema_version'] != 1 or record['kind'] != 'pulsar-home':
        raise StorageError('invalid home record')
    checked_id(record['snapshot_manifest_id'])
    for key in ('node_id', 'hub_path', 'path', 'verified_at'):
        if not isinstance(record[key], str) or not record[key] or '\0' in record[key]:
            raise StorageError(f'invalid home {key}')
    for key in ('hub_path', 'path'):
        if not Path(record[key]).is_absolute() or '..' in Path(record[key]).parts:
            raise StorageError(f'home {key} must be an absolute path')
    if not isinstance(record['verification'], dict) or record['verification'].get('snapshot_manifest_id') != record['snapshot_manifest_id']:
        raise StorageError('home verification does not name the expected manifest')
    return record


def validate_view(record: dict) -> dict:
    extra = {'spec_id', 'topology_id', 'rank', 'pinned', 'is_home_view'}
    if record.get('schema_version') == 3:
        extra.add('binding_schema')
        if type(record.get('binding_schema')) is not int or record['binding_schema'] not in (1, 2):
            raise StorageError('invalid shared-view binding schema')
    base = {k: v for k, v in record.items() if k not in extra}
    if type(record.get('schema_version')) is not int or record['schema_version'] not in (1, 2, 3):
        raise StorageError('invalid prepared-view schema')
    base['schema_version'] = 1
    base['kind'] = 'pulsar-home'
    validate_home(base)
    if set(record) != set(base) | extra or record.get('kind') != 'pulsar-prepared-view':
        raise StorageError('invalid prepared-view record')
    checked_id(record['spec_id'])
    if not isinstance(record['topology_id'], str) or not record['topology_id']:
        raise StorageError('prepared view requires a topology identity')
    if isinstance(record['rank'], bool) or not isinstance(record['rank'], int) or record['rank'] < 0:
        raise StorageError('invalid prepared-view rank')
    if not isinstance(record['pinned'], bool) or not isinstance(record['is_home_view'], bool):
        raise StorageError('prepared-view retention is unknown')
    return record


VIEW_IDENTITY_FIELDS = ('spec_id', 'node_id', 'rank', 'topology_id', 'hub_path',
                        'path', 'snapshot_manifest_id', 'is_home_view')


def shared_view(record: dict) -> dict:
    """Upgrade ownership semantics without moving files or changing record keys."""
    validate_view(record)
    if record['is_home_view']:
        raise StorageError('home bindings do not own working-copy storage')
    return {**record, 'schema_version': 3,
            'binding_schema': record.get('binding_schema', record['schema_version'])}


def reconcile_views(records: list[dict]) -> list[dict]:
    """Conservatively merge controller/node records, including partial upgrades."""
    result = {}
    for record in records:
        validate_view(record)
        key = view_record_key(record)
        previous = result.get(key)
        if previous:
            if any(previous[f] != record[f] for f in VIEW_IDENTITY_FIELDS):
                raise StorageError('controller and node view records disagree; inspect before mutation')
            record = {**record, 'pinned': previous['pinned'] or record['pinned']}
            if previous['schema_version'] == 3:
                record = shared_view(record)
        result[key] = record
    return list(result.values())


def copy_references(record: dict, records: list[dict]) -> list[dict]:
    """Every binding of this physical working copy; conflicting claims block use."""
    result = []
    for other in records:
        validate_view(other)
        if other['node_id'] != record['node_id']:
            continue
        a, b = Path(record['hub_path']), Path(other['hub_path'])
        if a == b or a in b.parents or b in a.parents:
            if any(other[f] != record[f] for f in ('hub_path', 'path', 'snapshot_manifest_id', 'is_home_view')):
                raise StorageError('prepared copy has conflicting ownership records')
            result.append(other)
    return result
