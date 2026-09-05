"""Read-only mount boundaries for serving paths, independent of archive policy."""
from pathlib import Path
import os
import re

from .integrity import StorageError

NETWORK_FILESYSTEMS = {'nfs', 'nfs4', 'cifs', 'smbfs', 'smb3', 'ceph', 'afs',
                       '9p', 'glusterfs', 'lustre', 'fuse.sshfs', 'fuse.glusterfs',
                       'fuse.s3fs', 'fuse.gcsfuse', 'fuse.rclone'}


def mount_rows(text):
    rows = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index('-')
            if separator < 6 or len(fields) <= separator + 2:
                raise ValueError()
            point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), fields[4])
            if not point.startswith('/'):
                raise ValueError()
            rows.append((Path(point), fields[separator + 1]))
        except (ValueError, IndexError) as exc:
            raise StorageError('mount information is incomplete') from exc
    if not rows:
        raise StorageError('mount information is unavailable')
    return rows


def require_serving_filesystem(path, *, mountinfo=None):
    """Reject known remote filesystems for live serving data; never mount anything.

    The configured archive has no filesystem-type restriction. This check is
    applied only to homes and prepared views, not archive sources or copies.
    It does not assert physical failure-domain independence.
    """
    target = Path(os.path.realpath(path))
    if mountinfo is None:
        try:
            mountinfo = Path('/proc/self/mountinfo').read_text()
        except OSError as exc:
            raise StorageError('cannot inspect serving storage mount') from exc
    matches = [(point, kind) for point, kind in mount_rows(mountinfo)
               if target == point or point in target.parents]
    if not matches:
        raise StorageError('serving storage mount is unknown')
    point, kind = max(matches, key=lambda pair: len(pair[0].parts))
    if kind in NETWORK_FILESYSTEMS:
        raise StorageError(f'live serving files cannot use {kind}; restore or prepare on node-local storage')
    return {'filesystem': kind, 'mount': str(point)}
