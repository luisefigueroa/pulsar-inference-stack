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

import json
import os
import sys
from pathlib import Path

from .integrity import StorageError, read_json, verify_manifest
from .state import checked_id


def find_archives(root: str | None, model_id: str, model_commit: str) -> dict:
    """The archives under ROOT whose manifest names MODEL_ID at MODEL_COMMIT.

    state is `not-configured` (no location, or archives are off), `unreadable`
    (the location or its archive folder could not be listed) or `searched`.
    A folder that is not a complete archive, or whose manifest cannot be
    verified, is counted in `skipped` and never matches."""
    result = {'schema_version': 1, 'kind': 'pulsar-archive-search', 'state': 'searched', 'root': root or None,
              'model_id': model_id, 'snapshot_revision': model_commit, 'archives': [], 'skipped': 0}
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
        except (OSError, ValueError, KeyError, TypeError):
            result['skipped'] += 1
            continue
        if manifest['model_id'] == model_id and manifest['snapshot_revision'] == model_commit:
            result['archives'].append({'snapshot_manifest_id': name, 'path': str(hub),
                                       'manifest_path': str(hub / 'manifest.json'),
                                       'total_bytes': manifest['total_bytes'], 'file_count': manifest['file_count']})
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
