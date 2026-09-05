import unittest
from model_library.filesystem import require_serving_filesystem
from model_library.integrity import StorageError


class ServingFilesystem(unittest.TestCase):
    def test_network_child_mount_is_not_hidden_by_local_parent(self):
        mounts='1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 0:2 / /data/remote rw - nfs4 host:/export rw\n'
        with self.assertRaisesRegex(StorageError,'cannot use nfs4'):
            require_serving_filesystem('/data/remote/models',mountinfo=mounts)
        self.assertEqual(require_serving_filesystem('/data/local',mountinfo=mounts)['filesystem'],'ext4')

    def test_mount_path_escaping_is_decoded(self):
        mounts='1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 0:2 / /data/remote\\040models rw - cifs //host/share rw\n'
        with self.assertRaisesRegex(StorageError,'cannot use cifs'):
            require_serving_filesystem('/data/remote models/model',mountinfo=mounts)

    def test_unknown_mount_information_fails_without_fallback(self):
        for value in ('','invalid'):
            with self.assertRaises(StorageError):
                require_serving_filesystem('/data/model',mountinfo=value)

if __name__=='__main__': unittest.main()
