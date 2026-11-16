"""Find archives of a model commit in the archive location, without a spec.

    python3 -m model_library.archive_search MODEL_ID MODEL_COMMIT

reads the archive location from PULSAR_COLD_ROOT (run it under
`model_library.configuration run --allow-unconfigured`, which sets the
effective location and holds the shared configuration lock) and prints one
JSON object. It reads each archive's manifest only; it hashes no model file and
changes nothing. An archive location that cannot be read is reported as
unreadable: whether it holds the files is unknown, not absent.
"""
from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path

from .integrity import StorageError, read_json, verify_manifest
from .state import checked_id


def _could_not_read(error: BaseException) -> bool:
    """Whether ERROR, or the error it was raised from, is the system refusing a
    read (permissions, input/output, a stale mount). A manifest that is absent,
    or that was read and is not valid, is not that. The integrity helpers wrap
    the system's error, so its cause is looked at too."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, OSError) and error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, None):
            return True
        error = error.__cause__ or error.__context__
    return False


def find_archives(root: str | None, model_id: str, model_commit: str) -> dict:
    """The archives under ROOT whose manifest names MODEL_ID at MODEL_COMMIT.

    state is `not-configured` (no location, or archives are off), `unreadable`
    (the location, its archive folder or the manifest of one of its archives
    could not be read, and no archive that could be read matched) or
    `searched`. A folder that is not a complete archive, or whose manifest was
    read and is not valid, is counted in `skipped` and never matches. A folder
    whose manifest could not be read is counted in `unread`: what it holds is
    unknown, so a search that found no match is then `unreadable`."""
    result = {'schema_version': 1, 'kind': 'pulsar-archive-search', 'state': 'searched', 'root': root or None,
              'model_id': model_id, 'snapshot_revision': model_commit, 'archives': [], 'skipped': 0, 'unread': 0}
    if not root:
        result['state'] = 'not-configured'
        return result
    parent = Path(os.path.realpath(root)) / 'pulsar-snapshots'
    try:
        if not Path(root).is_dir():
            raise OSError('archive location is not a directory')
        names = sorted(os.listdir(parent)) if parent.exists() or parent.is_symlink() else []
    except OSError:
        result['state'] = 'unreadable'
        return result
    for name in names:
        if name.startswith('.'):
            continue  # Staging in progress is not an archive.
        hub = parent / name
        try:
            checked_id(name)
            manifest = verify_manifest(read_json(hub / 'manifest.json'))
            if manifest['manifest_id'] != name:
                raise StorageError('archive name and manifest differ')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if _could_not_read(exc):
                result['unread'] += 1  # It may be the archive asked about: unknown, not absent.
            else:
                result['skipped'] += 1  # No manifest, or one that is not valid: not an archive of anything.
            continue
        if manifest['model_id'] == model_id and manifest['snapshot_revision'] == model_commit:
            result['archives'].append({'snapshot_manifest_id': name, 'path': str(hub),
                                       'manifest_path': str(hub / 'manifest.json'),
                                       'total_bytes': manifest['total_bytes'], 'file_count': manifest['file_count']})
    if result['unread'] and not result['archives']:
        result['state'] = 'unreadable'
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print('usage: python3 -m model_library.archive_search MODEL_ID MODEL_COMMIT', file=sys.stderr)
        return 2
    print(json.dumps(find_archives(os.environ.get('PULSAR_COLD_ROOT'), *arguments), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
