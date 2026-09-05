import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from release_spec import build_snapshot_manifest
from model_library.integrity import StorageError, atomic_json, read_json, verify_tree
from model_library.local import copy_snapshot, location, payload, restore, verify_archive
from model_library.state import Store, ensure_directory


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source/'config.json').write_bytes(b'{"model_type":"fixture"}\n')
        (self.source/'weights.bin').write_bytes(b'fixture weights')
        self.manifest = build_snapshot_manifest(model_id='test/model', snapshot_revision='a'*40,
            files=[{'path': p.name, 'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                   for p in self.source.iterdir()])

    def test_full_verify_and_metadata_fast_path(self):
        stamp = verify_tree(self.source, self.manifest)
        self.assertEqual(stamp['method'], 'sha256')
        self.assertEqual(verify_tree(self.source, self.manifest, stamp=stamp, full=False)['method'], 'metadata')

    def test_same_size_corruption_refuses_even_with_previous_stamp(self):
        stamp = verify_tree(self.source, self.manifest)
        (self.source/'weights.bin').write_bytes(b'Fixture weights')
        with self.assertRaisesRegex(StorageError, 'SHA-256'):
            verify_tree(self.source, self.manifest, stamp=stamp, full=False)

    def test_extra_or_missing_file_refuses(self):
        (self.source/'extra').write_bytes(b'x')
        with self.assertRaisesRegex(StorageError, 'file set'):
            verify_tree(self.source, self.manifest)
        (self.source/'extra').unlink()
        (self.source/'weights.bin').unlink()
        with self.assertRaisesRegex(StorageError, 'file set'):
            verify_tree(self.source, self.manifest)

    def test_symlink_file_and_directory_refuse(self):
        (self.source/'weights.bin').unlink()
        (self.source/'weights.bin').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(StorageError, 'link or special'):
            verify_tree(self.source, self.manifest)
        linked = self.base/'linked'
        linked.symlink_to(self.source)
        with self.assertRaises(StorageError):
            verify_tree(linked, self.manifest)

    def test_replaced_tree_does_not_use_old_stamp(self):
        stamp = verify_tree(self.source, self.manifest)
        moved = self.base/'old'
        self.source.rename(moved)
        import shutil
        shutil.copytree(moved, self.source)
        checked = verify_tree(self.source, self.manifest, stamp=stamp, full=False)
        self.assertEqual(checked['method'], 'sha256')

    def test_archive_restore_without_receipt_controller_or_hub(self):
        archive = self.base/'archive'
        hub, _ = copy_snapshot(self.source, archive, self.manifest, archive=True)
        self.assertTrue(verify_archive(archive, self.manifest)['verified'])
        target, stamp = restore(archive, self.base/'new-home', self.manifest)
        self.assertEqual((payload(target,self.manifest)/'weights.bin').read_bytes(), b'fixture weights')
        self.assertEqual(stamp['snapshot_manifest_id'], self.manifest['manifest_id'])
        self.assertTrue(hub.exists())

    def test_existing_archive_is_reverified_never_overwritten(self):
        archive = self.base/'archive'
        hub, _ = copy_snapshot(self.source, archive, self.manifest, archive=True)
        (payload(hub,self.manifest)/'weights.bin').write_bytes(b'bad')
        with self.assertRaises(StorageError):
            copy_snapshot(self.source, archive, self.manifest, archive=True)
        self.assertEqual((payload(hub,self.manifest)/'weights.bin').read_bytes(), b'bad')

    def test_archive_cannot_be_nested_in_removable_home_metadata(self):
        hub, _ = copy_snapshot(self.source, self.base/'home', self.manifest)
        with self.assertRaisesRegex(StorageError, 'overlap'):
            copy_snapshot(payload(hub,self.manifest),hub,self.manifest,archive=True)
        self.assertFalse((hub/'pulsar-snapshots').exists())

    def test_removal_refuses_nested_recovery_files_even_if_created_externally(self):
        from model_library.local import remove_managed_hub
        hub, stamp = copy_snapshot(self.source,self.base/'home',self.manifest)
        archive=hub/'operator-data'/'pulsar-snapshots'
        archive.mkdir(parents=True)
        recovery=archive/'keep.bin'; recovery.write_bytes(b'recovery data')
        with self.assertRaisesRegex(StorageError, 'recovery archive'):
            remove_managed_hub(hub,self.base/'home',self.manifest,verification=stamp)
        self.assertEqual(recovery.read_bytes(),b'recovery data')
        self.assertTrue(payload(hub,self.manifest).is_dir())

    def test_removal_refuses_nested_mount_before_deleting_files(self):
        from model_library.local import remove_managed_hub
        from unittest.mock import patch
        hub, stamp = copy_snapshot(self.source,self.base/'home',self.manifest)
        fake=f'1 0 8:1 / {hub}/mounted rw - ext4 /dev/example rw\n'
        with patch.object(Path,'read_text',return_value=fake):
            with self.assertRaisesRegex(StorageError,'mounted storage'):
                remove_managed_hub(hub,self.base/'home',self.manifest,verification=stamp)
        self.assertTrue(payload(hub,self.manifest).is_dir())

    def test_bad_copy_never_publishes_home(self):
        (self.source/'weights.bin').write_bytes(b'corrupt')
        with self.assertRaises(StorageError):
            copy_snapshot(self.source, self.base/'home', self.manifest)
        self.assertFalse(location(self.base/'home',self.manifest).exists())

    def test_atomic_json_refuses_symlink_and_duplicate_fields(self):
        p=self.base/'record.json'
        p.write_text('{"a":1,"a":2}')
        with self.assertRaisesRegex(StorageError,'duplicate'):
            read_json(p)
        p.unlink(); p.symlink_to(self.source/'config.json')
        with self.assertRaises(StorageError):
            atomic_json(p,{'a':3})
        self.assertEqual((self.source/'config.json').read_bytes(),b'{"model_type":"fixture"}\n')

    def test_store_does_not_follow_internal_namespace_links(self):
        root=self.base/'state'; root.mkdir()
        (root/'homes').symlink_to(self.source)
        with self.assertRaises(StorageError):
            Store(root).put('homes','b'*64,{'unsafe':True})

if __name__=='__main__':
    unittest.main()
