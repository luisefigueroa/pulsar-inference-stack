"""Private verification results shared only within one prepare invocation.

The Bash caller owns a fresh temporary directory and removes it on exit. A
cached result is only a candidate stamp: the node still checks the filesystem.
"""
import hashlib
import json
from pathlib import Path
import sys

from .integrity import atomic_json, read_json


def cache_path(root: str, record: dict) -> Path:
    identity = [record[key] for key in ('node_id', 'snapshot_manifest_id', 'path')]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return Path(root) / f'{key}.json'


def main() -> None:
    operation, root = sys.argv[1:]
    record = json.load(sys.stdin)
    path = cache_path(root, record)
    if operation == 'remember':
        atomic_json(path, {key: record[key] for key in ('verification', 'verified_at')})
    elif operation == 'lookup':
        print(json.dumps({**record, **read_json(path)} if path.exists() else None))
    else:
        raise ValueError('unknown preparation verification operation')


if __name__ == '__main__':
    main()
